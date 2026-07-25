import base64
import json
from urllib.parse import quote, unquote, urlsplit

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

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

TIMEOUT = httpx.Timeout(15.0, connect=15.0)


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

    client_kwargs = {"timeout": TIMEOUT, "follow_redirects": True}
    if proxy_spec:
        proxy_url = proxy_spec if "://" in proxy_spec else f"http://{proxy_spec}"
        client_kwargs["proxy"] = proxy_url

    # NOTE: deliberately not using `async with` here — that would close
    # the client (and its connection) as soon as this block exits, which
    # is *before* StreamingResponse actually drains the body below.
    # Result looked like "slow transfer" but was really a dead
    # connection after headers. Client is closed explicitly once
    # body_stream() finishes (or errors) instead.
    client = httpx.AsyncClient(**client_kwargs)
    try:
        upstream = client.build_request(
            request.method, target, headers=headers, content=body if body else None
        )
        resp = await client.send(upstream, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        return JSONResponse({"error": f"Upstream fetch failed: {exc}"}, status_code=502)

    out_headers = dict(resp.headers)
    out_headers["access-control-allow-origin"] = "*"
    out_headers["access-control-expose-headers"] = "*"
    # transfer-encoding is a hop-by-hop header; content-encoding should be
    # gone anyway since we requested identity. content-length we KEEP —
    # with identity encoding it now accurately reflects the streamed body.
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
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        body_stream(),
        status_code=resp.status_code,
        headers=out_headers,
    )
