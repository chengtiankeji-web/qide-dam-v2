# QideDAM 备份与灾难恢复

## 设计原则

QideDAM 的数据有三类，备份策略各不相同：

| 数据类 | 内容 | 备份方式 | 频率 | 保留期 |
|--------|-----|---------|------|--------|
| 业务数据 | PostgreSQL 全部表（含 vault_items 加密 payload）| `pg_dump` → R2 | 每天 03:00 UTC | R2 30 天 + 本地 7 天 |
| 凭证 | `.env.production`（含 VAULT_KEK_HEX）+ cloudflared config | GPG-AES256 加密 → R2 | 每周一 03:30 UTC | R2 90 天 + 本地 4 份 |
| 对象存储 | R2 上的资产文件 | **不需要备份** —— R2 自带 11 个 9 持久性 + 跨区域复制可选 | — | — |

**为什么不备份 R2 资产**：R2 本身就是冷存储，11 个 9 持久性（10⁻¹¹ 年丢失率）。如果担心账号风险，可以开 Cloudflare 的 cross-region replication（每月多 ¥几十）。

**为什么 PG + 凭证分两套桶 / 两套凭证**：principle of separation —— 即使数据库 backup 凭证泄露，攻击者拿到的也是加密的 PG dump（vault_items 仍然 AES-GCM 加密），打不开 Vault 内容；而凭证桶的备份是 GPG 二次加密，只有 1Password 里的 passphrase 才能解。

---

## 一次性安装（10 分钟 · 在生产服务器跑）

### Step 1 · Cloudflare R2 控制台准备

1. **创建 bucket**：`qidedam-backups`（与主 bucket `qidedam` 分开）
2. **加 lifecycle policy**：
   - 路径 `pg/` → 30 天后删除
   - 路径 `secrets/` → 90 天后删除
3. **创建 API token**（仅这一个 bucket 的读写权限）：
   - Cloudflare Dashboard → R2 → Manage R2 API Tokens → Create
   - Permissions: **Object Read & Write**
   - **限定 Bucket: qidedam-backups**（重要：不要给主 bucket 权限，防被横向攻击）
   - 拿到 ACCESS_KEY_ID + SECRET_ACCESS_KEY

### Step 2 · 在 `.env.production` 加 5 个变量

```bash
ssh -i ~/.ssh/qidedam.pem ubuntu@119.28.32.166
sudo nano /opt/qide-dam/.env.production
```

追加：

```
BACKUP_S3_ENDPOINT=https://<account_id>.r2.cloudflarestorage.com
BACKUP_S3_BUCKET=qidedam-backups
BACKUP_S3_ACCESS_KEY=<step 1.3 拿到的>
BACKUP_S3_SECRET_KEY=<step 1.3 拿到的>
SECRETS_GPG_PASSPHRASE=<openssl rand -base64 24>
```

把 `SECRETS_GPG_PASSPHRASE` 同时存到 **1Password 团队保险柜**，分类 "QideDAM Secrets"，标题 "Backup GPG passphrase"。**没它你以后恢复不了 .env 备份。**

### Step 3 · 跑安装脚本

```bash
cd /opt/qide-dam
sudo git pull origin main          # 拉最新 scripts/
sudo bash scripts/install_backups.sh
```

脚本会：
1. 检查 5 个 env 变量是否齐
2. 给 backup_*.sh 加可执行权限
3. 安装 4 个 systemd unit（service + timer × 2）
4. enable + start 两个 timer
5. 跑一次 dry-run（向 R2 上传 1 字节文件验权限）

输出最后会显示下次自动备份时间。

### Step 4 · 立即跑一次手动备份验证

```bash
sudo systemctl start qidedam-backup-pg.service
sudo journalctl -u qidedam-backup-pg.service -f
```

应看到：
```
▶ pg_dump start (db=qidedam user=qidedam)
✓ pg_dump done → /opt/qide-dam/backups/qidedam_YYYYMMDD_HHMMSS.sql.gz (XX MB)
▶ R2 upload → s3://qidedam-backups/pg/qidedam_YYYYMMDD_HHMMSS.sql.gz
✓ R2 upload done
```

---

## 日常运维

### 查看下次备份时间

```bash
systemctl list-timers qidedam-backup-*
```

### 查看备份历史

**本地**：
```bash
ls -lh /opt/qide-dam/backups/
```

**R2**：
```bash
sudo /opt/qide-dam/scripts/restore_pg.sh   # 不带参数 = 列出 R2 上所有 PG 备份
```

### 手动跑一次备份

```bash
sudo systemctl start qidedam-backup-pg.service       # PG
sudo systemctl start qidedam-backup-secrets.service  # 凭证
```

### 看历史日志

```bash
# systemd journal
sudo journalctl -u qidedam-backup-pg.service -n 100

# 自定义日志（更全）
sudo tail -100 /var/log/qidedam-backup.log
```

---

## 灾难恢复（DR）

### 演练（不动生产 · 推荐每月跑一次）

```bash
# 1. 列出可用备份
sudo /opt/qide-dam/scripts/restore_pg.sh

# 2. 恢复到测试库 qidedam_restore_test（默认行为）
sudo /opt/qide-dam/scripts/restore_pg.sh pg/qidedam_20260507_030000.sql.gz

# 3. 脚本自动跑完整性验证：
#    - 检查 alembic_version
#    - 计数 tenants / projects / users / assets / audit_events / vault_items
#
# 4. 检查无误后手动 DROP 测试库
docker compose --env-file .env.production -f docker-compose.prod.yml \
    exec postgres psql -U qidedam -d postgres \
    -c "DROP DATABASE qidedam_restore_test;"
```

### 真灾难恢复 · 生产覆盖

```bash
# ⚠️ 这会清空当前生产数据
sudo /opt/qide-dam/scripts/restore_pg.sh pg/qidedam_YYYYMMDD_HHMMSS.sql.gz --to-prod
# 必须输入完整 'YES I AM SURE' 才会继续
```

恢复后流程：
1. 脚本自动停 api / worker（防恢复中有写入）
2. DROP + RECREATE qidedam 库 + 跑 restore
3. 验证完整性
4. 自动重启 api / worker
5. **手动跑一次 healthz** + 烟雾测试，确认服务正常

### 凭证恢复（.env / cloudflared）

```bash
# 从 R2 拉加密备份
docker run --rm \
    -e AWS_ACCESS_KEY_ID="$BACKUP_S3_ACCESS_KEY" \
    -e AWS_SECRET_ACCESS_KEY="$BACKUP_S3_SECRET_KEY" \
    -e AWS_DEFAULT_REGION=auto \
    -v /tmp:/tmp \
    amazon/aws-cli:latest \
    s3 cp s3://qidedam-backups/secrets/qidedam_secrets_YYYYMMDD_HHMMSS.tar.gz.gpg \
    /tmp/secrets.tar.gz.gpg \
    --endpoint-url $BACKUP_S3_ENDPOINT

# 用 1Password 里的 passphrase 解密
gpg -d --batch --passphrase '从 1P 复制' /tmp/secrets.tar.gz.gpg | tar -xzC /
# 这会还原 /opt/qide-dam/.env.production + /etc/cloudflared/
```

---

## 完整灾难恢复 SOP（服务器整个挂了）

如果腾讯云 Lighthouse 实例彻底丢了（账号被封 / 数据中心毁了），从零重建：

1. **新 VPS**：开一台新的 Lighthouse / Vultr / Hetzner / 阿里云 ECS
2. **跑 bootstrap_hetzner.sh**：装 Docker + Nginx + Certbot + ufw + fail2ban
3. **拉代码**：`git clone github.com/chengtiankeji-web/qide-dam-v2 /opt/qide-dam`
4. **拉凭证 backup**：从 R2 用 1Password 里的 passphrase 解出 .env.production + cloudflared 配置
5. **重建容器**：`docker compose --env-file .env.production -f docker-compose.prod.yml up -d`
6. **跑 alembic upgrade head**（让结构跟上）
7. **拉最新 PG backup**：`scripts/restore_pg.sh pg/最新.sql.gz --to-prod`
8. **起 cloudflared**：会自动指向新 IP（Tunnel 是出站连 CF 的，无需改 DNS）
9. **healthz 验证 + 烟雾测试**

理论 RTO（恢复时间目标）：**< 2 小时**（从 0 起）。
理论 RPO（恢复点目标）：**< 24 小时**（每天一次备份）。如果要降到分钟级，配 PG WAL streaming 到 R2（P2 路线）。

---

## 备份成本

| 项 | 月成本 |
|---|--------|
| R2 存储 30 天 PG 备份（假设每份 100MB × 30 = 3GB）| ¥0.5（R2 ¥0.18/GB·月）|
| R2 存储 90 天 secrets 备份（每份 5KB × 13 ≈ 0）| ~¥0 |
| R2 出站（每月 1 次 DR 演练 ~100MB）| **¥0**（R2 出站免费）|
| Class A 操作（PUT × 30 + LIST × 几次）| ~¥0.01 |
| **合计** | **< ¥1 / 月** |

可以忽略。

---

## P2 升级路线（可选）

| 升级项 | 说明 | 复杂度 |
|--------|-----|--------|
| WAL streaming | PG WAL 实时推到 R2 → RPO 降到分钟级 | 中（需配 wal-g 或 pgbackrest）|
| Cross-region R2 replication | R2 自带 · 一键开 · 多 ¥XX/月 | 低 |
| 跨云备份 | R2 → 阿里云 OSS（防 CF 封号）| 中（加 cron 定时同步）|
| 自动化 DR 测试 | 每月 systemd timer 自动跑 restore_pg.sh + 对比 alembic_version | 低 |
| Backup 监控告警 | 备份失败 → 企微 / 邮件通知 Sam | 低（在 backup_pg.sh 末尾加 curl 调企微）|

---

## 故障排查

| 症状 | 原因 | 解 |
|------|-----|----|
| `pg_dump failed` | api / postgres 容器没跑 | `docker compose ps` 查 |
| `R2 upload failed: 403` | BACKUP_S3_* 凭证错 / token 没限到对应 bucket | 重新生成 R2 token 限定 qidedam-backups |
| `R2 upload failed: NoSuchBucket` | bucket 名打错 / 没在控制台创建 | 控制台手动建 |
| timer 不触发 | systemctl 没 enable / 时间错（用 UTC）| `systemctl list-timers --all` |
| dump 文件空 | postgres 容器名错 / docker compose 不读 .env | 必须显式 `--env-file .env.production` |
| GPG 解密报错 | passphrase 错 / 文件损坏 | 1Password 复制 passphrase · gpg -d 单独验证 |
