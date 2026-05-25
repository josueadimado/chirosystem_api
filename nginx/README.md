# Production nginx (api.reliefchiropractic.net)

## SSL certificates

Place your TLS files on the server before starting nginx:

```text
apps/api/nginx/ssl/cert.pem   → mounted as /etc/ssl/certs/cert.pem
apps/api/nginx/ssl/key.pem    → mounted as /etc/ssl/private/key.pem
```

Or set paths in `.env`:

```env
NGINX_SSL_CERT=/path/to/fullchain.pem
NGINX_SSL_KEY=/path/to/privkey.pem
```

(Let's Encrypt example: use `/etc/letsencrypt/live/api.reliefchiropractic.net/fullchain.pem` and `privkey.pem`.)

## Routes

| Path | Backend |
|------|---------|
| `/ws/voice` | `voice-ws:8001` (WebSocket, 3600s timeout) |
| `/health` | `api:8000` |
| `/` (everything else) | `api:8000` (Django + `/api/v1/...`) |

## Start

From `apps/api`:

```bash
docker compose -f docker-compose.prod.yml up -d nginx
```

Public URLs (with this nginx in front):

- API: `https://api.reliefchiropractic.net/api/v1/...`
- Voice WS: `wss://api.reliefchiropractic.net/ws/voice`
