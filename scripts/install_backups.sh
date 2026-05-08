#!/usr/bin/env bash
# QideDAM · 一键安装备份系统
#
# 跑法（在生产服务器 119.28.32.166 上）：
#   sudo /opt/qide-dam/scripts/install_backups.sh
#
# 执行内容：
#   1. 检查必需的 BACKUP_S3_* env 变量是否在 .env.production
#   2. 给 backup_*.sh 加 +x 权限
#   3. 写 systemd unit 文件 → /etc/systemd/system/
#   4. enable + start timer（每天 03:00 UTC = 北京 11:00；secrets 每周一 03:30 UTC）
#   5. 跑一次 backup_pg.sh 验证（dry-run 上传 1 字节文件先验权限）
#
# 卸载：
#   sudo systemctl disable --now qidedam-backup-pg.timer
#   sudo systemctl disable --now qidedam-backup-secrets.timer
#   sudo rm /etc/systemd/system/qidedam-backup-*.{service,timer}
#   sudo systemctl daemon-reload

set -euo pipefail

APP_DIR="/opt/qide-dam"
ENV_FILE="${APP_DIR}/.env.production"
SCRIPTS_DIR="${APP_DIR}/scripts"
SYSTEMD_DIR="/etc/systemd/system"

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: please run as root (sudo $0)"
    exit 1
fi

# ─── Step 1: 检查 env 变量 ──────────────────────────────────────
echo "▶ Step 1: checking BACKUP_S3_* env vars in $ENV_FILE"
MISSING=()
for var in BACKUP_S3_ENDPOINT BACKUP_S3_BUCKET BACKUP_S3_ACCESS_KEY BACKUP_S3_SECRET_KEY SECRETS_GPG_PASSPHRASE; do
    if ! grep -q "^${var}=" "$ENV_FILE"; then
        MISSING+=("$var")
    fi
done

if [ ${#MISSING[@]} -gt 0 ]; then
    echo ""
    echo "ERROR: 缺少必需变量："
    for v in "${MISSING[@]}"; do echo "    - $v"; done
    echo ""
    echo "请按以下步骤补齐 $ENV_FILE，然后重跑此脚本："
    echo ""
    echo "  # 1. 在 Cloudflare R2 控制台创建 bucket: qidedam-backups"
    echo "  #    并加 lifecycle rule: pg/* → 30 天后删除 / secrets/* → 90 天"
    echo ""
    echo "  # 2. 在同一控制台创建 R2 API token，权限只勾："
    echo "  #    - Object Read & Write"
    echo "  #    - 限定 bucket: qidedam-backups（重要 · 不要给主 bucket 权限）"
    echo ""
    echo "  # 3. 生成一个 GPG passphrase（用来加密 .env 备份）："
    cat <<'EOF'
  openssl rand -base64 24

  # 4. 把以下行追加到 .env.production：

BACKUP_S3_ENDPOINT=https://<account_id>.r2.cloudflarestorage.com
BACKUP_S3_BUCKET=qidedam-backups
BACKUP_S3_ACCESS_KEY=<step 2 拿到的 ACCESS_KEY_ID>
BACKUP_S3_SECRET_KEY=<step 2 拿到的 SECRET_ACCESS_KEY>
SECRETS_GPG_PASSPHRASE=<step 3 生成的字符串>

  # 5. 把 SECRETS_GPG_PASSPHRASE 也保存到 1Password —— 没它你恢复不了 .env 备份
EOF
    exit 1
fi
echo "  ✓ all required vars present"

# ─── Step 2: 权限 ────────────────────────────────────────────────
echo "▶ Step 2: chmod +x backup scripts"
chmod +x "${SCRIPTS_DIR}/backup_pg.sh"
chmod +x "${SCRIPTS_DIR}/backup_secrets.sh"
chmod +x "${SCRIPTS_DIR}/restore_pg.sh"

# ─── Step 3: 写 systemd unit 文件 ────────────────────────────────
echo "▶ Step 3: install systemd units"

cat > "${SYSTEMD_DIR}/qidedam-backup-pg.service" <<EOF
[Unit]
Description=QideDAM PostgreSQL backup → R2
Documentation=https://github.com/chengtiankeji-web/qide-dam-v2/blob/main/docs/BACKUP.md
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=${SCRIPTS_DIR}/backup_pg.sh
StandardOutput=journal
StandardError=journal
TimeoutStartSec=900

[Install]
WantedBy=multi-user.target
EOF

cat > "${SYSTEMD_DIR}/qidedam-backup-pg.timer" <<EOF
[Unit]
Description=Run QideDAM PG backup daily at 03:00 UTC (11:00 Beijing)

[Timer]
OnCalendar=*-*-* 03:00:00
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > "${SYSTEMD_DIR}/qidedam-backup-secrets.service" <<EOF
[Unit]
Description=QideDAM .env / cloudflared secrets backup → R2 (GPG-encrypted)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=${SCRIPTS_DIR}/backup_secrets.sh
StandardOutput=journal
StandardError=journal
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
EOF

cat > "${SYSTEMD_DIR}/qidedam-backup-secrets.timer" <<EOF
[Unit]
Description=Run QideDAM secrets backup weekly (Mon 03:30 UTC)

[Timer]
OnCalendar=Mon *-*-* 03:30:00
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target
EOF

echo "  ✓ written 4 unit files"

# ─── Step 4: enable + start ─────────────────────────────────────
echo "▶ Step 4: enable + start timers"
systemctl daemon-reload
systemctl enable --now qidedam-backup-pg.timer
systemctl enable --now qidedam-backup-secrets.timer

systemctl list-timers --all | grep qidedam || true

# ─── Step 5: dry-run 验证 ────────────────────────────────────────
echo ""
echo "▶ Step 5: dry-run verify (writing 1B to R2 to confirm credentials)"
TEST_KEY="_install_test_$(date +%s).txt"
echo "ok" | docker run --rm -i \
    -e AWS_ACCESS_KEY_ID="$(grep '^BACKUP_S3_ACCESS_KEY=' "$ENV_FILE" | cut -d= -f2- | tr -d '"')" \
    -e AWS_SECRET_ACCESS_KEY="$(grep '^BACKUP_S3_SECRET_KEY=' "$ENV_FILE" | cut -d= -f2- | tr -d '"')" \
    -e AWS_DEFAULT_REGION=auto \
    amazon/aws-cli:latest \
    s3 cp - "s3://$(grep '^BACKUP_S3_BUCKET=' "$ENV_FILE" | cut -d= -f2- | tr -d '"')/${TEST_KEY}" \
    --endpoint-url "$(grep '^BACKUP_S3_ENDPOINT=' "$ENV_FILE" | cut -d= -f2- | tr -d '"')" \
    && echo "  ✓ R2 credentials work"

# 删测试文件
docker run --rm \
    -e AWS_ACCESS_KEY_ID="$(grep '^BACKUP_S3_ACCESS_KEY=' "$ENV_FILE" | cut -d= -f2- | tr -d '"')" \
    -e AWS_SECRET_ACCESS_KEY="$(grep '^BACKUP_S3_SECRET_KEY=' "$ENV_FILE" | cut -d= -f2- | tr -d '"')" \
    -e AWS_DEFAULT_REGION=auto \
    amazon/aws-cli:latest \
    s3 rm "s3://$(grep '^BACKUP_S3_BUCKET=' "$ENV_FILE" | cut -d= -f2- | tr -d '"')/${TEST_KEY}" \
    --endpoint-url "$(grep '^BACKUP_S3_ENDPOINT=' "$ENV_FILE" | cut -d= -f2- | tr -d '"')" \
    > /dev/null

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  ✓ 备份系统安装完成"
echo "════════════════════════════════════════════════════════════"
echo ""
echo "下次自动备份时间："
systemctl list-timers qidedam-backup-pg.timer qidedam-backup-secrets.timer --no-pager
echo ""
echo "立即手动跑一次（推荐）："
echo "    sudo systemctl start qidedam-backup-pg.service"
echo "    sudo journalctl -u qidedam-backup-pg.service -f"
echo ""
echo "查看备份日志："
echo "    tail -f /var/log/qidedam-backup.log"
echo ""
echo "DR 演练（恢复到测试库）："
echo "    sudo /opt/qide-dam/scripts/restore_pg.sh"
echo ""
