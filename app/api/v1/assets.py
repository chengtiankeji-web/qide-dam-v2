"""Assets — search / list / detail / presigned upload / confirm / delete.

Sprint 1: small + presigned upload paths. Multipart upload deferred to Sprint 2.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.models.asset import Asset
from app.models.folder import Folder
from app.schemas.asset import (
    AssetOut,
    AssetUpdate,
    BulkMoveIn,
    BulkMoveOut,
    PresignedUploadIn,
    PresignedUploadOut,
)
from app.schemas.common import PageOut
from app.services import asset_service, audit_service, storage
from app.services.audit_service import AuditAction

router = APIRouter()


@router.post("/uploads/presign", response_model=PresignedUploadOut)
async def presign_upload(
    payload: PresignedUploadIn,
    skip_dedup: bool = False,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> PresignedUploadOut:
    """v3 phase 1.1 (2026-05-09): 同 project + 同 sha256 = 重复 → 409 Conflict
    body 含 sha256 时启用（watcher 默认带 · admin SPA 计算后带）
    skip_dedup=true 显式跳过 · 用于"我知道是同样的内容但仍想要副本"场景"""
    if not p.can_access_project(payload.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")
    try:
        asset, url, headers = await asset_service.register_presigned_upload(
            db, tenant_id=p.tenant_id, payload=payload, skip_dedup=skip_dedup
        )
    except asset_service.DuplicateAssetError as e:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "duplicate_asset",
                "message": "同项目下已有相同 sha256 的资产 · "
                           "调 ?skip_dedup=true 仍创建副本",
                "existing_asset": {
                    "id": str(e.existing.id),
                    "name": e.existing.name,
                    "size_bytes": e.existing.size_bytes,
                    "kind": e.existing.kind,
                    "created_at": e.existing.created_at.isoformat(),
                    "status": e.existing.status,
                },
            },
        ) from e
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    return PresignedUploadOut(
        asset_id=asset.id,
        upload_url=url,
        storage_key=asset.storage_key,
        method="PUT",
        headers=headers,
        expires_in=900,
    )


@router.post("/{asset_id}/uploads/confirm", response_model=AssetOut)
async def confirm_upload(
    asset_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> AssetOut:
    try:
        asset = await asset_service.confirm_upload(
            db, tenant_id=p.tenant_id, asset_id=asset_id
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return AssetOut.model_validate(asset)


def _can_see_secret(p: Principal) -> bool:
    """v3 P0-3 收尾补丁（2026-05-08）

    谁可以看到 sensitivity=secret 的资产元数据：
      - 人类（JWT）默认可以 —— admin SPA 展示 vault list 给用户看
        title / labels（但 reveal payload 仍需 vault:reveal scope + purpose）
      - API key / AI 默认不可以 —— 必须显式带 vault:reveal scope

    Vault payload 解密永远只能走 /v1/vault/{id}/reveal，与本函数无关。
    """
    if p.via == "jwt":
        return True
    return "vault:reveal" in (p.scopes or [])


async def _validate_folder_for_project(
    db: AsyncSession,
    *,
    folder_id: uuid.UUID | None,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
) -> None:
    """v3 phase 1.2 (2026-05-10): folder must belong to same project.

    Raises HTTPException(400) on cross-project / cross-tenant / not-found.
    folder_id=None is fine (= move to root).
    """
    if folder_id is None:
        return
    row = (await db.execute(
        select(Folder).where(
            Folder.id == folder_id,
            Folder.tenant_id == tenant_id,
            Folder.project_id == project_id,
            Folder.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"folder {folder_id} not found in project · 跨 project 移动暂不支持",
        )


@router.post("/_bulk/move", response_model=BulkMoveOut)
async def bulk_move(
    payload: BulkMoveIn,
    request: Request,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> BulkMoveOut:
    """v3 phase 1.2 (2026-05-10): 批量把多个 asset 移动到目标 folder（同 project）。

    流程：
      1. 拉所有 asset，校验都属于同 project（不同 project 的 asset 一并拒）
      2. 校验 target_folder_id 也属于同 project（None 等于"放回根"）
      3. 校验 principal 对该 project 有写权限
      4. 批量 UPDATE folder_id
      5. 写一条 asset.moved audit（target_id=project_id · metadata 里塞 asset_ids 和 to/from）
    """
    if not payload.asset_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "asset_ids empty")

    # Pull all candidate assets
    assets_q = await db.execute(
        select(Asset).where(
            Asset.id.in_(payload.asset_ids),
            Asset.tenant_id == p.tenant_id,
            Asset.deleted_at.is_(None),
        )
    )
    assets = list(assets_q.scalars().all())

    found_ids = {a.id for a in assets}
    failed: list[dict] = []
    for aid in payload.asset_ids:
        if aid not in found_ids:
            failed.append({"id": str(aid), "reason": "asset not found / archived / cross-tenant"})

    if not assets:
        return BulkMoveOut(moved=[], failed=failed)

    # All assets must share one project (cross-project bulk move 暂不支持)
    project_ids = {a.project_id for a in assets}
    if len(project_ids) > 1:
        for a in assets:
            failed.append({"id": str(a.id), "reason": f"cross-project bulk move 暂不支持 · project={a.project_id}"})
        return BulkMoveOut(moved=[], failed=failed)
    pid = next(iter(project_ids))

    # ACL: must have access to the project
    if not p.can_access_project(pid):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"no access to project {pid}")

    # Target folder validation (None = root)
    await _validate_folder_for_project(
        db, folder_id=payload.target_folder_id, tenant_id=p.tenant_id, project_id=pid
    )

    # Secret guard — none of the moved assets should be 'secret' for non-elevated principal
    moved: list[uuid.UUID] = []
    for a in assets:
        try:
            _assert_secret_allowed(a, p)
        except HTTPException:
            failed.append({"id": str(a.id), "reason": "sensitivity=secret · move denied"})
            continue
        a.folder_id = payload.target_folder_id
        moved.append(a.id)

    await db.flush()

    # One audit event per bulk operation (not per asset · keeps audit table sane)
    await audit_service.audit(
        db,
        action=AuditAction.ASSET_MOVED,
        tenant_id=p.tenant_id,
        project_id=pid,
        actor_user_id=p.user_id,
        actor_kind="user" if p.via == "jwt" else "api_key",
        target_kind="asset",
        target_id=None,
        request=request,
        metadata={
            "asset_ids": [str(i) for i in moved],
            "asset_count": len(moved),
            "target_folder_id": str(payload.target_folder_id) if payload.target_folder_id else None,
            "operation": "bulk_move",
            "failed_count": len(failed),
        },
    )

    return BulkMoveOut(moved=moved, failed=failed)


@router.get("", response_model=PageOut[AssetOut])
async def list_assets(
    project_id: uuid.UUID | None = Query(None),
    kind: str | None = Query(None),
    status_: str | None = Query(None, alias="status",
        description="ready / processing / uploading / failed · 默认 None=显示全部"),
    q: str | None = Query(None, description="full-text search across name/description/tags"),
    include_secret: bool | None = Query(
        None,
        description=(
            "v3 P0-3: 是否包含 sensitivity=secret 资产（vault_login / vault_identity / vault_note）。"
            "默认 None = 按 principal 推断："
            "JWT 用户默认 True（admin SPA 需要展示），"
            "api_key 默认 False（除非带 vault:reveal scope）。"
            "调用方显式传 false/true 时，false 永远生效（opt-out 永远准），"
            "true 仅在 caller 有权限时生效（无权时静默忽略 = 仍排除）。"
        ),
    ),
    show_trashed: bool = Query(
        False,
        description="True = 只显示回收站（deleted_at IS NOT NULL）· False = 默认显示活资产 · 永远不混合",
    ),
    folder_id: str | None = Query(
        None,
        description=(
            "phase 1.2 (2026-05-10) folder filter："
            "传 'root' 或 '__root__' = 只显示根目录（folder_id IS NULL）· "
            "传 UUID = 只显示该 folder 下的 · "
            "省略 = 不按 folder 过滤（项目下所有资产）"
        ),
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> PageOut[AssetOut]:
    if project_id and not p.can_access_project(project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")

    # 2026-04-29 fix: platform_admin 传了 project_id 时，
    # 自动从 project 反查 tenant_id（这样跨 tenant 选项目能看到资产）
    effective_tenant_id = p.tenant_id
    if project_id and p.is_platform_admin:
        from app.models.project import Project as _P
        proj = (await db.execute(select(_P).where(_P.id == project_id))).scalar_one_or_none()
        if proj:
            effective_tenant_id = proj.tenant_id

    # v3 P0-3 secret boundary
    if include_secret is False:
        effective_include_secret = False  # opt-out 永远准
    elif include_secret is True:
        effective_include_secret = _can_see_secret(p)  # 无权时静默降级
    else:
        effective_include_secret = _can_see_secret(p)  # 默认按 principal 推断

    # phase 1.2 folder filter parse
    folder_filter: str | uuid.UUID | None = None
    if folder_id is not None:
        if folder_id in ("root", "__root__", "null", ""):
            folder_filter = "__root__"
        else:
            try:
                folder_filter = uuid.UUID(folder_id)
            except ValueError:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"folder_id must be 'root', 'null', or a UUID · got {folder_id!r}",
                )

    items, total = await asset_service.list_assets(
        db,
        tenant_id=effective_tenant_id,
        project_id=project_id,
        kind=kind,
        status=status_,
        q=q,
        page=page,
        page_size=page_size,
        exclude_secret=not effective_include_secret,
        show_trashed=show_trashed,
        folder_filter=folder_filter,
    )
    # 2026-04-29 perf: 列表里给 image kind 一次性签 thumb sm presigned URL
    # 客户端不再需要单独 round-trip 拿 download-url
    out_items = []
    for a in items:
        ao = AssetOut.model_validate(a)
        if a.kind == "image" and a.thumbnails:
            tu: dict = {}
            for variant in ("sm", "md", "lg"):
                key = a.thumbnails.get(variant) if isinstance(a.thumbnails, dict) else None
                if key:
                    try:
                        tu[variant] = storage.presign_get(storage_key=key, expires_in=3600)
                    except Exception:  # noqa: BLE001
                        pass
            if tu:
                ao.thumb_urls = tu
        out_items.append(ao)
    return PageOut[AssetOut](
        items=out_items,
        total=total,
        page=page,
        page_size=page_size,
    )


async def _resolve_asset_tenant_id(
    db: AsyncSession, *, p: Principal, asset_id: uuid.UUID
) -> uuid.UUID:
    """For platform_admin, resolve the asset's tenant_id directly from the
    assets table (bypass JWT.tid). For other roles, use principal.tenant_id."""
    if not p.is_platform_admin:
        return p.tenant_id
    from app.models.asset import Asset as _A
    row = (await db.execute(select(_A).where(_A.id == asset_id))).scalar_one_or_none()
    return row.tenant_id if row else p.tenant_id


@router.get("/{asset_id}", response_model=AssetOut)
async def get_asset(
    asset_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> AssetOut:
    try:
        effective_tid = await _resolve_asset_tenant_id(db, p=p, asset_id=asset_id)
        asset = await asset_service.get_asset(db, tenant_id=effective_tid, asset_id=asset_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    if not p.can_access_project(asset.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access")

    # v3 P0-3 收尾补丁（2026-05-08）: secret 资产元数据需要权限
    # 之前 bug：api_key 拿到 vault_* 的完整 metadata（含 storage_key）
    # 现在：sensitivity=secret 必须 _can_see_secret · 否则 403
    # 注意：这只挡 metadata 元数据 · 真 payload 解密走 /v1/vault/{id}/reveal（更严）
    if asset.sensitivity_level == "secret" and not _can_see_secret(p):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "this asset is sensitivity=secret · need vault:reveal scope (api_key) "
            "or human JWT login · payload still requires reveal endpoint with purpose"
        )
    return AssetOut.model_validate(asset)


def _assert_secret_allowed(asset, p: Principal) -> None:
    """v3 P0-3 收尾补丁: 任何对 sensitivity=secret 资产的非 list 操作（get/patch/
    delete/download-url）都要走这个守卫。Vault 的 reveal 端点是更严的实例。"""
    if asset.sensitivity_level == "secret" and not _can_see_secret(p):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "this asset is sensitivity=secret · need vault:reveal scope (api_key) "
            "or human JWT login"
        )


@router.patch("/{asset_id}", response_model=AssetOut)
async def update_asset(
    asset_id: uuid.UUID,
    payload: AssetUpdate,
    request: Request,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> AssetOut:
    try:
        effective_tid = await _resolve_asset_tenant_id(db, p=p, asset_id=asset_id)
        asset = await asset_service.get_asset(db, tenant_id=effective_tid, asset_id=asset_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    if not p.can_access_project(asset.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access")
    _assert_secret_allowed(asset, p)

    data = payload.model_dump(exclude_unset=True)

    # 2026-05-10 phase 1.2: 如果改 folder_id，先校验目标 folder 同 project
    folder_changed = False
    old_folder_id = asset.folder_id
    if "folder_id" in data:
        new_folder_id = data["folder_id"]
        if new_folder_id != old_folder_id:
            await _validate_folder_for_project(
                db,
                folder_id=new_folder_id,
                tenant_id=effective_tid,
                project_id=asset.project_id,
            )
            folder_changed = True

    for field, value in data.items():
        setattr(asset, field, value)
    if asset.acl == "public" and not asset.public_url:
        asset.public_url = storage.public_url_for(asset.storage_key)
    await db.flush()

    # Audit: 移动单独写 asset.moved · 其他改动写 asset.updated
    if folder_changed:
        await audit_service.audit(
            db,
            action=AuditAction.ASSET_MOVED,
            tenant_id=effective_tid,
            project_id=asset.project_id,
            actor_user_id=p.user_id,
            actor_kind="user" if p.via == "jwt" else "api_key",
            target_kind="asset",
            target_id=asset.id,
            request=request,
            metadata={
                "from_folder_id": str(old_folder_id) if old_folder_id else None,
                "to_folder_id": str(asset.folder_id) if asset.folder_id else None,
                "operation": "single_move",
            },
        )

    return AssetOut.model_validate(asset)


@router.delete("/{asset_id}", status_code=204)
async def delete_asset(
    asset_id: uuid.UUID,
    hard: bool = Query(
        False,
        description="True = 永久删除（含 R2 对象 · 不可恢复 · 仅对回收站资产生效）· "
                    "False（默认）= soft delete · 进回收站 · 15 天后自动清",
    ),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> None:
    try:
        effective_tid = await _resolve_asset_tenant_id(db, p=p, asset_id=asset_id)
        # hard delete 时要从 trash 找 · soft delete 时只看活资产
        if hard:
            asset = await asset_service._get_asset_for_tenant_include_trashed(
                db, tenant_id=effective_tid, asset_id=asset_id
            )
        else:
            asset = await asset_service.get_asset(db, tenant_id=effective_tid, asset_id=asset_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    if not p.can_access_project(asset.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access")
    _assert_secret_allowed(asset, p)

    if hard:
        try:
            await asset_service.hard_delete_asset(
                db, tenant_id=effective_tid, asset_id=asset_id
            )
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    else:
        await asset_service.soft_delete_asset(
            db, tenant_id=effective_tid, asset_id=asset_id
        )


@router.post("/{asset_id}/restore", response_model=AssetOut)
async def restore_asset(
    asset_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> AssetOut:
    """从回收站恢复 · status 翻回 ready · deleted_at 清空"""
    try:
        effective_tid = await _resolve_asset_tenant_id(db, p=p, asset_id=asset_id)
        asset = await asset_service._get_asset_for_tenant_include_trashed(
            db, tenant_id=effective_tid, asset_id=asset_id
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    if not p.can_access_project(asset.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access")

    try:
        restored = await asset_service.restore_asset(
            db, tenant_id=effective_tid, asset_id=asset_id
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return AssetOut.model_validate(restored)


@router.delete("/_trash/empty", status_code=200)
async def empty_trash(
    project_id: uuid.UUID | None = Query(None, description="不传 = 清空整个 tenant 回收站"),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """清空回收站 · 永久删除（含 R2）。仅 tenant_admin / platform_admin · viewer / member 拒。"""
    if p.role not in {"tenant_admin", "platform_admin"} and not p.is_platform_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "empty trash requires admin role"
        )
    if project_id and not p.can_access_project(project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")

    # platform_admin 跨 tenant 时 · 默认还是用 principal.tenant_id
    # 如果传了 project_id，反查它的 tenant_id（跨 tenant 清场景）
    effective_tenant_id = p.tenant_id
    if project_id and p.is_platform_admin:
        from app.models.project import Project as _P
        proj = (await db.execute(select(_P).where(_P.id == project_id))).scalar_one_or_none()
        if proj:
            effective_tenant_id = proj.tenant_id

    result = await asset_service.empty_trash(
        db, tenant_id=effective_tenant_id, project_id=project_id
    )
    await db.commit()
    return result


@router.get("/{asset_id}/download-url")
async def get_download_url(
    asset_id: uuid.UUID,
    expires_in: int = Query(3600, ge=60, le=86400),
    variant: str | None = Query(None, description="sm/md/lg · image kind 才有 · 否则原图"),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        effective_tid = await _resolve_asset_tenant_id(db, p=p, asset_id=asset_id)
        asset = await asset_service.get_asset(db, tenant_id=effective_tid, asset_id=asset_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    if not p.can_access_project(asset.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access")
    _assert_secret_allowed(asset, p)

    # 2026-04-29 fix: variant=sm/md/lg 返回 thumbnail 缩略图 URL（如果存在）
    storage_key = asset.storage_key
    if variant and variant in ("sm", "md", "lg"):
        thumb_key = (asset.thumbnails or {}).get(variant)
        if thumb_key:
            storage_key = thumb_key
        # else: fallback to original

    url = storage.presign_get(storage_key=storage_key, expires_in=expires_in)
    return {"url": url, "expires_in": expires_in, "variant": variant or "original"}


# ─── phase 1 (2026-05-08): text-content 预览端点 ───────────────────
# 给 admin SPA 渲染 md / txt / code · 服务端从 R2 拉文本返回
# 避开浏览器 CORS（直接 PUT 到 presigned R2 URL 不带 Allow-Origin）
# 大于 256KB 的文件返回 413 · 引导用户下载

_TEXT_PREVIEW_EXTENSIONS = {
    "md", "markdown", "txt", "rst", "log", "csv", "tsv",
    "json", "jsonc", "yaml", "yml", "toml", "ini", "env", "conf",
    "py", "js", "jsx", "ts", "tsx", "mjs", "cjs", "vue", "svelte",
    "html", "htm", "xml", "css", "scss", "sass", "less", "styl",
    "sh", "bash", "zsh", "fish", "ps1", "bat", "cmd",
    "go", "rs", "java", "kt", "swift", "rb", "php", "pl", "lua",
    "sql", "graphql", "gql", "proto",
    "dockerfile", "makefile", "gitignore", "gitattributes",
    "tf", "hcl", "nginx",
}
_TEXT_PREVIEW_MAX_BYTES = 256 * 1024  # 256 KB


@router.get("/{asset_id}/text-content")
async def get_text_content(
    asset_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """读取 md / txt / 代码文件的纯文本内容。

    服务端代理拉取 R2 · 避开浏览器 CORS · 超 256KB 拒。
    返回 {"content": "<utf-8 text>", "size_bytes": int, "extension": str}
    """
    try:
        effective_tid = await _resolve_asset_tenant_id(db, p=p, asset_id=asset_id)
        asset = await asset_service.get_asset(db, tenant_id=effective_tid, asset_id=asset_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    if not p.can_access_project(asset.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access")
    _assert_secret_allowed(asset, p)

    ext = (asset.extension or "").lower().lstrip(".")
    if ext not in _TEXT_PREVIEW_EXTENSIONS:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"text preview not supported for extension '{ext}' · use download-url instead",
        )
    if asset.size_bytes and asset.size_bytes > _TEXT_PREVIEW_MAX_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"file too large for text preview ({asset.size_bytes} > {_TEXT_PREVIEW_MAX_BYTES}) · download instead",
        )

    # 2026-05-09 fix: 资产 status 不是 ready 就别去拉 R2 ·
    # 失败 multipart 留下 status=uploading 的僵尸行 · storage_key 指向不存在的对象 ·
    # 直 boto3 内部重试 30+s · 触发 cloudflared 502（不带 CORS 头）→ 前端蒙逼。
    # 直接 425 Too Early 让前端拿到带 CORS 的明确错误。
    if asset.status != "ready":
        raise HTTPException(
            status.HTTP_425_TOO_EARLY,
            f"asset status is '{asset.status}' · not ready for preview · "
            f"upload may be incomplete · check Assets list for status",
        )

    try:
        body = storage.get_object(asset.storage_key)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"R2 fetch failed: {e}",
        ) from e

    # 解码 · 优先 utf-8 · 回退 utf-8 ignore（不致命 · 显示 ⟨replacement⟩ 字符）
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("utf-8", errors="replace")

    return {
        "content": text,
        "size_bytes": len(body),
        "extension": ext,
        "asset_id": str(asset_id),
    }
