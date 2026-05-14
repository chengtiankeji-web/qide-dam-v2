#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# v3 P1.3 Phase 2-5 一键部署脚本（长期可行路径）
# ═══════════════════════════════════════════════════════════════════
#
# 用法：
#   cd ~/ClaudeCowork/code/qide-dam-v2
#   bash scripts/_deploy_phase_2345.sh
#
# 跳过 prompt（不推荐）：
#   bash scripts/_deploy_phase_2345.sh --yes
#
# 中途断了重跑：
#   每步都是幂等的 · 可以从头重跑（已完成的 alembic 升级自动跳过）·
#   backfill 用 state.json 续跑 · 不会重复处理已成功的 asset
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

PEM="$HOME/.ssh/qidedam.pem"
SERVER="ubuntu@119.28.32.166"
SERVER_PATH="/opt/qide-dam"
DC="sudo docker compose --env-file .env.production"  # 服务器上 docker compose 前缀

AUTO_YES=0
if [[ "${1:-}" == "--yes" || "${1:-}" == "-y" ]]; then
  AUTO_YES=1
fi

# ─── 工具函数 ─────────────────────────────────────────────────────────

color()   { printf "\033[%sm%s\033[0m\n" "$1" "$2"; }
header()  { echo; color "1;34" "═══ $* ═══"; }
ok()      { color "32" "  ✓ $*"; }
warn()    { color "33" "  ⚠ $*"; }
fatal()   { color "31" "  ✗ $*"; exit 1; }

confirm() {
  if [[ $AUTO_YES -eq 1 ]]; then
    return 0
  fi
  local ans
  read -r -p "  $* (y/N): " ans
  if [[ ! "$ans" =~ ^[Yy] ]]; then
    color "33" "  → 用户跳过 · 中止脚本"
    exit 0
  fi
}

remote() {
  ssh -i "$PEM" "$SERVER" "cd $SERVER_PATH && $*"
}

# ─── 前置检查 ─────────────────────────────────────────────────────────

cd "$(dirname "$0")/.." || fatal "脚本必须在 qide-dam-v2 项目根目录附近"
PROJECT_ROOT="$PWD"
[[ -f pyproject.toml ]] || fatal "当前目录不像 qide-dam-v2 (no pyproject.toml)"
[[ -f "$PEM" ]] || fatal "SSH key 不在 $PEM · 请确认或修改脚本"

color "1;36" "╔═══════════════════════════════════════════════════════════════╗"
color "1;36" "║  v3 P1.3 Phase 2-5 部署 · DB 层 sha256 强制 (long-term path)  ║"
color "1;36" "╚═══════════════════════════════════════════════════════════════╝"
echo
echo "  本地仓库：$PROJECT_ROOT"
echo "  目标服务器：$SERVER"
echo "  SSH key：$PEM"
echo
echo "  7 步流程（每步前会问 y/N · 不连贯）："
echo "    [1/7] 本地 commit + push 改动"
echo "    [2/7] 服务器拉新代码 + 重建 docker image"
echo "    [3/7] 装 alembic 012 (CHECK NOT VALID · 不阻塞存量)"
echo "    [4/7] Pilot backfill 5 行（dry-run → execute）"
echo "    [5/7] Backfill 剩下 ~140 行"
echo "    [6/7] 验证 0 不合规行"
echo "    [7/7] 装 alembic 013 (VALIDATE + NOT NULL · 最终封印)"
echo

confirm "全部开始？"

# ─── 第 1 步 · 本地 commit + push ────────────────────────────────────

header "[1/7] 本地 commit + push 改动"

echo "  当前分支：$(git rev-parse --abbrev-ref HEAD)"
echo "  改动统计："
git diff --stat HEAD 2>/dev/null | tail -15 || git status --short
echo

if git diff --quiet HEAD && [[ -z "$(git status --porcelain)" ]]; then
  warn "工作树干净 · 没有改动要 commit · 跳过"
else
  confirm "Commit + push 上面这些改动？"

  git add -A

  COMMIT_MSG="feat: v3 P1.3 phase 2-5 · DB-layer sha256 enforcement + structural fixes

Long-term-correct path for 100% data-integrity invariants:
- New asset_service.upload_inline_content() helper · atomic register+PUT+confirm
  with cleanup-on-failure · used by consolidate.apply (smart intake to follow)
- consolidate.py archive failures structured (ArchiveFailure list · no silent except)
- alembic 012 · CHECK constraint sha256 = '^[a-f0-9]{64}\$' NOT VALID
- alembic 013 (gated by backfill complete) · VALIDATE + ALTER NOT NULL
- scripts/backfill_asset_sha256.py · 145 alive empty-sha rows · state.json resumable
- dedup_by_name SQL ROW_NUMBER rewrite · OOM-safe at 100k+ scale
- dedup_by_name + Asset.id tie-breaker · idempotent guarantee restored
- live-summary HTTP 404 on missing project (no silent tenant fallback)
- alembic 010 docstring · NOW() tx-time semantics explained"

  git commit -m "$COMMIT_MSG"
  git push
  ok "已 push 到 $(git rev-parse --abbrev-ref HEAD)"
fi

# ─── 第 2 步 · 服务器 git pull + build ───────────────────────────────

header "[2/7] 服务器拉新代码 + 重建 docker image"
confirm "继续？"

echo "  → 检测服务器上当前 git 分支..."
SERVER_BRANCH=$(remote "git rev-parse --abbrev-ref HEAD")
LOCAL_BRANCH=$(git rev-parse --abbrev-ref HEAD)
echo "    服务器分支: $SERVER_BRANCH · 本地分支: $LOCAL_BRANCH"

if [[ "$SERVER_BRANCH" != "$LOCAL_BRANCH" ]]; then
  warn "分支不一致！服务器在 '$SERVER_BRANCH' · 你 push 到 '$LOCAL_BRANCH'"
  echo "    选 1：服务器 fetch + checkout 你的分支（临时部署 · 不推荐长期保留）"
  echo "    选 2：本地合并到 main · 服务器自动用 main（推荐 · 走正规流程）"
  echo
  echo "  推荐：先 Ctrl+C 退出本脚本 · 然后："
  echo "    gh pr merge 1 --squash --delete-branch"
  echo "    git checkout main && git pull"
  echo "    bash scripts/_deploy_phase_2345.sh"
  echo
  confirm "或者临时让服务器切到 '$LOCAL_BRANCH' 分支？（部署完务必切回 main）"

  echo "  → 服务器 fetch + checkout $LOCAL_BRANCH..."
  remote "git fetch origin $LOCAL_BRANCH"
  remote "git checkout $LOCAL_BRANCH"
  remote "git pull origin $LOCAL_BRANCH"
  warn "服务器现在在 $LOCAL_BRANCH · 部署完后请切回 main（脚本最后会提醒）"
else
  echo "  → git pull on server..."
  remote "git pull"
fi

echo "  → 检测可用 docker compose 服务..."
SERVICES=$(remote "$DC config --services" | tr -d '\r' | tr '\n' ' ')
echo "    服务: $SERVICES"

# 找出实际存在的 services（api 必备 · worker/beat 可选）
BUILD_SVC="api"
for svc in worker beat; do
  if echo " $SERVICES " | grep -q " $svc "; then
    BUILD_SVC="${BUILD_SVC} $svc"
  else
    warn "service '$svc' 不在 compose · 跳过"
  fi
done
echo "  → docker build ${BUILD_SVC}（~30-60 秒）..."
remote "$DC build ${BUILD_SVC}"
ok "build 完成"

# ─── 第 3 步 · alembic 012 ───────────────────────────────────────────

header "[3/7] 装 alembic 012 · CHECK NOT VALID · 不阻塞存量"
confirm "继续？"

echo "  → 重启 ${BUILD_SVC} 容器（拉新镜像）..."
remote "$DC up -d --force-recreate ${BUILD_SVC}"
sleep 6  # 等 api 容器健康

echo "  → alembic upgrade 012_sha256_check ..."
remote "$DC exec -T api alembic upgrade 012_sha256_check"
ok "012 装上 · 新写入立即受约束 · 存量不阻塞"

# ─── 第 4 步 · Pilot backfill ───────────────────────────────────────

header "[4/7] Pilot backfill 5 行 · dry-run 先看 sha 算出来正确"
confirm "继续？"

echo "  → dry-run 5 行..."
remote "$DC exec -T api python3 scripts/backfill_asset_sha256.py --dry-run --limit 5"

echo
confirm "上面的 sha 看上去正常吗？要真跑 --execute 5 行吗？"

remote "$DC exec -T api python3 scripts/backfill_asset_sha256.py --execute --limit 5"
ok "pilot 5 行已 backfill"

# ─── 第 5 步 · Backfill 全量 ────────────────────────────────────────

header "[5/7] Backfill 剩下所有空 sha 行 · --continue 复用 state.json"
echo "  预计：~140 行 · ~217 MB · R2 GET 成本 < ¥0.10 · 时间 1-3 分钟"
confirm "继续？"

remote "$DC exec -T api python3 scripts/backfill_asset_sha256.py --execute --continue"
ok "全量 backfill 跑完 · 看上面输出确认 stuck/failed 是 0"

# ─── 第 6 步 · 验证 ──────────────────────────────────────────────────

header "[6/7] 验证 0 不合规行（前置 sanity check · 必须 = 0）"

BAD=$(remote "$DC exec -T postgres psql -U qidedam -d qidedam -tA -c \"SELECT COUNT(*) FROM assets WHERE deleted_at IS NULL AND (sha256 IS NULL OR sha256 = '' OR sha256 !~ '^[a-f0-9]{64}\$');\"" | tr -d '[:space:]')

echo "  → 不合规行数: $BAD"
if [[ "$BAD" != "0" ]]; then
  warn "$BAD 行不合规 · alembic 013 会拒绝 VALIDATE"
  echo
  echo "  下一步排查："
  echo "  1) ssh -i $PEM $SERVER 'cat /tmp/qidedam_backfill_sha256.state.json' | jq '.stuck, .failed'"
  echo "  2) stuck 是 R2 上不存在的对象 · 应该 hard_delete"
  echo "  3) failed 是临时错误 · 重跑 backfill --continue 大概率能补上"
  echo
  fatal "中止部署 · 修完不合规行后重新跑本脚本"
fi
ok "0 不合规 · 可以装 013"

# ─── 第 7 步 · alembic 013 最终封印 ─────────────────────────────────

header "[7/7] 装 alembic 013 · VALIDATE + NOT NULL · 最终封印"
echo "  注意：装完后 sha256 是 DB 层硬约束 · 任何空写入会被 PG 直接拒"
echo "        downgrade 只能撤 NOT NULL · 不能撤 VALIDATE（PG 限制）"
confirm "确定要装吗？"

remote "$DC exec -T api alembic upgrade 013_sha256_not_null"
ok "013 装上 · 100% accuracy 不变式 DB 层强制"

# ─── 验证终态 ───────────────────────────────────────────────────────

header "✓ 全部完工 · 验证终态"

echo "  → 当前 alembic 版本："
remote "$DC exec -T api alembic current"

echo
echo "  → assets 表 sha256 列约束："
remote "$DC exec -T postgres psql -U qidedam -d qidedam -c \"\\d assets\"" | grep -A 2 "sha256\|chk_assets_sha256" | head -20

echo
color "1;32" "═══════════════════════════════════════════════════════════════"
color "1;32" "  全部完成 · v3 P1.3 phase 2-5 已上线"
color "1;32" "═══════════════════════════════════════════════════════════════"
echo
echo "  长期不变式已 DB 层封印："
echo "    ✓ sha256 NOT NULL"
echo "    ✓ CHECK sha256 ~ '^[a-f0-9]{64}\$' (VALID)"
echo "    ✓ partial unique (project_id, sha256) WHERE alive (alembic 010)"
echo "    ✓ upload_inline_content() helper · 失败自动清孤儿"
echo "    ✓ dedup_by_name SQL ROW_NUMBER · OOM-safe + idempotent"
echo "    ✓ live-summary 404 on missing project · 不静默 fallback"
echo
echo "  后续若有任何路径漏 sha · PG 直接拒 · 不再依赖应用层正确性。"

# ─── 分支提醒（若服务器切到 feature 分支了） ────────────────────────
if [[ "${SERVER_BRANCH:-}" != "${LOCAL_BRANCH:-}" && -n "${SERVER_BRANCH:-}" ]]; then
  echo
  warn "服务器现在在 $LOCAL_BRANCH · 建议合并 PR 后切回 main："
  echo "    本地：gh pr merge 1 --squash --delete-branch && git checkout main && git pull"
  echo "    SSH：ssh -i $PEM $SERVER 'cd $SERVER_PATH && git checkout main && git pull'"
fi
