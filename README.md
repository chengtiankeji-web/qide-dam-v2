# QideDAM v2 · 4-Sprint complete

AI-native multi-tenant Digital Asset Management platform.

## Status: All 4 sprints written (W1-W8)

| Sprint | Scope | Files | Status |
|---|---|---|---|
| 1 (W1-W2) | FastAPI kernel + multi-tenant + assets + R2 + JWT/API-Key + MCP stdio | 57 | ✅ |
| 2 (W3-W4) | Multipart upload + 5 Celery task bodies + Webhooks + MCP HTTP/SSE + 乡约顺德 migration | +21 | ✅ |
| 3 (W5-W6) | 通义千问 Vision auto-tagging + pgvector embeddings + vector search | +5 | ✅ |
| 4 (W7-W8) | Collections + Folders + Workflows + Share Links + Usage meters | +14 | ✅ |

## What's in the box

**16 DB tables**: tenants, projects, users, api_keys, assets, asset_versions, multipart_uploads, webhook_subscriptions, webhook_deliveries, collections, collection_assets, folders, workflows, workflow_steps, share_links, usage_meters.

**45 REST endpoints** across 14 routers (`/v1/auth`, `/v1/tenants`, `/v1/projects`, `/v1/assets`, `/v1/uploads/multipart/*`, `/v1/search/vector`, `/v1/webhooks`, `/v1/collections`, `/v1/folders`, `/v1/workflows`, `/v1/share-links`, `/v1/usage`, `/v1/healthz`, `/p/share/{token}/resolve`).

**12 MCP tools** (stdio + HTTP/SSE transports): list_assets, search_assets, get_asset, register_upload, confirm_upload, multipart_init, multipart_sign_part, multipart_complete, list_projects, update_asset_tags, get_download_url, delete_asset.

**6 Celery task suites**: image (Pillow thumbnails + EXIF), video (ffmpeg), document (pypdf + pdf2image), AI (DashScope qwen-vl-plus tagging + 768-dim text-embedding-v3), webhook delivery (HMAC-SHA256, exponential backoff, 6 attempts), pipeline orchestrator.

**3 alembic migrations**: 001_initial (Sprint 1), 002_webhooks (Sprint 2), 003_sprint4 (Sprint 4).

**Tests**: 24 / 24 passing (no DB / external services required).

## Quick start (local dev)

```bash
cp .env.example .env
docker compose up -d --build
docker compose exec api python -m scripts.init_db \
    --email admin@qide.com --password 'CHANGE_ME' --tenant-slug qide
open http://localhost:8000/docs
```

## Production deploy

Sam picked **GitHub + Hetzner CPX21 + Cloudflare R2**. Full guide: [`docs/DEPLOY.md`](docs/DEPLOY.md).

Quick path:

```bash
# On a fresh Hetzner CPX21:
curl -sSL https://raw.githubusercontent.com/<sam-org>/qide-dam-v2/main/scripts/bootstrap_hetzner.sh | bash
cd /opt/qide-dam && git clone https://github.com/<sam-org>/qide-dam-v2.git .
cp .env.example .env.production && nano .env.production   # fill R2 + JWT secret
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml exec api python -m scripts.init_db --email admin@qide.com --password '...' --tenant-slug qide
```

## Layout

```
qide-dam-v2/
├── app/
│   ├── api/v1/          14 routers
│   ├── core/            config / deps / logging / security
│   ├── db/              SQLAlchemy Base + lazy async session
│   ├── models/          16 ORM models
│   ├── schemas/         Pydantic
│   ├── services/        storage, asset, upload, webhook, ai, search,
│   │                    collection, folder, workflow, share_link, usage
│   ├── workers/         Celery app + image/video/document/ai/webhook + pipeline
│   ├── mcp/             FastMCP server (stdio) + HTTP/SSE bridge
│   └── main.py
├── alembic/versions/    001 + 002 + 003
├── docker/Dockerfile
├── docker-compose.yml          dev (with MinIO)
├── docker-compose.prod.yml     prod (R2 + Flower + Beat)
├── docs/DEPLOY.md              Hetzner + R2 production walkthrough
├── tests/               24 logic tests
├── scripts/             init_db, smoke_test, bootstrap_hetzner, migrate_xiangyue
└── .github/workflows/ci.yml
```

## Five seeded tenants × 9 default projects

| tenant | display | seeded projects |
|---|---|---|
| qide | 祁德 (中台) | core, dam, website, aivisible |
| qingxuan | 青玄 (HK) | kiln-ink, qingxuan-intel |
| zerun | 泽润 (深圳) | cmh |
| hemei | 和美共创 (顺德) | xiangyue-shunde |
| chengtian | 橙天 (Sam 个人) | personal |

## Migrating 乡约顺德 67 assets

```bash
# Build a manifest from `tcb storage list`:
tcb storage list -e cloud1-d3g818fgt7833accf -p merchants/ \
  | awk '{print $1"\t"$2"\t乡约顺德商家"}' > /tmp/xy_manifest.tsv

python -m scripts.migrate_xiangyue \
    --manifest /tmp/xy_manifest.tsv \
    --tenant-slug hemei --project-slug xiangyue-shunde
```

## Webhook signing

Subscribers verify deliveries with:

```python
import hmac, hashlib, time
sig = request.headers["X-Qide-Signature"]   # "t=1700000000,v1=abc..."
ts, _, hex_sig = sig.partition(",v1=")
ts = ts[2:]
expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
assert hmac.compare_digest(expected, hex_sig)
assert abs(int(time.time()) - int(ts)) < 300   # 5-min skew tolerance
```

## AI cost / latency notes

- **Default backend**: DashScope `qwen-vl-plus` (image tagging) + `text-embedding-v3` (768-dim).
- **Stub mode**: with no `DASHSCOPE_API_KEY` / `OPENAI_API_KEY` set, the AI service returns deterministic fake outputs so the pipeline still runs end-to-end during dev.
- **Per-image cost** (qwen-vl-plus 2026 pricing, est.): ~¥0.05 / image tag + ~¥0.001 / embedding. 10 TB of images = roughly ¥6k one-shot.
- **Latency**: ~2-4s per image (sequential VL call → text embedding). The Celery `media` queue handles this concurrently.
<!-- coderabbit smoke 2026-05-14 -->
