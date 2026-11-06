"""Configurable official-document layout with non-destructive XML edits.

Presets provide editable fonts, heading sequences, page numbers and margins.
The legacy ``company`` profile key remains readable for saved configurations;
it carries no organisation identity or mandatory national-standard claim.
This module never saves a document and proofreading never replaces wording.
"""
from __future__ import annotations

from ..platform_support import default_document_font

import math
import re
from copy import deepcopy
from datetime import date as calendar_date

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt
from docx.text.paragraph import Paragraph
from lxml import etree


DOC_TYPES = ("通知", "请示", "报告", "函", "批复", "会议纪要")
SEQUENCES = ("一、", "（一）", "1.", "（1）", "第一条", "一是", "不识别")
_CN = "零〇一二三四五六七八九十百千万两"
_SEQUENCES = {
    "一、": rf"^([{_CN}]+)、", "（一）": rf"^[（(]([{_CN}]+)[）)]",
    "1.": r"^(\d+)\.(?!\d)", "（1）": r"^[（(](\d+)[）)]",
    "第一条": rf"^第([{_CN}\d]+)条", "一是": rf"^([{_CN}]+)是",
}
_DEFAULT_TERMS = dict(pair.split("=", 1) for pair in (
    '登陆=登录', '帐号=账号', '帐户=账户', '帐套=账套',
    '帐面=账面', '帐款=账款', '帐务=账务', '帐龄=账龄',
    '记帐=记账', '对帐=对账', '台帐=台账', '报帐=报账',
    '入帐=入账', '冲帐=冲账', '转帐=转账', '挂帐=挂账',
    '做帐=做账', '截止目前=截至目前', '截止到目前=截至目前', '截至到=截至',
    '截止至=截至', '截止本=截至本', '做为=作为', '其它=其他',
    '布署=部署', '按排=安排', '按装=安装', '必竟=毕竟',
    '既使=即使', '及待=亟待', '急待解决=亟待解决', '一如继往=一如既往',
    '既往不究=既往不咎', '再接再励=再接再厉', '励行节约=厉行节约', '厉害关系=利害关系',
    '制定实施=制定并实施', '报批审核=报请审核', '制订制度=制定制度', '制订办法=制定办法',
    '制订规定=制定规定', '制订政策=制定政策', '给予解决=予以解决', '做出决定=作出决定',
    '做出部署=作出部署', '做出安排=作出安排', '做出贡献=作出贡献', '做出说明=作出说明',
    '作出成绩=取得成绩', '竟争=竞争', '竟争力=竞争力', '竟标=竞标',
    '竟价=竞价', '竟聘=竞聘', '竟赛=竞赛', '竟品=竞品',
    '竟合=竞合', '溶资=融资', '安祥=安详', '拔款=拨款',
    '拔付=拨付', '拔打=拨打', '斑布=颁布', '颁发实施=颁布实施',
    '报歉=抱歉', '爆光=曝光', '爆露=暴露', '辨证施策=辩证施策',
    '编篡=编纂', '部署署=部署', '侧隐=恻隐', '拆衷=折衷',
    '沉缅=沉湎', '称心如义=称心如意', '成份=成分', '充份=充分',
    '重迭=重叠', '出奇不意=出其不意', '穿流不息=川流不息', '凑和=凑合',
    '大姆指=大拇指', '担搁=耽搁', '抵毁=诋毁', '砥毁=诋毁',
    '叠代=迭代', '订金=定金', '渡假=度假', '发韧=发轫',
    '烦燥=烦躁', '防碍=妨碍', '费寝忘食=废寝忘食', '废驰=废弛',
    '分岐=分歧', '幅射=辐射', '复盖=覆盖', '付合=符合',
    '付诸于=付诸', '改弦更章=改弦更张', '甘败下风=甘拜下风', '膏梁子弟=膏粱子弟',
    '各行其事=各行其是', '功亏一匮=功亏一篑', '勾通=沟通', '估名钓誉=沽名钓誉',
    '贯输=灌输', '鬼计=诡计', '寒喧=寒暄', '汗流夹背=汗流浃背',
    '浩翰=浩瀚', '侯选=候选', '侯补=候补', '焕散=涣散',
    '涣然一新=焕然一新', '慌谬=荒谬', '汇萃=荟萃', '浑号=诨号',
    '机率=概率', '积毁消骨=积毁销骨', '急燥=急躁', '坚苦奋斗=艰苦奋斗',
    '检察错误=检查错误', '简炼=简练', '娇揉造作=矫揉造作', '交待=交代',
    '交纳=缴纳', '精减=精简', '精典=经典', '决窍=诀窍',
    '克苦=刻苦', '克期完成=如期完成', '烂用=滥用', '老俩口=老两口',
    '连锁反映=连锁反应', '了望=瞭望', '另售=零售', '流览=浏览',
    '漫延=蔓延', '冒然=贸然', '煤体=媒体', '美仑美奂=美轮美奂',
    '密诀=秘诀', '名信片=明信片', '明片=名片', '磨擦=摩擦',
    '默守成规=墨守成规', '拈量=掂量', '年青=年轻', '凝炼=凝练',
    '旁证博引=旁征博引', '赔礼到歉=赔礼道歉', '披星带月=披星戴月', '迫不急待=迫不及待',
    '迁徒=迁徙', '签定=签订', '欠收=歉收', '亲睐=青睐',
    '清淅=清晰', '趋之若骛=趋之若鹜', '人才倍出=人才辈出', '人材=人才',
    '融汇贯通=融会贯通', '如法泡制=如法炮制', '弱不经风=弱不禁风', '三翻五次=三番五次',
    '杀戳=杀戮', '山青水秀=山清水秀', '声名雀起=声名鹊起', '拾人牙惠=拾人牙慧',
    '始作佣者=始作俑者', '世外桃园=世外桃源', '事得其反=适得其反', '收迄=收讫',
    '疏竣=疏浚', '松驰=松弛', '题纲=提纲', '提练=提炼',
    '通辑=通缉', '妥贴=妥帖', '枉废心机=枉费心机', '危如垒卵=危如累卵',
    '委屈求全=委曲求全', '慰籍=慰藉', '乌烟障气=乌烟瘴气', '无精打彩=无精打采',
    '相形见拙=相形见绌', '消毁=销毁', '泄漏=泄露', '信口词黄=信口雌黄',
    '形像=形象', '渲泄=宣泄', '循私舞弊=徇私舞弊', '延申=延伸',
    '要决=要诀', '一愁莫展=一筹莫展', '一幅对联=一副对联', '一幅手套=一副手套',
    '以偏盖全=以偏概全', '引伸=引申', '饮鸠止渴=饮鸩止渴', '英雄倍出=英雄辈出',
    '有持无恐=有恃无恐', '原形必露=原形毕露', '责无旁代=责无旁贷', '仗义直言=仗义执言',
    '震憾=震撼', '振憾=震撼', '支离破粹=支离破碎', '座落=坐落',
    '坐阵=坐镇', '作崇=作祟',
))
_BOOLS = {
    "format_tables": False, "remove_extra_spaces": False, "split_line_breaks": False,
    "remove_blank_paragraphs": False, "preflight": True, "proofread": True,
    "company_term_check": True, "placeholder_check": True,
}
_TEXT_BOUNDARIES = {qn("w:" + n) for n in (
    "tab", "br", "cr", "drawing", "pict", "object", "fldChar", "instrText", "fldSimple",
)}
_BLANK_PROTECTED = _TEXT_BOUNDARIES | {qn("w:" + n) for n in (
    "sectPr", "bookmarkStart", "bookmarkEnd", "commentRangeStart", "commentRangeEnd",
    "commentReference", "hyperlink", "numPr", "pBdr", "pageBreakBefore", "sdt",
    "footnoteReference", "endnoteReference", "sym", "lastRenderedPageBreak", "ins", "del",
)}
_STYLE = "青简公文正文"
_PAGE_MARKER = "_QingjianPageNumber_"
_MAX_ISSUES = 500


def _text_option(value, label, *, allow_empty=False, limit=200):
    if not isinstance(value, str) or (not value.strip() and not allow_empty):
        raise ValueError(f"{label}必须为{'可为空的' if allow_empty else '非空'}文本。")
    if len(value) > limit or any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise ValueError(f"{label}过长或含有不支持的控制字符。")
    return value.strip()


def _number(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label}必须为数字。")
    if not low <= value <= high:
        raise ValueError(f"{label}必须在 {low} 至 {high} 之间。")
    return float(value)


def _keywords(value):
    # Regex alternatives and quantifier commas must remain inside their line.
    result = []
    for line in value.splitlines():
        if re.match(r"^(正则[:：]|regex:|re:)", line.strip(), re.I):
            result.append(line.strip())
        else:
            result.extend(x.strip() for x in re.split(r"[|,，;；]+", line) if x.strip())
    return result


def validate_official_options(options: dict | None = None) -> dict:
    """Validate all user input before changing the in-memory document."""
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise ValueError("公文排版配置必须为选项字典。")
    # Accept either the official sub-dict or the containing Word options.
    if "official" in options:
        if not isinstance(options["official"], dict):
            raise ValueError("official 必须为公文排版选项字典。")
        options = options["official"]
    result = {}
    for key, default, allowed in (
        ("profile", "company", ("company", "general")),
        ("operation", "format", ("format", "check")),
        ("doc_type", "通知", DOC_TYPES),
        ("page_numbers", "outside", ("outside", "center", "keep")),
        ("punctuation", "keep", ("keep", "zh", "en")),
    ):
        value = options.get(key, default)
        if value not in allowed:
            raise ValueError(f"{key} 选项无效。")
        result[key] = value
    for key, default in _BOOLS.items():
        value = options.get(key, default)
        if not isinstance(value, bool):
            raise ValueError(f"{key} 必须为布尔值。")
        result[key] = value
    for key, default, low, high in (
        ("body_size", 16, 6, 48), ("title_size", 22, 6, 72),
        ("line_pt", 28, 6, 100), ("indent_chars", 2, 0, 8),
        ("top_cm", 3.7, 0, 10), ("bottom_cm", 3.5, 0, 10),
        ("left_cm", 2.8, 0, 8), ("right_cm", 2.6, 0, 8),
    ):
        result[key] = _number(options.get(key, default), key, low, high)
    if result["left_cm"] + result["right_cm"] > 17:
        raise ValueError("左右页边距之和过大，A4 页面至少应保留 4 厘米内容宽度。")
    if result["top_cm"] + result["bottom_cm"] > 25:
        raise ValueError("上下页边距之和过大。")
    for key, default in (("body_font", "仿宋"), ("title_font", "方正小标宋简体")):
        result[key] = _text_option(options.get(key, default_document_font(default)), key, limit=80)
    result["company_name"] = _text_option(options.get("company_name", ""), "标准单位名称", allow_empty=True)
    custom = options.get("custom_terms", "")
    if isinstance(custom, dict):
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in custom.items()):
            raise ValueError("错词库的错误写法和建议写法必须为文本。")
        custom = "\n".join(f"{k}={v}" for k, v in custom.items())
    result["custom_terms"] = _text_option(custom, "自定义错词库", allow_empty=True, limit=30000)
    for line in result["custom_terms"].splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            parts = re.split(r"[=＝]", line, maxsplit=1)
            if len(parts) != 2 or not all(x.strip() for x in parts):
                raise ValueError("错词库每行须使用“错误写法=建议写法”。")
    headings = options.get("headings", [])
    if not isinstance(headings, (list, tuple)) or len(headings) > 4:
        raise ValueError("标题规则必须为最多四级的列表。")
    by_level = {}
    for i, rule in enumerate(headings, 1):
        if not isinstance(rule, dict):
            raise ValueError("每一级标题规则必须为选项字典。")
        level = rule.get("level", i)
        if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 4 or level in by_level:
            raise ValueError("标题级别须为不重复的 1 至 4。")
        by_level[level] = rule
    result["headings"] = []
    for level, sequence, font in zip(range(1, 5), SEQUENCES[:4], ("黑体", "楷体", "仿宋", "仿宋")):
        source = by_level.get(level, {})
        sequence = source.get("sequence", sequence)
        if sequence not in SEQUENCES:
            raise ValueError(f"第 {level} 级标题序号样式无效。")
        keytext = _text_option(source.get("keywords", ""), "标题关键字", allow_empty=True, limit=4000)
        for key in _keywords(keytext):
            if re.match(r"^(正则[:：]|regex:|re:)", key, re.I):
                pattern = re.sub(r"^(正则[:：]|regex:|re:)", "", key, flags=re.I).strip()
                if not pattern or len(pattern) > 256:
                    raise ValueError("标题正则表达式不能为空且不能超过 256 字符。")
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"第 {level} 级标题正则无效：{exc}") from exc
        result["headings"].append({
            "level": level, "sequence": sequence,
            "font": _text_option(source.get("font", default_document_font(font)), "标题字体", limit=80),
            "size": _number(source.get("size", 16), "标题字号", 6, 72), "keywords": keytext,
        })
    return result


def _all_paragraphs(document):
    result = []
    body_index, table_index = 0, 0
    for element in document.element.body.iter(qn("w:p")):
        ancestors = list(element.iterancestors())
        if any(x.tag == qn("w:txbxContent") for x in ancestors):
            continue  # Floating text boxes retain their layout and text.
        table = any(x.tag == qn("w:tc") for x in ancestors)
        if table:
            table_index += 1
            location = f"表格第 {table_index} 段"
        else:
            body_index += 1
            location = f"正文第 {body_index} 段"
        result.append((location, Paragraph(element, document._body), table))
    return result


def _visible_text(paragraph):
    # Paragraph.text includes hyperlinks in python-docx 1.2; fields also appear.
    return paragraph.text.strip()


def _metadata(text, paragraph, options):
    if re.fullmatch(r"[\u4e00-\u9fffA-Za-z]{1,16}[〔\[【（(]\d{4}[〕\]】）)]\s*\d+号", text):
        return True
    if text == options["company_name"] and text:
        return True
    return (len(text) < 60 and any(
        (r.get(qn("w:val")) or "").upper() in {"FF0000", "C00000"}
        for r in paragraph._p.iter(qn("w:color"))
    ))


def _main_title(paragraphs, options):
    candidates = [(p, _visible_text(p)) for _, p, table in paragraphs if not table and _visible_text(p)]
    for p, text in candidates:
        style = p.style
        if style is not None and (style.style_id in {"Title", "标题"} or style.name in {"Title", "标题"}):
            return p._p
    for p, text in candidates:
        if not _metadata(text, p, options) and not re.fullmatch(r"\d{4}年\d{1,2}月\d{1,2}日", text):
            return p._p
    return None


def _chinese_number(value):
    if value.isdigit():
        return int(value)
    digits = {c: i for i, c in enumerate("零一二三四五六七八九")}
    digits.update({"〇": 0, "两": 2})
    total, section, number = 0, 0, 0
    for char in value:
        if char in digits:
            number = digits[char]
        elif char in "十百千":
            section += (number or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            number = 0
        elif char == "万":
            total += (section + number or 1) * 10000
            section, number = 0, 0
        else:
            return None
    return total + section + number


def _match_heading(paragraph, options):
    text = _visible_text(paragraph)
    for rule in options["headings"]:
        for keyword in _keywords(rule["keywords"]):
            if keyword in {"智能识别", "自动识别"}:
                continue
            regex = re.match(r"^(正则[:：]|regex:|re:)", keyword, re.I)
            if regex and re.search(keyword[regex.end():].strip(), text[:1000]):
                return rule, "手动正则", None
            if not regex and ((keyword.startswith("=") and text == keyword[1:])
                              or (keyword.endswith("*") and text.startswith(keyword[:-1]))
                              or (not keyword.startswith("=") and not keyword.endswith("*") and keyword in text)):
                return rule, "手动关键字", None
    if len(text) <= 120:
        for rule in options["headings"]:
            pattern = _SEQUENCES.get(rule["sequence"])
            match = re.match(pattern, text) if pattern else None
            if match:
                return rule, "标题序号", _chinese_number(match.group(1))
    style = paragraph.style
    style_name = style.name if style is not None else ""
    match = re.fullmatch(r"(?:Heading|标题)\s*([1-4])", style_name, re.I)
    if match:
        return options["headings"][int(match[1]) - 1], "Word 标题样式", None
    outline = paragraph._p.find("./" + qn("w:pPr") + "/" + qn("w:outlineLvl"))
    if outline is not None and outline.get(qn("w:val"), "9") in {"0", "1", "2", "3"}:
        return options["headings"][int(outline.get(qn("w:val")))], "标题大纲级别", None
    if len(text) <= 120 and not text.endswith(("。", "！", "？", ";", "；")):
        if re.match(rf"^第[{_CN}\d]+[章节篇部](?:分)?", text) or text in {"摘要", "目录", "结语", "结束语"}:
            return options["headings"][0], "章/篇或独立标题", None
        if re.match(rf"^趋势[{_CN}\d]+[:：]", text):
            return options["headings"][1], "趋势标题", None
        decimal = re.match(r"^(\d+(?:\.\d+){1,3})(?:\s+|(?=[\u4e00-\u9fff]))", text)
        if decimal:
            values = [int(x) for x in decimal[1].split(".")]
            if all(x <= 99 for x in values):
                return options["headings"][len(values) - 1], "多级数字标题", values[-1]
    return None, "", None


def _heading_records(paragraphs, title, options):
    result = []
    for location, paragraph, table in paragraphs:
        if table or paragraph._p is title or not _visible_text(paragraph):
            continue
        rule, source, number = _match_heading(paragraph, options)
        if rule:
            decimal = re.match(r"^(\d+(?:\.\d+){1,3})(?:\s+|(?=[\u4e00-\u9fff]))", _visible_text(paragraph))
            result.append({"location": location, "text": _visible_text(paragraph)[:60],
                           "level": rule["level"], "source": source, "font": rule["font"],
                           "size": rule["size"], "sequence": rule["sequence"], "number": number,
                           "number_scope": decimal[1].rsplit(".", 1)[0] if decimal and source == "多级数字标题" else ""})
    return result


def _attachment_kind(text):
    if re.fullmatch(r"附件\s*\d*", text):
        return "marker"
    if re.match(r"^附件\s*(?:\d+\s*)?[:：]", text) or re.match(r"^附件\s*\d+\s+\S", text):
        return "description"
    return ""


def _layout_targets(paragraphs, title, options):
    """One classification shared by formatting and effective-format checks."""
    metadata = set()
    for _, p, table in paragraphs:
        if p._p is title:
            break
        if not table and _metadata(_visible_text(p), p, options):
            metadata.add(p._p)
    nonempty = [p for _, p, table in paragraphs if not table and _visible_text(p)]
    date = nonempty[-1]._p if nonempty and re.fullmatch(r"\d{4}年\s*\d{1,2}月\s*\d{1,2}日", _visible_text(nonempty[-1])) else None
    issuer = nonempty[-2]._p if date is not None and len(nonempty) >= 2 and len(_visible_text(nonempty[-2])) < 45 and not re.search(r"[。；！？]", _visible_text(nonempty[-2])) else None
    targets = {}
    for location, p, table in paragraphs:
        if (table and not options["format_tables"]) or p._p in metadata:
            continue
        text = _visible_text(p)
        target = dict(location=location, label="表格正文" if table else "正文", font=options["body_font"],
                      size=options["body_size"], indent=0 if table or not text else options["indent_chars"],
                      alignment=WD_ALIGN_PARAGRAPH.JUSTIFY, title=False, level=None)
        if not table and p._p is title:
            target.update(label="公文标题", font=options["title_font"], size=options["title_size"],
                          indent=0, alignment=WD_ALIGN_PARAGRAPH.CENTER, title=True)
        elif not table and (p._p is date or p._p is issuer):
            target.update(label="成文日期/署名", indent=0, alignment=WD_ALIGN_PARAGRAPH.RIGHT)
        elif not table:
            rule, _, _ = _match_heading(p, options)
            kind = _attachment_kind(text)
            if rule:
                target.update(label=f"{rule['level']} 级标题", font=rule["font"], size=rule["size"],
                              indent=0, alignment=WD_ALIGN_PARAGRAPH.LEFT, level=rule["level"])
            elif kind == "marker":
                target.update(label="附件页标记", font="黑体", indent=0, alignment=WD_ALIGN_PARAGRAPH.LEFT)
            elif kind == "description":
                target.update(label="附件说明")
        targets[p._p] = target
    return targets


def _style_properties(style, property_name):
    seen = set()
    while style is not None and style.style_id not in seen:
        seen.add(style.style_id)
        element = style.element.find(qn("w:" + property_name))
        if element is not None:
            yield element, "继承样式"
        style = style.base_style


def _paragraph_properties(paragraph, document):
    if paragraph._p.pPr is not None:
        yield paragraph._p.pPr, "直接设置"
    yield from _style_properties(paragraph.style, "pPr")
    defaults = document.styles.element.find("./" + qn("w:docDefaults") + "/" + qn("w:pPrDefault") + "/" + qn("w:pPr"))
    if defaults is not None:
        yield defaults, "继承文档默认值"


def _run_properties(run, paragraph, document, styles):
    rpr = run.find(qn("w:rPr"))
    if rpr is not None:
        yield rpr, "直接设置"
        style_ref = rpr.find(qn("w:rStyle"))
        if style_ref is not None:
            yield from _style_properties(styles.get(style_ref.get(qn("w:val"))), "rPr")
    yield from _style_properties(paragraph.style, "rPr")
    defaults = document.styles.element.find("./" + qn("w:docDefaults") + "/" + qn("w:rPrDefault") + "/" + qn("w:rPr"))
    if defaults is not None:
        yield defaults, "继承文档默认值"


def _effective(properties, child, attribute="val", default=None):
    for parent, source in properties:
        node = parent.find(qn("w:" + child))
        if node is not None:
            value = node.get(qn("w:" + attribute))
            if value is not None:
                return value, source
    return default, "未明确设置（含样式及文档默认值）"


def _content_runs(paragraph):
    for run in paragraph._p.iter(qn("w:r")):
        if next((a for a in run.iterancestors() if a.tag == qn("w:p")), None) is paragraph._p and any((node.text or "") for node in run.iter(qn("w:t"))):
            yield run


def _format_violations(document, paragraphs, title, options):
    issues = []
    targets = _layout_targets(paragraphs, title, options)
    styles = {s.style_id: s for s in document.styles}
    for _, paragraph, _ in paragraphs:
        target = targets.get(paragraph._p)
        if not target or not _visible_text(paragraph):
            continue
        wrong = []
        bad_font = bad_size = None
        for run in _content_runs(paragraph):
            properties = list(_run_properties(run, paragraph, document, styles))
            text = "".join(n.text or "" for n in run.iter(qn("w:t")))
            face = "eastAsia" if re.search(r"[\u3400-\u9fff]", text) else "ascii"
            font, font_source = _effective(properties, "rFonts", face)
            size, size_source = _effective(properties, "sz")
            if font != target["font"]:
                bad_font = bad_font or f"字体{'为“' + font + '”' if font else '无法明确确认'}（{font_source}），应为“{target['font']}”"
            try:
                matches_size = abs(float(size) / 2 - target["size"]) < .01
            except (ValueError, TypeError):
                matches_size = False
            if not matches_size:
                bad_size = bad_size or f"字号{str(float(size) / 2) + ' 磅' if size and size.isdigit() else '无法明确确认'}（{size_source}），应为 {target['size']:g} 磅"
        wrong.extend(value for value in (bad_font, bad_size) if value)
        properties = list(_paragraph_properties(paragraph, document))
        align, align_source = _effective(properties, "jc", default="left")
        actual_align = {"left": WD_ALIGN_PARAGRAPH.LEFT, "start": WD_ALIGN_PARAGRAPH.LEFT,
                        "right": WD_ALIGN_PARAGRAPH.RIGHT, "end": WD_ALIGN_PARAGRAPH.RIGHT,
                        "center": WD_ALIGN_PARAGRAPH.CENTER, "both": WD_ALIGN_PARAGRAPH.JUSTIFY}.get(align)
        if actual_align != target["alignment"]:
            labels = {WD_ALIGN_PARAGRAPH.CENTER: "居中", WD_ALIGN_PARAGRAPH.RIGHT: "右对齐",
                      WD_ALIGN_PARAGRAPH.LEFT: "左对齐", WD_ALIGN_PARAGRAPH.JUSTIFY: "两端对齐"}
            wrong.append(f"对齐方式为 {align}（{align_source}），应{labels[target['alignment']]}")
        line, line_source = _effective(properties, "spacing", "line")
        rule, _ = _effective(properties, "spacing", "lineRule", default="auto")
        wanted_line, wanted_rule = (240, "auto") if target["title"] else (options["line_pt"] * 20, "exact")
        try:
            line_ok = abs(float(line) - wanted_line) < .1 and rule == wanted_rule
        except (ValueError, TypeError):
            line_ok = False
        if not line_ok:
            desired = "单倍行距" if target["title"] else f"固定 {options['line_pt']:g} 磅行距"
            wrong.append(f"行距为 {rule}/{line or '未设置'}（{line_source}），应为{desired}")
        # Word numbering may define its own indent; it is preserved separately.
        numbered = any(parent.find(qn("w:numPr")) is not None for parent, _ in properties)
        if not numbered:
            chars, char_source = _effective(properties, "ind", "firstLineChars")
            first, first_source = _effective(properties, "ind", "firstLine", default="0")
            hanging_chars, _ = _effective(properties, "ind", "hangingChars", default="0")
            hanging, _ = _effective(properties, "ind", "hanging", default="0")
            try:
                actual_indent = float(chars) / 100 if chars is not None else (float(first) - float(hanging)) / (options["body_size"] * 20)
                if float(hanging_chars):
                    actual_indent = -float(hanging_chars) / 100
                indent_ok = abs(actual_indent - target["indent"]) < .03
            except (ValueError, TypeError):
                actual_indent, indent_ok = "无法确认", False
            if not indent_ok:
                wrong.append(f"首行缩进为 {actual_indent} 字符（{char_source if chars is not None else first_source}），应为 {target['indent']:g} 字符")
            for attr in ("left", "right", "start", "end", "leftChars", "rightChars", "startChars", "endChars"):
                value, source = _effective(properties, "ind", attr, default="0")
                try:
                    has_indent = abs(float(value)) > .1
                except (ValueError, TypeError):
                    has_indent = True
                if has_indent:
                    wrong.append(f"存在额外左右/悬挂缩进（{source}），应归零")
                    break
        if target["title"]:
            snap, source = _effective(properties, "snapToGrid", default="1")
            if snap not in {"0", "false", "off"}:
                wrong.append(f"尚未关闭对齐文档网格（{source}）")
        if wrong:
            issues.append(f"{target['location']}（{target['label']}）：" + "；".join(wrong) + "。")
        if len(issues) >= _MAX_ISSUES:
            break
    return issues


def _page_violations(document, options):
    if options["page_numbers"] == "keep":
        return []
    issues, inherited = [], {}
    outside = options["page_numbers"] == "outside"
    odd_even = bool(document.settings.odd_and_even_pages_header_footer)
    if outside and not odd_even:
        issues.append("尚未启用奇偶页不同页脚，不能确认奇右偶左外侧页码。")
    for index, section in enumerate(document.sections, 1):
        for ref in section._sectPr.findall(qn("w:footerReference")):
            inherited[ref.get(qn("w:type"))] = ref.get(qn("r:id"))
        kinds = ["default"] + (["even"] if odd_even else []) + (["first"] if section.different_first_page_header_footer else [])
        missing = []
        for kind in kinds:
            relation = document.part.rels.get(inherited.get(kind))
            root = relation.target_part.element if relation is not None and not relation.is_external else None
            found = root is not None and (any(re.search(r"\bPAGE\b", n.text or "", re.I) for n in root.iter(qn("w:instrText"))) or any(re.search(r"\bPAGE\b", n.get(qn("w:instr"), ""), re.I) for n in root.iter(qn("w:fldSimple"))))
            if not found:
                missing.append({"default": "普通/奇数页", "even": "偶数页", "first": "首页"}[kind])
        if missing:
            issues.append(f"第 {index} 节{'、'.join(missing)}页脚未发现 PAGE 页码域。")
    return issues


def _issues_preflight(document, paragraphs, title, options):
    issues = []
    for i, section in enumerate(document.sections, 1):
        landscape = section.orientation == WD_ORIENT.LANDSCAPE
        for key, label in (("top_cm", "上"), ("bottom_cm", "下"), ("left_cm", "左"), ("right_cm", "右")):
            current = getattr(section, key.replace("_cm", "_margin"))
            if current is None or abs(current.cm - options[key]) > 0.03:
                issues.append(f"第 {i} 节{label}页边距将调整为 {options[key]:g} 厘米。")
        width, height = ((29.7, 21) if landscape else (21, 29.7))
        if section.page_width is None or section.page_height is None or abs(section.page_width.cm - width) > 0.05 or abs(section.page_height.cm - height) > 0.05:
            issues.append(f"第 {i} 节将调整为 A4，保留现有横向/纵向及分节起始方式。")
        if landscape:
            issues.append(f"第 {i} 节为横向页面，将保留横向方向。")
    if title is None:
        issues.append("未发现可识别的正文标题。")
    if any(p._p.find("./" + qn("w:pPr") + "/" + qn("w:numPr")) is not None for _, p, _ in paragraphs):
        issues.append("发现 Word 自动编号，保留编号定义；请在标题识别清单及 Word 中复核实际显示层级。")
    if any(n.tag == qn("w:txbxContent") for n in document.element.body.iter()):
        issues.append("浮动文本框保留原有版式，未纳入正文格式与校对。")
    if any(p._p.find(".//" + qn("w:ins")) is not None or p._p.find(".//" + qn("w:del")) is not None for _, p, _ in paragraphs):
        issues.append("文档含修订记录；保留修订，正式发文前应在 Word 中复核修订状态。")
    for location, paragraph, table in paragraphs:
        if not table and _attachment_kind(_visible_text(paragraph)):
            issues.append(f"{location}含附件标记或说明；请人工核对附件页左上标记、第三行标题及附件说明位置，程序不搬动段落。")
    issues.extend(_format_violations(document, paragraphs, title, options))
    issues.extend(_page_violations(document, options))
    return issues[:_MAX_ISSUES]


def _company_name_variants(text, standard):
    """Suggest suffix differences only for a name explicitly supplied by the user.

    Do not guess abbreviations or ship an organisation-specific alias library.
    Other companies in the text are not compared with the configured issuer.
    """
    suffixes = ("股份有限公司", "有限责任公司", "有限公司", "股份公司", "公司")
    suffix = next((value for value in suffixes if standard.endswith(value)), None)
    if not suffix:
        return []
    stem = standard[:-len(suffix)]
    if len(stem) < 2:
        return []
    pattern = re.escape(stem) + "(?:" + "|".join(map(re.escape, suffixes)) + ")"
    return list(dict.fromkeys(match[0] for match in re.finditer(pattern, text)
                              if match[0] != standard))


def _proofread(paragraphs, headings, options):
    if not (options["proofread"] or options["company_term_check"] or options["placeholder_check"]):
        return []
    terms = dict(_DEFAULT_TERMS) if options["proofread"] else {}
    for line in options["custom_terms"].splitlines():
        if options["proofread"] and line.strip() and not line.lstrip().startswith("#"):
            wrong, right = re.split(r"[=＝]", line, maxsplit=1)
            terms[wrong.strip()] = right.strip()
    issues = []
    for location, paragraph, _ in paragraphs:
        text = _visible_text(paragraph)
        for wrong, right in terms.items():
            if wrong in text:
                issues.append(f"{location}疑似用词“{wrong}”，建议核对“{right}”（未替换）。")
        if options["company_term_check"] and options["company_name"]:
            for wrong in _company_name_variants(text, options["company_name"]):
                issues.append(f"{location}单位称谓“{wrong}”与设定名称“{options['company_name']}”存在后缀差异，请核对（未替换）。")
        if options["placeholder_check"] and re.search(r"(?i)(?<![A-Za-z])(?:X{2,}|TODO|TBD)(?![A-Za-z])|待补充|待完善|此处填写|请输入|【\s*】|〔\s*〕|（\s*）|\(\s*\)|(?:\d{4})?年\s*月\s*日", text):
            issues.append(f"{location}发现占位文字或空白日期，请正式发文前补齐。")
        if not options["proofread"]:
            if len(issues) >= _MAX_ISSUES:
                break
            continue
        pairs = {"（": "）", "(": ")", "[": "]", "【": "】", "〔": "〕", "《": "》"}
        stack, mismatch = [], False
        for char in text:
            if char in pairs:
                stack.append(char)
            elif char in pairs.values():
                if not stack or pairs[stack.pop()] != char:
                    mismatch = True
        if mismatch or stack:
            issues.append(f"{location}括号或书名号疑似不匹配。")
        if re.search(r"[，,。！？；：]{2,}", text):
            issues.append(f"{location}疑似连续标点，请人工核对。")
        for match in re.finditer(r"(?<!\d)(\d{4})(?:年|[-/.])(\d{1,2})(?:月|[-/.])(\d{1,2})(?:日)?(?!\d)", text):
            try:
                calendar_date(*(int(x) for x in match.groups()))
            except ValueError:
                issues.append(f"{location}日期“{match[0]}”不是有效日期。")
            else:
                if "年" not in match[0]:
                    issues.append(f"{location}日期“{match[0]}”建议核对是否改用汉字年月日。")
        if len(issues) >= _MAX_ISSUES:
            break
    # Child sequences may restart within each new parent; never flatten levels.
    if not options["proofread"]:
        return list(dict.fromkeys(issues))[:_MAX_ISSUES]
    previous = {}
    for heading in headings:
        level, number = heading["level"], heading["number"]
        for child in list(previous):
            if child > level:
                del previous[child]
        if number is not None:
            prior = previous.get(level)
            scope = heading.get("number_scope", "")
            if prior is not None and prior[0] == scope and number != prior[1] + 1:
                issues.append(f"{heading['location']}第 {level} 级标题序号疑似不连续或重复（上一处 {prior[1]}，本处 {number}），未自动重编号。")
            previous[level] = (scope, number)
    return list(dict.fromkeys(issues))[:_MAX_ISSUES]


def _base_audit(document, options):
    paragraphs = _all_paragraphs(document)
    title = _main_title(paragraphs, options)
    headings = _heading_records(paragraphs, title, options)
    return {
        "profile": options["profile"], "doc_type": options["doc_type"],
        "preset_note": "通用公文预设，字体、层级、页边距和页码均可调整；不作国家标准符合性认证。" if options["profile"] == "company" else "自定义公文版式，不作国家标准符合性认证。",
        "preflight": _issues_preflight(document, paragraphs, title, options) if options["preflight"] else [],
        "postflight": [], "headings": headings,
        "corrections": _proofread(paragraphs, headings, options),
        "counts": {"paragraphs": len(paragraphs), "table_paragraphs": sum(t for _, _, t in paragraphs),
                   "headings": len(headings), "heading_levels": {str(i): sum(h["level"] == i for h in headings) for i in range(1, 5)},
                   "split_paragraphs": 0, "removed_blank_paragraphs": 0, "cleaned_paragraphs": 0},
    }


def inspect_official_document(document, options: dict | None = None) -> dict:
    """Read-only checks; no styles, relationships, directories or files created."""
    normalized = validate_official_options(options)
    audit = _base_audit(document, normalized)
    warnings = ["校对结果仅提示疑似问题，不自动改写原文；标题识别应人工复核。"]
    return {"changes": 0, "warnings": warnings, "audit": audit}


def _editable_groups(paragraph):
    group, depth = [], 0
    for element in paragraph._p.iter():
        if element is paragraph._p:
            continue
        # Ignore descendants belonging to text boxes, nested paragraphs or simple fields.
        if any(a is not paragraph._p and a.tag in {qn("w:p"), qn("w:fldSimple"), qn("w:txbxContent")} for a in element.iterancestors() if a is not paragraph._p):
            continue
        if element.tag in _TEXT_BOUNDARIES:
            if group:
                yield group
                group = []
            if element.tag == qn("w:fldChar"):
                kind = element.get(qn("w:fldCharType"))
                depth = depth + 1 if kind == "begin" else max(0, depth - 1) if kind == "end" else depth
        elif element.tag == qn("w:t") and not depth:
            group.append(element)
    if group:
        yield group


_PROTECTED_TEXT = re.compile(r"https?://[^\s\u3000\u4e00-\u9fff，。；！？]+|[A-Za-z0-9_.+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}|(?<!\d)\d+(?:\.\d+)+(?!\d)|^[ \t]*\d+\.(?!\d)")


def _clean_string(text, options):
    if options["remove_extra_spaces"]:
        text = re.sub(r"[ \u3000\u00a0]+", " ", text)
        text = re.sub(r"(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])", "", text)
        text = re.sub(r"([、，。；：！？]) +", r"\1", text).strip(" ")
    if options["punctuation"] == "keep":
        return text
    protected = []
    def protect(match):
        protected.append(match[0])
        return chr(0xE000 + len(protected) - 1)
    text = _PROTECTED_TEXT.sub(protect, text)
    if options["punctuation"] == "zh":
        text = text.translate(str.maketrans(",;:?!()[]", "，；：？！（）【】"))
        text = re.sub(r"(?<=[\u4e00-\u9fff）】])\.", "。", text)
    else:
        text = text.translate(str.maketrans("，；：？！（）【】。", ",;:?!()[]."))
    for i, value in enumerate(protected):
        text = text.replace(chr(0xE000 + i), value)
    return text


def _rewrite_group(nodes, cleaned):
    # Map edits onto original text nodes instead of rebuilding runs or links.
    import difflib
    original = "".join(n.text or "" for n in nodes)
    offsets, total = [], 0
    for n in nodes:
        offsets.append(total)
        total += len(n.text or "")
    values = [n.text or "" for n in nodes]
    for tag, a, b, c, d in reversed(difflib.SequenceMatcher(None, original, cleaned, autojunk=False).get_opcodes()):
        if tag == "equal":
            continue
        left = max((i for i, start in enumerate(offsets) if start <= a), default=0)
        right = max((i for i, start in enumerate(offsets) if start < b), default=left)
        left_offset = a - offsets[left]
        right_offset = b - offsets[right]
        if left == right:
            values[left] = values[left][:left_offset] + cleaned[c:d] + values[left][right_offset:]
        else:
            values[left] = values[left][:left_offset] + cleaned[c:d]
            for i in range(left + 1, right):
                values[i] = ""
            values[right] = values[right][right_offset:]
    for node, value in zip(nodes, values):
        node.text = value
        if value.startswith(" ") or value.endswith(" "):
            node.set(qn("xml:space"), "preserve")


def _split_paragraph(paragraph, warnings):
    breaks = [e for e in paragraph._p.iter() if e.tag == qn("w:cr") or (e.tag == qn("w:br") and e.get(qn("w:type"), "textWrapping") == "textWrapping")]
    if not breaks:
        return 0
    allowed = {qn("w:r"), qn("w:hyperlink"), qn("w:smartTag")}
    if any(e.tag in {qn("w:fldChar"), qn("w:fldSimple")} for e in paragraph._p.iter()) or any(
        any(a is not paragraph._p and a.tag not in allowed for a in e.iterancestors() if a is not paragraph._p and paragraph._p in a.iterancestors()) for e in breaks
    ):
        if "含域或复杂容器的手动换行段落保留原结构，未拆段。" not in warnings:
            warnings.append("含域或复杂容器的手动换行段落保留原结构，未拆段。")
        return 0
    break_set = set(breaks)
    def fragments(node):
        if node in break_set:
            return [None, None]
        if not any(b in node.iterdescendants() for b in break_set):
            return [node]
        template = deepcopy(node)
        for child in list(template):
            if child.tag != qn("w:rPr"):
                template.remove(child)
        current, result = deepcopy(template), []
        for child in list(node):
            if child.tag == qn("w:rPr"):
                continue
            child_parts = fragments(child)
            for i, part in enumerate(child_parts):
                if i:
                    result.append(current)
                    current = deepcopy(template)
                if part is not None:
                    current.append(part)
        result.append(current)
        return result
    original = paragraph._p
    ppr = original.pPr
    containers = [original]
    for child in list(original):
        if child is ppr:
            continue
        original.remove(child)
        for i, part in enumerate(fragments(child)):
            if i:
                new = OxmlElement("w:p")
                if ppr is not None:
                    new.append(deepcopy(ppr))
                containers[-1].addnext(new)
                containers.append(new)
            if part is not None:
                containers[-1].append(part)
    for element in containers[:-1]:
        for section in list(element.findall("./" + qn("w:pPr") + "/" + qn("w:sectPr"))):
            section.getparent().remove(section)
    return len(containers) - 1


def _set_boolean(parent, name, value):
    element = parent.find(qn("w:" + name))
    if element is None:
        element = OxmlElement("w:" + name)
        parent.append(element)
    element.set(qn("w:val"), "1" if value else "0")


def _paragraph_format(paragraph, options, indent=0, *, title=False, alignment=None, level=None):
    paragraph.style = _STYLE
    pf = paragraph.paragraph_format
    pf.left_indent = Pt(0)
    pf.right_indent = Pt(0)
    pf.first_line_indent = Pt(options["body_size"] * indent)
    ind = paragraph._p.get_or_add_pPr().get_or_add_ind()
    for attr in ("leftChars", "rightChars", "startChars", "endChars"):
        ind.set(qn("w:" + attr), "0")
    ind.set(qn("w:firstLineChars"), str(round(indent * 100)))
    ind.attrib.pop(qn("w:hangingChars"), None)
    ind.attrib.pop(qn("w:start"), None)
    ind.attrib.pop(qn("w:end"), None)
    pf.space_before, pf.space_after = Pt(0), Pt(18 if title else 0)
    pf.line_spacing_rule = WD_LINE_SPACING.SINGLE if title else WD_LINE_SPACING.EXACTLY
    pf.line_spacing = 1 if title else Pt(options["line_pt"])
    pf.keep_with_next = bool(title or level)
    pf.widow_control = True
    paragraph.alignment = alignment if alignment is not None else (WD_ALIGN_PARAGRAPH.CENTER if title else WD_ALIGN_PARAGRAPH.JUSTIFY)
    ppr = paragraph._p.get_or_add_pPr()
    _set_boolean(ppr, "snapToGrid", False)
    _set_boolean(ppr, "contextualSpacing", False)
    outline = ppr.find(qn("w:outlineLvl"))
    if outline is None:
        outline = OxmlElement("w:outlineLvl")
        ppr.append(outline)
    outline.set(qn("w:val"), str(level - 1 if level else 9))


def _format_runs(paragraph, font, size, *, black=True):
    for run in paragraph._p.iter(qn("w:r")):
        if next((a for a in run.iterancestors() if a.tag == qn("w:p")), None) is not paragraph._p:
            continue
        rpr = run.find(qn("w:rPr"))
        if rpr is None:
            rpr = OxmlElement("w:rPr")
            run.insert(0, rpr)
        fonts = rpr.find(qn("w:rFonts"))
        if fonts is None:
            fonts = OxmlElement("w:rFonts")
            rpr.insert(0, fonts)
        for attr in ("ascii", "hAnsi", "eastAsia", "cs"):
            fonts.set(qn("w:" + attr), font)
        for attr in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme", "csTheme"):
            fonts.attrib.pop(qn("w:" + attr), None)
        for name in ("sz", "szCs"):
            element = rpr.find(qn("w:" + name))
            if element is None:
                element = OxmlElement("w:" + name)
                rpr.append(element)
            element.set(qn("w:val"), str(round(size * 2)))
        if black:
            color = rpr.find(qn("w:color"))
            if color is None:
                color = OxmlElement("w:color")
                rpr.append(color)
            color.attrib.clear()
            color.set(qn("w:val"), "000000")


def _body_style(document, options):
    styles = document.styles
    style = styles[_STYLE] if _STYLE in styles else styles.add_style(_STYLE, WD_STYLE_TYPE.PARAGRAPH)
    style.base_style = None  # Works even when the document has no Normal style.
    style.font.name, style.font.size = options["body_font"], Pt(options["body_size"])
    style.paragraph_format.left_indent = Pt(0)
    style.paragraph_format.right_indent = Pt(0)
    style.paragraph_format.first_line_indent = Pt(0)
    style.paragraph_format.space_before = Pt(0)
    style.paragraph_format.space_after = Pt(0)
    style.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    style.paragraph_format.line_spacing = Pt(options["line_pt"])
    fonts = style.element.get_or_add_rPr().get_or_add_rFonts()
    fonts.set(qn("w:eastAsia"), options["body_font"])


def _footer_has_page(paragraph):
    return any(re.search(r"\bPAGE\b", node.text or "", re.I) for node in paragraph._p.iter(qn("w:instrText"))) or any(
        re.search(r"\bPAGE\b", node.get(qn("w:instr"), ""), re.I) for node in paragraph._p.iter(qn("w:fldSimple"))
    )


def _clone_footer(source, destination):
    target = destination.part
    mapping = {}
    for relation in source.part.rels.values():
        mapping[relation.rId] = target.relate_to(relation.target_ref if relation.is_external else relation.target_part, relation.reltype, relation.is_external)
    root = destination._element
    for child in list(root):
        root.remove(child)
    next_mark = _next_bookmark_id(source.part.package)
    names, ids = {}, {}
    for mark in source._element.iter(qn("w:bookmarkStart")):
        old_id = mark.get(qn("w:id"), "0")
        ids[old_id] = str(next_mark)
        if mark.get(qn("w:name")):
            names[mark.get(qn("w:name"))] = mark.get(qn("w:name"))[:20] + "_copy_" + str(next_mark)
        next_mark += 1
    for child in source._element:
        clone = deepcopy(child)
        for element in clone.iter():
            for attr, value in list(element.attrib.items()):
                if attr.startswith("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}") and value in mapping:
                    element.set(attr, mapping[value])
            if element.tag in {qn("w:bookmarkStart"), qn("w:bookmarkEnd")}:
                old_id = element.get(qn("w:id"))
                if old_id in ids:
                    element.set(qn("w:id"), ids[old_id])
                old_name = element.get(qn("w:name"))
                if old_name in names:
                    element.set(qn("w:name"), names[old_name])
            if element.tag == qn("w:hyperlink") and element.get(qn("w:anchor")) in names:
                element.set(qn("w:anchor"), names[element.get(qn("w:anchor"))])
        root.append(clone)


def _next_bookmark_id(package):
    maximum = 899999
    for part in package.parts:
        if hasattr(part, "element"):
            for node in part.element.iter(qn("w:bookmarkStart")):
                try:
                    maximum = max(maximum, int(node.get(qn("w:id"), "0")))
                except ValueError:
                    pass
    return maximum + 1


def _page_paragraph(footer, alignment, options, marker, warnings):
    pages = [p for p in footer.paragraphs if _footer_has_page(p)]
    if pages:
        for paragraph in pages:
            simple = re.fullmatch(r"[\s\d—–\-]*", paragraph.text or "") is not None
            if simple:
                _paragraph_format(paragraph, options, alignment=alignment)
                pf = paragraph.paragraph_format
                pf.line_spacing_rule, pf.line_spacing = WD_LINE_SPACING.SINGLE, 1
                pf.left_indent = Pt(14 if alignment == WD_ALIGN_PARAGRAPH.LEFT else 0)
                pf.right_indent = Pt(14 if alignment == WD_ALIGN_PARAGRAPH.RIGHT else 0)
                _format_runs(paragraph, "宋体", 14)
            elif "既有页码与其他页脚文字同段，已保留原有页脚布局，请人工复核页码位置。" not in warnings:
                warnings.append("既有页码与其他页脚文字同段，已保留原有页脚布局，请人工复核页码位置。")
        return
    # Append a separate managed paragraph; never clear existing footer content.
    paragraph = footer.add_paragraph()
    mark = OxmlElement("w:bookmarkStart")
    mark.set(qn("w:id"), str(marker))
    mark.set(qn("w:name"), _PAGE_MARKER + str(marker))
    paragraph._p.append(mark)
    paragraph.add_run("—")
    for kind, text in (("begin", None), ("instruction", " PAGE "), ("separate", None), ("text", "1"), ("end", None)):
        run = paragraph.add_run()
        if kind in {"begin", "separate", "end"}:
            node = OxmlElement("w:fldChar")
            node.set(qn("w:fldCharType"), kind)
        elif kind == "instruction":
            node = OxmlElement("w:instrText")
            node.set(qn("xml:space"), "preserve")
            node.text = text
        else:
            node = OxmlElement("w:t")
            node.text = text
        run._r.append(node)
    paragraph.add_run("—")
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), str(marker))
    paragraph._p.append(end)
    _page_paragraph(footer, alignment, options, marker, warnings)


def _setup_pages(document, options, warnings):
    for section in document.sections:
        landscape = section.orientation == WD_ORIENT.LANDSCAPE
        section.page_width, section.page_height = (Cm(29.7), Cm(21)) if landscape else (Cm(21), Cm(29.7))
        for key in ("top", "bottom", "left", "right"):
            setattr(section, key + "_margin", Cm(options[key + "_cm"]))
        section.header_distance, section.footer_distance = Cm(1.5), Cm(1.5)
    mode = options["page_numbers"]
    if mode == "keep":
        return
    odd_even = bool(document.settings.odd_and_even_pages_header_footer)
    if mode == "outside" and not odd_even:
        # Enabling odd/even footers also enables odd/even headers globally.
        # Point each even header at the inherited default header to keep its
        # content, image relationships and layout exactly intact.
        default_header = None
        for section in document.sections:
            for ref in section._sectPr.findall(qn("w:headerReference")):
                if ref.get(qn("w:type")) == "default":
                    default_header = ref.get(qn("r:id"))
            if default_header:
                for ref in list(section._sectPr.findall(qn("w:headerReference"))):
                    if ref.get(qn("w:type")) == "even":
                        section._sectPr.remove(ref)
                ref = OxmlElement("w:headerReference")
                ref.set(qn("w:type"), "even")
                ref.set(qn("r:id"), default_header)
                section._sectPr.insert(0, ref)
        document.settings.odd_and_even_pages_header_footer = True
    processed = set()
    for section in document.sections:
        default = section.footer
        if mode == "outside" and not odd_even:
            # Create a separate even footer before modifying the odd footer.
            even = section.even_page_footer
            even.is_linked_to_previous = False
            _clone_footer(default, even)
        footers = [(default, WD_ALIGN_PARAGRAPH.RIGHT if mode == "outside" else WD_ALIGN_PARAGRAPH.CENTER)]
        if document.settings.odd_and_even_pages_header_footer:
            footers.append((section.even_page_footer, WD_ALIGN_PARAGRAPH.LEFT if mode == "outside" else WD_ALIGN_PARAGRAPH.CENTER))
        if section.different_first_page_header_footer:
            footers.append((section.first_page_footer, WD_ALIGN_PARAGRAPH.RIGHT if mode == "outside" else WD_ALIGN_PARAGRAPH.CENTER))
        for footer, alignment in footers:
            identity = footer.part.partname
            if identity in processed:
                continue
            processed.add(identity)
            _page_paragraph(footer, alignment, options, _next_bookmark_id(document.part.package), warnings)


def _postflight(document, paragraphs, title, options, metadata=()):
    issues = []
    for i, section in enumerate(document.sections, 1):
        for key in ("top", "bottom", "left", "right"):
            value = getattr(section, key + "_margin")
            if value is None or abs(value.cm - options[key + "_cm"]) > 0.03:
                issues.append(f"第 {i} 节页边距复核未通过。")
        landscape = section.orientation == WD_ORIENT.LANDSCAPE
        width, height = (29.7, 21) if landscape else (21, 29.7)
        if section.page_width is None or section.page_height is None or abs(section.page_width.cm - width) > .05 or abs(section.page_height.cm - height) > .05:
            issues.append(f"第 {i} 节 A4 纸张复核未通过。")
    issues.extend(_format_violations(document, paragraphs, title, options))
    issues.extend(_page_violations(document, options))
    return issues[:_MAX_ISSUES]


def _xml_snapshot(document):
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    return {str(part.partname): etree.tostring(etree.fromstring(part.blob, parser), method="c14n")
            for part in document.part.package.parts if str(part.partname).endswith(".xml")}


def apply_official_format(document, options: dict | None = None) -> dict:
    """Format the supplied Document in place; preserve embedded content/fields."""
    options = validate_official_options(options)
    if options["operation"] == "check":
        return inspect_official_document(document, options)
    before = _xml_snapshot(document)
    audit = _base_audit(document, options)
    warnings = []
    paragraphs = _all_paragraphs(document)
    for _, paragraph, table in paragraphs:
        if table and not options["format_tables"]:
            continue
        if options["split_line_breaks"]:
            audit["counts"]["split_paragraphs"] += _split_paragraph(paragraph, warnings)
    paragraphs = _all_paragraphs(document)
    for _, paragraph, table in paragraphs:
        if table and not options["format_tables"]:
            continue
        modified = False
        if options["remove_extra_spaces"] or options["punctuation"] != "keep":
            for group in _editable_groups(paragraph):
                original = "".join(x.text or "" for x in group)
                cleaned = _clean_string(original, options)
                if original != cleaned:
                    _rewrite_group(group, cleaned)
                    modified = True
        audit["counts"]["cleaned_paragraphs"] += int(modified)
        if options["remove_blank_paragraphs"] and not table and not _visible_text(paragraph) and not any(x.tag in _BLANK_PROTECTED for x in paragraph._p.iter()):
            paragraph._p.getparent().remove(paragraph._p)
            audit["counts"]["removed_blank_paragraphs"] += 1
    _body_style(document, options)
    paragraphs = _all_paragraphs(document)
    title = _main_title(paragraphs, options)
    recognized_headings = _heading_records(paragraphs, title, options)
    targets = _layout_targets(paragraphs, title, options)
    for _, paragraph, table in paragraphs:
        target = targets.get(paragraph._p)
        if not target:
            continue
        _paragraph_format(paragraph, options, target["indent"], title=target["title"], alignment=target["alignment"], level=target["level"])
        _format_runs(paragraph, target["font"], target["size"])
    _setup_pages(document, options, warnings)
    paragraphs = _all_paragraphs(document)
    audit["headings"] = recognized_headings
    audit["corrections"] = _proofread(paragraphs, audit["headings"], options)
    audit["postflight"] = _postflight(document, paragraphs, title, options)
    audit["counts"].update({"paragraphs": len(paragraphs), "headings": len(audit["headings"]),
        "heading_levels": {str(i): sum(h["level"] == i for h in audit["headings"]) for i in range(1, 5)}})
    if audit["corrections"]:
        warnings.append(f"校对发现 {len(audit['corrections'])} 项疑似问题，仅列入报告，未自动替换。")
    if audit["postflight"]:
        warnings.append(f"处理后有 {len(audit['postflight'])} 项需人工复核。")
    warnings.append("标题识别、特殊表格、附件页、署名用印和实际分页需在 Word 中复核；程序不生成签发日期或印章。")
    after = _xml_snapshot(document)
    changes = sum(before.get(name) != data for name, data in after.items()) + sum(name not in after for name in before)
    audit["counts"]["changed_parts"] = changes
    return {"changes": changes, "warnings": warnings, "audit": audit}


def create_official_template(options: dict | None = None, title: str = "", issuer: str = "", date: str = "", doc_type: str = "通知"):
    """Create editable examples without inventing document numbers or signoffs."""
    source = validate_official_options(options)
    source["doc_type"], source["operation"] = doc_type, "format"
    options = validate_official_options(source)
    title = _text_option(title, "模板标题", allow_empty=True)
    issuer = _text_option(issuer, "署名", allow_empty=True)
    date = _text_option(date, "成文日期", allow_empty=True, limit=80)
    document = Document()
    document.add_paragraph(title or (f"关于【事项】的{doc_type}" if doc_type != "会议纪要" else "【会议名称】会议纪要"))
    examples = {
        "通知": ("为【目的】，现将有关事项通知如下。", "一、工作要求", "请【责任部门】按【时限】落实【具体事项】。", "特此通知。"),
        "请示": ("根据【依据】，现就【事项】请示如下。", "一、请示事项", "拟【具体方案】，提请审议【需批准内容】。", "妥否，请批示。"),
        "报告": ("现将【事项】有关情况报告如下。", "一、基本情况", "【填写事实、数据及存在问题】。", "特此报告。"),
        "函": ("就【商洽事项】，现函告如下。", "一、商洽事项", "请就【具体事项】予以【答复或协助】。", "请予函复。"),
        "批复": ("【来文名称及文号】收悉。经研究，现批复如下。", "一、批复事项", "【填写经批准的具体意见及办理要求】。", "此复。"),
        "会议纪要": ("【会议时间】，【主持人】主持召开【会议名称】，纪要如下。", "一、议定事项", "会议议定：【填写议定事项、责任部门和完成时限】。", "【记录须以经确认的会议事实为准】。"),
    }
    for text in examples[doc_type]:
        document.add_paragraph(text)
    document.add_paragraph(issuer or "【发文单位署名】")
    document.add_paragraph(date or "【成文日期：以实际签发或会议通过日期为准】")
    apply_official_format(document, options)
    for paragraph in document.paragraphs[-2:]:
        _paragraph_format(paragraph, options, alignment=WD_ALIGN_PARAGRAPH.RIGHT)
        _format_runs(paragraph, options["body_font"], options["body_size"])
    return document
