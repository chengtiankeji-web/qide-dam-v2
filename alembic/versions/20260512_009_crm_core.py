"""crm_core — leads + contacts + accounts + deals + quotes + activities

Revision ID: 009_crm_core
Revises: 008_social_accounts
Create Date: 2026-05-12

CRM 核心模块（Rocdesk 替代第一阶段·v7 MVP）

包含 6 张表 + 4 个 enum 类型：
  1. accounts          公司层
  2. contacts          联系人（属于 account）
  3. leads             询盘（含 6 要素分级）
  4. deals             商机（lead → deal 流转后）
  5. quotes            报价单（含 line_items JSONB）
  6. crm_activities    通用活动 timeline（email/call/meeting/note）

设计原则：
  - 全部多租户 tenant_id 必填
  - 与现有 audit_events / vault / project 互通
  - leads 表 6 要素分级 + classification 字段是 CRM 灵魂
  - quotes/orders line_items 用 JSONB 不拆子表（避免过度规范化）
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY

revision = "009_crm_core"
down_revision = "008_social_accounts"  # ⚠️ 等小龙的 social_accounts 合 main 后才能跑


def upgrade() -> None:
    # ════════════════════════════════════════════════════════
    # 1. accounts · 公司层（B2B 客户的雇主公司）
    # ════════════════════════════════════════════════════════
    op.create_table(
        "accounts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        # 基本信息
        sa.Column("legal_name", sa.String(512)),
        sa.Column("display_name", sa.String(256), nullable=False),
        sa.Column("country", sa.String(64), index=True),
        sa.Column("country_code", sa.String(2)),  # ISO 3166-1 alpha-2
        sa.Column("industry", sa.String(128)),
        sa.Column("sub_industry", sa.String(128)),
        sa.Column("employee_count", sa.Integer),
        sa.Column("annual_revenue_usd", sa.BigInteger),
        sa.Column("founded_year", sa.Integer),
        # 联系信息
        sa.Column("website", sa.Text),
        sa.Column("primary_phone", sa.String(64)),
        sa.Column("primary_email", sa.String(256)),
        sa.Column("billing_address", JSONB),
        sa.Column("shipping_address", JSONB),
        # CRM 内部
        sa.Column("owner_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), index=True),
        sa.Column("primary_contact_id", UUID(as_uuid=True)),  # FK 后建（contacts 还没建）
        sa.Column("source", sa.String(64)),  # linkedin/cmh-form/whatsapp/email/etc.
        sa.Column("status", sa.String(32), server_default="active"),
        sa.Column("tags", ARRAY(sa.String(64))),
        sa.Column("notes", sa.Text),
        # AI 增强（DashScope 自动背调 · v7.1 起填）
        sa.Column("ai_company_intel", JSONB,
                  comment="DashScope 抓的公开数据·{founded, hq, products, key_persons, competitors, news}"),
        sa.Column("ai_competitor_score", sa.Float,
                  comment="0-1·与本租户是不是同行竞品"),
        sa.Column("ai_lead_quality_score", sa.Float,
                  comment="0-1·综合质量分（与本工厂匹配度）"),
        sa.Column("ai_last_updated_at", sa.TIMESTAMP(timezone=True)),
        # 标识
        sa.Column("external_ids", JSONB,
                  comment="第三方系统 id 映射·{linkedin_company_id, clearbit_id, crunchbase_id}"),
        # 元数据
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.CheckConstraint(
            "status IN ('active', 'inactive', 'archived', 'merged', 'spam')",
            name="ck_accounts_status",
        ),
    )
    op.create_index("ix_accounts_tenant_country", "accounts", ["tenant_id", "country"])
    op.create_index("ix_accounts_legal_name_trgm", "accounts", ["legal_name"],
                    postgresql_using="gin", postgresql_ops={"legal_name": "gin_trgm_ops"})

    # ════════════════════════════════════════════════════════
    # 2. contacts · 联系人（个人）
    # ════════════════════════════════════════════════════════
    op.create_table(
        "contacts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("account_id", UUID(as_uuid=True),
                  sa.ForeignKey("accounts.id", ondelete="SET NULL"), index=True),
        # 个人信息
        sa.Column("first_name", sa.String(128)),
        sa.Column("last_name", sa.String(128)),
        sa.Column("full_name", sa.String(256), nullable=False),
        sa.Column("title", sa.String(256)),     # CEO / CTO / Purchasing Director / etc.
        sa.Column("role_category", sa.String(64),
                  comment="decision_maker/influencer/user/admin/unknown"),
        sa.Column("department", sa.String(128)),
        sa.Column("seniority_level", sa.String(64),
                  comment="C-level/VP/Director/Manager/IC/Junior"),
        # 联系方式
        sa.Column("email", sa.String(256), index=True),
        sa.Column("email_verified_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("phone", sa.String(64)),
        sa.Column("mobile", sa.String(64)),
        sa.Column("whatsapp", sa.String(64)),
        sa.Column("wechat", sa.String(128)),
        # 社交
        sa.Column("linkedin_url", sa.Text),
        sa.Column("social_handles", JSONB,
                  comment="{twitter, instagram, facebook, tiktok, youtube}"),
        # CRM
        sa.Column("owner_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), index=True),
        sa.Column("source", sa.String(64)),
        sa.Column("source_inbox_id", UUID(as_uuid=True),
                  comment="如来自 social_inbox v5+ 表"),
        sa.Column("status", sa.String(32), server_default="active"),
        sa.Column("opt_in_marketing", sa.Boolean, server_default=sa.text("true")),
        sa.Column("opt_in_marketing_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("unsubscribed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("bounced", sa.Boolean, server_default=sa.text("false")),
        sa.Column("tags", ARRAY(sa.String(64))),
        sa.Column("notes", sa.Text),
        # AI
        sa.Column("ai_personality_profile", JSONB,
                  comment="DashScope 基于公开内容生成·{tone, interests, hot_buttons}"),
        sa.Column("ai_last_engagement_summary", sa.Text,
                  comment="上次沟通简介·用于 BD 复盘"),
        # 元数据
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.CheckConstraint(
            "status IN ('active', 'inactive', 'bounced', 'unsubscribed', 'spam', 'archived')",
            name="ck_contacts_status",
        ),
        sa.CheckConstraint(
            "role_category IS NULL OR role_category IN "
            "('decision_maker', 'influencer', 'user', 'admin', 'gatekeeper', 'unknown')",
            name="ck_contacts_role_category",
        ),
    )
    op.create_index("ix_contacts_tenant_email", "contacts", ["tenant_id", "email"])

    # 现在 accounts.primary_contact_id 可以加 FK
    op.create_foreign_key(
        "fk_accounts_primary_contact",
        "accounts", "contacts",
        ["primary_contact_id"], ["id"],
        ondelete="SET NULL",
    )

    # ════════════════════════════════════════════════════════
    # 3. leads · 询盘（核心 · 含 6 要素分级）
    # ════════════════════════════════════════════════════════
    op.create_table(
        "leads",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("factory_slug", sa.String(64), nullable=False, index=True,
                  comment="哪个工厂的询盘·跨工厂线索回流时用"),
        sa.Column("project_id", UUID(as_uuid=True),
                  sa.ForeignKey("projects.id", ondelete="SET NULL")),
        # 来源
        sa.Column("source", sa.String(32), nullable=False, index=True,
                  comment="linkedin/fb/ig/tiktok/whatsapp/email/cmh-form/share-link/cold/referral/other"),
        sa.Column("source_inbox_id", UUID(as_uuid=True),
                  comment="关联 social_inbox.id(v5+)·如来自社媒 DM"),
        sa.Column("source_share_link_id", UUID(as_uuid=True),
                  sa.ForeignKey("share_links.id", ondelete="SET NULL"),
                  comment="如来自 brand portal share link resolve"),
        sa.Column("source_campaign", sa.String(128),
                  comment="UTM source/medium/campaign·营销归因"),
        sa.Column("source_url", sa.Text),
        sa.Column("source_referrer", sa.Text),
        # 询盘人信息（首次记录·后续关联 contact）
        sa.Column("contact_id", UUID(as_uuid=True),
                  sa.ForeignKey("contacts.id", ondelete="SET NULL"), index=True),
        sa.Column("account_id", UUID(as_uuid=True),
                  sa.ForeignKey("accounts.id", ondelete="SET NULL")),
        sa.Column("contact_name", sa.String(256)),
        sa.Column("contact_email", sa.String(256), index=True),
        sa.Column("contact_phone", sa.String(64)),
        sa.Column("contact_company", sa.String(256)),
        sa.Column("contact_country", sa.String(64)),
        sa.Column("contact_role", sa.String(256)),
        sa.Column("contact_ip", sa.String(64)),
        sa.Column("contact_ua", sa.Text),
        # 询盘内容
        sa.Column("inquiry_text", sa.Text, nullable=False),
        sa.Column("inquiry_attachments", JSONB,
                  comment="[{asset_id, filename, mime}, ...]"),
        sa.Column("inquiry_language", sa.String(8)),  # 'en', 'zh', 'es', 'ar', etc.
        # ⭐ 6 要素分级（核心算法在 services/crm/classification.py）
        sa.Column("has_quantity", sa.Boolean, server_default=sa.text("false")),
        sa.Column("has_budget", sa.Boolean, server_default=sa.text("false")),
        sa.Column("has_timeline", sa.Boolean, server_default=sa.text("false")),
        sa.Column("has_specification", sa.Boolean, server_default=sa.text("false")),
        sa.Column("has_decision_role", sa.Boolean, server_default=sa.text("false")),
        sa.Column("has_company_info", sa.Boolean, server_default=sa.text("false")),
        sa.Column("six_factor_score", sa.Integer, server_default="0",
                  comment="0-6·六要素总分"),
        sa.Column("six_factor_breakdown", JSONB,
                  comment="每要素的具体捕获文本·{quantity:'1000 pcs', budget:'$5000', ...}"),
        sa.Column("classification", sa.String(1), index=True,
                  comment="A/B/C/D 类·算法自动·BD 可手工 override"),
        sa.Column("classification_overridden", sa.Boolean, server_default=sa.text("false")),
        sa.Column("classification_overridden_by", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        # 状态机
        sa.Column("status", sa.String(32), nullable=False, server_default="new",
                  index=True),
        sa.Column("assigned_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), index=True),
        sa.Column("assigned_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("first_contact_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("first_contact_by", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("last_activity_at", sa.TIMESTAMP(timezone=True), index=True),
        sa.Column("qualified_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("converted_to_deal_id", UUID(as_uuid=True)),  # FK 后建
        sa.Column("converted_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("lost_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("lost_reason", sa.String(128)),  # price/spec/timing/competitor/etc.
        sa.Column("lost_competitor", sa.String(256)),
        # AI 增强
        sa.Column("ai_intent_summary", sa.Text,
                  comment="DashScope 总结意图·1-2 句话"),
        sa.Column("ai_suggested_reply", sa.Text,
                  comment="DashScope 起草初稿·BD 审后发"),
        sa.Column("ai_competitors_mentioned", ARRAY(sa.String(128))),
        sa.Column("ai_translated_zh", sa.Text,
                  comment="中文翻译·BD 看着方便"),
        sa.Column("ai_urgency_score", sa.Float,
                  comment="0-1·急迫度"),
        sa.Column("ai_quality_score", sa.Float,
                  comment="0-1·综合质量·与工厂匹配度"),
        # 标签
        sa.Column("tags", ARRAY(sa.String(64))),
        sa.Column("notes", sa.Text),
        # 元数据
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('new', 'contacted', 'qualified', 'unqualified', "
            "'nurturing', 'converted', 'lost', 'spam', 'archived')",
            name="ck_leads_status",
        ),
        sa.CheckConstraint(
            "classification IS NULL OR classification IN ('A', 'B', 'C', 'D')",
            name="ck_leads_classification",
        ),
        sa.CheckConstraint(
            "six_factor_score BETWEEN 0 AND 6",
            name="ck_leads_six_factor_range",
        ),
    )
    op.create_index("ix_leads_tenant_status_created", "leads",
                    ["tenant_id", "status", "created_at"])
    op.create_index("ix_leads_tenant_factory_class", "leads",
                    ["tenant_id", "factory_slug", "classification"])
    op.create_index("ix_leads_assigned_status", "leads",
                    ["assigned_user_id", "status"])

    # ════════════════════════════════════════════════════════
    # 4. deals · 商机（qualified lead → deal）
    # ════════════════════════════════════════════════════════
    op.create_table(
        "deals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("factory_slug", sa.String(64), nullable=False, index=True),
        sa.Column("name", sa.String(256), nullable=False),
        # 关联
        sa.Column("account_id", UUID(as_uuid=True),
                  sa.ForeignKey("accounts.id", ondelete="SET NULL"), index=True),
        sa.Column("primary_contact_id", UUID(as_uuid=True),
                  sa.ForeignKey("contacts.id", ondelete="SET NULL")),
        sa.Column("lead_id", UUID(as_uuid=True),
                  sa.ForeignKey("leads.id", ondelete="SET NULL"),
                  comment="来源 lead·1 lead → N deal 可能"),
        # 状态机（Pipeline）
        sa.Column("stage", sa.String(32), nullable=False, server_default="prospect",
                  index=True),
        sa.Column("stage_changed_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()")),
        # 价值
        sa.Column("estimated_value_usd", sa.Numeric(12, 2)),
        sa.Column("probability_pct", sa.Integer, server_default="50"),
        sa.Column("weighted_value_usd", sa.Numeric(12, 2),
                  comment="estimated × probability·forecast 用"),
        sa.Column("currency", sa.String(8), server_default="USD"),
        # 关联资产
        sa.Column("related_sku_slugs", ARRAY(sa.String(128))),
        sa.Column("related_quote_ids", ARRAY(UUID(as_uuid=True))),
        # 时间预期
        sa.Column("expected_close_date", sa.Date),
        sa.Column("actual_close_date", sa.Date),
        # Ownership
        sa.Column("owner_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), index=True),
        # 结果
        sa.Column("won_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("won_value_usd", sa.Numeric(12, 2)),
        sa.Column("lost_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("lost_reason", sa.String(128)),
        sa.Column("lost_competitor", sa.String(256)),
        # 标签
        sa.Column("tags", ARRAY(sa.String(64))),
        sa.Column("notes", sa.Text),
        # 元数据
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.CheckConstraint(
            "stage IN ('prospect', 'qualified', 'proposal', 'negotiation', "
            "'closed_won', 'closed_lost', 'on_hold')",
            name="ck_deals_stage",
        ),
        sa.CheckConstraint(
            "probability_pct BETWEEN 0 AND 100",
            name="ck_deals_probability",
        ),
    )
    op.create_index("ix_deals_owner_stage", "deals", ["owner_user_id", "stage"])

    # leads.converted_to_deal_id FK 加（now deals table exists）
    op.create_foreign_key(
        "fk_leads_converted_to_deal",
        "leads", "deals",
        ["converted_to_deal_id"], ["id"],
        ondelete="SET NULL",
    )

    # ════════════════════════════════════════════════════════
    # 5. quotes · 报价单
    # ════════════════════════════════════════════════════════
    op.create_table(
        "quotes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("quote_number", sa.String(64), nullable=False),
        # 关联
        sa.Column("deal_id", UUID(as_uuid=True),
                  sa.ForeignKey("deals.id", ondelete="CASCADE"), index=True),
        sa.Column("account_id", UUID(as_uuid=True),
                  sa.ForeignKey("accounts.id", ondelete="SET NULL")),
        sa.Column("contact_id", UUID(as_uuid=True),
                  sa.ForeignKey("contacts.id", ondelete="SET NULL")),
        sa.Column("factory_slug", sa.String(64)),
        # Line items（JSONB·不拆子表·减少 join）
        sa.Column("line_items", JSONB, nullable=False, server_default="[]",
                  comment="""[{
                  sku_slug, sku_name, description, qty,
                  unit_price_usd, line_total_usd, hs_code, weight_kg
                  }]"""),
        # 汇总
        sa.Column("subtotal_usd", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("discount_usd", sa.Numeric(12, 2), server_default="0"),
        sa.Column("tax_usd", sa.Numeric(12, 2), server_default="0"),
        sa.Column("shipping_usd", sa.Numeric(12, 2), server_default="0"),
        sa.Column("total_usd", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(8), server_default="USD"),
        # 条款
        sa.Column("validity_days", sa.Integer, server_default="30"),
        sa.Column("payment_terms", sa.String(128),
                  comment="如：30% TT deposit, 70% before shipment"),
        sa.Column("delivery_terms", sa.String(32),
                  comment="Incoterms 2020·FOB/CIF/DDP/EXW/etc."),
        sa.Column("delivery_port", sa.String(128)),
        sa.Column("estimated_lead_time_days", sa.Integer),
        # 状态
        sa.Column("status", sa.String(32), nullable=False, server_default="draft",
                  index=True),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("sent_to_email", sa.String(256)),
        sa.Column("viewed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("accepted_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("declined_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True)),
        # 文件
        sa.Column("pdf_storage_key", sa.Text,
                  comment="生成的 PDF 在 R2 的 key·走现有 storage 服务"),
        sa.Column("pdf_generated_at", sa.TIMESTAMP(timezone=True)),
        # Ownership
        sa.Column("owner_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), index=True),
        sa.Column("created_by_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        # 内部备注 & 客户可见备注
        sa.Column("internal_notes", sa.Text),
        sa.Column("customer_notes", sa.Text),
        # 元数据
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("tenant_id", "quote_number",
                            name="uq_quotes_tenant_number"),
        sa.CheckConstraint(
            "status IN ('draft', 'sent', 'viewed', 'accepted', 'declined', "
            "'expired', 'revised', 'cancelled')",
            name="ck_quotes_status",
        ),
        sa.CheckConstraint(
            "delivery_terms IS NULL OR delivery_terms IN "
            "('EXW', 'FOB', 'CIF', 'CFR', 'DDP', 'DAP', 'FCA', 'CPT', 'CIP', 'DPU')",
            name="ck_quotes_incoterms",
        ),
    )

    # ════════════════════════════════════════════════════════
    # 6. crm_activities · 通用活动 timeline
    # ════════════════════════════════════════════════════════
    op.create_table(
        "crm_activities",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("activity_type", sa.String(32), nullable=False, index=True,
                  comment="email/call/meeting/note/task/whatsapp/sms/dm/visit/quote_sent/etc."),
        # 关联（多态·按 entity_type 分发）
        sa.Column("entity_type", sa.String(32), nullable=False,
                  comment="lead/contact/account/deal/quote"),
        sa.Column("entity_id", UUID(as_uuid=True), nullable=False, index=True),
        # 通用字段
        sa.Column("subject", sa.String(512)),
        sa.Column("description", sa.Text),
        sa.Column("performed_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("performed_by_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), index=True),
        sa.Column("duration_minutes", sa.Integer),
        # 邮件特有
        sa.Column("email_message_id", sa.Text),
        sa.Column("email_from", sa.String(256)),
        sa.Column("email_to", ARRAY(sa.String(256))),
        sa.Column("email_subject", sa.String(512)),
        sa.Column("email_body_preview", sa.Text),
        sa.Column("email_opened_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("email_clicked_at", sa.TIMESTAMP(timezone=True)),
        # 会议特有
        sa.Column("meeting_location", sa.String(256)),
        sa.Column("meeting_attendees", ARRAY(sa.String(256))),
        sa.Column("meeting_outcome", sa.Text),
        # Task 特有
        sa.Column("task_due_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("task_completed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("task_priority", sa.String(16)),  # low/medium/high/urgent
        # 通用 metadata
        sa.Column("metadata", JSONB),
        sa.Column("attachments", JSONB,
                  comment="[{asset_id, filename, mime}, ...]"),
        # 元数据
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "entity_type IN ('lead', 'contact', 'account', 'deal', 'quote')",
            name="ck_activities_entity_type",
        ),
    )
    op.create_index("ix_activities_entity",
                    "crm_activities", ["entity_type", "entity_id", "performed_at"])
    op.create_index("ix_activities_tenant_time",
                    "crm_activities", ["tenant_id", "performed_at"])


def downgrade() -> None:
    op.drop_table("crm_activities")
    op.drop_constraint("fk_leads_converted_to_deal", "leads", type_="foreignkey")
    op.drop_table("quotes")
    op.drop_table("deals")
    op.drop_table("leads")
    op.drop_constraint("fk_accounts_primary_contact", "accounts", type_="foreignkey")
    op.drop_table("contacts")
    op.drop_table("accounts")
