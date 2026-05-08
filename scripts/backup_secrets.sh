#!/usr/bin/env bash
# QideDAM · 加密备份 .env.production（含 VAULT_KEK / VAULT_HMAC / SECRET_KEY）
#
# .env.production 不在 pg_dump 里，必须独立备份。但凭证不能裸传 R2 ——
# 必须先用 GPG 对称加密，passphrase 仅 Sam 持有（1Password 里）。
#
# 跑法：
#   sudo /opt/qide-dam/scripts/backup_secrets.sh
# 默认每周一凌晨 3:30 由 systemd timer 触发（频率比 PG 备份低，因为 .env 改得少）。
#
# 解密步骤（DR 演练）：
#   gpg -d --batch --passphrase 'XXXX' \
#     /tmp/qidedam_secrets_YYYYMMDD.tar.gz.gpg | tar -xz

set -euo pipefail

APP_DIR="/opt/qide-dam"
ENV_FILE="${APP_DIR}/.env.production"
BACKUP_DIR="${APP_DIR}/backups"
LOG_FILE="/var/log/qidedam-backup.log"

log() { echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [secrets] $*" | tee -a "$LOG_FILE"; }

# ─── 加载凭证 ────────────────────────────────────────────────────
if [ ! -r "$ENV_FILE" ]; then
    log "ERROR: $ENV_FILE not readable"
    exit 3
fi

BACKUP_S3_ENDPOINT=$(grep '^BACKUP_S3_ENDPOINT=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' || true)
BACKUP_S3_BUCKET=$(grep '^BACKUP_S3_BUCKET=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' || true)
BACKUP_S3_ACCESS_KEY=$(grep '^BACKUP_S3_ACCESS_KEY=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' || true)
BACKUP_S3_SECRET_KEY=$(grep '^BACKUP_S3_SECRET_KEY=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' || true)
SECRETS_GPG_PASSPHRASE=$(grep '^SECRETS_GPG_PASSPHRASE=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' || true)

if [ -z "${SECRETS_GPG_PASSPHRASE:-}" ]; then
    log "ERROR: SECRETS_GPG_PASSPHRASE missing from $ENV_FILE"
    log "       Generate one with: openssl rand -base64 24"
    log "       Save it to 1Password AND add to .env.production"
    exit 3
fi

# ─── 打包 + 加密 ─────────────────────────────────────────────────
TIMESTAMP=$(date -u +'%Y%m%d_%H%M%S')
TAR_FILE="${BACKUP_DIR}/qidedam_secrets_${TIMESTAMP}.tar.gz"
GPG_FILE="${TAR_FILE}.gpg"
R2_KEY="secrets/qidedam_secrets_${TIMESTAMP}.tar.gz.gpg"

mkdir -p "$BACKUP_DIR"

log "▶ packing .env.production + cloudflared config"
tar -czf "$TAR_FILE" \
    -C / \
    "opt/qide-dam/.env.production" \
    $([ -d /etc/cloudflared ] && echo "etc/cloudflared/") \
    2>/dev/null

log "▶ GPG encrypting (AES-256, passphrase from .env)"
gpg --batch --yes \
    --passphrase "$SECRETS_GPG_PASSPHRASE" \
    --symmetric --cipher-algo AES256 \
    --output "$GPG_FILE" \
    "$TAR_FILE"
rm -f "$TAR_FILE"  # 立即删未加密版本

GPG_SIZE=$(du -h "$GPG_FILE" | cut -f1)
log "✓ encrypted bundle ready: $GPG_FILE ($GPG_SIZE)"

# ─── 上传 R2（separate path · separate retention）──────────────────
log "▶ R2 upload → s3://${BACKUP_S3_BUCKET}/${R2_KEY}"

if ! docker run --rm \
    -e AWS_ACCESS_KEY_ID="$BACKUP_S3_ACCESS_KEY" \
    -e AWS_SECRET_ACCESS_KEY="$BACKUP_S3_SECRET_KEY" \
    -e AWS_DEFAULT_REGION=auto \
    -v "${BACKUP_DIR}:/backups:ro" \
    amazon/aws-cli:latest \
    s3 cp "/backups/qidedam_secrets_${TIMESTAMP}.tar.gz.gpg" \
    "s3://${BACKUP_S3_BUCKET}/${R2_KEY}" \
    --endpoint-url "$BACKUP_S3_ENDPOINT" \
    --no-progress 2>&1 | tee -a "$LOG_FILE"; then
    log "ERROR: R2 upload failed (encrypted local copy preserved)"
    exit 2
fi

# 本地仅保留 4 份历史（密钥不变频繁备份没意义）
find "$BACKUP_DIR" -name "qidedam_secrets_*.tar.gz.gpg" -type f \
    -printf "%T@ %p\n" | sort -rn | tail -n +5 | cut -d' ' -f2- | xargs -r rm -v \
    | tee -a "$LOG_FILE"

log "✓ backup_secrets.sh done"
exit 0
