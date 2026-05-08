# QideDAM Cowork Watcher

把 `~/ClaudeCowork/` 文件夹里 Claude（或人）写的任何文件，**实时**自动同步到 DAM。

## 解决的问题

之前 4-29 跑了一次性的批量上传（264 文件入库）。但日常 Claude 在 ClaudeCowork 写文档（备忘录 / 代码 / 设计稿 / 周报），如果靠手动批量同步：
- 频次跟不上：Claude 一天可能写几十个文件
- 容易漏：Sam 忘了跑同步，文件就留在本地
- 没版本：覆盖写没记录

Watcher 解决了这三个：**实时 / 自动 / 不重复**。

## 工作流

```
Claude 在 ~/ClaudeCowork/ 写文件
        │
        ▼ macOS fsevents (watchdog)
debounce 3s（等 atomic write 完成）
        │
        ▼
sha256 计算 + 比对 state.json
        │
        ▼ 已传过 → SKIP
未传过：
  → 按路径决定 tenant + project + tags
  → POST /v1/assets/uploads/presign
  → PUT 到 R2 (presigned URL)
  → POST /v1/assets/{id}/uploads/confirm
  → state.json 记录 sha256 → asset_id
        │
        ▼
Celery worker 自动跑 pipeline：
  - image: Pillow 3 段缩略图 + EXIF
  - document/pdf: 提取页数 + 封面
  - 全部: AI 自动打标 + 向量入 embedding
```

## 安装（一次性 · Sam Mac 上 5 分钟）

### Step 1 · 在 admin SPA 创建 cowork project（如果还没有）

打开 https://dam.qidelinktech.com → Projects → Create

```
tenant:  qide
slug:    cowork
name:    Cowork Workspace（Claude 实时同步）
```

### Step 2 · 在 admin SPA Settings 创建 API key

- 名字：`cowork-watcher · sam-mac-studio`（写清楚是哪台机器，方便日后吊销）
- 复制出来的 dam_live_xxx 值，准备粘到配置文件

### Step 3 · 跑安装脚本

```bash
cd ~/ClaudeCowork/code/qide-dam-v2
bash scripts/install_cowork_watcher_macos.sh
```

第一次跑会：
1. pip 装依赖（watchdog / requests / tomli）
2. 生成默认配置 `~/.qidedam-watcher/config.toml`
3. 提示填 api_key 后再跑一次

### Step 4 · 编辑配置 + 第二次跑

```bash
nano ~/.qidedam-watcher/config.toml
# 找到 api_key = "" 改成 api_key = "dam_live_xxxx"
# 保存退出
```

```bash
bash scripts/install_cowork_watcher_macos.sh   # 第二次跑
```

这次会安装 launchd plist + 加载守护。

### Step 5 · 验证

```bash
# 实时看日志
tail -f ~/.qidedam-watcher/watcher.log

# 在另一个终端创个测试文件
echo "watcher test $(date)" > ~/ClaudeCowork/_watcher_test.md
```

3 秒后日志应输出：

```
▶ UPLOAD _watcher_test.md → qide/cowork / kind=document (XXX B)
  OK asset_id=<uuid>
```

打开 https://dam.qidelinktech.com → Assets → 选 qide tenant + cowork project，应能看到 `_watcher_test.md`。

---

## 配置参考

`~/.qidedam-watcher/config.toml` 可调几类东西：

### `[dam]` 段
- `api_url`：默认生产 URL
- `api_key`：必填
- `default_tenant_slug` / `default_project_slug`：不匹配任何 path_routes 时用
- 如果你想给某个客户账号上传，改成对应的 tenant_slug

### `[watch]` 段
- `root`：默认 `~/ClaudeCowork`
- `debounce_seconds`：文件写入后等多少秒上传（防 atomic-write 中间态被传）
- `max_simple_upload_mb`：超过这个走 multipart（**目前 watcher 不支持 multipart，超出会跳过**·后续 P1 加）

### `[filter]` 段
- `include_extensions`：只传白名单内的扩展名
- `exclude_path_segments`：路径任一段命中就跳（默认排 `.git` / `node_modules` / `__pycache__` / `dist` / `.venv` / `sessions` 等）
- `exclude_file_globs`：文件名 glob（默认排 `*.bak` / `*.tmp` / `*.swp` / `.DS_Store`）
- `max_file_mb`：超出大小直接跳（防误传 ISO / dataset）
- `min_file_bytes`：低于这个跳（防传空文件）

### `[[path_routes]]` 段
按路径前缀决定 tenant + project + 默认标签。第一条匹配优先。默认配了 5 条：

| 路径前缀 | 入到 |
|---------|------|
| `memory/projects/xiangyue-shunde` | hemei / xiangyue-shunde |
| `memory/projects/kiln-ink` | qingxuan / kiln-ink |
| `memory/projects/qide-dam` | qide / qide-dam |
| `code/qide-dam-v2` | qide / qide-dam（标签 +"code"）|
| `handover` | qide / cowork（标签 +"handover"）|

加新规则：编辑 config.toml 加 `[[path_routes]]` 段，重启 watcher：

```bash
launchctl unload  ~/Library/LaunchAgents/com.qide.cowork-dam-watcher.plist
launchctl load -w ~/Library/LaunchAgents/com.qide.cowork-dam-watcher.plist
```

---

## 日常使用

### 查看哪些文件已上传

```bash
cat ~/.qidedam-watcher/state.json | python3 -m json.tool | head -30
```

或在 admin SPA → Assets 页按 created_at desc 排序看最新。

### 暂停 watcher

```bash
launchctl unload ~/Library/LaunchAgents/com.qide.cowork-dam-watcher.plist
```

恢复：

```bash
launchctl load -w ~/Library/LaunchAgents/com.qide.cowork-dam-watcher.plist
```

### 重新上传某个文件

```bash
# 删除该文件的 sha256 记录，watcher 下次见到会重传
python3 -c "
import json
state = json.load(open('$HOME/.qidedam-watcher/state.json'))
state = {k: v for k, v in state.items() if 'XXXX' not in v.get('path', '')}
json.dump(state, open('$HOME/.qidedam-watcher/state.json', 'w'), indent=2)
"
touch ~/ClaudeCowork/path/to/file.md   # 让 fs event 重新触发
```

### 一次性回扫整个 ClaudeCowork（启动后）

Watcher 只对 fs event 反应。如果某些文件在 watcher 启动前就存在，不会自动扫。要手动一次：

```bash
find ~/ClaudeCowork -type f -newer ~/.qidedam-watcher/state.json -exec touch {} \;
```

或者更彻底：用 4-29 留下的 batch upload 工具：

```bash
cd ~/ClaudeCowork/handover/qidedam-bulk-import
# 看 README-给-Sam-看.md
```

---

## 安全考量

1. **API key 限到一个 project** —— admin SPA 创 key 时把 project 限定到 cowork（只能写这个 project，不能跨）。如果你想 path_routes 能投递到多个 project，那 key 必须有更广的权限 —— 自行权衡。
2. **API key 撤销** —— 换电脑或 key 泄漏，admin SPA 一键 DELETE，watcher 立即开始 401。
3. **不会上传 .env / 密钥** —— 默认 exclude_extensions 没含 .env / .key / .pem，且 exclude_file_globs 也排了一些。**但你最好还是别把这些文件放 ~/ClaudeCowork/。**
4. **不会上传 git 历史** —— `.git` / `.svn` 在 exclude_path_segments 内。
5. **state.json 含 asset_id 不含密钥** —— 即使被人看到也只是元数据。

---

## 故障排查

### Watcher 没启动 / launchctl list 看不到

```bash
cat ~/.qidedam-watcher/launchd.err.log
```

常见错误：
- `python3: command not found` → 检查 plist 里 python 路径
- `ImportError: watchdog` → pip3 install 失败 · 试 `pip3 install --break-system-packages watchdog requests tomli`

### 文件不上传

```bash
tail -50 ~/.qidedam-watcher/watcher.log
```

可能原因：
- 扩展名不在白名单（在 config.toml include_extensions 里加）
- 路径含 exclude 段（如 node_modules）
- 文件太大（max_file_mb）
- API key 错（401 错误会显示）
- project 不存在（log 会显示 `project not found`）

### 上传卡住

```bash
# 看是不是网络问题
curl -I https://dam-api.qidelinktech.com/v1/healthz
```

watcher 自带 3 次重试，超过会 GAVE UP 跳到下一个文件。

### 想关一段时间

```bash
launchctl unload ~/Library/LaunchAgents/com.qide.cowork-dam-watcher.plist
```

state.json 不会丢，恢复后还能继续。

---

## 卸载

```bash
launchctl unload ~/Library/LaunchAgents/com.qide.cowork-dam-watcher.plist
rm ~/Library/LaunchAgents/com.qide.cowork-dam-watcher.plist
rm -rf ~/.qidedam-watcher    # 删配置 + 状态 + 日志
# pip 包不删（系统级影响小）
```

也别忘了在 admin SPA Settings 撤销 cowork-watcher 那个 API key。

---

## 路线图

| 项 | 说明 | 优先级 |
|---|------|-------|
| Multipart 大文件支持 | 当前超 30MB 跳过，加 multipart 流程 | P1 |
| 删除事件同步 | 文件被删时也调 DELETE /v1/assets/{id}（默认不开 · 防误删）| P2 |
| Linux systemd-user 安装脚本 | 给在 Linux 跑 ClaudeCowork 的同事 | P2 |
| Conflict resolution | 同一 sha256 的不同路径处理 | P3 |
| Bandwidth throttle | 大批量上传时不打满网络 | P3 |
| Status indicator menu bar app | macOS 状态栏显示当前队列 / 已传计数 | P3 |
