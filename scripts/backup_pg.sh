#!/usr/bin/env bash
# QideDAM · PostgreSQL 全量备份 → Cloudflare R2
#
# 跑在生产服务器（/opt/qide-dam）上。systemd timer 每天凌晨 3:00 触发。
# 也可以手动跑：sudo /opt/qide-dam/scripts/backup_pg.sh
#
# 备份位置：
#   - 本地：/opt/qide-dam/backups/qidedam_YYYYMMDD_HHMMSS.sql.gz（保留最近 7 天）
#   - R2：s3://qidedam-backups/pg/YYYYMMDD_HHMMSS.sql.gz（lifecycle policy 30 天清）
#
# 失败时的退出码：
#   1  pg_dump 失败
#   2  R2 上传失败（数据已 dump 到本地但没上云 → 看本地）
#   3  必需的 env 变量缺失
#
# 日志：/var/log/qidedam-backup.log（systemd journalctl 也有）

set -euo pipefail

# ─── 配置 ─────────────────────────────────────────────────────────
APP_DIR="/opt/qide-dam"
COMPOSE_FILE="${APP_DIR}/docker-compose.prod.yml"
ENV_FILE="${APP_DIR}/.env.production"
BACKUP_DIR="${APP_DIR}/backups"
LOG_FILE="/var/log/qidedam-backup.log"
LOCAL_RETENTION_DAYS=7
LOCK_FILE="/var/run/qidedam-backup.lock"

# ─── 工具 ─────────────────────────────────────────────────────────
log() { echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$LOG_FILE"; }

cleanup() {
    rm -f "$LOCK_FILE"
}
trap cleanup EXIT

# ─── 防止并发 ────────────────────────────────────────────────────
if [ -f "$LOCK_FILE" ]; then
    log "ERROR: backup already running (lock file present)"
    exit 1
fi
touch "$LOCK_FILE"

mkdir -p "$BACKUP_DIR"
mkdir -p "$(dirname "$LOG_FILE")"

# ─── 加载凭证（从 .env.production）─────────────────────────────────
if [ ! -r "$ENV_FILE" ]; then
    log "ERROR: $ENV_FILE not readable"
    exit 3
fi

# 只导出我们需要的几个变量（避免 source 整个 .env 引入意外）
POSTGRES_USER=$(grep '^POSTGRES_USER=' "$ENV_FILE" | cut -d= -f2- | tr -d '"')
POSTGRES_DB=$(grep '^POSTGRES_DB=' "$ENV_FILE" | cut -d= -f2- | tr -d '"')

# Backup-only credentials —— 与主 S3_* 凭证分开放，权限只能写 qidedam-backups
BACKUP_S3_ENDPOINT=$(grep '^BACKUP_S3_ENDPOINT=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' || true)
BACKUP_S3_BUCKET=$(grep '^BACKUP_S3_BUCKET=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' || true)
BACKUP_S3_ACCESS_KEY=$(grep '^BACKUP_S3_ACCESS_KEY=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' || true)
BACKUP_S3_SECRET_KEY=$(grep '^BACKUP_S3_SECRET_KEY=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' || true)

if [ -z "${POSTGRES_USER:-}" ] || [ -z "${POSTGRES_DB:-}" ]; then
    log "ERROR: POSTGRES_USER / POSTGRES_DB missing from $ENV_FILE"
    exit 3
fi

if [ -z "${BACKUP_S3_ENDPOINT:-}" ] || [ -z "${BACKUP_S3_BUCKET:-}" ]; then
    log "ERROR: BACKUP_S3_* env vars missing — run scripts/install_backups.sh first"
    exit 3
fi

# ─── 跑备份 ──────────────────────────────────────────────────────
TIMESTAMP=$(date -u +'%Y%m%d_%H%M%S')
DUMP_FILE="${BACKUP_DIR}/qidedam_${TIMESTAMP}.sql.gz"
R2_KEY="pg/qidedam_${TIMESTAMP}.sql.gz"

log "▶ pg_dump start (db=$POSTGRES_DB user=$POSTGRES_USER)"

cd "$APP_DIR"
if ! docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres \
    pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=plain --no-owner --no-acl \
    | gzip -9 > "$DUMP_FILE"; then
    log "ERROR: pg_dump failed"
    rm -f "$DUMP_FILE"
    exit 1
fi

DUMP_SIZE=$(du -h "$DUMP_FILE" | cut -f1)
log "✓ pg_dump done → $DUMP_FILE ($DUMP_SIZE)"

# 完整性自检：能 gunzip + 末尾有 PostgreSQL EOL 注释
if ! gunzip -t "$DUMP_FILE" 2>/dev/null; then
    log "ERROR: dump file is not a valid gzip"
    rm -f "$DUMP_FILE"
    exit 1
fi

if ! gunzip -c "$DUMP_FILE" | tail -1 | grep -q "PostgreSQL database dump complete"; then
    log "WARN: dump may be incomplete (no end marker)"
fi

# ─── 上传 R2 ─────────────────────────────────────────────────────
log "▶ R2 upload → s3://${BACKUP_S3_BUCKET}/${R2_KEY}"

# 用 docker 跑 awscli 避免主机装依赖
if ! docker run --rm \
    -e AWS_ACCESS_KEY_ID="$BACKUP_S3_ACCESS_KEY" \
    -e AWS_SECRET_ACCESS_KEY="$BACKUP_S3_SECRET_KEY" \
    -e AWS_DEFAULT_REGION=auto \
    -v "${BACKUP_DIR}:/backups:ro" \
    amazon/aws-cli:latest \
    s3 cp "/backups/qidedam_${TIMESTAMP}.sql.gz" \
    "s3://${BACKUP_S3_BUCKET}/${R2_KEY}" \
    --endpoint-url "$BACKUP_S3_ENDPOINT" \
    --no-progress 2>&1 | tee -a "$LOG_FILE"; then
    log "ERROR: R2 upload failed (local copy preserved at $DUMP_FILE)"
    exit 2
fi

log "✓ R2 upload done"

# ─── 清理本地旧文件 ──────────────────────────────────────────────
log "▶ pruning local files older than ${LOCAL_RETENTION_DAYS} days"
find "$BACKUP_DIR" -name "qidedam_*.sql.gz" -mtime "+${LOCAL_RETENTION_DAYS}" -print -delete \
    | while read -r f; do log "  pruned: $f"; done

# ─── 完成 ────────────────────────────────────────────────────────
log "✓ backup_pg.sh done · ${DUMP_SIZE} → s3://${BACKUP_S3_BUCKET}/${R2_KEY}"
exit 0
