"""Smart Intake v4 · LLM 提示词模板

设计原则：
1. **结构化输出**：每个 prompt 要求 LLM 返 JSON·便于 parse + 失败兜底
2. **多语言**：中英混排（工厂资料多中文文件名 + 英文产品 spec）
3. **明确返回 schema**：每个 prompt 末尾给 example output
4. **Token 节省**：分类用 qwen-flash（便宜），entity 抽取用 qwen-plus（准）
5. **Failure tolerant**：所有调用方需要 wrap parse_json_safe（LLM 偶尔返非 JSON）

调用入口集中在 intake_service · 不在这里执行 LLM·只返 string。

测试：tests/intake/test_intake_prompts.py
"""
from __future__ import annotations

from typing import Optional


# ════════════════════════════════════════════════════════════
# 1. 文件名分类（filename → category + sku_slug + tags）
# ════════════════════════════════════════════════════════════

CLASSIFY_CATEGORIES = [
    "master",        # 产品主图（白底 / 单品干净拍摄）
    "lifestyle",     # 生活场景图（产品在用 / 模特持）
    "detail",        # 细节特写（材质 / 缝线 / 5 主色 / 包装结构）
    "packaging",     # 包装盒 / 礼盒 / 物流箱
    "spec",          # 规格图（含尺寸标注 / 多视角图）
    "video",         # 产品视频
    "factory",       # 工厂 / 车间 / 生产线
    "license",       # 营业执照 / 证书 / ISO 等资质
    "catalog",       # 产品手册 PDF
    "brand-logo",    # 品牌 LOGO / VI
    "other",         # 不在以上类别
]


def classify_filename_batch_prompt(
    factory_slug: str,
    filenames: list[dict],
    *,
    known_sku_slugs: Optional[list[str]] = None,
) -> str:
    """批量分类一组文件名·返 JSON array

    入参 filenames: [{"id": "...", "name": "...", "kind": "image|video|..."}]
    出参 example:
      [{"id":"f1","category":"master","sku_slug":"yushikou-handcream",
        "tags":["white-bg","english-pkg"],"confidence":0.92}]
    """
    known_skus_hint = ""
    if known_sku_slugs:
        known_skus_hint = (
            f"\n已知 SKU slug（优先匹配）：{', '.join(known_sku_slugs[:50])}\n"
        )

    file_lines = "\n".join(
        f"  - id={f['id']}, name={f['name']!r}, kind={f.get('kind', 'unknown')}"
        for f in filenames
    )

    return f"""你是工厂数字资产分类助手。工厂代号：{factory_slug}

任务：批量给以下文件分类 + 推测 SKU + 抽 tags。文件来自工厂提供的混乱资料目录。

输出 **必须** 是合法 JSON array · 每个元素 1 个文件 · 字段：
- id (string) · 输入照搬
- category (string) · 必须是这些值之一：{', '.join(CLASSIFY_CATEGORIES)}
- sku_slug (string|null) · 产品 slug · 用 kebab-case · 如 "yushikou-handcream" / "gostoo-modern-sofa-l01" · 若无法判断填 null
- tags (string[]) · 0-5 个英文标签 · 如 ["white-bg","outdoor","close-up","with-model"]
- confidence (number 0-1) · 你对此次分类的把握

分类规则（按优先级）：
1. license 类：含 "营业执照 / 资质 / 证书 / ISO / FDA / CE / license / cert" 等关键词
2. brand-logo 类：含 "logo / VI / 品牌识别 / LOGO" 等
3. catalog 类：PDF 文档·含 "catalog / brochure / 画册 / 手册"
4. spec 类：含 "spec / 尺寸 / 规格 / dimension / size / drawing"
5. packaging 类：含 "package / 包装 / 礼盒 / box / 外箱"
6. video 类：kind=video
7. factory 类：含 "工厂 / 车间 / production / factory / 生产线"
8. detail 类：含 "detail / 细节 / 特写 / closeup / close-up / material / 材质"
9. lifestyle 类：含 "lifestyle / 场景 / scene / model / 模特 / use case"
10. master 类（默认 image fallback）：单品干净拍摄·白底首选
11. other 类：以上都不匹配
{known_skus_hint}
**重要**：
- 不要返 markdown · 不要返解释 · **只返 JSON array**
- confidence < 0.5 时一定要给 "low_confidence_reason" 字段说明为什么不确定
- 文件名里有人名 / 客户名 / 私密信息时 · 在 tags 加 "sensitive"

文件列表（{len(filenames)} 个）：
{file_lines}

JSON output:"""


# ════════════════════════════════════════════════════════════
# 2. SKU 聚类（filename + 分类 → 真实 SKU 归并）
# ════════════════════════════════════════════════════════════

def cluster_skus_prompt(
    factory_slug: str,
    items_summary: list[dict],
) -> str:
    """LLM SKU 聚类·把同一 SKU 不同视角 / 命名变体归并

    入参 items_summary: [{"id":"...","name":"...","predicted_sku":"...","category":"..."}]
    出参 example:
      [{"sku_slug":"yushikou-handcream","sku_name_cn":"宇士口护手霜",
        "sku_name_en":"Yushikou Hand Cream","subcategory":null,
        "item_ids":["f1","f2","f3"],"confidence":0.88}]
    """
    items_lines = "\n".join(
        f"  - id={i['id']}, name={i['name']!r}, "
        f"predicted_sku={i.get('predicted_sku') or 'null'}, "
        f"category={i.get('category', 'unknown')}"
        for i in items_summary[:300]  # 防 token 爆
    )

    return f"""你是 SKU 聚类专家。工厂：{factory_slug}

任务：把以下文件按 **真实产品 SKU** 归类。同一 SKU 可能有多个变体命名（中文 / 拼音 / 英文 / 缩写 / 数字编号）·请合并。

输出 **必须** 是合法 JSON array · 每个元素 = 1 个 SKU · 字段：
- sku_slug (string) · kebab-case · 选最稳定的命名做主 slug
- sku_name_cn (string|null) · 中文名（如果文件名透露）
- sku_name_en (string|null) · 英文名
- subcategory (string|null) · 子品类（如沙发/床/床垫 for 家具厂）
- item_ids (string[]) · 归入此 SKU 的所有 item id
- confidence (number 0-1)

聚类启发：
1. **数字编号优先**：如 "yushikou-001" + "yushikou-handcream-01.jpg" + "宇士口护手霜.png" 全归一个 SKU
2. **去版本号**：如 "v1 / v2 / final / 终稿 / 修改版"·这些不影响 SKU 划分
3. **去尺寸 / 颜色**：如 "red / black / large / 大号"·若是同款不同色尺·归一个 SKU
4. **不要过度聚合**：如 "modern-sofa" + "modern-bed" 不能合 · 是不同产品
5. **不确定时分开**：每个 SKU 至少 confidence > 0.6 才合并

**重要**：
- 不要返 markdown · 不要返解释 · **只返 JSON array**
- 一个 item 只能归入 1 个 SKU · 如归不到任何 SKU 则不出现在输出（会被归到 "其他/未分类"）
- subcategory 仅在工厂明显分子品类时填（如家具厂 = sofa/bed/mattress；手霜厂只有一类 = null）

文件列表（{len(items_summary)} 个）：
{items_lines}

JSON output:"""


# ════════════════════════════════════════════════════════════
# 3. docx / pdf entity 抽取（资料 → 公司 entity 信息）
# ════════════════════════════════════════════════════════════

def extract_entity_prompt(
    factory_slug: str,
    document_text: str,
) -> str:
    """从公司介绍 docx / catalog PDF 抽取 entity 字段

    入参 document_text: 截至 8000 字（外层 caller 负责截断）
    出参 example:
      {"legal_name":"深圳市艺欣恒...","year_established":1998,
       "factory_location":"深圳市龙岗区","employee_count":"50-100",
       "main_products":["化妆品展示架","电子烟展示架"],
       "certifications":["ISO9001","FDA"],...}
    """
    return f"""你是工厂资料结构化抽取专家。工厂代号：{factory_slug}

任务：从下方文档抽取标准化 entity 字段。**只抽取明确写出来的事实** · 不要推测 · 不要编。

输出 **必须** 是合法 JSON object · 字段（缺失就 null·不能编）：
- legal_name (string|null) · 完整公司全称
- short_name (string|null) · 简称 / 品牌名
- credit_code (string|null) · 统一社会信用代码（18 位）
- year_established (integer|null) · 成立年份
- factory_location (string|null) · 工厂详细地址
- showroom_location (string|null) · 展厅 / 办公地址（如与工厂不同）
- factory_area_sqm (integer|null) · 工厂面积平方米
- employee_count (string|null) · 员工规模·如 "50-100" / "200+" / "10-50"
- main_products (string[]) · 主营产品 · 最多 5 项 · 中文
- main_industries (string[]) · 应用行业 · 如 ["化妆品","电子烟","3C 数码"]
- target_markets (string[]) · 出口市场 · 如 ["United States","Europe","Japan"]
- certifications (string[]) · 资质证书 · 如 ["ISO9001","ISO14001","FDA","CE","BSCI"]
- annual_capacity (string|null) · 年产能描述
- moq (string|null) · 最小起订量
- lead_time (string|null) · 交货周期
- payment_terms (string[]) · 如 ["T/T 30%+70%","L/C at sight"]
- main_contact_name (string|null) · 主联系人姓名
- main_contact_title (string|null) · 职务·如 "总经理 / 销售总监"
- main_contact_email (string|null)
- main_contact_phone (string|null) · 含国际区号
- website (string|null)
- decision_makers (string[]) · 决策人 · 如 ["唐总","李总"]

**重要**：
- 不要返 markdown · 不要返解释 · **只返 JSON object**
- 任何字段如果文档没明说就填 null（**绝不编**·B2B 写错 = 客户合同风险）
- main_products 用文档中文原词·不翻译
- year_established 若文档说 "26 年生产经验" 而不给年份·按今年 - 26 计算填年份 · 同时在 "extraction_notes" 字段记录推算依据
- 数字字段 (year / area / count) 必须解析为数字不要字符串

文档内容：
---
{document_text[:8000]}
---

JSON output:"""


# ════════════════════════════════════════════════════════════
# 4. 视觉审核（Qwen-VL 单图）· 复用 v3 ai_service · 仅 prompt
# ════════════════════════════════════════════════════════════

VISUAL_AUDIT_PROMPT = """你是产品图视觉审核员。请检查这张图片：

1. 是否清晰可用（不模糊·不黑屏·不重复）
2. 主体类型：master / lifestyle / detail / packaging / spec / factory / other
3. 5 个主色 RGB hex（如 #FFFFFF / #F5E6D3 / ...）
4. 是否含人脸 / 是否含中文文字 / 是否含品牌 LOGO
5. 文件是否疑似 license / 证书（高优先级标识）

输出 JSON object · 字段：
- usable (boolean)
- subject_type (string) · 上述 7 类
- dominant_colors (string[]) · 5 个 hex
- has_face (boolean)
- has_chinese_text (boolean)
- has_brand_logo (boolean)
- is_license_doc (boolean)
- quality_notes (string|null) · 若 usable=false·说明原因

**只返 JSON · 不要解释**"""


# ════════════════════════════════════════════════════════════
# 5. Cost helpers · 估算 token 消耗（给 admin SPA 显示）
# ════════════════════════════════════════════════════════════

# 通义千问 qwen-plus 价格（2026-05 · 阿里云千问公开价）
# Input: ¥0.0008 / 1K tokens · Output: ¥0.002 / 1K tokens
# qwen-vl-plus: Input ¥0.008 / 1K tokens
PRICE_PER_1K_INPUT = 0.0008   # qwen-plus
PRICE_PER_1K_OUTPUT = 0.002
PRICE_VL_PER_1K_INPUT = 0.008  # qwen-vl-plus


def estimate_classify_cost_cny(
    file_count: int,
    avg_filename_len: int = 50,
    *,
    batch_size: int = 30,
) -> float:
    """估算批量分类的 ¥ 成本

    一个 batch = 30 文件·prompt 大约 3000 input tokens + 1500 output tokens
    """
    n_batches = (file_count + batch_size - 1) // batch_size
    input_tokens = n_batches * 3000
    output_tokens = n_batches * 1500
    return round(
        input_tokens / 1000 * PRICE_PER_1K_INPUT
        + output_tokens / 1000 * PRICE_PER_1K_OUTPUT,
        4,
    )


def estimate_cluster_cost_cny(item_count: int) -> float:
    """SKU 聚类成本·一次性·item 上限 300"""
    n = min(item_count, 300)
    input_tokens = 500 + n * 30   # prompt 头 + 每 item ~30 token
    output_tokens = n * 20         # 每 cluster ~20 token
    return round(
        input_tokens / 1000 * PRICE_PER_1K_INPUT
        + output_tokens / 1000 * PRICE_PER_1K_OUTPUT,
        4,
    )


def estimate_entity_extract_cost_cny(doc_count: int = 1) -> float:
    """每个 doc ~8000 input tokens + 800 output tokens"""
    input_tokens = doc_count * 8000
    output_tokens = doc_count * 800
    return round(
        input_tokens / 1000 * PRICE_PER_1K_INPUT
        + output_tokens / 1000 * PRICE_PER_1K_OUTPUT,
        4,
    )


def estimate_visual_audit_cost_cny(image_count: int) -> float:
    """Qwen-VL 视觉审核·每图 ~1500 input tokens + 200 output tokens"""
    input_tokens = image_count * 1500
    output_tokens = image_count * 200
    return round(
        input_tokens / 1000 * PRICE_VL_PER_1K_INPUT
        + output_tokens / 1000 * PRICE_PER_1K_OUTPUT,
        4,
    )


def estimate_total_job_cost_cny(
    file_count: int,
    image_count: int,
    doc_count: int = 0,
    *,
    skip_visual: bool = True,  # 默认跳 VL（贵 10×） · 仅 confidence<0.6 才 VL 兜底
) -> dict:
    """估算整个 intake job 的 ¥ 成本上限

    返回明细 + total · admin SPA 显示给用户决策"
    """
    classify = estimate_classify_cost_cny(file_count)
    cluster = estimate_cluster_cost_cny(file_count)
    entity = estimate_entity_extract_cost_cny(doc_count) if doc_count else 0.0
    visual = estimate_visual_audit_cost_cny(image_count) if not skip_visual else 0.0
    total = round(classify + cluster + entity + visual, 4)
    return {
        "classify": classify,
        "cluster": cluster,
        "entity_extract": entity,
        "visual_audit": visual,
        "total_cny": total,
        "skip_visual": skip_visual,
    }


__all__ = [
    "CLASSIFY_CATEGORIES",
    "classify_filename_batch_prompt",
    "cluster_skus_prompt",
    "extract_entity_prompt",
    "VISUAL_AUDIT_PROMPT",
    "estimate_classify_cost_cny",
    "estimate_cluster_cost_cny",
    "estimate_entity_extract_cost_cny",
    "estimate_visual_audit_cost_cny",
    "estimate_total_job_cost_cny",
]
