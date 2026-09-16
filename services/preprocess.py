from __future__ import annotations

import re


_SENTENCE_ENDINGS = set("。！？!?；;")
_CLOSERS = set("\"'”’）)]】}")
_LIST_LINE_RE = re.compile(r"^\s*(?:[-*•]|[\d一二三四五六七八九十]+[\.、\)])\s+")


def clean_text(text: str) -> str:
    """统一文本格式：规范化换行符、压缩多余空白、合并超过两个的连续空行。

    处理步骤：
    1. 统一换行符为 \\n，全角空格替换为半角
    2. 压缩行内多个空格/Tab 为单个空格
    3. 3 个以上连续空行合并为 2 个
    4. 去除每行首尾空白
    """
    # 统一换行符，兼容 Windows（\\r\\n）和旧 Mac（\\r）
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\u3000", " ")
    # 行内多余空白压缩为单个空格
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [line.strip() for line in text.split("\n")]
    cleaned = "\n".join(lines)
    # 行 strip 后可能再次产生连续空行，再压缩一次
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


_ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "e.g", "i.e"}


def _period_ends_sentence(text: str, index: int) -> bool:
    # Decimal points, versions, domains and emails are not sentence boundaries.
    next_index = index + 1
    while next_index < len(text) and text[next_index] in _CLOSERS:
        next_index += 1
    if next_index < len(text) and not text[next_index].isspace():
        return False
    previous = re.search(r"([A-Za-z.]+)$", text[:index])
    if previous:
        word = previous.group(1)
        if word.lower() in _ABBREVIATIONS:
            return False
        if len(word) == 1 and word.isupper() and next_index < len(text):
            return False
        if re.fullmatch(r"(?:[A-Za-z]\.)+[A-Za-z]", word):
            # An acronym may end a sentence; retain it before a lowercase continuation.
            following = text[next_index:].lstrip()
            return not following or following[0].isupper()
    return True


def _split_sentences_with_spans(text: str) -> list[dict]:
    """Sentence spans use Python character offsets in cleaned text, never byte offsets.

    Keep short sentences for statistics; chunk_text groups them for model inference.
    """
    spans = []
    start = 0
    index = 0

    def append_span(end: int) -> None:
        nonlocal start
        raw = text[start:end]
        left = start + len(raw) - len(raw.lstrip())
        right = end - (len(raw) - len(raw.rstrip()))
        if left < right:
            spans.append({"start": left, "end": right, "text": text[left:right]})
        start = end

    while index < len(text):
        ch = text[index]
        boundary = ch in _SENTENCE_ENDINGS or (ch == "." and _period_ends_sentence(text, index))
        if boundary:
            end = index + 1
            while end < len(text) and (text[end] in _CLOSERS or text[end] in _SENTENCE_ENDINGS):
                end += 1
            append_span(end)
            index = end
        elif ch == "\n" and index + 1 < len(text) and text[index + 1] == "\n":
            append_span(index + 2)
            index += 2
        else:
            index += 1
    append_span(len(text))
    return spans


def split_sentences(text: str) -> list[str]:
    """Return sentences without merging short ones, for unbiased length statistics."""
    return [item["text"] for item in _split_sentences_with_spans(clean_text(text))]


def detect_language(text: str) -> str:
    """粗粒度语言识别：zh / en / mixed。"""
    cleaned = clean_text(text)
    if not cleaned:
        return "mixed"

    zh_chars = len(re.findall(r"[\u4e00-\u9fff]", cleaned))
    en_words = len(re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", cleaned))
    latin_chars = len(re.findall(r"[A-Za-z]", cleaned))

    has_zh = zh_chars >= 8
    has_en = en_words >= 8 or latin_chars >= 40

    if has_zh and has_en:
        zh_weight = zh_chars
        en_weight = latin_chars
        ratio = min(zh_weight, en_weight) / max(zh_weight, en_weight, 1)
        return "mixed" if ratio >= 0.18 else ("zh" if zh_weight > en_weight else "en")
    if has_zh:
        return "zh"
    if has_en:
        return "en"
    return "zh" if zh_chars >= latin_chars else "en"


def chunk_text(text: str, target_size: int = 320, min_size: int = 180, max_size: int = 480) -> list[dict]:
    """Group sentence spans with a hard character limit, including unpunctuated text.

    Offsets refer to clean_text(text). Whitespace counts toward the context budget.
    max_size is a character bound, not a guarantee about model token counts.
    """
    if not 0 < min_size <= target_size <= max_size:
        raise ValueError("Require 0 < min_size <= target_size <= max_size")
    cleaned = clean_text(text)
    spans = []
    for sentence in _split_sentences_with_spans(cleaned):
        start, end = sentence["start"], sentence["end"]
        while start < end:
            stop = min(start + max_size, end)
            if stop < end:
                # Prefer a word boundary, but always make progress on long tokens/CJK.
                space = cleaned.rfind(" ", start + max_size // 2, stop)
                if space != -1:
                    stop = space
            raw = cleaned[start:stop]
            left = start + len(raw) - len(raw.lstrip())
            right = stop - (len(raw) - len(raw.rstrip()))
            if left < right:
                spans.append({"start": left, "end": right})
            start = stop
            while start < end and cleaned[start].isspace():
                start += 1

    grouped = []
    for span in spans:
        if grouped:
            current = grouped[-1]
            projected = span["end"] - current["start"]
            current_size = current["end"] - current["start"]
            if projected <= max_size and (projected <= target_size or current_size < min_size):
                current["end"] = span["end"]
                continue
        grouped.append(dict(span))
    if len(grouped) > 1 and grouped[-1]["end"] - grouped[-1]["start"] < min_size:
        if grouped[-1]["end"] - grouped[-2]["start"] <= max_size:
            tail = grouped.pop()
            grouped[-1]["end"] = tail["end"]
    return [
        {"chunk_id": index, **span, "text": cleaned[span["start"]:span["end"]]}
        for index, span in enumerate(grouped)
    ]


def detect_genre(text: str) -> str:
    """通过关键词命中数和结构特征判断文本文体，决定后续评分策略的保守程度。

    优先级顺序（从高到低）：
    list_or_table → academic → business_doc → seo_blog → marketing → translation_like → general

    文体影响：academic/business_doc/list_or_table 触发保守策略（降低 AI 分）；
    marketing 触发激进策略（略微提高 AI 分）。
    """
    cleaned = clean_text(text)
    if not cleaned:
        return "general"

    lowered = cleaned.lower()
    lines = [line for line in cleaned.split("\n") if line.strip()]
    line_count = max(len(lines), 1)
    # 列表行：以 -/*/ 序号 开头，或包含表格分隔符 | 和制表符
    list_hits = sum(1 for line in lines if _LIST_LINE_RE.match(line) or "|" in line or "\t" in line)
    if list_hits / line_count >= 0.4:
        return "list_or_table"

    # 学术文：论文关键词或参考文献格式 [数字]
    academic_hits = sum(
        lowered.count(keyword)
        for keyword in ("abstract", "introduction", "methodology", "results", "conclusion", "参考文献", "摘要", "本文", "研究", "实验", "样本")
    )
    if academic_hits >= 3 or re.search(r"\[\d+\]", cleaned):
        return "academic"

    # 商业文档：PRD/OKR/里程碑等项目管理关键词
    business_hits = sum(
        lowered.count(keyword)
        for keyword in ("prd", "kpi", "okr", "roadmap", "milestone", "owner", "deadline", "budget", "交付", "里程碑", "目标", "需求", "负责人", "验收")
    )
    if business_hits >= 3:
        return "business_doc"

    # SEO 博客：搜索优化相关关键词
    seo_hits = sum(
        lowered.count(keyword)
        for keyword in ("seo", "关键词", "长尾词", "搜索量", "流量", "转化率", "收录", "排名")
    )
    if seo_hits >= 2:
        return "seo_blog"

    # 营销文案：促销/活动类关键词
    marketing_hits = sum(
        lowered.count(keyword)
        for keyword in ("立即", "马上", "限时", "品牌", "增长", "欢迎", "点击", "免费", "exclusive", "offer", "campaign")
    )
    if marketing_hits >= 3:
        return "marketing"

    # 翻译文本：含翻译标记词，或正文开头同时含中英文且有冒号
    translation_hits = sum(
        lowered.count(keyword)
        for keyword in ("译文", "原文", "translation", "translated", "source text", "target text", "直译", "意译")
    )
    mixed_language = bool(re.search(r"[\u4e00-\u9fff]", cleaned) and re.search(r"[A-Za-z]{3,}", cleaned))
    if translation_hits >= 1 or (mixed_language and "：" in cleaned and "：" in cleaned[:60]):
        return "translation_like"

    return "general"
