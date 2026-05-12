"""quotes_service 集成测试 · 重点 line_items 计算 + 状态机 + PDF 渲染"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.crm.quotes_service import (
    ALLOWED_QUOTE_TRANSITIONS, _calculate_totals,
)


# ════════════════════════════════════════════════════════════
# line_items 计算 · 纯函数·不需 DB
# ════════════════════════════════════════════════════════════

def test_calculate_totals_basic():
    """简单 2 行 line_items"""
    items = [
        {"sku_slug": "a", "qty": 10, "unit_price_usd": 5.0},
        {"sku_slug": "b", "qty": 20, "unit_price_usd": 2.5},
    ]
    result = _calculate_totals(items)
    # 每行 line_total_usd 回写
    assert result["line_items"][0]["line_total_usd"] == 50.0
    assert result["line_items"][1]["line_total_usd"] == 50.0
    # subtotal
    assert result["subtotal_usd"] == Decimal("100")
    assert result["total_usd"] == Decimal("100")


def test_calculate_totals_with_discount_tax_shipping():
    """带 discount + tax + shipping"""
    items = [{"qty": 5, "unit_price_usd": 100}]
    result = _calculate_totals(
        items,
        discount_usd=Decimal("50"),
        tax_usd=Decimal("30"),
        shipping_usd=Decimal("20"),
    )
    assert result["subtotal_usd"] == Decimal("500")
    # 500 - 50 + 30 + 20 = 500
    assert result["total_usd"] == Decimal("500")


def test_calculate_totals_zero_qty():
    """0 数量 · 应该 0 小计"""
    items = [{"qty": 0, "unit_price_usd": 100}]
    result = _calculate_totals(items)
    assert result["subtotal_usd"] == Decimal("0")
    assert result["line_items"][0]["line_total_usd"] == 0.0


def test_calculate_totals_handles_none_values():
    """None 容错"""
    items = [{"qty": None, "unit_price_usd": None}]
    result = _calculate_totals(items)
    assert result["subtotal_usd"] == Decimal("0")


def test_calculate_totals_fractional_qty():
    """小数数量（订 kg 单位）"""
    items = [{"qty": 2.5, "unit_price_usd": 40}]
    result = _calculate_totals(items)
    assert result["subtotal_usd"] == Decimal("100")


# ════════════════════════════════════════════════════════════
# 状态机
# ════════════════════════════════════════════════════════════

def test_quote_state_machine_completeness():
    """所有 quote status 在 transitions table"""
    statuses = {"draft", "sent", "viewed", "accepted", "declined",
                "expired", "revised", "cancelled"}
    assert statuses == set(ALLOWED_QUOTE_TRANSITIONS.keys())


def test_quote_state_machine_draft_can_send_or_cancel():
    """draft 只能 → sent / cancelled"""
    targets = ALLOWED_QUOTE_TRANSITIONS["draft"]
    assert "sent" in targets
    assert "cancelled" in targets
    # 不能直接 accepted
    assert "accepted" not in targets


def test_quote_state_machine_cancelled_is_terminal():
    """cancelled 是终态"""
    assert ALLOWED_QUOTE_TRANSITIONS["cancelled"] == set()


def test_quote_state_machine_sent_can_revise():
    """sent 可以 revised（客户要求改）"""
    assert "revised" in ALLOWED_QUOTE_TRANSITIONS["sent"]


def test_quote_state_machine_accepted_can_only_cancel():
    """accepted 极少能再变 · 只 cancelled 兜底"""
    assert ALLOWED_QUOTE_TRANSITIONS["accepted"] == {"cancelled"}


# ════════════════════════════════════════════════════════════
# DB 集成（需 fixture）
# ════════════════════════════════════════════════════════════

pytestmark = pytest.mark.asyncio


@pytest.mark.skip(reason="Requires async db fixture + ReportLab installed")
async def test_create_quote_generates_number(db, principal):
    """quote_number 按年生成 · Q-2026-0001"""
    from app.services.crm import quotes_service

    quote = await quotes_service.create_quote(
        db, principal=principal, tenant_id=principal.tenant_id,
        deal_id=None,
        line_items=[{"sku_slug": "test", "qty": 1, "unit_price_usd": 100}],
    )
    assert quote.quote_number.startswith("Q-2026-")
    assert len(quote.quote_number) >= 9


@pytest.mark.skip(reason="Requires async db fixture + ReportLab + R2 storage")
async def test_generate_pdf_writes_to_r2(db, principal):
    """PDF 生成 + 上传 R2"""
    from app.services.crm import quotes_service

    quote = await quotes_service.create_quote(
        db, principal=principal, tenant_id=principal.tenant_id,
        deal_id=None,
        line_items=[{
            "sku_slug": "yushikou-handcream",
            "description": "Vaseline Hand Cream 250ml",
            "qty": 1000,
            "unit_price_usd": 1.20,
        }],
        payment_terms="30% TT, 70% before shipment",
        delivery_terms="FOB",
        delivery_port="Shanghai",
    )
    quote_after, signed_url = await quotes_service.generate_pdf(
        db, principal=principal, quote_id=quote.id,
    )
    assert quote_after.pdf_storage_key is not None
    assert quote_after.pdf_generated_at is not None
    assert signed_url.startswith("http")
