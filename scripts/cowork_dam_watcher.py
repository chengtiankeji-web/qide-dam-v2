#!/usr/bin/env python3
"""
QideDAM Cowork Watcher · 实时把 ~/ClaudeCowork/ 同步到 DAM

跑在 Sam Mac 上的常驻守护（macOS launchd / Linux systemd-user）。
每当 Claude（或 Sam 自己）在 ClaudeCowork 文件夹下写入新文件时，自动：

  1. 计算 sha256 + 比对 state 文件 → 已传过的跳过
  2. 按 include/exclude 规则过滤
  3. 按路径推断 tenant + project + folder + tags
  4. 走 DAM 标准两步上传：presign → PUT → confirm
  5. 更新 state.json

依赖（首次跑前装）：
    pip install --user watchdog requests tomli

配置文件：~/.qidedam-watcher/config.toml （首次跑生成模板）
状态文件：~/.qidedam-watcher/state.json
日志：    ~/.qidedam-watcher/watcher.log

用法：
    # 前台跑（调试）
    python scripts/cowork_dam_watcher.py

    # 后台跑（macOS launchd）—— 跑 install_cowork_watcher_macos.sh 自动配
    # 后台跑（Linux systemd-user）
    systemctl --user start qidedam-watcher
"""
from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import sys
import time
from dataclasses import dataclass
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
LOG_FILE = CONFIG_DIR / "watcher.log"

# ─── 默认配置模板 ────────────────────────────────────────────────
DEFAULT_CONFIG_TOML = """\
# QideDAM Cowork Watcher 配置
# 文档：docs/COWORK_WATCHER.md

[dam]
# 生产 API URL
api_url = "https://dam-api.qidelinktech.com"

# API key · 在 dam.qidelinktech.com Settings 页创建
# 名字建议："cowork-watcher · <你的设备名>" · scope: assets:write
api_key = ""

# 默认归属租户 + 项目（不匹配任何 path_routes 时用）
default_tenant_slug = "qide"
default_project_slug = "cowork"   # ⚠️ 需要先在 admin SPA 创建这个 project

[watch]
# 监听的根目录（ClaudeCowork 在 Sam Mac 上的位置）
root = "~/ClaudeCowork"

# 文件写完后等多少秒再上传（atomic write 可能分多次刷盘）
debounce_seconds = 3.0

# 上传失败重试次数
max_retries = 3

# 单文件大小上限（MB）· 超出走 multipart
max_simple_upload_mb = 30

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
exclude_path_segments = [
    ".git", ".svn", ".hg",
    "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
    "dist", "build", ".next", ".nuxt", ".vercel",
    ".npm-global", ".venv", "venv", "env",
    ".DS_Store", "Thumbs.db",
    "sessions",                     # Cowork sandbox 临时文件
    ".upload-state.json",           # bulk-import 残留
]

# 黑名单：文件名 glob（任意层级）
exclude_file_globs = [
    "*.bak", "*.tmp", "*.swp", "*.lock", "*.log",
    ".DS_Store", "Thumbs.db", "._*",
]

# 大于此值（MB）跳过（防误传 ISO / 大型 dataset）
max_file_mb = 200

# 小于此值（字节）跳过（防误传 atomic-write 中间态）
min_file_bytes = 1

[[path_routes]]
# 路径前缀匹配 → tenant / project / 默认标签
# 第一条匹配优先
prefix = "memory/projects/xiangyue-shunde"
tenant_slug = "hemei"
project_slug = "xiangyue-shunde"
tags = ["xiangyue-shunde"]

[[path_routes]]
prefix = "memory/projects/kiln-ink"
tenant_slug = "qingxuan"
project_slug = "kiln-ink"
tags = ["kiln-ink"]

[[path_routes]]
prefix = "memory/projects/qide-dam"
tenant_slug = "qide"
project_slug = "dam"
tags = ["qide-dam"]

[[path_routes]]
prefix = "code/qide-dam-v2"
tenant_slug = "qide"
project_slug = "dam"
tags = ["qide-dam", "code"]

[[path_routes]]
prefix = "handover"
tenant_slug = "qide"
project_slug = "cowork"
tags = ["handover"]
"""

# ─── 日志 ─────────────────────────────────────────────────────────
def setup_logging() -> logging.Logger:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("qidedam-watcher")
    logger.setLevel(logging.INFO)
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

    root: Path
    debounce_seconds: float
    max_retries: int
    max_simple_upload_mb: int

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
            print(f"\n初次跑 — 已生成默认配置: {CONFIG_FILE}", file=sys.stderr)
            print("请编辑文件，填入 api_key 后重新启动。", file=sys.stderr)
            print("(在 https://dam.qidelinktech.com Settings 页创建 api_key)", file=sys.stderr)
            sys.exit(0)

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
            root=Path(os.path.expanduser(data["watch"]["root"])).resolve(),
            debounce_seconds=float(data["watch"].get("debounce_seconds", 3.0)),
            max_retries=int(data["watch"].get("max_retries", 3)),
            max_simple_upload_mb=int(data["watch"].get("max_simple_upload_mb", 30)),
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


# ─── 状态 ─────────────────────────────────────────────────────────
class State:
    """{ sha256 → {asset_id, uploaded_at, path} } 持久化在 state.json"""

    def __init__(self) -> None:
        self.path = STATE_FILE
        self.data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except Exception as exc:
                log.warning(f"state file corrupt, starting fresh: {exc}")
                self.data = {}

    def has(self, sha: str) -> bool:
        return sha in self.data

    def add(self, sha: str, asset_id: str, rel_path: str) -> None:
        self.data[sha] = {
            "asset_id": asset_id,
            "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "path": rel_path,
        }
        self._flush()

    def _flush(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False))
        tmp.replace(self.path)


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


# ─── DAM API client（最小子集）────────────────────────────────────
class DAMClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({
            "X-DAM-API-Key": cfg.api_key,
            "User-Agent": "qidedam-cowork-watcher/1.0",
        })
        self._tenant_id_cache: dict[str, str] = {}
        self._project_id_cache: dict[tuple[str, str], str] = {}

    def _url(self, path: str) -> str:
        return f"{self.cfg.api_url}{path}"

    def resolve_project_id(self, tenant_slug: str, project_slug: str) -> Optional[str]:
        cache_key = (tenant_slug, project_slug)
        if cache_key in self._project_id_cache:
            return self._project_id_cache[cache_key]

        # 先查 tenant_id
        if tenant_slug not in self._tenant_id_cache:
            r = self.session.get(self._url("/v1/tenants"))
            if r.status_code != 200:
                log.error(f"list tenants failed: HTTP {r.status_code} {r.text[:200]}")
                return None
            for t in r.json():
                self._tenant_id_cache[t["slug"]] = t["id"]

        tenant_id = self._tenant_id_cache.get(tenant_slug)
        if not tenant_id:
            log.error(f"tenant not found: {tenant_slug}")
            return None

        # 再查 project_id
        r = self.session.get(self._url("/v1/projects"), params={"tenant_id": tenant_id})
        if r.status_code != 200:
            log.error(f"list projects failed: HTTP {r.status_code} {r.text[:200]}")
            return None

        for p in r.json():
            if p.get("slug") == project_slug and p.get("tenant_id") == tenant_id:
                self._project_id_cache[cache_key] = p["id"]
                return p["id"]

        log.error(f"project not found: {tenant_slug}/{project_slug}")
        return None

    def upload(self, fpath: Path, project_id: str, kind: str,
               folder: str, tags: list[str], sha: str) -> Optional[str]:
        """两步上传：presign → PUT → confirm。返回 asset_id 或 None。

        注：服务端 PresignedUploadIn schema 只接受
        project_id / filename / mime_type / size_bytes / sha256 / acl / manual_tags。
        kind 由扩展名服务端自动推断；folder_path 不在 presign 阶段设（如需归类
        到具体 folder，确认后用 PATCH /v1/assets/{id} 设 folder_id）。
        """
        size = fpath.stat().st_size
        mime = mimetypes.guess_type(fpath.name)[0] or "application/octet-stream"

        # ── Step 1: presign ──
        body = {
            "project_id": project_id,
            "filename": fpath.name,
            "mime_type": mime,
            "size_bytes": size,
            "sha256": sha,
            "acl": "project",
            "manual_tags": tags,
        }
        r = self.session.post(self._url("/v1/assets/uploads/presign"), json=body)
        if r.status_code not in (200, 201):
            log.error(f"presign failed: HTTP {r.status_code} {r.text[:300]}")
            return None
        presign = r.json()
        asset_id = presign["asset_id"]
        upload_url = presign["upload_url"]
        upload_headers = presign.get("headers", {})

        # ── Step 2: PUT to S3 / R2 ──
        with fpath.open("rb") as f:
            put = requests.put(
                upload_url,
                data=f,
                headers={"Content-Type": mime, **upload_headers},
                timeout=600,
            )
        if put.status_code not in (200, 204):
            log.error(f"R2 PUT failed: HTTP {put.status_code} {put.text[:300]}")
            return None

        # ── Step 3: confirm ──
        r = self.session.post(self._url(f"/v1/assets/{asset_id}/uploads/confirm"))
        if r.status_code not in (200, 201):
            log.error(f"confirm failed: HTTP {r.status_code} {r.text[:300]}")
            return None

        return asset_id


# ─── 路由 + 过滤 ─────────────────────────────────────────────────
def should_skip(rel_path: Path, cfg: Config) -> Optional[str]:
    """返回 None = 不跳过；否则返回跳过原因字符串"""
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


def route_for(rel_path: Path, cfg: Config) -> tuple[str, str, list[str]]:
    """决定文件入哪个 tenant + project + 默认标签"""
    rel_str = str(rel_path).replace(os.sep, "/")
    for r in cfg.routes:
        if rel_str.startswith(r.prefix.replace(os.sep, "/")):
            return r.tenant_slug, r.project_slug, list(r.tags)
    return cfg.default_tenant_slug, cfg.default_project_slug, []


def derive_folder_and_tags(rel_path: Path, base_tags: list[str]) -> tuple[str, list[str]]:
    parts = rel_path.parent.parts
    folder = "/" + "/".join(parts) if parts else "/"
    auto_tags = []
    if parts:
        auto_tags.append(parts[0])
    if len(parts) > 1:
        auto_tags.append(parts[1])
    return folder, list(dict.fromkeys(base_tags + auto_tags))  # 去重保序


def sha256_file(fpath: Path) -> str:
    h = hashlib.sha256()
    with fpath.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ─── 主处理流程 ─────────────────────────────────────────────────
class Uploader:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = State()
        self.client = DAMClient(cfg)
        self._pending: dict[Path, float] = {}  # fpath → first_seen_at

    def schedule(self, fpath: Path) -> None:
        """记录待处理文件 + debounce timestamp"""
        self._pending[fpath] = time.time() + self.cfg.debounce_seconds

    def tick(self) -> None:
        """主循环每 0.5s 调一次：处理 debounce 已到的文件"""
        now = time.time()
        ready = [f for f, t in self._pending.items() if t <= now]
        for fpath in ready:
            self._pending.pop(fpath, None)
            try:
                self._process(fpath)
            except Exception as exc:
                log.exception(f"process failed for {fpath}: {exc}")

    def _process(self, fpath: Path) -> None:
        if not fpath.exists() or not fpath.is_file():
            return
        if not str(fpath).startswith(str(self.cfg.root)):
            return

        rel_path = fpath.relative_to(self.cfg.root)
        size = fpath.stat().st_size

        if size < self.cfg.min_file_bytes:
            return
        if size > self.cfg.max_file_mb * 1024 * 1024:
            log.info(f"SKIP {rel_path}: too large ({size / 1024 / 1024:.1f} MB)")
            return

        skip = should_skip(rel_path, self.cfg)
        if skip:
            return

        if size > self.cfg.max_simple_upload_mb * 1024 * 1024:
            log.warning(f"SKIP {rel_path}: > {self.cfg.max_simple_upload_mb}MB · multipart not implemented yet")
            return

        sha = sha256_file(fpath)
        if self.state.has(sha):
            return  # 已传过

        tenant_slug, project_slug, base_tags = route_for(rel_path, self.cfg)
        folder, tags = derive_folder_and_tags(rel_path, base_tags)
        ext = rel_path.suffix.lstrip(".")
        kind = kind_of(ext)

        project_id = self.client.resolve_project_id(tenant_slug, project_slug)
        if not project_id:
            log.error(f"SKIP {rel_path}: project {tenant_slug}/{project_slug} not resolvable")
            return

        log.info(f"UPLOAD {rel_path} → {tenant_slug}/{project_slug} {folder} kind={kind} ({size}B)")

        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                asset_id = self.client.upload(fpath, project_id, kind, folder, tags, sha)
                if asset_id:
                    self.state.add(sha, asset_id, str(rel_path))
                    log.info(f"  OK asset_id={asset_id}")
                    return
            except Exception as exc:
                log.error(f"  attempt {attempt} failed: {exc}")
            time.sleep(2 ** attempt)
        log.error(f"  GAVE UP after {self.cfg.max_retries} attempts: {rel_path}")


# ─── watchdog 事件 → uploader.schedule ─────────────────────────
class CoworkHandler(FileSystemEventHandler):
    def __init__(self, uploader: Uploader):
        self.uploader = uploader

    def on_created(self, event):
        if not event.is_directory:
            self.uploader.schedule(Path(event.src_path))

    def on_modified(self, event):
        if not event.is_directory:
            self.uploader.schedule(Path(event.src_path))

    def on_moved(self, event):
        if not event.is_directory:
            self.uploader.schedule(Path(event.dest_path))


# ─── main ────────────────────────────────────────────────────────
def main() -> None:
    cfg = Config.load_or_init()
    log.info(f"qidedam-watcher start · root={cfg.root}")

    if not cfg.root.exists():
        log.error(f"root path does not exist: {cfg.root}")
        sys.exit(2)

    uploader = Uploader(cfg)
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
