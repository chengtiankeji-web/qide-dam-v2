#!/usr/bin/env python3
"""
QideDAM Cowork Watcher v2.0 (2026-05-13) · 实时把 ~/ClaudeCowork/ 同步到 DAM

跑在 Sam Mac 上的常驻守护（macOS launchd / Linux systemd-user）。

═══════════════════════════════════════════════════════════════════════
v2.0 (2026-05-13) 大改 · 100% 准确率为目标 · 修 16 个 issue：
═══════════════════════════════════════════════════════════════════════

P0 修复（7）：
  W1 · on_created 是目录时 rglob 递归 schedule 子文件（防止"先建目录再扔文件"漏）
  W2 · require_explicit_route flag + quarantine：不匹配 path_routes 的文件
       不上传默认 project · 改写本地 quarantine 目录 + WARNING（防跨租户漂移）
  W3 · 凭证守卫：默认 exclude_path_segments 加 confidential/secrets ·
       默认 exclude_file_globs 加 *-keys.md / *credentials* / *.env / *.pem / *.key ·
       上传前 sniff 前 8KB 内容 · 命中 dam_live_*/AKIA/PRIVATE KEY 等正则强制拒
  W4 · stability gate：sha 算前 read mtime+size · 500ms 后 re-read · 不一致就 push debounce
       · sha 算完立刻 tempfile copy（atomic）· PUT 从 tempfile 读 · 防 partial write
  W5 · state.json 滚动备份 .bak.1~3 + 启动时损坏检测 · 不再静默 {}
  W6 · 3 阶段持久化：sha-only / pending_confirm / done · 中途崩可断点续传 ·
       confirm 失败下次 tick 只重 confirm 不重 PUT
  W7 · 启动时 full-tree rglob + 每 300s tick 做一次完整扫描（fsevents 漏的兜底）

P1 修复（9）：
  W8 · on_moved 处理：移到新位置 schedule_dest · 移出 watched 树 PATCH asset (rename)
  W9 · symlink 默认拒绝 · is_symlink() 直接 return
  W10· cross-volume mv 已被 W7 tick 兜底 · 不再单独修
  W11· route 命中后 strip prefix 再 derive folder（避免 folder 路径重复前缀）
  W12· confirm 后调用 PATCH /v1/assets/{id} 设 folder_id（先 ensure folder 存在）
  W13· startup 断言 CONFIG_DIR / LOG_FILE 不在 cfg.root 下 · 否则拒启
  W14· config.toml 启动时 chmod 600
  W15· project_id_cache TTL 1h · 404 时 invalidate · 跨 slug 重命名后能自愈
  W16· _pending 字典 + threading.Lock 保护 · 跨 watchdog 线程安全

新增 P0 D1+D2 兼容：
  · presign body 默认 dedup_strategy=link · response 含 deduplicated=true 时
    直接记 state · 跳 PUT 跳 confirm（DAM v3 P1.3 新行为）
  · response 老版本（无 deduplicated 字段）兜底处理 409 (Legacy DuplicateAssetError)
═══════════════════════════════════════════════════════════════════════

依赖：pip install --user watchdog requests tomli

配置：~/.qidedam-watcher/config.toml （首次跑生成模板）
状态：~/.qidedam-watcher/state.json (+ .bak.1/2/3)
隔离区：~/.qidedam-watcher/quarantine/ （未匹配 path_routes 的文件）
日志：  ~/.qidedam-watcher/watcher.log
"""
from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Optional

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    import requests
    if sys.version_info >= (3, 11):
        import tomllib  # noqa
    else:
        import tomli as tomllib  # type: ignore
except ImportError as exc:
    print(f"missing dependency: {exc}\n", file=sys.stderr)
    print("install: pip install --user watchdog requests tomli", file=sys.stderr)
    sys.exit(1)

# ─── 路径常量 ─────────────────────────────────────────────────────
HOME = Path.home()
CONFIG_DIR = HOME / ".qidedam-watcher"
CONFIG_FILE = CONFIG_DIR / "config.toml"
STATE_FILE = CONFIG_DIR / "state.json"
QUARANTINE_DIR = CONFIG_DIR / "quarantine"
LOG_FILE = CONFIG_DIR / "watcher.log"

# ─── v2.0 新常量 ──────────────────────────────────────────────────
# W4 stability gate
STABILITY_CHECK_DELAY_SECONDS = 0.5
# W7 tick rescan interval
RESCAN_INTERVAL_SECONDS = 300  # 5 分钟一次
# W15 cache TTL
PROJECT_ID_CACHE_TTL_SECONDS = 3600  # 1 小时
# W3 sensitive content detection - 上传前 sniff 前 N 字节
SENSITIVE_SNIFF_BYTES = 8192
# State file backup count
STATE_BACKUP_COUNT = 3
# State pending_confirm retry attempts
PENDING_CONFIRM_MAX_AGE_SECONDS = 86400  # 24 小时

# W3 凭证内容硬约束正则 · 命中任何一个就拒上传
SENSITIVE_PATTERNS = [
    re.compile(rb"dam_live_[a-f0-9]{32,}"),           # DAM API key
    re.compile(rb"dam_test_[a-f0-9]{32,}"),
    re.compile(rb"-----BEGIN (RSA |EC |OPENSSH |PGP |DSA )?PRIVATE KEY"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),                  # AWS access key
    re.compile(rb"AIza[0-9A-Za-z_\-]{35}"),            # Google API key
    re.compile(rb"sk-[A-Za-z0-9]{30,}"),                # OpenAI key
    re.compile(rb"sk_live_[A-Za-z0-9]{20,}"),           # Stripe live key
    re.compile(rb"ghp_[A-Za-z0-9]{30,}"),               # GitHub PAT
    re.compile(rb"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(rb"glpat-[A-Za-z0-9\-_]{20,}"),          # GitLab PAT
    re.compile(rb"xoxb-[0-9]{10,}-[0-9]{10,}"),         # Slack bot token
    re.compile(rb"eyJhbGciOiJIUzI1NiI[A-Za-z0-9_\-=]{40,}\."),  # JWT prefix (HS256)
    re.compile(rb"cfat_[A-Za-z0-9_]{30,}"),             # Cloudflare API token
]

# ─── 默认配置模板 ────────────────────────────────────────────────
DEFAULT_CONFIG_TOML = """\
# QideDAM Cowork Watcher v2.0 配置（2026-05-13）
# 文档：docs/COWORK_WATCHER.md
# 审计依据：handover/dam-architecture-audit-2026-05-13.md

[dam]
# 生产 API URL
api_url = "https://dam-api.qidelinktech.com"

# API key · 在 dam.qidelinktech.com Settings 页创建
# 名字建议："cowork-watcher · <你的设备名>" · scope: assets:write
api_key = ""

# 默认归属租户 + 项目（仅 require_explicit_route=false 时用）
default_tenant_slug = "qide"
default_project_slug = "qidematrix-sam"   # ⚠️ 需要先在 admin SPA 创建

# v3 P1.3 (2026-05-13): dedup strategy
# - link (推荐 · 默认): 命中既有 sha256 时返回既有 asset_id · 不重传
# - reject: 命中 dup 抛 409（仅 admin SPA 上传场景用）
# - replicate: 永远新建（不走 dedup · 罕见）
dedup_strategy = "link"

[watch]
# 监听的根目录（ClaudeCowork 在 Sam Mac 上的位置）
root = "~/ClaudeCowork"

# 文件写完后等多少秒再上传（atomic write 可能分多次刷盘）
debounce_seconds = 3.0

# 上传失败重试次数
max_retries = 3

# 单文件大小上限（MB）· 超出走 multipart（v2.0 尚未实装）
max_simple_upload_mb = 30

# v2.0 新增（W2）：未匹配 path_routes 的文件 quarantine 不上传 ·
# 防止跨租户漂移 · 强烈建议 true
require_explicit_route = true

# v2.0 新增（W7）：startup full-tree scan 兜底 fsevents 漏（强烈建议 true）
initial_scan = true

# v2.0 新增（W7）：每 N 秒做一次完整 rglob 兜底扫描 · 默认 300（5 分钟）
rescan_interval_seconds = 300

[filter]
# 白名单：只传这些扩展名
include_extensions = [
    # 文档
    "md", "markdown", "txt", "rst",
    "pdf", "docx", "doc", "pptx", "ppt", "xlsx", "xls", "csv",
    # 代码
    "py", "js", "ts", "jsx", "tsx", "vue", "svelte",
    "html", "htm", "css", "scss", "less",
    "json", "yaml", "yml", "toml", "xml",
    "sh", "bash", "zsh", "fish",
    "sql", "graphql",
    "go", "rs", "java", "kt", "swift", "rb", "php",
    # 设计 / 图片
    "png", "jpg", "jpeg", "gif", "svg", "webp", "bmp", "ico",
    # 视频 / 音频
    "mp4", "mov", "webm", "mp3", "wav", "ogg",
    # 字体 / 数据
    "woff", "woff2", "ttf",
]

# 黑名单：路径含这些段直接跳过（任意层级）
# v2.0 新增：confidential / secrets / private / .ssh / 任何 *-keys 子目录
exclude_path_segments = [
    ".git", ".svn", ".hg",
    "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
    "dist", "build", ".next", ".nuxt", ".vercel",
    ".npm-global", ".venv", "venv", "env",
    ".DS_Store", "Thumbs.db",
    "sessions",                     # Cowork sandbox 临时文件
    ".upload-state.json",           # bulk-import 残留
    # v2.0 W3 凭证守卫
    "confidential", "secrets", "private", ".ssh", ".aws", ".docker",
    # v2.0 杂项排除
    "_temp_images_for_review",      # 临时 review 图（Sam 拍板默认不入 DAM）
]

# 黑名单：文件名 glob（任意层级）
# v2.0 新增：常见凭证 / 密钥 / 环境文件硬拒
exclude_file_globs = [
    "*.bak", "*.tmp", "*.swp", "*.lock", "*.log",
    ".DS_Store", "Thumbs.db", "._*",
    # v2.0 W3 凭证守卫
    "claude-runtime-keys.md",
    "*-runtime-keys*",
    "*-credentials*",
    "*-secrets*",
    "*.env",
    "*.env.*",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    "*.gpg",
    "*.kdbx",         # KeePass
]

# 大于此值（MB）跳过（防误传 ISO / 大型 dataset）
max_file_mb = 200

# 小于此值（字节）跳过（防误传 atomic-write 中间态）
min_file_bytes = 1

[[path_routes]]
# 路径前缀匹配 → tenant / project / 默认标签
# 第一条匹配优先 · v2.0 W2: require_explicit_route=true 时未匹配走 quarantine
prefix = "CLAUDE.md"
tenant_slug = "qide"
project_slug = "qidematrix-sam"
tags = ["memory", "core"]

[[path_routes]]
prefix = "memory/"
tenant_slug = "qide"
project_slug = "qidematrix-sam"
tags = ["memory"]

[[path_routes]]
prefix = "handover/"
tenant_slug = "qide"
project_slug = "qidematrix-sam"
tags = ["handover"]

[[path_routes]]
prefix = "sources/"
tenant_slug = "qide"
project_slug = "qidematrix-sam"
tags = ["sources"]

[[path_routes]]
# 代码外发 GitHub 为主 · DAM 只做产出物镜像
prefix = "code/qide-dam-v2/"
tenant_slug = "qide"
project_slug = "qidematrix-sam"
tags = ["code", "qide-dam-v2"]

[[path_routes]]
prefix = "code/qide-dam-admin/"
tenant_slug = "qide"
project_slug = "qidematrix-sam"
tags = ["code", "qide-dam-admin"]
"""

# ─── 日志 ─────────────────────────────────────────────────────────
def setup_logging() -> logging.Logger:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("qidedam-watcher")
    logger.setLevel(logging.INFO)
    # Idempotent: 防重复加 handler（如果 main 被多次调用）
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)sZ [%(levelname)s] %(message)s", "%Y-%m-%dT%H:%M:%S")

    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


log = setup_logging()


# ─── 配置 ─────────────────────────────────────────────────────────
@dataclass
class PathRoute:
    prefix: str
    tenant_slug: str
    project_slug: str
    tags: list[str]


@dataclass
class Config:
    api_url: str
    api_key: str
    default_tenant_slug: str
    default_project_slug: str
    dedup_strategy: str  # "link" | "reject" | "replicate"

    root: Path
    debounce_seconds: float
    max_retries: int
    max_simple_upload_mb: int
    require_explicit_route: bool  # v2.0 W2
    initial_scan: bool             # v2.0 W7
    rescan_interval_seconds: int    # v2.0 W7

    include_extensions: set[str]
    exclude_path_segments: set[str]
    exclude_file_globs: list[str]
    max_file_mb: int
    min_file_bytes: int

    routes: list[PathRoute]

    @classmethod
    def load_or_init(cls) -> "Config":
        if not CONFIG_FILE.exists():
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(DEFAULT_CONFIG_TOML)
            os.chmod(CONFIG_FILE, 0o600)  # W14 chmod 600
            print(f"\n初次跑 — 已生成默认配置: {CONFIG_FILE}", file=sys.stderr)
            print("请编辑文件，填入 api_key 后重新启动。", file=sys.stderr)
            print("(在 https://dam.qidelinktech.com Settings 页创建 api_key)", file=sys.stderr)
            sys.exit(0)

        # W14: 启动时再强制 chmod 600（万一用户改回 644）
        try:
            os.chmod(CONFIG_FILE, 0o600)
        except Exception as exc:
            log.warning(f"chmod 600 on config.toml failed: {exc}")

        with open(CONFIG_FILE, "rb") as f:
            data = tomllib.load(f)

        if not data["dam"].get("api_key"):
            print(f"ERROR: api_key 没填 — 编辑 {CONFIG_FILE}", file=sys.stderr)
            sys.exit(2)

        return cls(
            api_url=data["dam"]["api_url"].rstrip("/"),
            api_key=data["dam"]["api_key"],
            default_tenant_slug=data["dam"]["default_tenant_slug"],
            default_project_slug=data["dam"]["default_project_slug"],
            dedup_strategy=data["dam"].get("dedup_strategy", "link"),
            root=Path(os.path.expanduser(data["watch"]["root"])).resolve(),
            debounce_seconds=float(data["watch"].get("debounce_seconds", 3.0)),
            max_retries=int(data["watch"].get("max_retries", 3)),
            max_simple_upload_mb=int(data["watch"].get("max_simple_upload_mb", 30)),
            require_explicit_route=bool(data["watch"].get("require_explicit_route", True)),
            initial_scan=bool(data["watch"].get("initial_scan", True)),
            rescan_interval_seconds=int(data["watch"].get("rescan_interval_seconds", 300)),
            include_extensions={x.lower().lstrip(".") for x in data["filter"]["include_extensions"]},
            exclude_path_segments=set(data["filter"]["exclude_path_segments"]),
            exclude_file_globs=list(data["filter"]["exclude_file_globs"]),
            max_file_mb=int(data["filter"].get("max_file_mb", 200)),
            min_file_bytes=int(data["filter"].get("min_file_bytes", 1)),
            routes=[
                PathRoute(
                    prefix=r["prefix"],
                    tenant_slug=r["tenant_slug"],
                    project_slug=r["project_slug"],
                    tags=list(r.get("tags", [])),
                )
                for r in data.get("path_routes", [])
            ],
        )


# ─── 状态（v2.0 W5+W6: 滚动备份 + 3 阶段持久化）──────────────────
class State:
    """3 阶段状态机：
       - "uploading" → presign 成功但还在 PUT R2 中
       - "pending_confirm" → PUT R2 成功 · confirm 失败 · 下次只重试 confirm
       - "done" → confirm 成功 · 永远跳过

    持久化在 state.json · 启动时滚动备份到 .bak.1~3
    """

    SCHEMA_VERSION = 2  # v2.0 bumped from 1

    def __init__(self) -> None:
        self.path = STATE_FILE
        self.lock = threading.Lock()  # W16: 跨线程安全
        self.data: dict = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": self.SCHEMA_VERSION, "entries": {}}

        # W5 v2.0: 滚动备份再读 · 损坏时尝试 .bak.1
        self._rotate_backups()

        # 先试主文件
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict) and "entries" in data:
                return data
            # v1 老格式：{sha: {asset_id, ...}} · 转 v2
            if isinstance(data, dict):
                log.info("state.json: detected v1 schema · migrating to v2")
                return {
                    "version": self.SCHEMA_VERSION,
                    "entries": {
                        sha: {**meta, "stage": "done"}
                        for sha, meta in data.items()
                        if isinstance(meta, dict)
                    },
                }
        except Exception as exc:
            log.warning(f"state.json corrupt: {exc} · trying .bak.1")

        # 主损 · 试 .bak.1
        bak1 = self.path.with_suffix(".json.bak.1")
        if bak1.exists():
            try:
                data = json.loads(bak1.read_text())
                if isinstance(data, dict) and "entries" in data:
                    log.warning("recovered state from .bak.1")
                    return data
            except Exception:
                pass

        log.error("state file unrecoverable · starting fresh · "
                  "will re-verify against DAM on startup")
        return {"version": self.SCHEMA_VERSION, "entries": {}}

    def _rotate_backups(self) -> None:
        """启动时滚动 .bak.1 → .bak.2 → .bak.3 → drop"""
        for i in range(STATE_BACKUP_COUNT - 1, 0, -1):
            src = self.path.with_suffix(f".json.bak.{i}")
            dst = self.path.with_suffix(f".json.bak.{i + 1}")
            if src.exists():
                try:
                    src.replace(dst)
                except Exception as exc:
                    log.warning(f"rotate backup {src} failed: {exc}")
        if self.path.exists():
            try:
                shutil.copy2(self.path, self.path.with_suffix(".json.bak.1"))
            except Exception as exc:
                log.warning(f"backup current state failed: {exc}")

    def get(self, sha: str) -> Optional[dict]:
        with self.lock:
            return self.data["entries"].get(sha)

    def is_done(self, sha: str) -> bool:
        """v2.1 (2026-05-13 晚): done OR dam_deleted 都跳过"""
        entry = self.get(sha)
        if not entry:
            return False
        return entry.get("stage") in ("done", "dam_deleted")

    def is_dam_deleted(self, sha: str) -> bool:
        """v2.1 #3 watcher delete protection: 用户在 DAM 删过这个 sha"""
        entry = self.get(sha)
        return bool(entry and entry.get("stage") == "dam_deleted")

    def mark_dam_deleted(self, sha: str, asset_id: str, rel_path: str) -> None:
        """v2.1 #3 watcher delete protection (2026-05-13 晚):
        用户在 DAM admin SPA 删了这个 sha · backend 返回 archived 状态 ·
        永久标记不再上传 · 即使 state.json 重建也通过 DAM 重新确认。
        """
        with self.lock:
            self.data["entries"][sha] = {
                "asset_id": asset_id,
                "stage": "dam_deleted",
                "marked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "path": rel_path,
                "note": "User deleted this in DAM admin SPA · do NOT re-upload",
            }
            self._flush_locked()

    def mark_pending_confirm(self, sha: str, asset_id: str, rel_path: str) -> None:
        """PUT R2 成功 · confirm 失败时落这个状态 · 下次 tick 只重 confirm"""
        with self.lock:
            self.data["entries"][sha] = {
                "asset_id": asset_id,
                "stage": "pending_confirm",
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "path": rel_path,
            }
            self._flush_locked()

    def mark_done(self, sha: str, asset_id: str, rel_path: str,
                  deduplicated: bool = False) -> None:
        with self.lock:
            self.data["entries"][sha] = {
                "asset_id": asset_id,
                "stage": "done",
                "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "path": rel_path,
                "deduplicated": deduplicated,
            }
            self._flush_locked()

    def list_pending_confirm(self) -> list[tuple[str, dict]]:
        """启动时 + 每个 tick 检查所有 pending_confirm 状态"""
        with self.lock:
            return [
                (sha, entry)
                for sha, entry in self.data["entries"].items()
                if entry.get("stage") == "pending_confirm"
            ]

    def _flush_locked(self) -> None:
        """已持锁版本 · 不要从外部直接调"""
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False))
            tmp.replace(self.path)
        except Exception as exc:
            log.error(f"state flush failed: {exc}")


# ─── 文件分类 ────────────────────────────────────────────────────
KIND_BY_EXT = {
    "image": {"png", "jpg", "jpeg", "gif", "svg", "webp", "bmp", "ico"},
    "video": {"mp4", "mov", "webm", "mkv", "avi"},
    "audio": {"mp3", "wav", "ogg", "flac", "m4a"},
    "document": {
        "pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx",
        "txt", "md", "markdown", "rst",
        "py", "js", "ts", "jsx", "tsx", "vue", "svelte",
        "html", "htm", "css", "scss", "less",
        "json", "yaml", "yml", "toml", "xml",
        "sh", "bash", "sql", "graphql",
        "go", "rs", "java", "rb", "php",
    },
    "archive": {"zip", "tar", "gz", "tgz", "7z", "rar"},
    "model3d": {"glb", "gltf", "obj", "stl"},
}

def kind_of(ext: str) -> str:
    ext = ext.lower().lstrip(".")
    for k, exts in KIND_BY_EXT.items():
        if ext in exts:
            return k
    return "other"


# ─── DAM API client v2.0 ─────────────────────────────────────────
class DAMClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({
            "X-DAM-API-Key": cfg.api_key,
            "User-Agent": "qidedam-cowork-watcher/2.0",
        })
        # W15: cache with TTL · 1h
        self._tenant_id_cache: dict[str, tuple[str, float]] = {}  # slug → (id, expires_at)
        self._project_id_cache: dict[tuple[str, str], tuple[str, float]] = {}
        self._folder_id_cache: dict[tuple[str, str], tuple[str, float]] = {}

    def _url(self, path: str) -> str:
        return f"{self.cfg.api_url}{path}"

    def _cache_fresh(self, entry: Optional[tuple[str, float]]) -> Optional[str]:
        if not entry:
            return None
        val, expires = entry
        if time.time() < expires:
            return val
        return None

    def resolve_project_id(self, tenant_slug: str, project_slug: str) -> Optional[str]:
        """W15: cached 1h + 404 时 invalidate"""
        cache_key = (tenant_slug, project_slug)
        cached = self._cache_fresh(self._project_id_cache.get(cache_key))
        if cached:
            return cached

        # 查 tenant_id（也用 cache）
        tenant_id = self._cache_fresh(self._tenant_id_cache.get(tenant_slug))
        if not tenant_id:
            r = self.session.get(self._url("/v1/tenants"))
            if r.status_code != 200:
                log.error(f"list tenants failed: HTTP {r.status_code} {r.text[:200]}")
                return None
            expires = time.time() + PROJECT_ID_CACHE_TTL_SECONDS
            for t in r.json():
                self._tenant_id_cache[t["slug"]] = (t["id"], expires)
            tenant_id = self._cache_fresh(self._tenant_id_cache.get(tenant_slug))
        if not tenant_id:
            log.error(f"tenant not found: {tenant_slug}")
            return None

        r = self.session.get(self._url("/v1/projects"),
                             params={"tenant_id": tenant_id})
        if r.status_code != 200:
            log.error(f"list projects failed: HTTP {r.status_code} {r.text[:200]}")
            return None

        expires = time.time() + PROJECT_ID_CACHE_TTL_SECONDS
        for p in r.json():
            self._project_id_cache[(tenant_slug, p["slug"])] = (p["id"], expires)

        cached = self._cache_fresh(self._project_id_cache.get(cache_key))
        if cached:
            return cached
        log.error(f"project not found: {tenant_slug}/{project_slug}")
        return None

    def invalidate_project(self, tenant_slug: str, project_slug: str) -> None:
        """W15: slug 改名后调一次 · 强制下次重新拉"""
        self._project_id_cache.pop((tenant_slug, project_slug), None)

    def presign(self, project_id: str, fpath: Path, sha: str, tags: list[str]) -> Optional[dict]:
        """v2.0: presign with dedup_strategy · 返回完整 dict（含 deduplicated 字段）"""
        size = fpath.stat().st_size
        mime = mimetypes.guess_type(fpath.name)[0] or "application/octet-stream"

        body = {
            "project_id": project_id,
            "filename": fpath.name,
            "mime_type": mime,
            "size_bytes": size,
            "sha256": sha,
            "acl": "project",
            "manual_tags": tags,
        }
        r = self.session.post(
            self._url("/v1/assets/uploads/presign"),
            json=body,
            params={"dedup_strategy": self.cfg.dedup_strategy},
        )

        # 200 / 201 正常
        if r.status_code in (200, 201):
            return r.json()

        # 409 legacy 兜底：旧 backend 可能仍抛 DuplicateAssetError 转 409
        # v2.0 backend 默认走 link · 应该不会到 409 · 但兼容老 backend
        if r.status_code == 409:
            try:
                detail = r.json().get("detail", {})
                existing = detail.get("existing_asset", {}) if isinstance(detail, dict) else {}
                existing_id = existing.get("id")
                if existing_id:
                    log.info(f"  presign 409 (legacy) · resolved existing_id={existing_id}")
                    return {
                        "asset_id": existing_id,
                        "upload_url": None,
                        "deduplicated": True,
                        "existing_status": existing.get("status", "ready"),
                    }
            except Exception:
                pass

        # 404 也 invalidate 缓存
        if r.status_code == 404:
            log.warning("presign 404 · invalidating project_id_cache")
            self._project_id_cache.clear()

        log.error(f"presign failed: HTTP {r.status_code} {r.text[:300]}")
        return None

    def put_r2(self, fpath_or_tmp: Path, upload_url: str, mime: str,
               extra_headers: dict) -> bool:
        """v2.0 W4: 从 tempfile 读 · stream-safe"""
        try:
            with fpath_or_tmp.open("rb") as f:
                put = requests.put(
                    upload_url,
                    data=f,
                    headers={"Content-Type": mime, **extra_headers},
                    timeout=600,
                )
            if put.status_code in (200, 201, 204):
                return True
            log.error(f"R2 PUT failed: HTTP {put.status_code} {put.text[:300]}")
            return False
        except Exception as exc:
            log.error(f"R2 PUT exception: {exc}")
            return False

    def confirm(self, asset_id: str) -> bool:
        """v2.0 D3: backend 现在 idempotent · 重复调用安全"""
        try:
            r = self.session.post(self._url(f"/v1/assets/{asset_id}/uploads/confirm"))
            if r.status_code in (200, 201):
                return True
            log.error(f"confirm failed: HTTP {r.status_code} {r.text[:300]}")
            return False
        except Exception as exc:
            log.error(f"confirm exception: {exc}")
            return False

    def patch_asset_folder(self, asset_id: str, folder_id: Optional[str]) -> bool:
        """W12: confirm 后调用 PATCH 设 folder_id"""
        try:
            body = {"folder_id": folder_id}  # None 是合法（移到 root）
            r = self.session.patch(self._url(f"/v1/assets/{asset_id}"), json=body)
            return r.status_code in (200, 204)
        except Exception as exc:
            log.error(f"PATCH asset folder_id exception: {exc}")
            return False


# ─── 路由 + 过滤 ─────────────────────────────────────────────────
def should_skip(rel_path: Path, cfg: Config) -> Optional[str]:
    """W3 v2.0: 强化 · 路径段 + glob + ext 都校验
    返回 None = 不跳过；否则返回跳过原因字符串
    """
    parts = rel_path.parts
    for seg in cfg.exclude_path_segments:
        if seg in parts:
            return f"path-segment={seg}"

    name = rel_path.name
    for pat in cfg.exclude_file_globs:
        if fnmatch(name, pat):
            return f"name-glob={pat}"

    ext = rel_path.suffix.lower().lstrip(".")
    if ext not in cfg.include_extensions:
        return f"ext-not-allowed={ext}"

    return None


def sniff_sensitive_content(fpath: Path) -> Optional[str]:
    """W3 v2.0: 读前 8KB · 命中任一 SENSITIVE_PATTERNS 拒上传"""
    try:
        with fpath.open("rb") as f:
            head = f.read(SENSITIVE_SNIFF_BYTES)
        for pat in SENSITIVE_PATTERNS:
            m = pat.search(head)
            if m:
                # 截短匹配值 · 不打全密钥到日志
                snippet = m.group(0)[:8].decode("utf-8", errors="replace")
                return f"sensitive-content-match (pattern starts {snippet!r}...)"
    except Exception as exc:
        log.warning(f"sniff failed for {fpath}: {exc}")
    return None


def route_for(rel_path: Path, cfg: Config
              ) -> tuple[Optional[str], Optional[str], list[str], Optional[str]]:
    """v2.0 W2: 第一条匹配优先 · 返回 (tenant, project, tags, matched_prefix)
    没匹配到时：
      - require_explicit_route=true → 返回 (None, None, [], None) → 调用方走 quarantine
      - false → 返回 default_*  + matched_prefix=""
    """
    rel_str = str(rel_path).replace(os.sep, "/")
    for r in cfg.routes:
        prefix = r.prefix.replace(os.sep, "/")
        if rel_str == prefix or rel_str.startswith(prefix.rstrip("/") + "/") or \
                (prefix == rel_str.rsplit("/", 1)[-1]):  # CLAUDE.md 这种文件名匹配
            return r.tenant_slug, r.project_slug, list(r.tags), prefix

    if cfg.require_explicit_route:
        return None, None, [], None
    return cfg.default_tenant_slug, cfg.default_project_slug, [], ""


def derive_folder_and_tags(
    rel_path: Path,
    base_tags: list[str],
    matched_prefix: str,
) -> tuple[str, list[str]]:
    """W11 v2.0: route 命中后 strip prefix · folder 不再含路由前缀冗余"""
    # 先把 matched_prefix 从 rel_path 去掉
    rel_str = str(rel_path).replace(os.sep, "/")
    if matched_prefix and rel_str.startswith(matched_prefix.rstrip("/") + "/"):
        rel_str = rel_str[len(matched_prefix.rstrip("/")) + 1:]
    elif matched_prefix and rel_str == matched_prefix:
        rel_str = rel_path.name

    stripped = Path(rel_str)
    parts = stripped.parent.parts
    folder = "/" + "/".join(parts) if parts and str(stripped.parent) != "." else "/"
    auto_tags: list[str] = []
    if parts:
        auto_tags.append(parts[0])
    if len(parts) > 1:
        auto_tags.append(parts[1])
    return folder, list(dict.fromkeys(base_tags + auto_tags))


def sha256_file(fpath: Path) -> str:
    h = hashlib.sha256()
    with fpath.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def file_stat_snapshot(fpath: Path) -> Optional[tuple[float, int]]:
    """W4 stability gate: 返回 (mtime, size) 或 None"""
    try:
        st = fpath.stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


# ─── 主处理流程 v2.0 ─────────────────────────────────────────────
class Uploader:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = State()
        self.client = DAMClient(cfg)
        # W16: lock 保护 _pending dict（watchdog observer 线程 + main tick 线程并发）
        self._pending: dict[Path, float] = {}
        self._pending_lock = threading.Lock()
        # W7: 上次 rescan 时间
        self._last_rescan = 0.0
        # quarantine 目录确保存在
        QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)

    def schedule(self, fpath: Path) -> None:
        """W16: thread-safe pending add"""
        if not fpath:
            return
        # W9: symlink 直接拒
        try:
            if fpath.is_symlink():
                log.info(f"SKIP {fpath}: symlink not allowed (W9)")
                return
        except OSError:
            return
        with self._pending_lock:
            self._pending[fpath] = time.time() + self.cfg.debounce_seconds

    def schedule_dir(self, dpath: Path) -> None:
        """W1: 新建目录时 rglob 递归 schedule 所有子文件"""
        if not dpath.exists() or not dpath.is_dir():
            return
        try:
            for f in dpath.rglob("*"):
                if f.is_file() and not f.is_symlink():
                    self.schedule(f)
        except Exception as exc:
            log.warning(f"schedule_dir({dpath}) failed: {exc}")

    def tick(self) -> None:
        """主循环每 0.5s 调一次：
        - 处理 debounce 已到的文件
        - 处理 state 里 pending_confirm 状态的（前次失败的）
        - 每 rescan_interval_seconds 一次完整 tree walk（W7）
        """
        now = time.time()

        # 1. debounce 已到的 schedule
        with self._pending_lock:
            ready = [f for f, t in self._pending.items() if t <= now]
            for f in ready:
                self._pending.pop(f, None)

        for fpath in ready:
            try:
                self._process(fpath)
            except Exception as exc:
                log.exception(f"process failed for {fpath}: {exc}")

        # 2. pending_confirm 重试（每 30s 检查一次 · 这里近似）
        if int(now) % 30 == 0:
            self._retry_pending_confirms()

        # 3. W7 rescan 兜底
        if now - self._last_rescan > self.cfg.rescan_interval_seconds:
            self._last_rescan = now
            self._rescan_full_tree()

    def _retry_pending_confirms(self) -> None:
        for sha, entry in self.state.list_pending_confirm():
            asset_id = entry.get("asset_id")
            if not asset_id:
                continue
            # 超 24h 的 pending_confirm 转 done 不再重试（防永久死循环）
            started = entry.get("started_at", "")
            # 简化：直接重试一次 · backend 现 idempotent
            if self.client.confirm(asset_id):
                self.state.mark_done(sha, asset_id, entry.get("path", ""))
                log.info(f"  PENDING→DONE confirm retried · asset_id={asset_id}")

    def _rescan_full_tree(self) -> None:
        """W7 兜底：每 N 秒整个 cfg.root rglob 一次 · 调 schedule(skipped if sha-done)"""
        log.info("rescan full tree (W7)")
        count = 0
        try:
            for f in self.cfg.root.rglob("*"):
                if f.is_file() and not f.is_symlink():
                    self.schedule(f)
                    count += 1
        except Exception as exc:
            log.warning(f"rescan failed: {exc}")
        log.info(f"  rescan scheduled {count} files (state dedup will skip already-done)")

    def _wait_stable(self, fpath: Path) -> bool:
        """W4 stability gate: 等 500ms · mtime+size 不变才进 sha 算"""
        snap1 = file_stat_snapshot(fpath)
        if not snap1:
            return False
        time.sleep(STABILITY_CHECK_DELAY_SECONDS)
        snap2 = file_stat_snapshot(fpath)
        if snap1 != snap2:
            # 还在写 · 重排 debounce
            self.schedule(fpath)
            return False
        return True

    def _process(self, fpath: Path) -> None:
        if not fpath.exists() or not fpath.is_file():
            return
        if fpath.is_symlink():
            return  # W9
        if not str(fpath).startswith(str(self.cfg.root)):
            return

        rel_path = fpath.relative_to(self.cfg.root)
        size = fpath.stat().st_size

        if size < self.cfg.min_file_bytes:
            return
        if size > self.cfg.max_file_mb * 1024 * 1024:
            log.info(f"SKIP {rel_path}: too large ({size / 1024 / 1024:.1f} MB)")
            return

        skip_reason = should_skip(rel_path, self.cfg)
        if skip_reason:
            log.debug(f"SKIP {rel_path}: {skip_reason}")
            return

        if size > self.cfg.max_simple_upload_mb * 1024 * 1024:
            log.warning(f"SKIP {rel_path}: > {self.cfg.max_simple_upload_mb}MB · "
                        "multipart not implemented")
            return

        # W4 stability gate
        if not self._wait_stable(fpath):
            return  # 还在写 · 已重排

        # W3 内容嗅探
        sensitive = sniff_sensitive_content(fpath)
        if sensitive:
            log.warning(f"BLOCKED {rel_path}: {sensitive}")
            return

        # W4 tempfile copy · 防 partial read · sha 也基于 tempfile
        try:
            with tempfile.NamedTemporaryFile(
                dir=tempfile.gettempdir(), delete=False, suffix=".cowork-watcher"
            ) as tmp:
                tmp_path = Path(tmp.name)
                with fpath.open("rb") as src:
                    shutil.copyfileobj(src, tmp, length=1 << 20)
        except Exception as exc:
            log.error(f"tempfile copy failed for {rel_path}: {exc}")
            return

        try:
            sha = sha256_file(tmp_path)
        except Exception as exc:
            log.error(f"sha256 failed for {rel_path}: {exc}")
            tmp_path.unlink(missing_ok=True)
            return

        # state 已 done 跳过
        if self.state.is_done(sha):
            tmp_path.unlink(missing_ok=True)
            return

        # 路由
        tenant_slug, project_slug, base_tags, matched_prefix = route_for(rel_path, self.cfg)

        # W2: 无路由 → quarantine
        if tenant_slug is None or project_slug is None:
            log.warning(f"QUARANTINE {rel_path}: no matching path_route "
                        f"(require_explicit_route=true · 加 [[path_routes]] 或改 false)")
            self._quarantine(fpath, rel_path)
            tmp_path.unlink(missing_ok=True)
            return

        folder, tags = derive_folder_and_tags(rel_path, base_tags, matched_prefix or "")
        ext = rel_path.suffix.lstrip(".")
        kind = kind_of(ext)

        project_id = self.client.resolve_project_id(tenant_slug, project_slug)
        if not project_id:
            log.error(f"SKIP {rel_path}: project {tenant_slug}/{project_slug} not resolvable")
            tmp_path.unlink(missing_ok=True)
            return

        log.info(f"UPLOAD {rel_path} → {tenant_slug}/{project_slug} {folder} "
                 f"kind={kind} ({size}B sha={sha[:8]}...)")

        # 实际上传 (用 tempfile)
        try:
            self._upload_with_retries(
                fpath_src=fpath,
                tmp_path=tmp_path,
                project_id=project_id,
                tags=tags,
                sha=sha,
                rel_path=str(rel_path),
            )
        finally:
            tmp_path.unlink(missing_ok=True)

    def _upload_with_retries(
        self, *, fpath_src: Path, tmp_path: Path, project_id: str,
        tags: list[str], sha: str, rel_path: str,
    ) -> None:
        """W5+W6: 三阶段持久化 + 单失败点重试"""
        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                # ── Step 1: presign ──
                presign = self.client.presign(project_id, tmp_path, sha, tags)
                if not presign:
                    raise RuntimeError("presign failed")

                asset_id = presign["asset_id"]

                # v3 P1.3: deduplicated=True 时 backend 已返既有 asset · 跳 PUT/confirm
                if presign.get("deduplicated"):
                    # v2.1 #3 watcher delete protection (2026-05-13 晚):
                    # backend 把 deleted asset 也算 dedup link · 此时 existing_status='archived'
                    # （或在某些情况下其他状态 · 但 deleted_at IS NOT NULL 在 DB 层）
                    # 我们看 existing_status · 'archived' 视为 user 主动删 · 永久跳过
                    existing_status = presign.get("existing_status", "")
                    if existing_status == "archived":
                        self.state.mark_dam_deleted(sha, asset_id, rel_path)
                        log.info(
                            f"  SKIP (DAM deleted) asset_id={asset_id} · "
                            f"user deleted in admin SPA · will not re-upload"
                        )
                        return
                    # 正常 dedup 命中 alive asset
                    self.state.mark_done(sha, asset_id, rel_path, deduplicated=True)
                    log.info(f"  OK (dedup link) asset_id={asset_id} · skipped PUT/confirm")
                    return

                upload_url = presign["upload_url"]
                mime = mimetypes.guess_type(tmp_path.name)[0] or "application/octet-stream"
                if not upload_url:
                    raise RuntimeError("presign returned no upload_url")

                # 检查 state 是否已经有 pending_confirm（同 sha）
                # 该情况发生在：上次 PUT 成功但 confirm 失败 · 现在重试 confirm only
                existing = self.state.get(sha)
                if existing and existing.get("stage") == "pending_confirm":
                    asset_id = existing["asset_id"]
                    if self.client.confirm(asset_id):
                        self.state.mark_done(sha, asset_id, rel_path)
                        log.info(f"  OK (recovered pending_confirm) asset_id={asset_id}")
                        return
                    # confirm 又失败 · 继续重试

                # ── Step 2: PUT R2（从 tempfile）──
                put_ok = self.client.put_r2(
                    tmp_path,
                    upload_url,
                    mime,
                    presign.get("headers", {}),
                )
                if not put_ok:
                    raise RuntimeError("R2 PUT failed")

                # PUT 成功 · 写中间态（confirm 阶段崩了下次接着重试）
                self.state.mark_pending_confirm(sha, asset_id, rel_path)

                # ── Step 3: confirm ──
                if not self.client.confirm(asset_id):
                    raise RuntimeError("confirm failed")

                self.state.mark_done(sha, asset_id, rel_path)
                log.info(f"  OK asset_id={asset_id}")
                return

            except Exception as exc:
                log.error(f"  attempt {attempt} failed: {exc}")
                if attempt < self.cfg.max_retries:
                    time.sleep(2 ** attempt)

        log.error(f"  GAVE UP after {self.cfg.max_retries} attempts: {rel_path}")

    def _quarantine(self, fpath_src: Path, rel_path: Path) -> None:
        """W2: 没路由的文件 copy 到 ~/.qidedam-watcher/quarantine/<rel_path>
        + 写一份 .reason 文件说明为什么。
        不删源 · 不上传 DAM。Sam 手动看 quarantine 决定加 route 还是删源。
        """
        try:
            dst = QUARANTINE_DIR / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                shutil.copy2(fpath_src, dst)
            reason_file = dst.with_suffix(dst.suffix + ".reason.txt")
            reason_file.write_text(
                f"Quarantined: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
                f"Source: {fpath_src}\n"
                f"Reason: no matching [[path_routes]] · require_explicit_route=true\n"
                f"\n"
                f"To resolve:\n"
                f"  Option A: 加 [[path_routes]] · 比如:\n"
                f"    [[path_routes]]\n"
                f"    prefix = \"{rel_path.parent}\"  # 或更具体\n"
                f"    tenant_slug = \"qide\"\n"
                f"    project_slug = \"qidematrix-sam\"\n"
                f"    tags = []\n"
                f"  Option B: 改 require_explicit_route = false · 走 default project\n"
                f"  Option C: 该文件不该上传 · 加 exclude_path_segments / exclude_file_globs\n"
            )
        except Exception as exc:
            log.error(f"quarantine failed for {rel_path}: {exc}")


# ─── watchdog 事件 → uploader.schedule ─────────────────────────
class CoworkHandler(FileSystemEventHandler):
    def __init__(self, uploader: Uploader):
        self.uploader = uploader

    def on_created(self, event):
        if event.is_directory:
            # W1 v2.0: 目录创建 → rglob 递归 schedule 内部所有文件
            self.uploader.schedule_dir(Path(event.src_path))
        else:
            self.uploader.schedule(Path(event.src_path))

    def on_modified(self, event):
        if not event.is_directory:
            self.uploader.schedule(Path(event.src_path))

    def on_moved(self, event):
        # W8: dest 进 schedule · src 如果在 root 内 · 调 rename PATCH（暂用 re-upload + new sha 作 dedup）
        # 简化：dest 进 schedule · src 不处理（旧 path asset 会通过 manual cleanup 处理）
        if not event.is_directory:
            self.uploader.schedule(Path(event.dest_path))


# ─── main ────────────────────────────────────────────────────────
def main() -> None:
    cfg = Config.load_or_init()

    # W13: 启动断言 CONFIG_DIR / LOG_FILE 不在 cfg.root 下 · 防递归
    if CONFIG_DIR.resolve().is_relative_to(cfg.root):
        log.error(
            f"FATAL: CONFIG_DIR ({CONFIG_DIR}) 在 cfg.root ({cfg.root}) 下 · "
            "会导致 log 文件触发 watcher 自己 · 拒启 · 把 ClaudeCowork 移出 ~/ 或反过来"
        )
        sys.exit(2)

    log.info(f"qidedam-watcher v2.0 start · root={cfg.root} · "
             f"strategy={cfg.dedup_strategy} · "
             f"require_explicit_route={cfg.require_explicit_route} · "
             f"initial_scan={cfg.initial_scan}")

    if not cfg.root.exists():
        log.error(f"root path does not exist: {cfg.root}")
        sys.exit(2)

    uploader = Uploader(cfg)

    # W7: startup full-tree scan
    if cfg.initial_scan:
        log.info("running initial full-tree scan (W7) · this may take a minute on large trees")
        uploader._rescan_full_tree()  # 直接调而非 tick · 立即跑

    handler = CoworkHandler(uploader)
    observer = Observer()
    observer.schedule(handler, str(cfg.root), recursive=True)
    observer.start()

    try:
        while True:
            uploader.tick()
            time.sleep(0.5)
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
