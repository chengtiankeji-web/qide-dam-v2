#!/usr/bin/env bash
# QideDAM Cowork Watcher · macOS 安装脚本
#
# 跑法（Sam Mac 上）：
#   bash scripts/install_cowork_watcher_macos.sh
#
# 行为：
#   1. 用 pip3 装依赖（watchdog requests tomli）
#   2. 让 Sam 创建并填配置文件 ~/.qidedam-watcher/config.toml
#   3. 在 dam.qidelinktech.com 创建一个名为 "cowork" 的 project（手动 / Sam 做）
#   4. 写 launchd plist → ~/Library/LaunchAgents/com.qide.cowork-dam-watcher.plist
#   5. launchctl load
#
# 卸载：
#   launchctl unload ~/Library/LaunchAgents/com.qide.cowork-dam-watcher.plist
#   rm ~/Library/LaunchAgents/com.qide.cowork-dam-watcher.plist

set -euo pipefail

SCRIPT_PATH="$(cd "$(dirname "$0")" && pwd)/cowork_dam_watcher.py"
PLIST_NAME="com.qide.cowork-dam-watcher"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"
LOG_DIR="$HOME/.qidedam-watcher"

# ─── 检查 ──────────────────────────────────────────────────────
if [[ "$(uname)" != "Darwin" ]]; then
    echo "ERROR: 这个脚本只在 macOS 上跑。Linux 用 install_cowork_watcher_systemd.sh"
    exit 1
fi

if [ ! -f "$SCRIPT_PATH" ]; then
    echo "ERROR: 找不到 $SCRIPT_PATH"
    exit 1
fi

PYTHON_BIN="$(command -v python3 || true)"
if [ -z "$PYTHON_BIN" ]; then
    echo "ERROR: python3 没装 · brew install python3"
    exit 1
fi

# ─── Step 1: 依赖 ──────────────────────────────────────────────
echo "▶ Step 1: 安装 Python 依赖"
"$PYTHON_BIN" -m pip install --user --quiet watchdog requests tomli || {
    echo "ERROR: pip install 失败 · 试试 pip3 install --break-system-packages watchdog requests tomli"
    exit 1
}
echo "  ✓ 依赖装好"

# ─── Step 2: 跑一次脚本生成默认配置 ─────────────────────────────
echo ""
echo "▶ Step 2: 生成默认配置（如果不存在）"
mkdir -p "$LOG_DIR"
"$PYTHON_BIN" "$SCRIPT_PATH" 2>&1 | head -10 || true

if ! grep -q '^api_key = "."' "$LOG_DIR/config.toml" 2>/dev/null; then
    if grep -q '^api_key = ""' "$LOG_DIR/config.toml" 2>/dev/null; then
        echo ""
        echo "════════════════════════════════════════════════════════════"
        echo "  请先到 https://dam.qidelinktech.com Settings 创建 API key"
        echo "  名字建议：cowork-watcher · $(scutil --get ComputerName 2>/dev/null || hostname)"
        echo "  scope：默认就行（assets:write 自带）"
        echo ""
        echo "  然后编辑 $LOG_DIR/config.toml"
        echo "  填入 api_key 后再次跑此脚本继续。"
        echo "════════════════════════════════════════════════════════════"
        exit 0
    fi
fi

# ─── Step 3: 写 launchd plist ──────────────────────────────────
echo ""
echo "▶ Step 3: 写 launchd plist → $PLIST_PATH"

mkdir -p "$(dirname "$PLIST_PATH")"
cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_BIN}</string>
        <string>${SCRIPT_PATH}</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>ThrottleInterval</key>
    <integer>30</integer>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/launchd.out.log</string>

    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/launchd.err.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>${HOME}</string>
    </dict>
</dict>
</plist>
EOF

# ─── Step 4: 加载 ──────────────────────────────────────────────
echo ""
echo "▶ Step 4: 加载 launchd"

# 如已加载先卸载（重装场景）
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load -w "$PLIST_PATH"

# 验证
sleep 1
if launchctl list | grep -q "$PLIST_NAME"; then
    echo "  ✓ launchd 已加载"
else
    echo "  ⚠️ launchctl list 没看到 · 检查 ${LOG_DIR}/launchd.err.log"
fi

# ─── 完成 ──────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════"
echo "  ✓ Cowork Watcher 安装完成"
echo "════════════════════════════════════════════════════════════"
echo ""
echo "实时日志（按 Ctrl+C 退出）："
echo "    tail -f ${LOG_DIR}/watcher.log"
echo ""
echo "启停："
echo "    launchctl unload  $PLIST_PATH    # 停"
echo "    launchctl load -w $PLIST_PATH    # 起"
echo ""
echo "改配置后重启："
echo "    launchctl unload  $PLIST_PATH"
echo "    launchctl load -w $PLIST_PATH"
echo ""
echo "测试：在 ~/ClaudeCowork/ 下创建一个 .md 文件 → 1-3 秒后日志会显示 UPLOAD"
echo ""
