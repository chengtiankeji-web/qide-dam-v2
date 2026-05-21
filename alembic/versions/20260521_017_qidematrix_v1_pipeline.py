"""QideMatrix v1 · 8 阶段业务流 pipeline + 事件总线 + 邮件 outbox + 派单 + 健康度

Revision ID: 017_qm_v1_pipeline
Revises: 016_qm_topic_monitor
Create Date: 2026-05-21

═══════════════════════════════════════════════════════════════════════
背景：
═══════════════════════════════════════════════════════════════════════
QideMatrix v1 按 8 阶段业务流（Sam 2026-05-21 拍板）·
入口 = CMH 入驻表单 · 不是 qidematrix.com 注册：

  S1 · CMH 入驻表单 + 素材上传 (入口)
  S2 · AI 出海诊断报告 (auto · LLM + PDF)
  S3 · DAM 自动入库 (触发器 · workspace 自动建)
  S4 · 12 平台社媒矩阵搭建 (运营接单)
  S5 · 内容批量生产 (SEO 引擎 multi-tenant)
  S6 · 询盘转化 (CRM v7 + AI 客服 + 自动报价)
  S7 · 派单工厂 (订单交付 + 物流 + 收款 + 报关)
  S8 · 链路健康仪表盘 (实时监控 + 月报)

7 张新表（前缀 qm_）：
  1. qm_pipeline_events     · 事件总线 (immutable / audit-style)
  2. qm_onboardings         · S1 入驻申请 (跟 CMH /factory-apply 对接)
  3. qm_diagnostics         · S2 AI 诊断报告
  4. qm_email_outbox        · 邮件队列 (Resend / SMTP 发送)
  5. qm_orders              · S7 派单订单
  6. qm_quotes              · S6 自动报价
  7. qm_health_metrics      · S8 链路健康度时序

事件总线设计：
  - PostgreSQL LISTEN/NOTIFY 简单实现（不引 Redis Streams）
  - qm_pipeline_events 既是日志又是死信
  - audit_events_immutable 模式 · BEFORE UPDATE/DELETE 触发器 RAISE EXCEPTION
  - 14 个事件类型（onboarding.* / diagnostic.* / dam.* / social.* / content.* / lead.* / order.* / health.*）

═══════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from alembic import op

revision: str = "017_qm_v1_pipeline"
down_revision: Union[str, None] = "016_qm_topic_monitor"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ═══════════════════════════════════════════════════════════════
    # 1. qm_pipeline_events · 事件总线 (immutable)
    # ═══════════════════════════════════════════════════════════════
    op.execute(
        """
        CREATE TABLE qm_pipeline_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id UUID REFERENCES qm_workspaces(id) ON DELETE CASCADE,
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

            event_type VARCHAR(64) NOT NULL,
            stage VARCHAR(4) NOT NULL,
            actor_kind VARCHAR(20) NOT NULL DEFAULT 'system',
            actor_id UUID,

            subject_kind VARCHAR(50),
            subject_id UUID,

            payload JSONB NOT NULL DEFAULT '{}'::jsonb,

            -- 投递状态机
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            attempts INT NOT NULL DEFAULT 0,
            last_error TEXT,
            last_attempt_at TIMESTAMPTZ,
            delivered_at TIMESTAMPTZ,

            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT chk_qm_pipeline_events_stage
                CHECK (stage IN ('S1','S2','S3','S4','S5','S6','S7','S8')),
            CONSTRAINT chk_qm_pipeline_events_status
                CHECK (status IN ('pending','processing','delivered','failed','parked')),
            CONSTRAINT chk_qm_pipeline_events_actor_kind
                CHECK (actor_kind IN ('system','user','ai_agent','external'))
        );
        CREATE INDEX idx_qm_events_workspace_created
            ON qm_pipeline_events(workspace_id, created_at DESC);
        CREATE INDEX idx_qm_events_status_pending
            ON qm_pipeline_events(status, created_at)
            WHERE status IN ('pending','processing','failed');
        CREATE INDEX idx_qm_events_type_stage
            ON qm_pipeline_events(event_type, stage);
        CREATE INDEX idx_qm_events_subject
            ON qm_pipeline_events(subject_kind, subject_id);
        """
    )

    # 不可改触发器（同 audit_events 模式 · 5-07 v3 P0 已验证）
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qm_pipeline_events_immutable() RETURNS TRIGGER AS $$
        BEGIN
            -- 允许 status / attempts / last_error / last_attempt_at / delivered_at 状态机推进
            -- 其他字段不可改
            IF (
                OLD.event_type IS DISTINCT FROM NEW.event_type OR
                OLD.workspace_id IS DISTINCT FROM NEW.workspace_id OR
                OLD.tenant_id IS DISTINCT FROM NEW.tenant_id OR
                OLD.stage IS DISTINCT FROM NEW.stage OR
                OLD.actor_kind IS DISTINCT FROM NEW.actor_kind OR
                OLD.actor_id IS DISTINCT FROM NEW.actor_id OR
                OLD.subject_kind IS DISTINCT FROM NEW.subject_kind OR
                OLD.subject_id IS DISTINCT FROM NEW.subject_id OR
                OLD.payload IS DISTINCT FROM NEW.payload OR
                OLD.created_at IS DISTINCT FROM NEW.created_at
            ) THEN
                RAISE EXCEPTION 'qm_pipeline_events: core fields are immutable (event_type/workspace_id/tenant_id/stage/actor/subject/payload/created_at)';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_qm_pipeline_events_immutable
            BEFORE UPDATE ON qm_pipeline_events
            FOR EACH ROW EXECUTE FUNCTION qm_pipeline_events_immutable();

        CREATE OR REPLACE FUNCTION qm_pipeline_events_no_delete() RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'qm_pipeline_events is append-only — DELETE blocked';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_qm_pipeline_events_no_delete
            BEFORE DELETE ON qm_pipeline_events
            FOR EACH ROW EXECUTE FUNCTION qm_pipeline_events_no_delete();
        """
    )

    # LISTEN/NOTIFY · publish 时自动 NOTIFY · workers 监听 qm_event channel
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qm_pipeline_events_notify() RETURNS TRIGGER AS $$
        BEGIN
            PERFORM pg_notify(
                'qm_event',
                json_build_object(
                    'event_id', NEW.id,
                    'event_type', NEW.event_type,
                    'stage', NEW.stage,
                    'workspace_id', NEW.workspace_id,
                    'subject_id', NEW.subject_id
                )::text
            );
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_qm_pipeline_events_notify
            AFTER INSERT ON qm_pipeline_events
            FOR EACH ROW EXECUTE FUNCTION qm_pipeline_events_notify();
        """
    )

    # ═══════════════════════════════════════════════════════════════
    # 2. qm_onboardings · S1 入驻申请
    # ═══════════════════════════════════════════════════════════════
    op.execute(
        """
        CREATE TABLE qm_onboardings (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            workspace_id UUID REFERENCES qm_workspaces(id) ON DELETE SET NULL,

            -- 客户基本信息（来自 CMH /factory-apply）
            factory_name VARCHAR(200) NOT NULL,
            contact_name VARCHAR(100) NOT NULL,
            contact_email VARCHAR(200) NOT NULL,
            contact_phone VARCHAR(50),
            contact_wechat VARCHAR(100),
            website_url VARCHAR(500),
            business_license_number VARCHAR(50),
            company_description TEXT,

            -- v1 升级字段 (5-10 个 · 给 S2 诊断 AI 用)
            product_categories TEXT[],
            target_markets TEXT[],
            export_stage VARCHAR(20),
            existing_social_urls JSONB DEFAULT '[]'::jsonb,
            monthly_budget VARCHAR(20),
            desired_services TEXT[],
            top_skus TEXT,
            biggest_pain_point TEXT,

            -- 来源 + 状态机
            source VARCHAR(50) NOT NULL DEFAULT 'cmh_factory_apply',
            source_ref VARCHAR(200),
            current_stage VARCHAR(4) NOT NULL DEFAULT 'S1',
            stage_status VARCHAR(20) NOT NULL DEFAULT 'submitted',
            assigned_operator_id UUID REFERENCES users(id),

            -- 关联资产
            asset_ids UUID[] DEFAULT '{}'::uuid[],
            diagnostic_id UUID,

            extra_metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT chk_qm_onboardings_stage
                CHECK (current_stage IN ('S1','S2','S3','S4','S5','S6','S7','S8','done')),
            CONSTRAINT chk_qm_onboardings_status
                CHECK (stage_status IN ('submitted','processing','blocked','ready','done','rejected')),
            CONSTRAINT chk_qm_onboardings_export_stage
                CHECK (export_stage IS NULL OR export_stage IN ('awareness','tried','converted','stable')),
            CONSTRAINT chk_qm_onboardings_budget
                CHECK (monthly_budget IS NULL OR monthly_budget IN ('<500','500-2000','2000-5000','5000+'))
        );
        CREATE INDEX idx_qm_onboardings_tenant_created
            ON qm_onboardings(tenant_id, created_at DESC);
        CREATE INDEX idx_qm_onboardings_stage_status
            ON qm_onboardings(current_stage, stage_status);
        CREATE INDEX idx_qm_onboardings_workspace
            ON qm_onboardings(workspace_id) WHERE workspace_id IS NOT NULL;
        CREATE INDEX idx_qm_onboardings_operator
            ON qm_onboardings(assigned_operator_id) WHERE assigned_operator_id IS NOT NULL;
        CREATE UNIQUE INDEX uq_qm_onboardings_source_ref
            ON qm_onboardings(source, source_ref) WHERE source_ref IS NOT NULL;
        """
    )

    # ═══════════════════════════════════════════════════════════════
    # 3. qm_diagnostics · S2 AI 诊断报告
    # ═══════════════════════════════════════════════════════════════
    op.execute(
        """
        CREATE TABLE qm_diagnostics (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            onboarding_id UUID NOT NULL REFERENCES qm_onboardings(id) ON DELETE CASCADE,
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            workspace_id UUID REFERENCES qm_workspaces(id) ON DELETE SET NULL,

            -- LLM 元信息
            model_name VARCHAR(100) NOT NULL,
            model_provider VARCHAR(50) NOT NULL,
            prompt_tokens INT,
            completion_tokens INT,

            -- 评分 (0-100)
            readiness_score INT NOT NULL,
            brand_score INT,
            product_score INT,
            channel_score INT,
            ops_score INT,
            compliance_score INT,

            -- 输出
            recommended_tier VARCHAR(20) NOT NULL,
            recommended_plan VARCHAR(50),
            industry_benchmark JSONB DEFAULT '{}'::jsonb,
            roadmap_30d JSONB DEFAULT '[]'::jsonb,
            roadmap_90d JSONB DEFAULT '[]'::jsonb,
            roadmap_365d JSONB DEFAULT '[]'::jsonb,
            risks JSONB DEFAULT '[]'::jsonb,
            executive_summary TEXT,
            full_report_markdown TEXT,

            -- PDF
            pdf_asset_id UUID REFERENCES assets(id) ON DELETE SET NULL,
            pdf_signed_url TEXT,
            pdf_signed_until TIMESTAMPTZ,

            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            error_message TEXT,
            generated_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT chk_qm_diagnostics_status
                CHECK (status IN ('pending','running','ready','failed')),
            CONSTRAINT chk_qm_diagnostics_tier
                CHECK (recommended_tier IN ('starter','pro','enterprise')),
            CONSTRAINT chk_qm_diagnostics_readiness_range
                CHECK (readiness_score BETWEEN 0 AND 100)
        );
        CREATE INDEX idx_qm_diagnostics_onboarding
            ON qm_diagnostics(onboarding_id);
        CREATE INDEX idx_qm_diagnostics_tenant
            ON qm_diagnostics(tenant_id, created_at DESC);
        CREATE INDEX idx_qm_diagnostics_status
            ON qm_diagnostics(status) WHERE status IN ('pending','running');
        """
    )

    # 加 FK 回填 onboardings.diagnostic_id
    op.execute(
        """
        ALTER TABLE qm_onboardings
            ADD CONSTRAINT fk_qm_onboardings_diagnostic_id
            FOREIGN KEY (diagnostic_id) REFERENCES qm_diagnostics(id) ON DELETE SET NULL;
        """
    )

    # ═══════════════════════════════════════════════════════════════
    # 4. qm_email_outbox · 邮件队列
    # ═══════════════════════════════════════════════════════════════
    op.execute(
        """
        CREATE TABLE qm_email_outbox (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            workspace_id UUID REFERENCES qm_workspaces(id) ON DELETE SET NULL,

            template_key VARCHAR(64) NOT NULL,
            locale VARCHAR(10) NOT NULL DEFAULT 'zh-CN',

            to_email VARCHAR(200) NOT NULL,
            to_name VARCHAR(200),
            cc_emails TEXT[],
            bcc_emails TEXT[],
            from_email VARCHAR(200) NOT NULL DEFAULT 'no-reply@qidelinktech.cn',
            reply_to VARCHAR(200),

            subject TEXT NOT NULL,
            body_text TEXT,
            body_html TEXT,
            attachments JSONB DEFAULT '[]'::jsonb,
            template_vars JSONB DEFAULT '{}'::jsonb,

            -- 关联
            onboarding_id UUID REFERENCES qm_onboardings(id) ON DELETE SET NULL,
            diagnostic_id UUID REFERENCES qm_diagnostics(id) ON DELETE SET NULL,
            related_event_id UUID REFERENCES qm_pipeline_events(id) ON DELETE SET NULL,

            -- 状态机
            status VARCHAR(20) NOT NULL DEFAULT 'queued',
            send_after TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            attempts INT NOT NULL DEFAULT 0,
            max_attempts INT NOT NULL DEFAULT 5,
            last_error TEXT,
            last_attempt_at TIMESTAMPTZ,
            sent_at TIMESTAMPTZ,
            provider VARCHAR(20),
            provider_msg_id VARCHAR(200),

            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT chk_qm_email_outbox_status
                CHECK (status IN ('queued','sending','sent','failed','cancelled'))
        );
        CREATE INDEX idx_qm_email_outbox_ready_to_send
            ON qm_email_outbox(send_after, status)
            WHERE status = 'queued';
        CREATE INDEX idx_qm_email_outbox_tenant_status
            ON qm_email_outbox(tenant_id, status, created_at DESC);
        CREATE INDEX idx_qm_email_outbox_to
            ON qm_email_outbox(to_email);
        """
    )

    # ═══════════════════════════════════════════════════════════════
    # 5. qm_quotes · S6 自动报价
    # ═══════════════════════════════════════════════════════════════
    op.execute(
        """
        CREATE TABLE qm_quotes (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            workspace_id UUID NOT NULL REFERENCES qm_workspaces(id) ON DELETE CASCADE,
            lead_id UUID,

            -- 询盘信息
            buyer_email VARCHAR(200),
            buyer_name VARCHAR(200),
            buyer_country VARCHAR(2),
            buyer_company VARCHAR(200),

            -- 报价内容
            product_name VARCHAR(200) NOT NULL,
            product_sku VARCHAR(100),
            quantity INT NOT NULL,
            unit_price_usd NUMERIC(10,2) NOT NULL,
            currency VARCHAR(3) NOT NULL DEFAULT 'USD',
            incoterms VARCHAR(10) NOT NULL DEFAULT 'FOB',
            lead_time_days INT,
            valid_until DATE,

            -- 计算
            line_items JSONB NOT NULL DEFAULT '[]'::jsonb,
            total_value_usd NUMERIC(12,2),

            -- 文件
            pdf_asset_id UUID REFERENCES assets(id) ON DELETE SET NULL,
            nnn_contract_asset_id UUID REFERENCES assets(id) ON DELETE SET NULL,

            -- LLM 元信息
            model_name VARCHAR(100),
            generation_method VARCHAR(20) NOT NULL DEFAULT 'ai',

            status VARCHAR(20) NOT NULL DEFAULT 'draft',
            sent_at TIMESTAMPTZ,
            accepted_at TIMESTAMPTZ,
            rejected_reason TEXT,

            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT chk_qm_quotes_status
                CHECK (status IN ('draft','sent','accepted','rejected','expired')),
            CONSTRAINT chk_qm_quotes_generation_method
                CHECK (generation_method IN ('ai','manual','template'))
        );
        CREATE INDEX idx_qm_quotes_workspace_created
            ON qm_quotes(workspace_id, created_at DESC);
        CREATE INDEX idx_qm_quotes_lead
            ON qm_quotes(lead_id) WHERE lead_id IS NOT NULL;
        CREATE INDEX idx_qm_quotes_status
            ON qm_quotes(workspace_id, status);
        """
    )

    # ═══════════════════════════════════════════════════════════════
    # 6. qm_orders · S7 派单订单
    # ═══════════════════════════════════════════════════════════════
    op.execute(
        """
        CREATE TABLE qm_orders (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            workspace_id UUID NOT NULL REFERENCES qm_workspaces(id) ON DELETE CASCADE,
            quote_id UUID REFERENCES qm_quotes(id) ON DELETE SET NULL,
            lead_id UUID,

            order_number VARCHAR(50) NOT NULL,

            -- 买家
            buyer_email VARCHAR(200),
            buyer_name VARCHAR(200),
            buyer_country VARCHAR(2),
            shipping_address JSONB,

            -- 派给哪个工厂
            assigned_factory_kind VARCHAR(20) NOT NULL,
            assigned_factory_id VARCHAR(100),
            assigned_factory_name VARCHAR(200),

            -- 商品快照
            product_line_items JSONB NOT NULL DEFAULT '[]'::jsonb,
            total_value_usd NUMERIC(12,2) NOT NULL,
            incoterms VARCHAR(10) NOT NULL DEFAULT 'FOB',

            -- 物流
            logistics_recommendation JSONB,
            chosen_logistics VARCHAR(50),
            tracking_number VARCHAR(100),
            shipped_at TIMESTAMPTZ,
            delivered_at TIMESTAMPTZ,

            -- 收款
            payment_method VARCHAR(30),
            payment_received_at TIMESTAMPTZ,
            payment_amount_usd NUMERIC(12,2),

            -- 报关
            hs_codes JSONB DEFAULT '[]'::jsonb,
            customs_status VARCHAR(20),

            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            current_stage VARCHAR(30) NOT NULL DEFAULT 'placed',

            extra_metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT uq_qm_orders_order_number UNIQUE (order_number),
            CONSTRAINT chk_qm_orders_status
                CHECK (status IN ('pending','accepted','in_production','shipped','delivered','completed','cancelled','disputed')),
            CONSTRAINT chk_qm_orders_factory_kind
                CHECK (assigned_factory_kind IN ('own','cmh','external'))
        );
        CREATE INDEX idx_qm_orders_workspace_created
            ON qm_orders(workspace_id, created_at DESC);
        CREATE INDEX idx_qm_orders_status
            ON qm_orders(status, current_stage);
        CREATE INDEX idx_qm_orders_factory
            ON qm_orders(assigned_factory_kind, assigned_factory_id);
        CREATE INDEX idx_qm_orders_quote
            ON qm_orders(quote_id) WHERE quote_id IS NOT NULL;
        """
    )

    # ═══════════════════════════════════════════════════════════════
    # 7. qm_health_metrics · S8 链路健康度时序
    # ═══════════════════════════════════════════════════════════════
    op.execute(
        """
        CREATE TABLE qm_health_metrics (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id UUID NOT NULL REFERENCES qm_workspaces(id) ON DELETE CASCADE,
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            onboarding_id UUID REFERENCES qm_onboardings(id) ON DELETE SET NULL,

            metric_date DATE NOT NULL,
            stage VARCHAR(4) NOT NULL,

            -- 状态
            stage_status VARCHAR(20) NOT NULL,
            blocked_days INT NOT NULL DEFAULT 0,

            -- 量化指标
            traffic_count INT DEFAULT 0,
            lead_count INT DEFAULT 0,
            qualified_lead_count INT DEFAULT 0,
            order_count INT DEFAULT 0,
            revenue_usd NUMERIC(12,2) DEFAULT 0,
            content_published_count INT DEFAULT 0,
            social_posts_count INT DEFAULT 0,

            -- 平台分布 + 地理分布
            platform_breakdown JSONB DEFAULT '{}'::jsonb,
            geo_breakdown JSONB DEFAULT '{}'::jsonb,

            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT uq_qm_health_metrics_day_stage
                UNIQUE (workspace_id, metric_date, stage),
            CONSTRAINT chk_qm_health_metrics_stage
                CHECK (stage IN ('S1','S2','S3','S4','S5','S6','S7','S8')),
            CONSTRAINT chk_qm_health_metrics_status
                CHECK (stage_status IN ('green','yellow','red','idle'))
        );
        CREATE INDEX idx_qm_health_workspace_date
            ON qm_health_metrics(workspace_id, metric_date DESC);
        CREATE INDEX idx_qm_health_blocked
            ON qm_health_metrics(workspace_id, stage)
            WHERE stage_status IN ('yellow','red');
        """
    )


def downgrade() -> None:
    # 顺序与 FK 依赖反向
    op.execute("DROP TABLE IF EXISTS qm_health_metrics CASCADE")
    op.execute("DROP TABLE IF EXISTS qm_orders CASCADE")
    op.execute("DROP TABLE IF EXISTS qm_quotes CASCADE")
    op.execute("DROP TABLE IF EXISTS qm_email_outbox CASCADE")
    op.execute("ALTER TABLE qm_onboardings DROP CONSTRAINT IF EXISTS fk_qm_onboardings_diagnostic_id")
    op.execute("DROP TABLE IF EXISTS qm_diagnostics CASCADE")
    op.execute("DROP TABLE IF EXISTS qm_onboardings CASCADE")
    op.execute("DROP TRIGGER IF EXISTS trg_qm_pipeline_events_notify ON qm_pipeline_events")
    op.execute("DROP TRIGGER IF EXISTS trg_qm_pipeline_events_immutable ON qm_pipeline_events")
    op.execute("DROP TRIGGER IF EXISTS trg_qm_pipeline_events_no_delete ON qm_pipeline_events")
    op.execute("DROP FUNCTION IF EXISTS qm_pipeline_events_notify()")
    op.execute("DROP FUNCTION IF EXISTS qm_pipeline_events_immutable()")
    op.execute("DROP FUNCTION IF EXISTS qm_pipeline_events_no_delete()")
    op.execute("DROP TABLE IF EXISTS qm_pipeline_events CASCADE")
