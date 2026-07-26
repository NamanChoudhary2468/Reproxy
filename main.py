import asyncio
import base64
import json
import os
from contextlib import asynccontextmanager
from urllib.parse import quote, unquote, urlsplit

from curl_cffi.requests import AsyncSession
from curl_cffi import requests as curl_requests
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

TIMEOUT = 15.0
# Caps how many upstream transfers can be in flight at once. Video
# players open several parallel Range requests when seeking — on a
# small instance, too many concurrent streams is what was causing OOM,
# not any single request. Tune via env var to match instance RAM.
MAX_CONCURRENT_STREAMS = int(os.environ.get("MAX_CONCURRENT_STREAMS", "3"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # NOTE: session is intentionally NOT shared across requests — a
    # single AsyncSession hit by concurrent requests caused
    # "CURLM_ADDED_ALREADY" (multi error 7) and connection resets under
    # real concurrent load (video players opening several parallel
    # Range requests). Each request gets its own session; only the
    # semaphore below is shared, to cap total concurrency for memory.
    app.state.stream_semaphore = asyncio.Semaphore(MAX_CONCURRENT_STREAMS)
    yield


app = FastAPI(lifespan=lifespan)

# Headers forwarded from the incoming client request to the target,
# same set as the original Workers script.
FORWARD_HEADERS = [
    "range",
    "if-range",
    "if-none-match",
    "if-modified-since",
    "authorization",
    "cookie",
    "user-agent",
    "accept",
    "accept-language",
    "accept-encoding",
    "referer",
    "origin",
]


def encode_base64url(data: str) -> str:
    return base64.urlsafe_b64encode(data.encode()).decode().rstrip("=")


def decode_base64url(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode()).decode()


@app.get("/create")
async def create(request: Request):
    params = request.query_params
    target = params.get("url")
    headers_param = params.get("headers")
    cookies_param = params.get("cookies")
    proxy_param = params.get("proxy")

    if not target:
        return JSONResponse({"error": "Missing url"}, status_code=400)

    try:
        parsed = urlsplit(target)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError
    except ValueError:
        return JSONResponse({"error": "Invalid URL"}, status_code=400)

    headers = None
    if headers_param:
        try:
            headers = json.loads(headers_param)
        except json.JSONDecodeError:
            return JSONResponse({"error": "Invalid headers JSON"}, status_code=400)

    cookies = None
    if cookies_param:
        try:
            cookies = json.loads(cookies_param)
        except json.JSONDecodeError:
            return JSONResponse({"error": "Invalid cookies JSON"}, status_code=400)

    proxy = None
    if proxy_param:
        try:
            p = urlsplit(proxy_param if "://" in proxy_param else f"http://{proxy_param}")
            if not p.hostname:
                raise ValueError
            proxy = proxy_param
        except ValueError:
            return JSONResponse({"error": "Invalid proxy spec"}, status_code=400)

    payload = json.dumps({"url": target, "headers": headers, "cookies": cookies, "proxy": proxy})
    encoded = encode_base64url(payload)

    base = str(request.base_url).rstrip("/")
    return JSONResponse({"url": f"{base}/{encoded}"})


@app.api_route("/{token}", methods=["GET", "POST", "PUT", "DELETE", "HEAD", "PATCH", "OPTIONS"])
async def reproxy(token: str, request: Request):
    try:
        payload = json.loads(decode_base64url(token))
    except Exception:
        return Response("Invalid token", status_code=400)

    target = payload.get("url")
    custom_headers = payload.get("headers")
    custom_cookies = payload.get("cookies")
    proxy_spec = payload.get("proxy")

    if not target:
        return Response("Invalid token", status_code=400)

    headers = {}
    for h in FORWARD_HEADERS:
        v = request.headers.get(h)
        if v:
            headers[h] = v

    if custom_headers:
        for k, v in custom_headers.items():
            headers[k] = v

    if custom_cookies:
        headers["cookie"] = "; ".join(f"{k}={v}" for k, v in custom_cookies.items())

    # Force uncompressed responses so the upstream Content-Length stays
    # accurate end-to-end (httpx auto-decompresses gzip/br/deflate,
    # which would otherwise make a forwarded Content-Length wrong).
    headers["accept-encoding"] = "identity"

    body = await request.body()

    proxy_url = None
    if proxy_spec:
        proxy_url = proxy_spec if "://" in proxy_spec else f"http://{proxy_spec}"

    session = AsyncSession(timeout=TIMEOUT)
    semaphore = request.app.state.stream_semaphore

    # Bound how many transfers run at once (protects small instances from
    # OOM when a video player opens many parallel Range requests).
    await semaphore.acquire()
    try:
        resp = await session.request(
            request.method,
            target,
            headers=headers,
            data=body if body else None,
            proxy=proxy_url,
            allow_redirects=True,
            accept_encoding="identity",  # keep Content-Length accurate end-to-end
            stream=True,
        )
    except curl_requests.errors.RequestsError as exc:
        semaphore.release()
        await session.close()
        return JSONResponse({"error": f"Upstream fetch failed: {exc}"}, status_code=502)
    except Exception:
        semaphore.release()
        await session.close()
        raise

    out_headers = dict(resp.headers)
    out_headers["access-control-allow-origin"] = "*"
    out_headers["access-control-expose-headers"] = "*"
    out_headers.pop("transfer-encoding", None)
    out_headers.pop("content-encoding", None)

    # Prefer the upstream's own Content-Disposition filename (e.g. file
    # hosts that serve everything from a generic /serve?t=... path with
    # the real name only present in this header). Fall back to the URL
    # path only if upstream didn't supply one.
    if "content-disposition" not in {k.lower() for k in out_headers}:
        try:
            raw_name = unquote(urlsplit(target).path.rsplit("/", 1)[-1])
            if raw_name:
                safe_name = raw_name.replace('"', "")
                out_headers["content-disposition"] = (
                    f'attachment; filename="{safe_name}"; filename*=UTF-8\'\'{quote(raw_name)}'
                )
        except Exception:
            pass

    async def body_stream():
        try:
            async for chunk in resp.aiter_content():
                yield chunk
        finally:
            await resp.aclose()
            await session.close()
            semaphore.release()

    return StreamingResponse(
        body_stream(),
        status_code=resp.status_code,
        headers=out_headers,
    )
