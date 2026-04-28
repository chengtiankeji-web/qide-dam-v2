#!/usr/bin/env bash
# Quick end-to-end smoke test for a running QideDAM instance.
# Usage:  BASE=http://localhost:8000 EMAIL=admin@qide.com PASSWORD=secret bash scripts/smoke_test.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
EMAIL="${EMAIL:-admin@qide.com}"
PASSWORD="${PASSWORD:-changeme}"
TENANT="${TENANT:-qide}"

echo "==> 1. Health"
curl -sf "$BASE/v1/healthz" | grep -q '"status":"ok"'

echo "==> 2. Login"
TOKEN=$(curl -sf -X POST "$BASE/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\",\"tenant_slug\":\"$TENANT\"}" \
  | python -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')

echo "    token: ${TOKEN:0:24}..."

echo "==> 3. /me"
curl -sf "$BASE/v1/auth/me" -H "Authorization: Bearer $TOKEN" | python -m json.tool

echo "==> 4. List tenants"
curl -sf "$BASE/v1/tenants" -H "Authorization: Bearer $TOKEN" | python -m json.tool | head -40

echo "==> 5. List projects"
curl -sf "$BASE/v1/projects" -H "Authorization: Bearer $TOKEN" | python -m json.tool | head -40

echo "==> 6. List assets (expect empty)"
curl -sf "$BASE/v1/assets?page_size=5" -H "Authorization: Bearer $TOKEN" | python -m json.tool

echo
echo "==> All smoke checks passed."
