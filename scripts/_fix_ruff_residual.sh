#!/usr/bin/env bash
# Fix 残留的 31 个 ruff lint case
# 用法: cd ~/ClaudeCowork/code/qide-dam-v2 && bash scripts/_fix_ruff_residual.sh
set -euo pipefail

cd "$(dirname "$0")/.."  # repo root

echo "===== 1. E702: contacts_service.py 6 处分号合并 ====="
python3 <<'PY'
import re
p = "app/services/crm/contacts_service.py"
s = open(p).read()
before = s.count("; changed = True")
s2 = re.sub(
    r"(\s+)existing\.(\w+) = (\w+); changed = True",
    r"\1existing.\2 = \3\n\1changed = True",
    s,
)
open(p, "w").write(s2)
print(f"  fixed {before} sites")
PY

echo "===== 2. B007: 未用 loop 变量加下划线 ====="
sed -i '' 's/for dirpath, _, filenames in os.walk/for _dirpath, _, filenames in os.walk/' app/services/intake_service.py
sed -i '' 's/for factor_name, factor_data in breakdown.items()/for _factor_name, factor_data in breakdown.items()/' tests/crm/test_classification_advanced.py
echo "  intake_service.py + test_classification_advanced.py fixed"

echo "===== 3. F841: 删未用变量 started ====="
sed -i '' '/started = entry.get("started_at", "")/d' scripts/cowork_dam_watcher.py
echo "  cowork_dam_watcher.py fixed"

echo "===== 4. N802: _DEFAULT_TENANT_FALLBACK → _default_tenant_fallback ====="
sed -i '' 's/_DEFAULT_TENANT_FALLBACK/_default_tenant_fallback/g' app/workers/tasks_cleanup.py
echo "  tasks_cleanup.py fixed"

echo "===== 5. PLR1714: 合并比较 ====="
sed -i '' 's|raw == "1" \* 64 or raw == "0" \* 64|raw in {"1" * 64, "0" * 64}|' app/services/image_transform_service.py
echo "  image_transform_service.py fixed"

echo "===== 6. UP036: 删 sys.version_info < 3.11 兼容块 ====="
python3 <<'PY'
p = "scripts/cowork_dam_watcher.py"
s = open(p).read()
old = """    if sys.version_info >= (3, 11):
        import tomllib  # noqa
    else:
        try:
            import tomli as tomllib  # noqa
        except ImportError:
            tomllib = None  # type: ignore"""
new = "    import tomllib  # noqa"
if old in s:
    open(p, "w").write(s.replace(old, new))
    print("  UP036 block removed")
else:
    print("  WARN: UP036 anchor mismatch · manual check needed at scripts/cowork_dam_watcher.py:68-76")
PY

echo "===== 7. pyproject.toml per-file-ignores (17 个合理 case) ====="
python3 <<'PY'
p = "pyproject.toml"
s = open(p).read()

block = """
[tool.ruff.lint.per-file-ignores]
# N814: `Project as _P` / `Asset as _A` 短别名是有意为之·提升内嵌查询可读性
"app/api/v1/assets.py" = ["N814"]
"app/api/v1/usage.py" = ["N814"]
"app/api/v1/wecom.py" = ["N814", "E402"]
# E402: image_transform_service.py 后半部分是 phase B 懒加载模块·intentional
"app/services/image_transform_service.py" = ["E402"]
# B017: 加密 InvalidTag 在不同 backend 抛不同 Exception·pytest.raises(Exception) intentional
"tests/test_v3_security.py" = ["B017"]
"""

if "[tool.ruff.lint.per-file-ignores]" in s:
    print("  already present · skip")
else:
    open(p, "a").write(block)
    print("  appended to pyproject.toml")
PY

echo ""
echo "===== 8. 终残 ruff check ====="
ruff check . --statistics 2>&1 | tail -15 || true

echo ""
echo "===== 9. 文件改动统计 ====="
git diff --stat | tail -15

echo ""
echo "===== 全部完成 · 看上面输出 ====="
