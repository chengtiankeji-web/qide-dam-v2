#!/usr/bin/env bash
# QideMatrix v1 · 端到端 smoke test（2026-05-21）
#
# 用法：
#   export TOKEN="<platform_admin JWT 或 dam_live_ API key>"
#   export TENANT_ID="<泽润 tenant UUID>"
#   export BASE="https://dam-api.qidelinktech.com"
#   bash qm_v1_smoke_test.sh
#
# 期望全绿：8 步全部 PASS
set -euo pipefail

# ─── 检查环境 ──────────────────────────────────────────────────
: "${TOKEN:?TOKEN env var required}"
: "${TENANT_ID:?TENANT_ID env var required}"
: "${BASE:=https://dam-api.qidelinktech.com}"

PASS=0
FAIL=0

ok()   { echo "  ✅ $*"; PASS=$((PASS+1)); }
fail() { echo "  ❌ $*"; FAIL=$((FAIL+1)); }

step() { echo ""; echo "═══ $* ═══"; }

API() {
  local method="$1" path="$2"; shift 2
  curl -s -X "$method" "$BASE$path" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    "$@"
}

# ─── T1 · 提交入驻申请 ──────────────────────────────────────────
step "T1 · POST /v1/qm/onboardings"

RESP=$(API POST /v1/qm/onboardings -d "{
  \"factory_name\": \"Smoke Test Factory v1\",
  \"contact_name\": \"Test User\",
  \"contact_email\": \"smoketest+$(date +%s)@qidelinktech.cn\",
  \"product_categories\": [\"acrylic\", \"display-stand\"],
  \"target_markets\": [\"US\", \"AU\"],
  \"export_stage\": \"tried\",
  \"monthly_budget\": \"500-2000\",
  \"desired_services\": [\"建站\", \"社媒\"],
  \"biggest_pain_point\": \"不知道怎么投放海外社媒\",
  \"tenant_id\": \"$TENANT_ID\"
}")

OB_ID=$(echo "$RESP" | jq -r .id 2>/dev/null || echo "")
if [[ -n "$OB_ID" && "$OB_ID" != "null" ]]; then
  ok "onboarding 创建: $OB_ID"
else
  fail "onboarding 创建失败: $RESP"
  exit 1
fi

# ─── T2 · 等 90s 让 drain → 诊断跑完 ──────────────────────────
step "T2 · 等 90s 让事件总线跑完 onboarding → diagnostic.ready"
sleep 90

# ─── T3 · 看诊断 ─────────────────────────────────────────────
step "T3 · GET /v1/qm/diagnostics/by-onboarding/$OB_ID"
DIAG=$(API GET "/v1/qm/diagnostics/by-onboarding/$OB_ID")
DIAG_STATUS=$(echo "$DIAG" | jq -r .status 2>/dev/null || echo "")
READINESS=$(echo "$DIAG" | jq -r .readiness_score 2>/dev/null || echo "0")

if [[ "$DIAG_STATUS" == "ready" ]]; then
  ok "诊断完成 · readiness=$READINESS · tier=$(echo "$DIAG" | jq -r .recommended_tier)"
elif [[ "$DIAG_STATUS" == "running" || "$DIAG_STATUS" == "pending" ]]; then
  echo "  ⏳ 诊断还在跑（status=$DIAG_STATUS · 等多 30s 再看）"
  sleep 30
  DIAG=$(API GET "/v1/qm/diagnostics/by-onboarding/$OB_ID")
  DIAG_STATUS=$(echo "$DIAG" | jq -r .status)
  if [[ "$DIAG_STATUS" == "ready" ]]; then
    ok "诊断完成（延迟）"
  else
    fail "诊断超时 status=$DIAG_STATUS"
  fi
else
  fail "诊断失败 status=$DIAG_STATUS · error=$(echo "$DIAG" | jq -r .error_message)"
fi

# ─── T4 · 看事件流 ───────────────────────────────────────────
step "T4 · 看事件流"
EVENTS=$(API GET "/v1/qm/pipeline-events?subject_id=$OB_ID&limit=20")
EVENT_COUNT=$(echo "$EVENTS" | jq 'length')
DELIVERED=$(echo "$EVENTS" | jq '[.[] | select(.status=="delivered")] | length')
echo "  事件总数: $EVENT_COUNT · 已投递: $DELIVERED"

if [[ "$EVENT_COUNT" -ge 3 && "$DELIVERED" -ge 2 ]]; then
  ok "事件流健康"
  echo "$EVENTS" | jq -r '.[] | "    \(.event_type) [\(.stage)] · \(.status) · attempts=\(.attempts)"'
else
  fail "事件流不完整 · 检查 drain 是否在跑"
fi

# ─── T5 · 看邮件 outbox ──────────────────────────────────────
step "T5 · 看邮件 outbox"
EMAILS=$(API GET "/v1/qm/emails?limit=10")
EMAIL_COUNT=$(echo "$EMAILS" | jq 'length')

if [[ "$EMAIL_COUNT" -ge 1 ]]; then
  ok "邮件 outbox 有 $EMAIL_COUNT 封"
  echo "$EMAILS" | jq -r '.[] | "    \(.template_key) → \(.to_email) · \(.status)"' | head -5
else
  fail "邮件 outbox 空 · 检查 worker 是否在跑"
fi

# ─── T6 · 运营队列 ───────────────────────────────────────────
step "T6 · 运营队列 /v1/qm/onboardings/queue"
QUEUE=$(API GET "/v1/qm/onboardings/queue")
IN_QUEUE=$(echo "$QUEUE" | jq "[.[] | select(.onboarding_id==\"$OB_ID\")] | length")

if [[ "$IN_QUEUE" -eq 1 ]]; then
  ok "新 onboarding 在运营队列中"
else
  fail "新 onboarding 不在队列（queue len=$IN_QUEUE）"
fi

# ─── T7 · 接单 ───────────────────────────────────────────────
step "T7 · 接单 · 启 S4"
ASSIGN_RESP=$(API POST "/v1/qm/onboardings/$OB_ID/assign" -d '{}')
NEW_STAGE=$(echo "$ASSIGN_RESP" | jq -r .current_stage 2>/dev/null || echo "")

if [[ "$NEW_STAGE" == "S4" ]]; then
  ok "已推到 S4 · stage_status=$(echo "$ASSIGN_RESP" | jq -r .stage_status)"
else
  fail "接单失败 stage=$NEW_STAGE"
fi

# ─── T8 · 健康度重算 ─────────────────────────────────────────
step "T8 · 健康度重算"
RECOMPUTE=$(API POST "/v1/qm/health/recompute?target_date=$(date +%F)")
echo "  recompute: $RECOMPUTE"
sleep 5

SNAPSHOT=$(API GET "/v1/qm/health/snapshot")
SNAP_COUNT=$(echo "$SNAPSHOT" | jq 'length')

if [[ "$SNAP_COUNT" -ge 1 ]]; then
  ok "健康度 snapshot $SNAP_COUNT 个 workspace"
else
  fail "健康度 snapshot 空"
fi

# ─── 总结 ────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════"
echo "  PASS: $PASS · FAIL: $FAIL"
echo "════════════════════════════════════════"

if [[ "$FAIL" -gt 0 ]]; then
  echo "❌ smoke test 有 $FAIL 个失败 · 检查上面 fail 行"
  exit 1
else
  echo "✅ 全绿 · QideMatrix v1 端到端通"
  exit 0
fi
