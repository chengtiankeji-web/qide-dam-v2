#!/usr/bin/env bash
# QideDAM v3 P0-3 收尾补丁 · 端到端验证脚本
#
# 跑前提：部署 commit (assets.py secret 边界补丁) 完成
# 跑法：bash scripts/verify_secret_boundary.sh <JWT>
#
# 期望全 PASS · 任何 FAIL 都是真 bug · 贴日志找我修

set -eo pipefail
# 注：不开 -u（nounset）· python | bash subshell + 偶发空字符串组合下
# 报 "VAR?: unbound" 误伤 · 下面的 :- 兜底足够防卫

if [ $# -lt 1 ]; then
    echo "用法: $0 <JWT_TOKEN>"
    echo ""
    echo "JWT 从 admin SPA localStorage 拿："
    echo "  浏览器 → F12 → Application → Local Storage → dam.qidelinktech.com"
    echo "  找 qidedam-auth.state.token 复制 eyJ... 那串"
    exit 1
fi

TOKEN="$1"
API="${API:-https://dam-api.qidelinktech.com}"
COWORK="56866880-9c00-4132-90f5-831c003d56ac"  # qide tenant 的 cowork project

PASS=0
FAIL=0

ok()   { echo "  ✓ PASS · $1"; PASS=$((PASS+1)); }
fail() { echo "  ✗ FAIL · $1"; FAIL=$((FAIL+1)); }

# ═══════════════════════════════════════════════════════════
echo "▶ Setup: 创建测试 vault + 测试 api_key"

VAULT_RESP=$(curl -s -X POST "$API/v1/vault" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"project_id\":\"$COWORK\",\"vault_kind\":\"login\",\"title\":\"secret-boundary-test\",\"payload\":{\"username\":\"u\",\"password\":\"P\",\"domain\":\"x.com\"}}")
VAULT_ID=$(echo "$VAULT_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))")
ASSET_ID=$(echo "$VAULT_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('asset_id',''))")
echo "  vault_id=$VAULT_ID  asset_id=$ASSET_ID"

KEY_RESP=$(curl -s -X POST "$API/v1/auth/api-keys" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d '{"name":"secret-boundary-test","scopes":["assets:read","assets:write"]}')
APIKEY=$(echo "$KEY_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('raw_key',''))")
KEY_ID=$(echo "$KEY_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))")
echo "  api_key=${APIKEY:0:20}...  id=$KEY_ID"

KEY_VAULT_RESP=$(curl -s -X POST "$API/v1/auth/api-keys" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d '{"name":"secret-boundary-test-with-vault-scope","scopes":["assets:read","vault:reveal"]}')
APIKEY_VAULT=$(echo "$KEY_VAULT_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('raw_key',''))")
KEY_VAULT_ID=$(echo "$KEY_VAULT_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))")
echo "  api_key (vault scope)=${APIKEY_VAULT:0:20}...  id=$KEY_VAULT_ID"

# 即使中途 fail trap 也要跑 cleanup 把 vault + 两个 api_key 删掉
cleanup() {
    echo ""
    echo "▶ Cleanup (trap)"
    [ -n "${VAULT_ID:-}" ] && curl -s -X DELETE "$API/v1/vault/$VAULT_ID" \
        -H "Authorization: Bearer $TOKEN" -o /dev/null 2>/dev/null && echo "  · vault deleted"
    [ -n "${KEY_ID:-}" ] && curl -s -X DELETE "$API/v1/auth/api-keys/$KEY_ID" \
        -H "Authorization: Bearer $TOKEN" -o /dev/null 2>/dev/null && echo "  · api_key deleted"
    [ -n "${KEY_VAULT_ID:-}" ] && curl -s -X DELETE "$API/v1/auth/api-keys/$KEY_VAULT_ID" \
        -H "Authorization: Bearer $TOKEN" -o /dev/null 2>/dev/null && echo "  · api_key (vault scope) deleted"
}
trap cleanup EXIT

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Test Cases"
echo "═══════════════════════════════════════════════════════════"

# ─── Test 1 · api_key 普通 list /v1/assets 不应包含 secret ──
echo ""
echo "Test 1: api_key (no vault scope) GET /v1/assets · 应不见 vault_login"
RESP=$(curl -s "$API/v1/assets?kind=vault_login&page_size=20" -H "X-DAM-API-Key: $APIKEY")
COUNT=$(echo "$RESP" | python3 -c "import sys,json
try: d = json.load(sys.stdin); print(d.get('total', -1))
except Exception as e: print(-1, file=sys.stderr); print(-1)" 2>/dev/null || echo -1)
COUNT="${COUNT:--1}"
[ "$COUNT" = "0" ] && ok "返回 0 条 vault_login（secret 已过滤）" || fail "返回 $COUNT 条 vault_login（应该是 0）"

# ─── Test 2 · api_key 显式 ?include_secret=true 仍应被屏蔽 ──
echo ""
echo "Test 2: api_key 显式 ?include_secret=true · 无 vault:reveal scope · 仍应被屏蔽"
RESP=$(curl -s "$API/v1/assets?kind=vault_login&include_secret=true&page_size=20" -H "X-DAM-API-Key: $APIKEY")
COUNT=$(echo "$RESP" | python3 -c "import sys,json
try: d = json.load(sys.stdin); print(d.get('total', -1))
except Exception as e: print(-1, file=sys.stderr); print(-1)" 2>/dev/null || echo -1)
COUNT="${COUNT:--1}"
[ "$COUNT" = "0" ] && ok "无权 caller 强制 include_secret 被静默忽略 → 返回 0" || fail "返回 $COUNT 条（应该 0 · 无 scope 不该 honor）"

# ─── Test 3 · api_key (vault:reveal scope) ?include_secret=true 应能看到 ──
echo ""
echo "Test 3: api_key (有 vault:reveal scope) ?include_secret=true · 应看到测试 vault"
RESP=$(curl -s "$API/v1/assets?kind=vault_login&include_secret=true&page_size=20" -H "X-DAM-API-Key: $APIKEY_VAULT")
COUNT=$(echo "$RESP" | python3 -c "import sys,json
try: d = json.load(sys.stdin); print(d.get('total', -1))
except Exception as e: print(-1, file=sys.stderr); print(-1)" 2>/dev/null || echo -1)
COUNT="${COUNT:--1}"
[ "$COUNT" -ge "1" ] && ok "有 scope 能看到（total=$COUNT）" || fail "返回 $COUNT 条（应该 ≥ 1）"

# ─── Test 4 · api_key 直接 GET /v1/assets/{vault_asset_id} 应 403 ──
echo ""
echo "Test 4: api_key (no vault scope) GET /v1/assets/{vault_asset_id} · 应 403"
HTTP=$(curl -s -o /dev/null -w "%{http_code}" "$API/v1/assets/$ASSET_ID" -H "X-DAM-API-Key: $APIKEY")
[ "$HTTP" = "403" ] && ok "HTTP $HTTP" || fail "HTTP $HTTP（应该 403）"

# ─── Test 5 · api_key (vault:reveal scope) GET /v1/assets/{id} 应 200 ──
echo ""
echo "Test 5: api_key (有 vault:reveal scope) GET /v1/assets/{vault_asset_id} · 应 200"
HTTP=$(curl -s -o /dev/null -w "%{http_code}" "$API/v1/assets/$ASSET_ID" -H "X-DAM-API-Key: $APIKEY_VAULT")
[ "$HTTP" = "200" ] && ok "HTTP $HTTP" || fail "HTTP $HTTP（应该 200）"

# ─── Test 6 · api_key 调 reveal 仍应被拒（核心防线）──
echo ""
echo "Test 6: api_key (no vault scope) POST /vault/{id}/reveal · 应 403（这条之前就过了 · 回归测试）"
HTTP=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$API/v1/vault/$VAULT_ID/reveal?purpose=verify" -H "X-DAM-API-Key: $APIKEY")
[ "$HTTP" = "403" ] && ok "HTTP $HTTP" || fail "HTTP $HTTP（应该 403）"

# ─── Test 7 · JWT 用户列 list 应能看到 secret（admin SPA 需要展示）──
echo ""
echo "Test 7: JWT GET /v1/assets · 默认应看到 vault_login（admin SPA UX）"
RESP=$(curl -s "$API/v1/assets?kind=vault_login&page_size=20" -H "Authorization: Bearer $TOKEN")
COUNT=$(echo "$RESP" | python3 -c "import sys,json
try: d = json.load(sys.stdin); print(d.get('total', -1))
except Exception as e: print(-1, file=sys.stderr); print(-1)" 2>/dev/null || echo -1)
COUNT="${COUNT:--1}"
[ "$COUNT" -ge "1" ] && ok "JWT 默认看到 secret（total=$COUNT）" || fail "JWT 看不到（total=$COUNT · 应 ≥ 1）"

# ─── Test 8 · JWT 显式 ?include_secret=false 应被尊重（opt-out）──
echo ""
echo "Test 8: JWT ?include_secret=false · 应 0 vault_login（opt-out 永远准）"
RESP=$(curl -s "$API/v1/assets?kind=vault_login&include_secret=false&page_size=20" -H "Authorization: Bearer $TOKEN")
COUNT=$(echo "$RESP" | python3 -c "import sys,json
try: d = json.load(sys.stdin); print(d.get('total', -1))
except Exception as e: print(-1, file=sys.stderr); print(-1)" 2>/dev/null || echo -1)
COUNT="${COUNT:--1}"
[ "$COUNT" = "0" ] && ok "opt-out 生效 · total=$COUNT" || fail "opt-out 没生效 · total=$COUNT"

# 主流程结束前清理（trap 也会兜底 · 这里显式跑一次）
cleanup
trap - EXIT

# ═══════════════════════════════════════════════════════════
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  结果: $PASS PASS · $FAIL FAIL"
echo "═══════════════════════════════════════════════════════════"
[ "$FAIL" = "0" ] && exit 0 || exit 1
