"""widen multipart_uploads.upload_id from VARCHAR(256) to TEXT

Revision ID: 005_widen_upload_id
Revises: 004_v3_security
Create Date: 2026-05-09

Bug：admin SPA Upload 页提交 multipart init 时 INSERT 报
  StringDataRightTruncationError: value too long for type character varying(256)

R2 实测返回的 multipart upload_id 约 330 字符（base64 编码的内部 token）·
S3 / MinIO 较短（~70）但 R2 显著更长。VARCHAR(256) 截断后 INSERT 拒。

修法：列改 TEXT · 不限制长度 · pg 内部存储一样高效 · 索引也照样能建。
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "005_widen_upload_id"
down_revision: Union[str, None] = "004_v3_security"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "multipart_uploads",
        "upload_id",
        existing_type=sa.String(256),
        type_=sa.Text(),
        existing_nullable=False,
    )


def downgrade() -> None:
    # downgrade 风险: 如果有现存行 upload_id 长度 > 256 会失败
    # 实操不会发生：multipart 上传完 / abort 后行就删了 · 表通常空
    op.alter_column(
        "multipart_uploads",
        "upload_id",
        existing_type=sa.Text(),
        type_=sa.String(256),
        existing_nullable=False,
    )
