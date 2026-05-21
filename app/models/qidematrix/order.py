"""QideMatrix v1 · S6 报价 + S7 派单 + 订单交付 models

Quote · S6 自动报价（基于工厂能力 + 客户产品）+ NNN 合同
Order · S7 派单订单（订单状态机 · 物流 · 收款 · 报关）
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class QmQuote(Base):
    """S6 自动报价 + NNN 合同自动套用"""
    __tablename__ = "qm_quotes"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    # 询盘信息
    buyer_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    buyer_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    buyer_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    buyer_company: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # 报价内容
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    product_sku: Mapped[str | None] = mapped_column(String(100), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    incoterms: Mapped[str] = mapped_column(String(10), nullable=False, default="FOB")
    lead_time_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)

    line_items: Mapped[list[dict]] = mapped_column(JSONB, default=list, nullable=False)
    total_value_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    pdf_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("assets.id", ondelete="SET NULL"), nullable=True
    )
    nnn_contract_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("assets.id", ondelete="SET NULL"), nullable=True
    )

    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    generation_method: Mapped[str] = mapped_column(String(20), nullable=False, default="ai")

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class QmOrder(Base):
    """S7 派单订单 · 物流 + 收款 + 报关

    assigned_factory_kind:
      own       客户自有工厂 (大多数 case · 客户 = 工厂)
      cmh       CMH 认证工厂 (客户 = 品牌方 / 卖家 · 派给艺欣恒 / 高思图等)
      external  外部工厂 (临时合作 · 不在 CMH 池)
    """
    __tablename__ = "qm_orders"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    quote_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("qm_quotes.id", ondelete="SET NULL"), nullable=True
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    order_number: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)

    # 买家
    buyer_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    buyer_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    buyer_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    shipping_address: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # 派给哪个工厂
    assigned_factory_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    assigned_factory_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    assigned_factory_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # 商品快照
    product_line_items: Mapped[list[dict]] = mapped_column(JSONB, default=list, nullable=False)
    total_value_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    incoterms: Mapped[str] = mapped_column(String(10), nullable=False, default="FOB")

    # 物流
    logistics_recommendation: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    chosen_logistics: Mapped[str | None] = mapped_column(String(50), nullable=True)
    tracking_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 收款
    payment_method: Mapped[str | None] = mapped_column(String(30), nullable=True)
    payment_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payment_amount_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    # 报关
    hs_codes: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    customs_status: Mapped[str | None] = mapped_column(String(20), nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    current_stage: Mapped[str] = mapped_column(String(30), nullable=False, default="placed")

    extra_metadata: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
