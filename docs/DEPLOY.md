# Deploy guide — Hetzner CPX21 + Cloudflare R2

This is the production setup decision Sam picked: **GitHub + Hetzner CPX21 + Cloudflare R2**.

## Cost baseline

| Item | Provider | Monthly |
|------|----------|---------|
| VPS (Hetzner CPX21 — 3 vCPU, 4 GB RAM, 80 GB SSD, 20 TB traffic) | Hetzner | ~¥120 (€10.59) |
| Object storage (10 TB) | Cloudflare R2 | ~¥1,100 (US$150) |
| Egress traffic | Cloudflare R2 | **¥0 — free** |
| Domain (`dam-api.qide.com` already on `qide.com`) | existing | ¥0 |
| **Total** | | **~¥1,220 / month** |

## Step 1 — Cloudflare R2

1. Cloudflare dashboard → R2 → Create bucket `qidedam-prod` (region: any — R2 is global).
2. R2 → Manage R2 API Tokens → Create API token:
   - Permissions: **Object Read & Write**
   - Specific bucket: `qidedam-prod`
3. Copy the `Account ID` (URL: `https://<account_id>.r2.cloudflarestorage.com`).
4. Copy `Access Key ID` + `Secret Access Key` — these go into `.env.production` as `S3_ACCESS_KEY` / `S3_SECRET_KEY`.
5. (Optional CDN) — bucket Settings → Public Access → connect to a custom domain `cdn.qide.com`. Set `S3_PUBLIC_BASE_URL=https://cdn.qide.com` so public assets get a friendly URL instead of a presigned URL.
6. CORS (if needed for browser direct uploads):
   ```json
   [{
     "AllowedOrigins": ["https://qingxuantech.work", "https://aivisible.top"],
     "AllowedMethods": ["GET", "PUT", "POST", "DELETE", "HEAD"],
     "AllowedHeaders": ["*"],
     "ExposeHeaders": ["ETag"]
   }]
   ```

## Step 2 — Hetzner CPX21

1. Create at **Falkenstein (Germany)** or **Ashburn (US)** depending on customer geography. For Sam's customer base (US Pasadena + 深圳 / 出海 europe), Falkenstein is the safe default.
2. Image: Ubuntu 22.04. Add SSH key.
3. After SSH-ing in:
   ```bash
   curl -sSL https://raw.githubusercontent.com/<sam-org>/qide-dam-v2/main/scripts/bootstrap_hetzner.sh | bash
   ```
4. Clone the repo to `/opt/qide-dam`:
   ```bash
   cd /opt/qide-dam
   git clone https://github.com/<sam-org>/qide-dam-v2.git .
   ```

## Step 3 — Configure `.env.production`

```bash
cp .env.example .env.production
nano .env.production
```

Replace these critical values:

```ini
APP_ENV=production
DEBUG=false
SECRET_KEY=<openssl rand -hex 32>
POSTGRES_PASSWORD=<openssl rand -base64 24>
DATABASE_URL=postgresql+asyncpg://qidedam:<password>@postgres:5432/qidedam
DATABASE_URL_SYNC=postgresql+psycopg2://qidedam:<password>@postgres:5432/qidedam

S3_ENDPOINT=https://<account_id>.r2.cloudflarestorage.com
S3_REGION=auto
S3_BUCKET=qidedam-prod
S3_ACCESS_KEY=<R2 access key>
S3_SECRET_KEY=<R2 secret>
S3_USE_SSL=true
S3_PUBLIC_BASE_URL=https://cdn.qide.com  # optional

CORS_ORIGINS=https://qingxuantech.work,https://aivisible.top,https://kiln-ink.com,https://qidelinktech.com,https://chinamakershub.com

# Sprint 3 — fill in when ready
DASHSCOPE_API_KEY=<dashscope key>
```

## Step 4 — Boot

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

This brings up:
- `postgres` (pgvector/pg16)
- `redis` (7-alpine)
- `api` (uvicorn 4 workers, port 127.0.0.1:8000)
- `worker` (Celery, 4 concurrency, all 4 queues)
- `beat` (Celery Beat for scheduled cleanup)
- `flower` (port 127.0.0.1:5555 — Celery monitor)

`alembic upgrade head` runs automatically before uvicorn binds.

## Step 5 — Bootstrap admin

```bash
docker compose -f docker-compose.prod.yml exec api python -m scripts.init_db \
    --email admin@qide.com --password '<strong>' --tenant-slug qide
```

## Step 6 — Expose externally

**Option A — Nginx + Certbot (simplest):**
```bash
sudo nano /etc/nginx/sites-available/dam-api
# (paste the proxy_pass config — see below)
sudo ln -s /etc/nginx/sites-available/dam-api /etc/nginx/sites-enabled/
sudo certbot --nginx -d dam-api.qide.com
```

```nginx
server {
    server_name dam-api.qide.com;
    client_max_body_size 100M;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

**Option B — Cloudflare Tunnel (recommended — no inbound port open):**
```bash
cloudflared tunnel login
cloudflared tunnel create qide-dam
cloudflared tunnel route dns qide-dam dam-api.qide.com
# create config:
cat > /etc/cloudflared/config.yml <<EOF
tunnel: qide-dam
credentials-file: /root/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: dam-api.qide.com
    service: http://localhost:8000
  - hostname: dam-mcp.qide.com
    service: http://localhost:8001
  - service: http_status:404
EOF
cloudflared service install
```

## Step 7 — Smoke test

```bash
BASE=https://dam-api.qide.com EMAIL=admin@qide.com PASSWORD='<strong>' \
    bash scripts/smoke_test.sh
```

## Step 8 — Backups

Daily PG dump → R2:

```bash
# /etc/cron.daily/dam-pg-backup.sh  (chmod +x)
#!/usr/bin/env bash
set -e
TIMESTAMP=$(date +%Y%m%d_%H%M)
DUMP=/tmp/qidedam_${TIMESTAMP}.sql.gz
docker compose -f /opt/qide-dam/docker-compose.prod.yml exec -T postgres \
    pg_dump -U qidedam qidedam | gzip > "$DUMP"
aws s3 cp "$DUMP" s3://qidedam-backups/pg/${TIMESTAMP}.sql.gz \
    --endpoint-url https://<account_id>.r2.cloudflarestorage.com
rm "$DUMP"
```

Retention: a Cloudflare R2 lifecycle rule on `qidedam-backups` — delete after 30 days.

## Step 9 — Monitoring

- **UptimeRobot** (free) — monitor `https://dam-api.qide.com/v1/healthz` every 5 minutes.
- **Flower** dashboard at `https://flower.qide.com` (proxy to `127.0.0.1:5555`, gate behind Cloudflare Access for admin-only).
- **Webhook delivery dashboard** — view `WebhookDelivery` rows where `status='dead'` in `psql` for stuck integrations.

## Frontend integration

Once deployed, update each frontend's DAM_API base URL:

| Frontend | File | Change |
|----------|------|--------|
| 青玄情报中心 | Cloudflare Worker `wrangler.toml` env | `DAM_API=https://dam-api.qide.com` |
| 乡约顺德 小程序 | `utils/dam.js` | `BASE_URL=https://dam-api.qide.com` |
| Kiln & Ink | `next.config.js` | env var `NEXT_PUBLIC_DAM_API` |
| AiVisible | env var | same |

API keys: log into `/v1/auth/api-keys` as platform admin and issue one per frontend.
