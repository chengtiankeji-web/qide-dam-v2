#!/usr/bin/env bash
# QideDAM · 从 R2 备份恢复 PostgreSQL（DR 演练 + 生产灾难恢复）
#
# 跑法：
#   sudo /opt/qide-dam/scripts/restore_pg.sh                         # 列出可用备份
#   sudo /opt/qide-dam/scripts/restore_pg.sh pg/qidedam_20260507_030000.sql.gz
#                                                                     # 恢复指定文件
#
# 默认行为是恢复到 _restore_test 数据库（演练用，不动生产）。
# 加 --to-prod 才会真覆盖 qidedam 主库（**会清空当前数据**）。

set -euo pipefail

APP_DIR="/opt/qide-dam"
ENV_FILE="${APP_DIR}/.env.production"
COMPOSE_FILE="${APP_DIR}/docker-compose.prod.yml"
RESTORE_DIR="${APP_DIR}/restore"

mkdir -p "$RESTORE_DIR"

POSTGRES_USER=$(grep '^POSTGRES_USER=' "$ENV_FILE" | cut -d= -f2- | tr -d '"')
POSTGRES_DB=$(grep '^POSTGRES_DB=' "$ENV_FILE" | cut -d= -f2- | tr -d '"')
BACKUP_S3_ENDPOINT=$(grep '^BACKUP_S3_ENDPOINT=' "$ENV_FILE" | cut -d= -f2- | tr -d '"')
BACKUP_S3_BUCKET=$(grep '^BACKUP_S3_BUCKET=' "$ENV_FILE" | cut -d= -f2- | tr -d '"')
BACKUP_S3_ACCESS_KEY=$(grep '^BACKUP_S3_ACCESS_KEY=' "$ENV_FILE" | cut -d= -f2- | tr -d '"')
BACKUP_S3_SECRET_KEY=$(grep '^BACKUP_S3_SECRET_KEY=' "$ENV_FILE" | cut -d= -f2- | tr -d '"')

awscli() {
    docker run --rm \
        -e AWS_ACCESS_KEY_ID="$BACKUP_S3_ACCESS_KEY" \
        -e AWS_SECRET_ACCESS_KEY="$BACKUP_S3_SECRET_KEY" \
        -e AWS_DEFAULT_REGION=auto \
        -v "${RESTORE_DIR}:/restore" \
        amazon/aws-cli:latest \
        --endpoint-url "$BACKUP_S3_ENDPOINT" \
        "$@"
}

# ─── 模式 1：列出可用备份 ─────────────────────────────────────────
if [ $# -eq 0 ]; then
    echo "== 可用备份（最近 30 天）=="
    awscli s3 ls "s3://${BACKUP_S3_BUCKET}/pg/" --human-readable
    echo ""
    echo "用法：sudo $0 pg/qidedam_YYYYMMDD_HHMMSS.sql.gz [--to-prod]"
    echo ""
    echo "默认恢复到 ${POSTGRES_DB}_restore_test（生产库不受影响）"
    echo "--to-prod 才会真覆盖生产库（注意：会先 DROP 再 RECREATE）"
    exit 0
fi

R2_KEY="$1"
TO_PROD="${2:-}"

# ─── 下载 ──────────────────────────────────────────────────────
LOCAL_FILE="${RESTORE_DIR}/$(basename "$R2_KEY")"
echo "▶ downloading s3://${BACKUP_S3_BUCKET}/${R2_KEY}"
awscli s3 cp "s3://${BACKUP_S3_BUCKET}/${R2_KEY}" "/restore/$(basename "$R2_KEY")"

# ─── 完整性自检 ──────────────────────────────────────────────────
echo "▶ integrity check"
gunzip -t "$LOCAL_FILE" || { echo "ERROR: bad gzip"; exit 1; }
gunzip -c "$LOCAL_FILE" | tail -1 | grep -q "PostgreSQL database dump complete" \
    || { echo "WARN: end marker missing — dump may be incomplete"; }

# ─── 恢复目标库 ──────────────────────────────────────────────────
cd "$APP_DIR"

if [ "$TO_PROD" = "--to-prod" ]; then
    TARGET_DB="$POSTGRES_DB"
    echo ""
    echo "⚠️  警告：你即将恢复到生产数据库 [$TARGET_DB]"
    echo "    这会丢失当前所有数据。"
    echo ""
    read -p "    确认继续？输入 'YES I AM SURE' 才会继续：" CONFIRM
    if [ "$CONFIRM" != "YES I AM SURE" ]; then
        echo "abort."
        exit 1
    fi

    # 停掉 api / worker（避免恢复中有写入）
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" stop api worker
else
    TARGET_DB="${POSTGRES_DB}_restore_test"
    echo "▶ creating restore-test db: $TARGET_DB"
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres \
        psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE IF EXISTS $TARGET_DB;"
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres \
        psql -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE $TARGET_DB OWNER $POSTGRES_USER;"
fi

# ─── 跑 restore ─────────────────────────────────────────────────
echo "▶ restoring → $TARGET_DB"
gunzip -c "$LOCAL_FILE" \
    | docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres \
        psql -U "$POSTGRES_USER" -d "$TARGET_DB" -v ON_ERROR_STOP=1

# ─── 完整性验证 ──────────────────────────────────────────────────
echo "▶ verifying restored db"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres \
    psql -U "$POSTGRES_USER" -d "$TARGET_DB" -c \
    "SELECT version_num FROM alembic_version;"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres \
    psql -U "$POSTGRES_USER" -d "$TARGET_DB" -c \
    "SELECT
        (SELECT count(*) FROM tenants) AS tenants,
        (SELECT count(*) FROM projects) AS projects,
        (SELECT count(*) FROM users) AS users,
        (SELECT count(*) FROM assets) AS assets,
        (SELECT count(*) FROM audit_events) AS audit_events,
        (SELECT count(*) FROM vault_items) AS vault_items;"

if [ "$TO_PROD" = "--to-prod" ]; then
    echo "▶ restarting api / worker"
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d api worker
fi

echo ""
echo "✓ restore complete → $TARGET_DB"
echo "  本地下载文件保留在: $LOCAL_FILE"
[ "$TO_PROD" != "--to-prod" ] && echo "  这是演练库 · 生产数据库未动 · 检查无误后可手动 DROP"
