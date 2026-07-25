# pyreproxy

FastAPI reproxy service — same shape as the Workers version (`/create` +
base64url token endpoint), but real Python (httpx) so http:// **and**
https:// targets work through a plain HTTP proxy (or SOCKS5), no
platform TLS restrictions.

## Endpoints

### `GET /create?url=...&headers=...&cookies=...&proxy=...`

- `url` (required) — target URL
- `headers` (optional) — JSON object of extra headers
- `cookies` (optional) — JSON object of cookie key/values
- `proxy` (optional) — `host:port` or `http://user:pass@host:port`
  (also accepts `socks5://user:pass@host:port` — httpx supports SOCKS5
  proxies too, via the `httpx[socks]` extra already in requirements.txt)

Returns `{ "url": "<base>/<token>" }`.

### `ANY /{token}`

Decodes the token and forwards the request to the target, through the
proxy if one was set at `/create` time. Streams the response back with
the same header-forwarding, cookie-injection, and
`Content-Disposition` filename behavior as the original worker.

## Local run

```bash
docker build -t pyreproxy .
docker run -p 8000:8000 pyreproxy
```

```bash
curl "http://localhost:8000/create?url=https://example.com"
curl "http://localhost:8000/<token-from-above>"
```

## Deploy on Koyeb (Docker)

1. Push this folder to a GitHub repo (or use Koyeb CLI to deploy
   directly from local Docker build).
2. Koyeb dashboard → **Create Service** → **Docker** → point at your
   repo (Koyeb auto-detects the `Dockerfile`).
3. Koyeb sets `$PORT` automatically — the `Dockerfile`'s `CMD` already
   reads it, no config needed.
4. Deploy. You'll get a `https://<app>-<org>.koyeb.app` URL —
   that's your `base_url` for `/create`.

### Koyeb CLI alternative

```bash
koyeb service create pyreproxy \
  --docker . \
  --port 8000:http \
  --route /:8000
```

## Notes

- `proxy` credentials are stored inside the base64 token itself (same
  as the original design) — anyone with the token URL can see/reuse
  the proxy creds if they decode it. If that's a concern, keep proxy
  config server-side (env var) instead of per-token.
- No request size limit is enforced here — add one if this is
  public-facing (e.g. reject bodies over N MB before forwarding).
- `follow_redirects=True` is set — same as the original worker's
  `redirect: "follow"`.
