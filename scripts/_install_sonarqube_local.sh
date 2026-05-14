#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# SonarQube 本地 docker 一键装 + 配置（macOS）
# ═══════════════════════════════════════════════════════════════════
#
# 用法：
#   bash scripts/_install_sonarqube_local.sh
#
# 流程：
#   1. 写 ~/qide-quality/docker-compose.yml
#   2. docker compose up -d + 等 healthy（首次 60-120 秒）
#   3. 提示 Sam 浏览器登录改密 + 建 project + 拿 token
#   4. 写 sonar-project.properties 给 qide-dam-v2 + qide-dam-admin
#   5. brew install sonar-scanner（如未装）
#   6. 真跑一次扫描验证
#
# 每个交互步骤前 prompt y/N · 防误按
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

QUALITY_DIR="${HOME}/qide-quality"
BACKEND_REPO="${HOME}/ClaudeCowork/code/qide-dam-v2"
FRONTEND_REPO="${HOME}/ClaudeCowork/code/qide-dam-admin"

# ─── 工具函数 ─────────────────────────────────────────────────────────

color()   { printf "\033[%sm%s\033[0m\n" "$1" "$2"; }
header()  { echo; color "1;34" "═══ $* ═══"; }
ok()      { color "32" "  ✓ $*"; }
warn()    { color "33" "  ⚠ $*"; }
fatal()   { color "31" "  ✗ $*"; exit 1; }

confirm() {
  local ans
  read -r -p "  $* (y/N): " ans
  if [[ ! "${ans}" =~ ^[Yy] ]]; then
    color "33" "  → 中止"
    exit 0
  fi
}

# ─── 前置检查 ─────────────────────────────────────────────────────────

color "1;36" "╔═══════════════════════════════════════════════════════════════╗"
color "1;36" "║  SonarQube 本地 docker 一键装 · macOS · 6 步                    ║"
color "1;36" "╚═══════════════════════════════════════════════════════════════╝"
echo
echo "  目录：${QUALITY_DIR}"
echo "  后端 repo：${BACKEND_REPO}"
echo "  前端 repo：${FRONTEND_REPO}"
echo

command -v docker >/dev/null 2>&1 || fatal "docker 没装 · 先装 Docker Desktop"
docker info >/dev/null 2>&1 || fatal "docker daemon 没运行 · 启动 Docker Desktop"

if ! command -v brew >/dev/null 2>&1; then
  warn "brew 没装 · sonar-scanner 步骤会跳过 · 建议先装 brew"
fi

[[ -d "${BACKEND_REPO}" ]] || warn "${BACKEND_REPO} 不存在 · 跳过后端配置"
[[ -d "${FRONTEND_REPO}" ]] || warn "${FRONTEND_REPO} 不存在 · 跳过前端配置"

confirm "开始？"

# ─── 第 1 步 · 写 docker-compose.yml ──────────────────────────────────

header "[1/6] 写 ${QUALITY_DIR}/docker-compose.yml"

mkdir -p "${QUALITY_DIR}"
cd "${QUALITY_DIR}"

cat > docker-compose.yml << 'COMPOSE'
# SonarQube 本地 community edition · 装 by Claude 2026-05-14
services:
  sonarqube:
    image: sonarqube:community
    container_name: qide-sonarqube
    ports:
      - "127.0.0.1:9000:9000"  # 只 bind localhost · 不向外网开放
    environment:
      SONAR_ES_BOOTSTRAP_CHECKS_DISABLE: "true"  # 跳过 Elasticsearch 引导检查
    volumes:
      - sonarqube_data:/opt/sonarqube/data
      - sonarqube_logs:/opt/sonarqube/logs
      - sonarqube_extensions:/opt/sonarqube/extensions
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "wget", "--quiet", "--tries=1", "--spider", "http://localhost:9000/api/system/status"]
      interval: 10s
      timeout: 5s
      retries: 30
      start_period: 60s

volumes:
  sonarqube_data:
  sonarqube_logs:
  sonarqube_extensions:
COMPOSE

ok "docker-compose.yml 写入"

# ─── 第 2 步 · docker compose up -d + 等 healthy ─────────────────────

header "[2/6] 启动 SonarQube 容器（首次 60-120 秒 · 后续秒起）"
confirm "继续？"

docker compose up -d
echo
echo "  → 等容器 healthy（最多 5 分钟）..."

for i in $(seq 1 60); do
  status=$(docker inspect --format='{{.State.Health.Status}}' qide-sonarqube 2>/dev/null || echo "starting")
  if [[ "${status}" == "healthy" ]]; then
    ok "SonarQube healthy"
    break
  fi
  printf "  . %s (%d/60)\n" "${status}" "${i}"
  sleep 5
done

if [[ "${status:-}" != "healthy" ]]; then
  warn "5 分钟还没 healthy · 查日志：docker logs qide-sonarqube --tail 50"
  warn "继续往下走 · 你可以浏览器试 http://localhost:9000 看能不能打开"
fi

# ─── 第 3 步 · 浏览器初始化（手动） ────────────────────────────────────

header "[3/6] 浏览器手动操作：登录 + 改密 + 建项目 + 拿 token"
echo
echo "  1) 打开 浏览器 → http://localhost:9000"
echo "  2) 用 admin / admin 登录"
echo "  3) 强制改密码 · 用 1Password 存（建议：QideSonar2026!）"
echo "  4) 左上角 → Projects → Create Project → 选 'Manually'"
echo "     · Project key: qide-dam-v2"
echo "     · Display name: QideDAM v2 Backend"
echo "  5) 选 'Locally' → Generate a token"
echo "     · Name: cli-scanner"
echo "     · Expires: 30 days (建议) 或 No expiration"
echo "     · 复制 token（一次性显示 · 错过就重生成）"
echo "  6) 同样再建一个 project：qide-dam-admin / QideDAM Admin SPA（用同 token）"
echo
echo "  跑完上面 6 步 · 回这里粘 token："
echo

read -r -p "  粘 SonarQube token: " SONAR_TOKEN
if [[ -z "${SONAR_TOKEN}" ]]; then
  fatal "token 不能为空"
fi
if [[ ! "${SONAR_TOKEN}" =~ ^[a-zA-Z0-9_-]+$ ]]; then
  warn "token 看起来格式不对 · 但还是用一下（可能有特殊字符）"
fi

# 暂存到 ~/qide-quality/.env · gitignore'd
cat > "${QUALITY_DIR}/.env" << EOF
# SonarQube 本地 token · 装 by Claude 2026-05-14
# 不要 commit 这个文件
SONAR_TOKEN=${SONAR_TOKEN}
EOF
chmod 600 "${QUALITY_DIR}/.env"
ok "token 存到 ${QUALITY_DIR}/.env（chmod 600）"

# ─── 第 4 步 · 写 sonar-project.properties ─────────────────────────────

header "[4/6] 写 sonar-project.properties 到 2 个 repo"

if [[ -d "${BACKEND_REPO}" ]]; then
  cat > "${BACKEND_REPO}/sonar-project.properties" << 'PROPS'
sonar.projectKey=qide-dam-v2
sonar.projectName=QideDAM v2 Backend

sonar.sources=app,scripts
sonar.tests=tests

sonar.python.version=3.11
sonar.exclusions=**/__pycache__/**,**/migrations/**,**/dist/**,**/.venv/**,**/*.tsbuildinfo

sonar.host.url=http://localhost:9000
# sonar.token 通过 CLI 传入 · 不写文件防泄漏
PROPS
  ok "sonar-project.properties → qide-dam-v2"
fi

if [[ -d "${FRONTEND_REPO}" ]]; then
  cat > "${FRONTEND_REPO}/sonar-project.properties" << 'PROPS'
sonar.projectKey=qide-dam-admin
sonar.projectName=QideDAM Admin SPA

sonar.sources=src
sonar.tests=src
sonar.test.inclusions=**/*.test.ts,**/*.test.tsx

sonar.exclusions=**/node_modules/**,**/dist/**,**/.wrangler/**,**/components/ui/**

sonar.typescript.tsconfigPath=tsconfig.json

sonar.host.url=http://localhost:9000
PROPS
  ok "sonar-project.properties → qide-dam-admin"
fi

# ─── 第 5 步 · brew install sonar-scanner ─────────────────────────────

header "[5/6] 装 sonar-scanner CLI（一次性）"

if command -v sonar-scanner >/dev/null 2>&1; then
  ok "sonar-scanner 已装 · $(sonar-scanner --version 2>&1 | grep -E 'SonarScanner CLI|INFO.*sonar-scanner' | head -1)"
else
  if command -v brew >/dev/null 2>&1; then
    confirm "brew install sonar-scanner？"
    brew install sonar-scanner
    ok "sonar-scanner 装上"
  else
    warn "没 brew · 手动装：https://docs.sonarsource.com/sonarqube-community-build/analyzing-source-code/scanners/sonarscanner/"
    fatal "中止"
  fi
fi

# ─── 第 6 步 · 真跑一次扫描验证 ─────────────────────────────────────────

header "[6/6] 首次扫描 · qide-dam-v2 后端"
confirm "扫一下后端（~30-60 秒 · 看终端输出 + 浏览器 dashboard）？"

cd "${BACKEND_REPO}"
sonar-scanner -Dsonar.token="${SONAR_TOKEN}"

echo
ok "后端扫描完成 · 看 http://localhost:9000/dashboard?id=qide-dam-v2"

echo
confirm "接着扫前端 qide-dam-admin？"

cd "${FRONTEND_REPO}"
sonar-scanner -Dsonar.token="${SONAR_TOKEN}"

echo
ok "前端扫描完成 · 看 http://localhost:9000/dashboard?id=qide-dam-admin"

# ─── 收尾 ────────────────────────────────────────────────────────────

color "1;32" "═══════════════════════════════════════════════════════════════"
color "1;32" "  SonarQube 装完 · 全部走完"
color "1;32" "═══════════════════════════════════════════════════════════════"
echo
echo "  Dashboard："
echo "    http://localhost:9000/dashboard?id=qide-dam-v2"
echo "    http://localhost:9000/dashboard?id=qide-dam-admin"
echo
echo "  以后再扫："
echo "    cd ~/ClaudeCowork/code/qide-dam-v2"
echo "    source ~/qide-quality/.env && sonar-scanner -Dsonar.token=\$SONAR_TOKEN"
echo
echo "  推荐每周跑一次 / 大 release 前必跑。"
echo "  容器自动 restart · Docker Desktop 关了启回来 SonarQube 自动起。"
echo
echo "  停掉：cd ~/qide-quality && docker compose down"
echo "  日志：docker logs qide-sonarqube --tail 100"
