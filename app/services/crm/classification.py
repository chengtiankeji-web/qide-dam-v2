"""6 要素询盘分级算法·CRM 灵魂

输入：原始询盘文本 + 可选附加上下文（来源 / 联系人 role 等）
输出：6 个布尔标志 + 总分 0-6 + 分类 A/B/C/D

设计原则：
1. **规则优先 · LLM 兜底**·90% 询盘走 regex 0 LLM 成本
2. **多语言支持**·英文 / 中文 / 西班牙 / 阿拉伯 / 印尼 / 越南
3. **可解释**·breakdown 字段返回每要素捕获的具体文本（BD 看到知道为啥被分这类）
4. **可 override**·BD 可手工调分类（写 classification_overridden=true）

6 要素：
  ① has_quantity         提了数量
  ② has_budget           提了预算
  ③ has_timeline         提了时限
  ④ has_specification    提了规格
  ⑤ has_decision_role    决策人身份（看 contact role + signature）
  ⑥ has_company_info     公司背调可查（domain email + 公司名）

分级：
  A 类·≥5 要素 + 决策人          24h 内必跟·🔴 红色提示
  B 类·3-4 要素                    3 天内跟·🟡 黄
  C 类·1-2 要素                    进 nurture 邮件链·🟢 绿
  D 类·0 要素                      自动归档 / spam·⚪️ 灰

测试 fixture：见 tests/crm/test_classification.py
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ════════════════════════════════════════════════════════════
# 1. Regex 模式（按要素分组·多语言）
# ════════════════════════════════════════════════════════════

# ── 要素 1: 数量 ──────────────────────────────────────
QUANTITY_PATTERNS = [
    # 英文
    re.compile(r"\b(\d{2,7}(?:[.,]\d+)?)\s*(pcs?|pieces?|units?|sets?|cartons?|boxes?|pallets?|containers?|moq|tons?|kg|lbs?)\b", re.I),
    re.compile(r"\b(?:qty|quantity|order|need|require)\s*[:=of]*\s*(\d{2,7})", re.I),
    re.compile(r"\b(\d{1,3})\s*x\s*(\d+)\s*(?:ft|m|cm|inches?|inch)\b", re.I),  # "10x40 ft container"
    # 中文
    re.compile(r"(\d{2,7})\s*(个|件|套|箱|台|片|条|kg|公斤|吨)"),
    re.compile(r"(?:数量|订购|需要|采购)\s*[:：]?\s*(\d{2,7})"),
    re.compile(r"(一|二|三|四|五|六|七|八|九|十)千(?:多)?(个|件|套|箱|台)"),
    # 缩写
    re.compile(r"\b(\d{1,3})k\s*(?:pcs?|units?)\b", re.I),  # "5k pcs"
]

# ── 要素 2: 预算 ──────────────────────────────────────
BUDGET_PATTERNS = [
    # 英文（含币种）
    re.compile(r"(?:budget|spend|invest|price\s+range|cost)\s*[:=of]*\s*[\$￥€£¥]\s?(\d{1,3}(?:[,.]?\d{3})*(?:[,.]\d+)?)", re.I),
    re.compile(r"[\$￥€£¥]\s?(\d{1,3}(?:[,.]?\d{3})*(?:[,.]\d+)?)\s*(?:k|m|million|thousand)?\b"),
    re.compile(r"\b(\d{1,3}(?:[,.]?\d{3})*(?:[,.]\d+)?)\s*(USD|RMB|CNY|EUR|GBP|JPY|AUD|HKD)\b", re.I),
    re.compile(r"\b(?:price|cost|unit\s+price)\s+(?:at|of|around|approximately)\s*[\$￥€£¥]?\s?(\d+)", re.I),
    # 中文
    re.compile(r"(?:预算|出价|价位|价格区间)\s*[:：]?\s*(\d+(?:[,.]\d+)?)\s*(元|万|千)?"),
    re.compile(r"\b(\d+)\s*美金\b"),
    # 范围
    re.compile(r"\$\s?(\d{1,5})\s*[-~]\s*\$?\s?(\d{1,5})", re.I),
]

# ── 要素 3: 时限 ──────────────────────────────────────
TIMELINE_PATTERNS = [
    # 紧迫信号
    re.compile(r"\b(asap|urgent|rush|immediately|priority|expedite|soon as possible)\b", re.I),
    re.compile(r"(尽快|紧急|急需|赶时间|急单)"),
    # 具体时间窗·英文月份（加宽·允许 "by mid-July" / "before end of August"）
    re.compile(r"\b(?:by|before|end\s+of|until|deadline)\b.{0,30}\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\b", re.I),
    re.compile(r"\b(?:by|before)\s+Q[1-4]\b", re.I),
    re.compile(r"\b(?:by|before)\s+\d{1,2}[/-]\d{1,2}\b", re.I),
    re.compile(r"\b(?:within|in)\s+(\d{1,2})\s+(day|week|month)s?\b", re.I),
    re.compile(r"\b(this|next|coming)\s+(week|month|quarter|year)\b", re.I),
    # 中文（加宽·"8 月底前 / 月底 / 月初 / 月中"）
    re.compile(r"(\d{1,2})\s*(天|周|个月|个星期)\s*内"),
    re.compile(r"(本|下|本月|下月|这|这个)\s*(周|月|季度|年)"),
    re.compile(r"(\d{1,2})\s*月\s*(底|初|中)?\s*(前|内|之前)"),
    re.compile(r"(\d{1,2})\s*月\s*\d{1,2}\s*[日号]\s*(前|内|之前)"),
    # 交货期
    re.compile(r"\b(lead\s+time|delivery\s+time|shipment|production\s+time)\s*[:=]?\s*(\d+)", re.I),
    re.compile(r"(交期|交货|生产周期|出货时间)\s*[:：]?\s*(\d+)"),
]

# ── 要素 4: 规格 ──────────────────────────────────────
SPEC_PATTERNS = [
    # 物理规格
    re.compile(r"\b(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*[xX×]?\s*(\d+(?:\.\d+)?)?\s*(mm|cm|m|inch|inches|ft)\b", re.I),
    re.compile(r"\b(width|height|depth|length|diameter|thickness|weight|capacity)\s*[:=]?\s*\d+", re.I),
    # 材料 / 颜色
    re.compile(r"\b(material|color|colour|finish|coating|grade|spec(?:ification)?|model|type)\s*[:=]?\s*[A-Za-z0-9]+", re.I),
    re.compile(r"\b(stainless|steel|aluminum|plastic|cotton|silk|wool|leather|ABS|PE|PP|PVC|wood|MDF|particleboard)\b", re.I),
    # 报价请求
    re.compile(r"\b(quote|quotation|RFQ|RFP|proforma\s+invoice|PI)\b", re.I),
    re.compile(r"\b(send|provide|share)\s+(?:me|us)?\s*(?:a\s+)?(quote|catalog|spec\s+sheet|datasheet)", re.I),
    # 中文
    re.compile(r"(规格|材质|颜色|尺寸|材料|工艺|涂层)\s*[:：]"),
    re.compile(r"(报价|询价|发\s*PI|形式发票|样品)"),
    # 文件附件信号（间接证据·有附件 = 应该有规格）
    re.compile(r"\b(attached|attachment|see\s+(?:the\s+)?file)\b", re.I),
]

# ── 要素 5: 决策人身份 ─────────────────────────────────
DECISION_ROLE_TITLES = {
    # 英文 · C-level
    "ceo", "cto", "cfo", "coo", "cmo", "cpo",
    "founder", "co-founder", "owner", "president", "proprietor",
    # VP / Director
    "vp", "vice president", "director", "head of", "general manager", "gm",
    # 采购 / 供应链
    "purchasing", "procurement", "sourcing", "supply chain",
    "buyer", "category manager", "merchandising",
    # 中文
    "总经理", "总裁", "董事", "采购", "供应链", "进口", "总监",
    "经理", "主管", "负责人",
}

DECISION_ROLE_PATTERNS = [
    re.compile(r"\b(?:i\s+am|i'm|my\s+role|my\s+position|my\s+title)\s+(?:the\s+)?(\w[\w\s]{2,40})", re.I),
    re.compile(r"\b(CEO|CTO|CFO|COO|CMO|VP|Director|Head|Owner|Founder|GM|Manager|Purchasing|Sourcing|Buyer)\b"),
]

# ── 要素 6: 公司信息 ───────────────────────────────────
# 公司域名 email pattern: 假设个人邮箱（gmail / yahoo / qq / hotmail / outlook / 163 / 126）= 个人 · 否则 = 公司
PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.co.jp",
    "hotmail.com", "outlook.com", "live.com", "msn.com", "icloud.com", "me.com",
    "qq.com", "163.com", "126.com", "sina.com", "sina.cn", "foxmail.com",
    "aliyun.com", "139.com", "189.cn",
    "yandex.com", "yandex.ru", "mail.ru", "rambler.ru",
    "naver.com", "daum.net", "hanmail.net",
}

# 公司名 patterns·严格匹配（要求"with/at/from <Capitalized name>" 或公司后缀）
# 不能太松·"I am interested in your products" 不应被误判
COMPANY_NAME_PATTERNS = [
    # "I am from / with / at <Company>" 必须有介词
    re.compile(r"\b(?:I'm|I am|We're|We are)\s+(?:from|with|at|representing)\s+([A-Z][\w&\.'-]+(?:\s+[A-Z][\w&\.'-]+){0,4})"),
    # "on behalf of <Company>"
    re.compile(r"\b(?:on behalf of|representing|working for)\s+([A-Z][\w&\.'-]+(?:\s+[A-Z][\w&\.'-]+){0,4})"),
    # 带公司后缀（强证据）
    re.compile(r"\b(\w[\w\s&'-]+?)\s+(?:Co\.?,?\s*Ltd\.?|Inc\.?|LLC|GmbH|S\.A\.|Pvt\.?\s+Ltd\.?|Pty\.?\s+Ltd\.?|Corporation|Corp\.?)\b"),
    # 中文
    re.compile(r"(?:我们|本公司|我司|我们是|来自)\s*[「『\"]?([一-龥\w]{2,30}(?:公司|集团|有限|实业|贸易|工厂))[」』\"]?"),
]


# ════════════════════════════════════════════════════════════
# 2. 数据结构
# ════════════════════════════════════════════════════════════

@dataclass
class ClassificationInput:
    """6 要素分级算法输入"""
    inquiry_text: str
    contact_email: str | None = None
    contact_company: str | None = None
    contact_role: str | None = None
    contact_phone: str | None = None
    source: str | None = None
    has_attachments: bool = False


@dataclass
class FactorBreakdown:
    """单要素详情·告诉 BD 为啥这要素 hit / 没 hit"""
    detected: bool = False
    confidence: float = 0.0  # 0-1
    evidence: list[str] = field(default_factory=list)  # 捕获的具体文本片段


@dataclass
class ClassificationResult:
    """完整分级结果"""
    has_quantity: bool
    has_budget: bool
    has_timeline: bool
    has_specification: bool
    has_decision_role: bool
    has_company_info: bool

    score: int  # 0-6
    classification: str  # 'A' / 'B' / 'C' / 'D'

    breakdown: dict  # {quantity: FactorBreakdown, budget: ..., ...}

    def to_db_dict(self) -> dict:
        """返回写到 leads 表的 dict"""
        return {
            "has_quantity": self.has_quantity,
            "has_budget": self.has_budget,
            "has_timeline": self.has_timeline,
            "has_specification": self.has_specification,
            "has_decision_role": self.has_decision_role,
            "has_company_info": self.has_company_info,
            "six_factor_score": self.score,
            "six_factor_breakdown": {
                k: {"detected": v.detected, "evidence": v.evidence[:3]}
                for k, v in self.breakdown.items()
            },
            "classification": self.classification,
        }


# ════════════════════════════════════════════════════════════
# 3. 单要素检测函数
# ════════════════════════════════════════════════════════════

def detect_quantity(text: str) -> FactorBreakdown:
    """要素 ① 数量"""
    result = FactorBreakdown()
    for pattern in QUANTITY_PATTERNS:
        for match in pattern.finditer(text):
            result.detected = True
            result.evidence.append(match.group(0))
    result.confidence = 1.0 if result.detected else 0.0
    return result


def detect_budget(text: str) -> FactorBreakdown:
    """要素 ② 预算"""
    result = FactorBreakdown()
    for pattern in BUDGET_PATTERNS:
        for match in pattern.finditer(text):
            # 过滤明显误判（如电话号码）
            captured = match.group(0)
            if re.search(r"\b\d{10,}\b", captured):  # 10+ 位数字 = 可能电话
                continue
            result.detected = True
            result.evidence.append(captured)
    result.confidence = 1.0 if result.detected else 0.0
    return result


def detect_timeline(text: str) -> FactorBreakdown:
    """要素 ③ 时限"""
    result = FactorBreakdown()
    for pattern in TIMELINE_PATTERNS:
        for match in pattern.finditer(text):
            result.detected = True
            result.evidence.append(match.group(0))
    result.confidence = 1.0 if result.detected else 0.0
    return result


def detect_specification(text: str, has_attachments: bool = False) -> FactorBreakdown:
    """要素 ④ 规格"""
    result = FactorBreakdown()
    for pattern in SPEC_PATTERNS:
        for match in pattern.finditer(text):
            result.detected = True
            result.evidence.append(match.group(0))
    # 附件存在给予弱证据
    if has_attachments and not result.detected:
        result.detected = True
        result.evidence.append("[attachment present]")
        result.confidence = 0.5
    elif result.detected:
        result.confidence = 1.0
    return result


def detect_decision_role(
    text: str,
    contact_role: str | None = None,
    contact_email: str | None = None,
) -> FactorBreakdown:
    """要素 ⑤ 决策人身份"""
    result = FactorBreakdown()

    # 1) 直接 contact_role 字段
    if contact_role:
        role_lower = contact_role.lower().strip()
        for title in DECISION_ROLE_TITLES:
            if title in role_lower:
                result.detected = True
                result.evidence.append(f"role:{contact_role}")
                break

    # 2) 邮件签名 / 文本中检测
    for pattern in DECISION_ROLE_PATTERNS:
        for match in pattern.finditer(text):
            captured = match.group(0)
            captured_lower = captured.lower()
            for title in DECISION_ROLE_TITLES:
                if title in captured_lower:
                    result.detected = True
                    result.evidence.append(captured)
                    break

    # 3) 邮箱前缀启发式（不强证据）
    if contact_email and "@" in contact_email:
        local = contact_email.split("@")[0].lower()
        if any(x in local for x in ("ceo", "cto", "purchasing", "buyer", "sourcing", "founder")):
            if not result.detected:
                result.detected = True
                result.evidence.append(f"email:{contact_email}")
                result.confidence = 0.7  # 弱

    if result.detected and result.confidence == 0:
        result.confidence = 1.0
    return result


def detect_company_info(
    text: str,
    contact_email: str | None = None,
    contact_company: str | None = None,
) -> FactorBreakdown:
    """要素 ⑥ 公司信息（背调可查）"""
    result = FactorBreakdown()

    # 1) 公司名 field 已填
    if contact_company and len(contact_company.strip()) >= 3:
        result.detected = True
        result.evidence.append(f"company_field:{contact_company}")

    # 2) 域名邮箱（非个人邮箱）
    if contact_email and "@" in contact_email:
        domain = contact_email.split("@")[1].lower().strip()
        if domain not in PERSONAL_EMAIL_DOMAINS:
            result.detected = True
            result.evidence.append(f"company_domain:{domain}")

    # 3) 文本里提到公司名
    for pattern in COMPANY_NAME_PATTERNS:
        match = pattern.search(text)
        if match:
            captured = match.group(0)
            if not result.detected:
                result.detected = True
            result.evidence.append(captured)
            break

    result.confidence = 1.0 if result.detected else 0.0
    return result


# ════════════════════════════════════════════════════════════
# 4. 主分类函数
# ════════════════════════════════════════════════════════════

def classify_lead(input_data: ClassificationInput) -> ClassificationResult:
    """主入口·跑 6 要素 → 算分 → 分类

    复杂度：O(n) text scan · 大文本（1MB+）也 < 100ms · 不用 LLM 0 token 成本
    """
    text = input_data.inquiry_text or ""

    # 6 个要素
    breakdown = {
        "quantity": detect_quantity(text),
        "budget": detect_budget(text),
        "timeline": detect_timeline(text),
        "specification": detect_specification(text, input_data.has_attachments),
        "decision_role": detect_decision_role(
            text, input_data.contact_role, input_data.contact_email
        ),
        "company_info": detect_company_info(
            text, input_data.contact_email, input_data.contact_company
        ),
    }

    # 分数
    score = sum(1 for f in breakdown.values() if f.detected)

    # 分类规则
    has_decision_role = breakdown["decision_role"].detected
    if score >= 5 and has_decision_role:
        classification = "A"   # 24h 内必跟
    elif score >= 5:
        classification = "B"   # 没决策人但要素全 = 仍中优先级
    elif 3 <= score <= 4:
        classification = "B"
    elif 1 <= score <= 2:
        classification = "C"
    else:
        classification = "D"

    return ClassificationResult(
        has_quantity=breakdown["quantity"].detected,
        has_budget=breakdown["budget"].detected,
        has_timeline=breakdown["timeline"].detected,
        has_specification=breakdown["specification"].detected,
        has_decision_role=breakdown["decision_role"].detected,
        has_company_info=breakdown["company_info"].detected,
        score=score,
        classification=classification,
        breakdown=breakdown,
    )


# ════════════════════════════════════════════════════════════
# 5. LLM 增强（v7.1 起·暂留接口）
# ════════════════════════════════════════════════════════════

async def classify_lead_with_llm(
    input_data: ClassificationInput,
    fallback_to_rules: bool = True,
) -> ClassificationResult:
    """LLM 增强分级·适合复杂多语言询盘 + 间接表达

    步骤：
    1. 先跑 rule-based classification（永远跑·当 baseline）
    2. 如果 score 处于边界（2-3 · 即 B/C 之间）·调 DashScope qwen-plus 复核
    3. LLM 看到能"理解"的语义（反讽 / 间接 / 行业行话）·可能修正 rule 结果
    4. fallback_to_rules: 如 LLM 失败 · 用 rule 结果（生产必开）

    成本：~500 tokens / 询盘 × ¥4/1M = ¥0.002 / 询盘
        · 99% 询盘 0 LLM（明确 A/D 直接出）
        · 1% 边界（B/C 模糊）才调 LLM
        · 100 工厂日询盘 1000 条·LLM 部分 ¥0.02 · 月 ¥0.6
    """
    rule_result = classify_lead(input_data)

    # 边界值（B/C 之间）才调 LLM · A 类与 D 类不需要复核
    if not (2 <= rule_result.score <= 3):
        return rule_result

    try:
        from app.services import ai_service
        if not ai_service.has_provider():
            # 无 LLM key · 直接返 rule 结果
            return rule_result

        # 调 LLM
        llm_judgment = await _llm_judge_inquiry(
            input_data.inquiry_text,
            rule_result,
        )
        if llm_judgment is None:
            return rule_result

        # LLM 给出更高 confidence 的分类·覆盖 rule
        if (
            llm_judgment.get("confidence", 0) >= 0.75
            and llm_judgment.get("classification") in ("A", "B", "C", "D")
        ):
            new_class = llm_judgment["classification"]
            # 只允许向上修正（C → B / B → A）· 不允许向下（防 LLM 误降级）
            if _class_rank(new_class) <= _class_rank(rule_result.classification):
                rule_result.classification = new_class
        return rule_result

    except Exception:  # noqa: BLE001
        if fallback_to_rules:
            return rule_result
        raise


def _class_rank(c: str) -> int:
    return {"A": 1, "B": 2, "C": 3, "D": 4}.get(c, 5)


async def _llm_judge_inquiry(
    inquiry_text: str,
    rule_result: ClassificationResult,
) -> dict | None:
    """调 DashScope qwen-plus 复核·返回 {classification, confidence, reasoning}"""
    import json

    import httpx

    from app.core.config import settings
    from app.services import ai_service

    prompt = f"""你是 B2B 外贸询盘分级专家。请根据以下规则判断询盘类别：

A 类：高价值买家·已含具体数量+预算+时限+规格+决策人身份+公司背调可查（5/6 要素以上）
B 类：中等价值·3-4 要素清楚·缺一两关键信息
C 类：弱信号·只 1-2 要素·需 nurture 培育
D 类：spam / 无意义 / 0 要素

询盘原文：
\"\"\"
{inquiry_text[:2000]}
\"\"\"

规则算法初判：{rule_result.classification}（score={rule_result.score}/6）

请你**只看询盘文本本身**重新判断·特别注意：
- 反讽 / 间接表达（"prices are high" 可能暗示预算）
- 行业行话（"sample order" 暗示尝试性下单 · "PI" 暗示报价请求）
- 紧迫信号（"end of month" 类时限）

输出 JSON：
{{
  "classification": "A"/"B"/"C"/"D",
  "confidence": 0.0-1.0,
  "reasoning": "<50 字内·为啥这判定>"
}}
只输出 JSON·不要其它文字。
"""

    if not settings.DASHSCOPE_API_KEY:
        return None

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                ai_service.DASHSCOPE_TEXT_GEN_URL,
                json={
                    "model": "qwen-plus",
                    "input": {"messages": [{"role": "user", "content": prompt}]},
                    "parameters": {"result_format": "message",
                                  "max_tokens": 200, "temperature": 0.1},
                },
                headers={"Authorization": f"Bearer {settings.DASHSCOPE_API_KEY}"},
            )
            resp.raise_for_status()
            data = resp.json()
            # 解析返回
            content = (
                data.get("output", {}).get("choices", [{}])[0]
                    .get("message", {}).get("content", "")
            )
            # 提取 JSON
            content = content.strip()
            if content.startswith("```json"):
                content = content[7:]
            elif content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()
            return json.loads(content)
    except Exception:
        return None
