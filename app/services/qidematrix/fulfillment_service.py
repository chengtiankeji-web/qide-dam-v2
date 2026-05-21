"""S7 · 派单 · 物流推荐 · 收款工具 · 报关 HS Code

4 个子能力：
  1. recommend_factory   · 派单决策树（own/cmh/external）
  2. recommend_logistics · 物流推荐（按目的国+货量+紧急度）
  3. recommend_payment   · 收款工具推荐（按目的国+月流水）
  4. lookup_hs_code      · HS code 匹配（按产品类目 · 简单 keyword 映射）

设计原则：
- 不依赖外部 API · 全部用 lookup table + 规则（生产阶段可接菜鸟国际 / SF 国际 API）
- 推荐结果包含：方案名 / 估算成本 / 时效 / 推荐理由 / 风险提示
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.qidematrix.order import QmOrder

logger = get_logger("qm.fulfillment")


# ═════════════════════════════════════════════════════════════════════
# 1. 派单决策树 · 推荐工厂
# ═════════════════════════════════════════════════════════════════════

CMH_FACTORIES: list[dict] = [
    {
        "id": "yixinheng",
        "name": "深圳市艺欣恒有机制品有限公司",
        "categories": ["acrylic", "display-stand", "cosmetic-display", "electronic-display"],
        "moq": 100,
        "lead_time_days": 30,
        "markets": ["US", "CA", "GB", "DE", "AU"],
        "page_url": "https://chinamakershub.com/factories/yixinheng-acrylic/",
    },
    {
        "id": "gostoo",
        "name": "高思图家具有限公司",
        "categories": ["furniture", "custom-furniture", "wooden-furniture"],
        "moq": 50,
        "lead_time_days": 45,
        "markets": ["US", "AU", "NZ", "SG"],
        "page_url": "https://chinamakershub.com/factories/gostoo-furniture/",
    },
    # 庆福年 / 博能 等待 Sam 谈成后入档
]


def recommend_factory(
    *,
    product_category: str | None,
    buyer_country: str | None,
    quantity: int,
    customer_owns_factory: bool = False,
) -> dict[str, Any]:
    """派单决策

    - 客户本身就是工厂 → own
    - 否则在 CMH 池里按类目 + 国别 + MOQ 匹配
    - 都不匹配 → external（手动派）
    """
    if customer_owns_factory:
        return {
            "kind": "own",
            "factory_id": None,
            "factory_name": "客户自有工厂",
            "reason": "客户即工厂 · 直接派给客户生产线",
        }

    if not product_category:
        return {
            "kind": "external",
            "factory_id": None,
            "factory_name": None,
            "reason": "未指定产品类目 · 需运营手动派工厂",
        }

    cat_lower = product_category.lower()
    candidates = []
    for f in CMH_FACTORIES:
        # 类目匹配
        cat_match = any(c in cat_lower or cat_lower in c for c in f["categories"])
        if not cat_match:
            continue
        # MOQ 检查
        if quantity < f["moq"]:
            continue
        # 国别匹配（不强制 · 是 nice-to-have）
        market_match = (
            buyer_country and buyer_country.upper() in f["markets"]
        )
        candidates.append((f, market_match))

    if candidates:
        # 优先 market_match=True 的
        candidates.sort(key=lambda x: (not x[1], x[0]["lead_time_days"]))
        winner = candidates[0][0]
        return {
            "kind": "cmh",
            "factory_id": winner["id"],
            "factory_name": winner["name"],
            "factory_page_url": winner["page_url"],
            "lead_time_days": winner["lead_time_days"],
            "reason": (
                f"匹配 CMH 工厂 · 类目 {product_category} · "
                f"MOQ {winner['moq']} · 目标市场 "
                f"{'✓' if candidates[0][1] else '不在熟区'}"
            ),
        }

    return {
        "kind": "external",
        "factory_id": None,
        "factory_name": None,
        "reason": f"CMH 池无匹配工厂（类目 {product_category} / qty {quantity}）· 需手动派",
    }


# ═════════════════════════════════════════════════════════════════════
# 2. 物流推荐
# ═════════════════════════════════════════════════════════════════════

# 按国别 + 货量 + 紧急度的简化决策表
LOGISTICS_MATRIX: dict[str, list[dict]] = {
    "US": [
        {"method": "海运拼箱 LCL", "weight_kg_range": (50, 1000), "days": 35, "cost_usd_per_kg": 2.5, "carrier": "Matson / OOCL"},
        {"method": "海运整柜 FCL 20'", "weight_kg_range": (1000, 28000), "days": 30, "cost_usd_per_kg": 0.6, "carrier": "OOCL"},
        {"method": "空运标准", "weight_kg_range": (10, 500), "days": 10, "cost_usd_per_kg": 7.5, "carrier": "DHL / FedEx"},
        {"method": "空运经济", "weight_kg_range": (10, 1000), "days": 18, "cost_usd_per_kg": 4.5, "carrier": "Aramex"},
        {"method": "快递 DHL Express", "weight_kg_range": (0.1, 50), "days": 5, "cost_usd_per_kg": 15.0, "carrier": "DHL"},
    ],
    "AU": [
        {"method": "海运拼箱 LCL", "weight_kg_range": (50, 1000), "days": 25, "cost_usd_per_kg": 2.2, "carrier": "ANL / OOCL"},
        {"method": "海运整柜 FCL 20'", "weight_kg_range": (1000, 28000), "days": 22, "cost_usd_per_kg": 0.5, "carrier": "ANL"},
        {"method": "空运标准", "weight_kg_range": (10, 500), "days": 7, "cost_usd_per_kg": 7.0, "carrier": "DHL"},
    ],
    "GB": [
        {"method": "海运拼箱 LCL", "weight_kg_range": (50, 1000), "days": 35, "cost_usd_per_kg": 2.8, "carrier": "Maersk"},
        {"method": "海运整柜 FCL 20'", "weight_kg_range": (1000, 28000), "days": 32, "cost_usd_per_kg": 0.7, "carrier": "Maersk"},
        {"method": "中欧班列", "weight_kg_range": (1000, 28000), "days": 22, "cost_usd_per_kg": 1.0, "carrier": "DB Schenker"},
        {"method": "空运标准", "weight_kg_range": (10, 500), "days": 8, "cost_usd_per_kg": 6.5, "carrier": "DHL"},
    ],
    "DE": [
        # 沿用 GB 表（欧洲西部相近）
    ],
    "_default": [
        {"method": "海运拼箱 LCL", "weight_kg_range": (50, 1000), "days": 40, "cost_usd_per_kg": 3.0, "carrier": "多承运商"},
        {"method": "空运经济", "weight_kg_range": (10, 1000), "days": 14, "cost_usd_per_kg": 5.5, "carrier": "Aramex"},
    ],
}


def recommend_logistics(
    *,
    buyer_country: str | None,
    weight_kg: float,
    urgency: str = "normal",
) -> dict[str, Any]:
    """物流推荐

    urgency: normal (默认) / urgent (≤ 10 天) / economy (优先低价)
    """
    country = (buyer_country or "").upper()
    options = LOGISTICS_MATRIX.get(country) or LOGISTICS_MATRIX["_default"]
    if not options and country in ("DE", "FR", "NL"):
        options = LOGISTICS_MATRIX.get("GB", LOGISTICS_MATRIX["_default"])

    # 过滤权重范围
    eligible = [
        o for o in options
        if o["weight_kg_range"][0] <= weight_kg <= o["weight_kg_range"][1]
    ]
    if not eligible:
        eligible = options  # fallback

    # 按 urgency 排序
    if urgency == "urgent":
        eligible.sort(key=lambda o: o["days"])
    elif urgency == "economy":
        eligible.sort(key=lambda o: o["cost_usd_per_kg"])
    else:
        # normal · cost × days 综合
        eligible.sort(key=lambda o: o["cost_usd_per_kg"] * o["days"])

    recommendations = []
    for o in eligible[:3]:
        est_cost = round(weight_kg * o["cost_usd_per_kg"], 2)
        recommendations.append({
            "method": o["method"],
            "estimated_days": o["days"],
            "estimated_cost_usd": est_cost,
            "cost_per_kg_usd": o["cost_usd_per_kg"],
            "carrier_hint": o["carrier"],
        })

    return {
        "buyer_country": country or "unknown",
        "weight_kg": weight_kg,
        "urgency": urgency,
        "top_3": recommendations,
        "recommended": recommendations[0] if recommendations else None,
    }


# ═════════════════════════════════════════════════════════════════════
# 3. 收款工具推荐
# ═════════════════════════════════════════════════════════════════════

PAYMENT_TOOLS: list[dict] = [
    {
        "name": "PingPong",
        "fee_pct": 1.0,
        "supports": ["US", "GB", "DE", "FR", "JP", "AU"],
        "for_monthly_usd": (1000, 200000),
        "notes": "中国本土团队 · 中文客服 · 提现到国内人民币 · 适合中小工厂",
    },
    {
        "name": "WorldFirst",
        "fee_pct": 0.5,
        "supports": ["US", "GB", "EU", "AU", "CA"],
        "for_monthly_usd": (5000, 500000),
        "notes": "Alipay 旗下 · 多币种本地账户 · 适合 Amazon FBA",
    },
    {
        "name": "Payoneer",
        "fee_pct": 1.0,
        "supports": ["US", "GB", "EU", "JP", "AU", "CA", "SG"],
        "for_monthly_usd": (1000, 500000),
        "notes": "全球通用 · 接 Stripe/Amazon/eBay · 提现需手续费",
    },
    {
        "name": "Stripe",
        "fee_pct": 2.9,
        "supports": ["US", "GB", "EU", "AU", "CA"],
        "for_monthly_usd": (100, 100000),
        "notes": "适合 DTC / 独立站 · 信用卡为主 · 中国主体不直接支持 · 走 HK/SG",
    },
    {
        "name": "信用证 LC",
        "fee_pct": 0.3,
        "supports": ["*"],
        "for_monthly_usd": (50000, 10000000),
        "notes": "大额订单 · 银行信用 · 流程慢但买家安全感强",
    },
]


def recommend_payment(
    *,
    buyer_country: str | None,
    monthly_revenue_usd: float,
) -> dict[str, Any]:
    """收款工具推荐"""
    country = (buyer_country or "").upper()
    eligible = []

    for tool in PAYMENT_TOOLS:
        supports = tool["supports"]
        if "*" not in supports and country and country not in supports:
            continue
        low, high = tool["for_monthly_usd"]
        if monthly_revenue_usd < low or monthly_revenue_usd > high:
            continue
        eligible.append(tool)

    # 按 fee_pct 升序
    eligible.sort(key=lambda t: t["fee_pct"])

    return {
        "buyer_country": country or "any",
        "monthly_usd": monthly_revenue_usd,
        "top_3": [
            {
                "name": t["name"],
                "fee_pct": t["fee_pct"],
                "notes": t["notes"],
                "monthly_cost_estimate_usd": round(monthly_revenue_usd * t["fee_pct"] / 100, 2),
            }
            for t in eligible[:3]
        ],
    }


# ═════════════════════════════════════════════════════════════════════
# 4. HS Code 查询
# ═════════════════════════════════════════════════════════════════════

HS_CODE_KEYWORDS: dict[str, list[str]] = {
    # 制造业 / 工业品（前 6 位通用 · 美 10 位 · 中 13 位 / 简化）
    "9403.60.80": ["furniture", "wooden", "table", "chair", "wood", "家具", "木制"],
    "3924.10.40": ["plastic kitchen", "plastic table", "plastic", "塑料制品", "塑料厨具"],
    "3926.40.00": ["plastic display", "acrylic display", "亚克力", "亚克力展架"],
    "8541.40.95": ["led", "led module", "led 模组"],
    "6307.90.98": ["textile", "fabric", "纺织品", "织物"],
    "8504.40.95": ["adapter", "power supply", "电源", "适配器"],
    "9405.10.60": ["lamp", "lighting", "灯具", "led light"],
    "_default": [],
}


def lookup_hs_code(product_name: str, product_category: str | None = None) -> list[dict]:
    """简单 keyword 匹配 · 返回 top 3 候选 HS code

    生产应该接 US ITC HTS API / EU TARIC / 中国 GACC · 这里走简化 lookup。
    """
    text = f"{product_name} {product_category or ''}".lower()
    matches = []
    for hs_code, keywords in HS_CODE_KEYWORDS.items():
        if hs_code == "_default":
            continue
        for kw in keywords:
            if kw.lower() in text:
                matches.append({
                    "hs_code": hs_code,
                    "matched_keyword": kw,
                    "confidence": "high" if len(kw) > 6 else "medium",
                })
                break

    if not matches:
        return [{
            "hs_code": "9999.99.99",
            "matched_keyword": None,
            "confidence": "none",
            "note": "未匹配 · 需手动查询 US ITC HTS（hts.usitc.gov）或 GACC（china-trade.com.cn）",
        }]

    return matches[:3]


# ═════════════════════════════════════════════════════════════════════
# 5. 综合 · 给一个订单一键算 全套履约建议
# ═════════════════════════════════════════════════════════════════════

async def compute_fulfillment_recommendation(
    db: AsyncSession,
    *,
    order_id: uuid.UUID,
) -> dict[str, Any]:
    """订单已派单 · 综合算物流 + 收款 + HS code · 写回 order.logistics_recommendation"""
    result = await db.execute(select(QmOrder).where(QmOrder.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        return {"error": "order not found"}

    # 物流（用 line_items 估算重量 · 没有就按 100 kg 默认）
    total_weight = 100.0
    for item in (order.product_line_items or []):
        w = item.get("weight_kg") or item.get("weight")
        if w:
            try:
                total_weight += float(w) * int(item.get("quantity") or 1)
            except (ValueError, TypeError):
                continue

    logistics = recommend_logistics(
        buyer_country=order.buyer_country,
        weight_kg=total_weight,
        urgency="normal",
    )

    # 收款
    payment = recommend_payment(
        buyer_country=order.buyer_country,
        monthly_revenue_usd=float(order.total_value_usd),
    )

    # HS code
    primary_product = ""
    if order.product_line_items:
        primary_product = order.product_line_items[0].get("name", "")
    hs_codes = lookup_hs_code(primary_product)

    recommendation = {
        "logistics": logistics,
        "payment": payment,
        "hs_codes": hs_codes,
    }

    order.logistics_recommendation = recommendation
    order.hs_codes = hs_codes
    from datetime import UTC, datetime
    order.updated_at = datetime.now(UTC)

    return recommendation
