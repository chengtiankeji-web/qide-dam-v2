"""QideMatrix v1 · 5 个核心邮件模板（zh-CN + en-US）

模板键（template_key）：
  welcome              · S1 客户提交入驻后 · 立即发
  diagnostic_ready     · S2 诊断报告完成 · 附 PDF signed URL
  social_ready         · S4 12 平台社媒矩阵搭建完成
  first_lead           · S6 第 1 个询盘进来
  monthly_report       · S8 每月 1 号客户月报 · 附 PDF
"""
from __future__ import annotations

from typing import Any


# 渲染时支持的变量占位符（Jinja2-style {{var}}）· 不引 Jinja · 用 str.replace 简化
def _render(template: str, vars_dict: dict[str, Any]) -> str:
    output = template
    for k, v in vars_dict.items():
        output = output.replace(f"{{{{{k}}}}}", str(v) if v is not None else "")
    return output


# ═════════════════════════════════════════════════════════════════════
# 模板库
# ═════════════════════════════════════════════════════════════════════

TEMPLATES: dict[str, dict[str, dict[str, str]]] = {
    # ─── welcome ────────────────────────────────────────────────────
    "welcome": {
        "zh-CN": {
            "subject": "{{factory_name}} · 感谢入驻 · 您的出海诊断 24h 内送达",
            "body_text": """{{contact_name}}，您好

感谢 {{factory_name}} 入驻 ChinaMakersHub。

我们已收到您的入驻申请 + 上传的素材 · 系统会在 24 小时内完成出海诊断 · 报告会发到这个邮箱。

诊断报告包含：
1. 工厂出海准备度评分（5 个维度 · 0-100 分）
2. 同行业 benchmark 对比
3. 30 / 90 / 365 天具体行动清单
4. 推荐服务方案 + 预算建议

期间您不用做任何事 · 我们的运营会主动联系您 + 给您一个专属对接群。

如有疑问 · 直接回这封邮件即可。

祁德商链科技
{{from_email}}
""",
            "body_html": """<p>{{contact_name}}，您好</p>
<p>感谢 <strong>{{factory_name}}</strong> 入驻 ChinaMakersHub。</p>
<p>我们已收到您的入驻申请 + 上传的素材 · 系统会在 <strong>24 小时内</strong>完成出海诊断 · 报告会发到这个邮箱。</p>
<p>诊断报告包含：</p>
<ol>
<li>工厂出海准备度评分（5 个维度 · 0-100 分）</li>
<li>同行业 benchmark 对比</li>
<li>30 / 90 / 365 天具体行动清单</li>
<li>推荐服务方案 + 预算建议</li>
</ol>
<p>期间您不用做任何事 · 我们的运营会主动联系您 + 给您一个专属对接群。</p>
<p>如有疑问 · 直接回这封邮件即可。</p>
<p>—— 祁德商链科技<br>{{from_email}}</p>""",
        },
        "en-US": {
            "subject": "{{factory_name}} · Welcome · Your export diagnostic arrives in 24h",
            "body_text": """Hello {{contact_name}},

Thank you for joining ChinaMakersHub.

We've received your factory onboarding form and uploaded materials. Our system will generate a complete export-readiness diagnostic within 24 hours and send the report to this email.

The diagnostic includes:
1. Export readiness score (5 dimensions, 0-100)
2. Industry benchmark comparison
3. Specific 30/90/365-day action plans
4. Recommended service tier with budget guidance

You don't need to do anything in the meantime. Our operations team will reach out and set up a dedicated channel with you.

Just reply to this email if you have any questions.

Best,
Qide Group
{{from_email}}
""",
            "body_html": """<p>Hello {{contact_name}},</p>
<p>Thank you for joining <strong>ChinaMakersHub</strong>.</p>
<p>We've received your factory onboarding form and uploaded materials. Our system will generate a complete export-readiness diagnostic within <strong>24 hours</strong> and send the report to this email.</p>
<ol>
<li>Export readiness score (5 dimensions, 0-100)</li>
<li>Industry benchmark comparison</li>
<li>Specific 30/90/365-day action plans</li>
<li>Recommended service tier with budget guidance</li>
</ol>
<p>You don't need to do anything in the meantime. Our operations team will reach out and set up a dedicated channel with you.</p>
<p>Just reply to this email if you have any questions.</p>
<p>—— Qide Group<br>{{from_email}}</p>""",
        },
    },

    # ─── diagnostic_ready ──────────────────────────────────────────
    "diagnostic_ready": {
        "zh-CN": {
            "subject": "{{factory_name}} · 您的出海诊断报告（评分 {{readiness_score}}/100）",
            "body_text": """{{contact_name}}，您好

{{factory_name}} 的出海诊断报告已生成。

【核心结论】
- 出海准备度评分：{{readiness_score}} / 100
- 推荐路径：{{recommended_path}}
- 推荐方案：{{recommended_plan}}

【报告摘要】
{{executive_summary}}

【完整报告下载】
{{pdf_signed_url}}
（链接有效期 24 小时 · 过期可回复邮件重新发送）

【下一步】
我们的运营会在 1-2 个工作日内联系您 · 一起对齐 30 天行动计划。

如果您希望立刻开始 · 可回复这封邮件预约电话。

祁德商链科技
{{from_email}}
""",
            "body_html": """<p>{{contact_name}}，您好</p>
<p><strong>{{factory_name}}</strong> 的出海诊断报告已生成。</p>
<h3>核心结论</h3>
<ul>
<li>出海准备度评分：<strong>{{readiness_score}} / 100</strong></li>
<li>推荐路径：{{recommended_path}}</li>
<li>推荐方案：{{recommended_plan}}</li>
</ul>
<h3>报告摘要</h3>
<p>{{executive_summary}}</p>
<h3>完整报告下载</h3>
<p><a href="{{pdf_signed_url}}">点击下载 PDF 报告</a><br>
<em>（链接有效期 24 小时 · 过期可回复邮件重新发送）</em></p>
<h3>下一步</h3>
<p>我们的运营会在 1-2 个工作日内联系您 · 一起对齐 30 天行动计划。</p>
<p>如果您希望立刻开始 · 可回复这封邮件预约电话。</p>
<p>—— 祁德商链科技<br>{{from_email}}</p>""",
        },
        "en-US": {
            "subject": "{{factory_name}} · Your export-readiness diagnostic (score {{readiness_score}}/100)",
            "body_text": """Hello {{contact_name}},

Your export-readiness diagnostic for {{factory_name}} is ready.

KEY FINDINGS
- Readiness score: {{readiness_score}} / 100
- Recommended path: {{recommended_path}}
- Recommended plan: {{recommended_plan}}

EXECUTIVE SUMMARY
{{executive_summary}}

DOWNLOAD FULL REPORT
{{pdf_signed_url}}
(Link valid for 24h; reply to this email for a fresh link.)

NEXT STEPS
Our team will reach out within 1-2 business days to align on a 30-day action plan.

If you want to start immediately, just reply to schedule a call.

Best,
Qide Group
{{from_email}}
""",
            "body_html": """<p>Hello {{contact_name}},</p>
<p>Your export-readiness diagnostic for <strong>{{factory_name}}</strong> is ready.</p>
<h3>Key Findings</h3>
<ul>
<li>Readiness score: <strong>{{readiness_score}} / 100</strong></li>
<li>Recommended path: {{recommended_path}}</li>
<li>Recommended plan: {{recommended_plan}}</li>
</ul>
<h3>Executive Summary</h3>
<p>{{executive_summary}}</p>
<h3>Download Full Report</h3>
<p><a href="{{pdf_signed_url}}">Download PDF report</a><br>
<em>(Link valid for 24h; reply to this email for a fresh link.)</em></p>
<h3>Next Steps</h3>
<p>Our team will reach out within 1-2 business days to align on a 30-day action plan.</p>
<p>If you want to start immediately, just reply to schedule a call.</p>
<p>—— Qide Group<br>{{from_email}}</p>""",
        },
    },

    # ─── social_ready ─────────────────────────────────────────────
    "social_ready": {
        "zh-CN": {
            "subject": "{{factory_name}} · 您的社媒矩阵已搭建（{{account_count}} 个账号上线）",
            "body_text": """{{contact_name}}，您好

{{factory_name}} 的海外社媒矩阵已经搭建完成 · 共 {{account_count}} 个账号上线：

{{accounts_list}}

接下来 · 内容团队会按照您的产品 + 目标市场批量产出素材 · 每周 {{content_frequency}} 条 · 自动跨平台发布。

您只需要：
1. 收到第 1 条内容 24 小时内 · 给我们一个 OK / 改 / 暂停 反馈
2. 之后由我们运营自动跑 · 您只需每月看一次月报

如果对哪个账号 / 平台有疑问 · 直接回邮件。

祁德商链科技
{{from_email}}
""",
            "body_html": """<p>{{contact_name}}，您好</p>
<p><strong>{{factory_name}}</strong> 的海外社媒矩阵已经搭建完成 · 共 <strong>{{account_count}}</strong> 个账号上线：</p>
<pre>{{accounts_list}}</pre>
<p>接下来 · 内容团队会按照您的产品 + 目标市场批量产出素材 · 每周 {{content_frequency}} 条 · 自动跨平台发布。</p>
<p>您只需要：</p>
<ol>
<li>收到第 1 条内容 24 小时内 · 给我们一个 OK / 改 / 暂停 反馈</li>
<li>之后由我们运营自动跑 · 您只需每月看一次月报</li>
</ol>
<p>—— 祁德商链科技<br>{{from_email}}</p>""",
        },
        "en-US": {
            "subject": "{{factory_name}} · Your social matrix is live ({{account_count}} accounts)",
            "body_text": """Hello {{contact_name}},

Your overseas social matrix is live with {{account_count}} accounts:

{{accounts_list}}

Next: our content team will produce {{content_frequency}} posts per week, tuned to your products and target markets, with auto-distribution across platforms.

What we need from you:
1. Give us OK/edit/pause feedback on the first content piece (within 24h).
2. After that, our ops team runs it. You'll get a monthly report.

Reply to this email with any questions.

Best,
Qide Group
{{from_email}}
""",
            "body_html": "",
        },
    },

    # ─── first_lead ────────────────────────────────────────────────
    "first_lead": {
        "zh-CN": {
            "subject": "🎯 {{factory_name}} · 第 1 个海外询盘进来了",
            "body_text": """{{contact_name}}，您好

{{factory_name}} 收到第 1 个海外询盘：

【询盘信息】
- 买家：{{buyer_name}}（{{buyer_country}}）
- 来源：{{lead_source}}
- 询盘 6 要素评分：{{lead_grade}} 类
- 询盘内容摘要：{{lead_summary}}

【AI 客服已自动回复 1 封专业邮件】
回复内容已发买家 · 您可以登录 dashboard 查看。

【运营接下来要做的】
- A 类（高质量）→ 微信加买家 + Sam 24h 内介入
- B/C 类 → AI 自动跟进 3 轮 / 转 A 类后通知您
- D 类（低质量）→ 归档

如果您想立即看完整询盘 · 回邮件即可。

祁德商链科技
{{from_email}}
""",
            "body_html": "",
        },
        "en-US": {
            "subject": "🎯 {{factory_name}} · First overseas inquiry received",
            "body_text": """Hello {{contact_name}},

{{factory_name}} just received its first overseas inquiry.

INQUIRY DETAILS
- Buyer: {{buyer_name}} ({{buyer_country}})
- Source: {{lead_source}}
- 6-factor grade: {{lead_grade}}
- Summary: {{lead_summary}}

AI auto-reply sent. View full thread in your dashboard.

NEXT STEPS
- Grade A → Sam will engage on WeChat within 24h
- Grade B/C → AI follows up over 3 rounds, escalates if quality rises
- Grade D → Archived

Reply to this email if you want to see the full inquiry now.

Best,
Qide Group
{{from_email}}
""",
            "body_html": "",
        },
    },

    # ─── monthly_report ────────────────────────────────────────────
    "monthly_report": {
        "zh-CN": {
            "subject": "{{factory_name}} · {{report_month}} 出海月报",
            "body_text": """{{contact_name}}，您好

{{factory_name}} {{report_month}} 出海月报已生成。

【本月 4 个核心 KPI】
- 流量：{{traffic_count}}（环比 {{traffic_delta_pct}}%）
- 询盘：{{lead_count}} 个（A {{grade_a_count}} / B {{grade_b_count}} / C {{grade_c_count}}）
- 订单：{{order_count}} 个
- 收入：US$ {{revenue_usd}}

【完整月报 PDF】
{{pdf_signed_url}}
（含各平台分布、地理分布、链路健康度、下月建议）

【AI 运营建议】
{{ai_recommendations}}

如有问题 · 回邮件即可。

祁德商链科技
{{from_email}}
""",
            "body_html": "",
        },
        "en-US": {
            "subject": "{{factory_name}} · {{report_month}} export performance report",
            "body_text": """Hello {{contact_name}},

{{factory_name}}'s {{report_month}} report is ready.

THIS MONTH (4 CORE KPIs)
- Traffic: {{traffic_count}} ({{traffic_delta_pct}}% MoM)
- Leads: {{lead_count}} (A {{grade_a_count}} / B {{grade_b_count}} / C {{grade_c_count}})
- Orders: {{order_count}}
- Revenue: US$ {{revenue_usd}}

FULL REPORT PDF
{{pdf_signed_url}}
(Includes platform breakdown, geo breakdown, link health, next-month recommendations.)

AI RECOMMENDATIONS
{{ai_recommendations}}

Reply with any questions.

Best,
Qide Group
{{from_email}}
""",
            "body_html": "",
        },
    },
}


def render_email(
    *,
    template_key: str,
    locale: str,
    template_vars: dict[str, Any],
    from_email: str = "no-reply@qidelinktech.cn",
) -> dict[str, str]:
    """渲染邮件 · 返回 {subject, body_text, body_html}"""
    template_set = TEMPLATES.get(template_key)
    if not template_set:
        raise ValueError(f"unknown template_key: {template_key}")

    locale_template = template_set.get(locale) or template_set.get("zh-CN") or next(iter(template_set.values()))

    all_vars = {**template_vars, "from_email": from_email}

    return {
        "subject": _render(locale_template["subject"], all_vars),
        "body_text": _render(locale_template["body_text"], all_vars),
        "body_html": _render(locale_template.get("body_html") or "", all_vars),
    }
