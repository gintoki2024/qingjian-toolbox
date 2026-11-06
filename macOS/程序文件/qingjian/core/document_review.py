"""Rule-based, offline document comparison, consistency review and DOCX chapters.

No OCR or semantic inference is performed. Sources are never overwritten. DOCX
assembly deliberately refuses constructs whose references cannot be preserved
reliably; ordinary inline images, tables, hyperlinks, styles and lists are copied.
"""
from __future__ import annotations

import contextlib
import difflib
import html
import re
import zipfile
from collections import defaultdict
from copy import deepcopy
from pathlib import Path

from .common import (CancelledError, atomic_write, ensure_cancel, expand_inputs,
                     make_result, notify, safe_name, unique_path, write_report_book)
from .content_ops import EXTENSIONS, _iter_units, _zip_check


def _field(key, title, kind="text", default="", **kwargs):
    return dict(key=key, title=title, kind=kind, default=default, **kwargs)


TOOL_SPECS = {
    "document_compare": dict(
        title="文档版本比对", nav="文档版本比对", group="材料核查",
        description="对照两份Word、电子PDF或文本，定位文字的新增、删除和修改。",
        note="队列第一份为旧版，第二份为新版。按段落、表格行或PDF页比较文字，输出HTML对照与Excel清单；不比较图片或排版，不识别扫描PDF。",
        extensions={".docx", ".pdf", ".txt", ".md"},
        fields=[_field("ignore_whitespace", "比较时忽略空白差异", "bool", True),
                _field("password", "PDF打开密码", "password", "")]),
    "material_consistency": dict(
        title="跨材料一致性核查", nav="跨材料一致性", group="材料核查",
        description="按指定关键词或正则提取字段，核对各份材料中的取值与预期。",
        note="每个字段在每份文件中都检查；所有命中保留来源。关键词方式读取同一行中关键词后的值，表格相邻单元格用制表符分隔。只执行明确规则，不判断语义；图片不识别。",
        extensions=set(EXTENSIONS),
        fields=[_field("rules", "核查字段（可添加多组）", "rules", [],
                       columns=[("name", "字段名"), ("pattern", "定位关键词或正则"), ("expected", "预期值（可留空）")]),
                _field("match_kind", "定位方式", "choice", "keyword", choices=[("关键词后取值", "keyword"), ("正则提取", "regex")]),
                _field("ignore_whitespace", "比较时忽略空白差异", "bool", True),
                _field("password", "PDF打开密码", "password", ""),
                _field("recursive", "包含子文件夹", "bool", True)]),
    "word_chapters": dict(
        title="Word章节合并与拆分", nav="Word章节整理", group="文档自动化",
        description="按标题级别拆分DOCX，或按文件队列合并成可编辑Word。",
        note="拆分依据Word标题样式或大纲级别，标题前内容另存为前言；合并按队列顺序。常规正文、表格、图片、超链接、基本样式及编号予以保留。复杂分节、页眉页脚、脚注、批注修订、域与交叉引用会明确拦截，请先整理副本。",
        extensions={".docx"},
        fields=[_field("action", "处理方式", "choice", "split", choices=[("按标题级别拆分", "split"), ("按队列顺序合并", "merge")]),
                _field("heading_level", "拆分标题级别", "int", 1, minimum=1, maximum=9, when=("action", ["split"])),
                _field("page_break", "合并时每份文档另起一页", "bool", True, when=("action", ["merge"])),
                _field("recursive", "包含子文件夹", "bool", False)]),
}


GUIDES = {
    "document_compare": dict(
        intro="检查同一材料两次修改之间的文字差异，原文保留。",
        quick="① 添加旧版和新版　② 调整顺序　③ 查看HTML对照及Excel差异。",
        steps=["添加恰好两份DOCX、电子PDF、TXT或MD文件，旧版在前、新版在后。",
               "需要区分空格、换行变化时，取消忽略空白；加密PDF填写打开密码。",
               "开始处理后打开HTML对照，按旧版和新版的位置核实差异；Excel便于筛选留痕。",
               "相同文字在不同位置移动时可能显示为删除和新增。结果不包含图片、格式或扫描文字变化。"],
        terms=[("比较单位", "Word为段落或表格行，PDF为页内文字，文本为非空行。"),
               ("修改", "相邻删除与新增按顺序配对；它是文字对照提示，需要结合原稿确认。")],
        example="示例旧稿有金额100万元，新稿改为120万元，并增加一句实施说明。"),
    "material_consistency": dict(
        intro="用明确规则核查项目名称、编号、金额等字段，列出缺失、多值和跨文件差异。",
        quick="① 添加材料　② 增加字段及定位规则　③ 查看异常与全部提取值。",
        steps=["添加需要互相核对的材料，可选DOCX、电子PDF、表格、TXT或HTML。",
               "每行增加一个字段。关键词方式例如字段名“项目名称”、关键词“项目名称”，从其后读取同一行内容。",
               "正则方式例如 项目名称[：:]\\s*([^\\s]+)，有捕获组时读取第一组。预期值可留空，留空时只核查各材料是否一致。",
               "每个字段默认应出现在每份文件中。未命中列为缺失，同一文件命中不同值列为多值。",
               "查看Excel“核查结论”和“全部命中”，根据文件、页或行位置回到原件核实。读取失败单列，不会视为一致。"],
        terms=[("关键词后取值", "忽略紧随关键词的冒号及空白，取至该行末尾；一个单元格后还有多列时建议用正则限定。"),
               ("预期值", "填写时还检查是否与指定文本一致；不自动换算金额单位或解释近义词。"),
               ("复杂正则", "为避免卡住，禁止嵌套重复、回溯引用及环视等表达式；单行正则输入最多4096字符。")],
        example="示例报告写“青禾项目”，申请表写“青和项目”，规则会列出名称不一致并保留原文。"),
    "word_chapters": dict(
        intro="把长文档按实际标题拆开，或将多份常规DOCX按顺序合并。",
        quick="① 添加DOCX　② 选择拆分或合并　③ 检查生成Word和处理清单。",
        steps=["拆分前在Word中把章节标题设置成“标题1”等实际样式，只把字号放大不算标题。",
               "选择拆分级别，例如1表示每个一级标题开始一份文件，包含其下所有正文和次级标题。标题前内容保存为前言。",
               "合并时在文件清单调整队列顺序，可选每份文档另起一页。最终页面尺寸以第一份文件为准。",
               "原件保留，生成可编辑DOCX和Excel处理清单。图片及基本样式会随正文保留。",
               "含复杂分节、页眉页脚、脚注、批注修订、域与交叉引用的文档会明确报错；请另存副本整理后再使用。生成后复核长表跨页和复杂排版。"],
        terms=[("标题级别", "使用段落大纲级别或标题样式识别，不凭文字内容猜测。"),
               ("基本样式", "保留段落、字符和表格样式；合并使用第一份文档的主题与页面尺寸，主题字体或颜色可能随之改变。")],
        example="示例文档包含前言、第一章和第二章，各章有表格与一张图片，可拆分后再次合并。"),
}


_MAX_FILE = 100 * 1024 * 1024
_MAX_UNITS = 5000
_MAX_TEXT = 2_000_000
_MAX_HITS = 50000
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _bool(options, key, default):
    value = options.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} 必须为布尔值。")
    return value


def _normalize(text, ignore=True):
    return re.sub(r"\s+", "", str(text)) if ignore else str(text)


def _safe_regex(pattern):
    """Accept a useful non-recursive extraction subset, with bounded input lines."""
    if len(pattern) > 500:
        raise ValueError("正则表达式最多500字符。")
    try:
        from re import _parser as parser, _constants as constants
    except ImportError:  # Python 3.10's supported parser names.
        import sre_parse as parser
        import sre_constants as constants
    try:
        parsed = parser.parse(pattern, 0)
    except re.error as exc:
        raise ValueError(f"无效正则表达式：{exc}") from exc
    repetitions = 0
    flattened = []
    if parsed.state.flags & ~re.UNICODE:
        raise ValueError("正则不支持内嵌匹配标志，请使用普通关键词和捕获组。")

    def inspect(items, repeated=False):
        nonlocal repetitions
        for operator, operand in items:
            if operator in {constants.ASSERT, constants.ASSERT_NOT, constants.GROUPREF, constants.GROUPREF_EXISTS}:
                raise ValueError("正则不支持环视或回溯引用，请使用明确的关键词与捕获组。")
            if operator in {constants.MAX_REPEAT, constants.MIN_REPEAT, getattr(constants, "POSSESSIVE_REPEAT", None)}:
                minimum, maximum, child = operand
                if repeated or (maximum != constants.MAXREPEAT and maximum > 4096):
                    raise ValueError("正则不支持嵌套重复或超过4096的重复次数。")
                atom = list(child)
                while len(atom) == 1 and atom[0][0] == constants.SUBPATTERN:
                    atom = list(atom[0][1][-1])
                if len(atom) != 1 or atom[0][0] not in {constants.LITERAL, constants.NOT_LITERAL, constants.IN, constants.ANY, constants.CATEGORY}:
                    raise ValueError("正则重复项只支持单个字符或字符集合，请简化规则。")
                if minimum != maximum:
                    repetitions += 1
                    flattened.append(("repeat", atom[0]))
                else:
                    flattened.append(("fixed_repeat", (atom[0], minimum)))
                inspect(child, True)
            elif operator == constants.SUBPATTERN:
                if operand[1] or operand[2]:
                    raise ValueError("正则不支持分组内匹配标志。")
                inspect(operand[-1], repeated)
            elif operator == constants.BRANCH:
                raise ValueError("正则不支持分支，请用字符集合或独立字段规则。")
            elif operator == getattr(constants, "ATOMIC_GROUP", None):
                inspect(operand, repeated)
            elif not repeated:
                flattened.append((operator, operand))
    inspect(parsed)
    if repetitions > 2:
        raise ValueError("每条正则最多含两个重复项，请简化或使用关键词定位。")
    if repetitions == 2:
        indices = [index for index, token in enumerate(flattened) if token[0] == "repeat"]
        first, second = [flattened[index][1] for index in indices]

        def accepts(atom, char):
            op, value = atom
            if op == constants.LITERAL:
                return ord(char) == value
            if op == constants.NOT_LITERAL:
                return ord(char) != value
            if op == constants.ANY:
                return True
            if op == constants.CATEGORY:
                predicates = {constants.CATEGORY_SPACE: char.isspace(), constants.CATEGORY_NOT_SPACE: not char.isspace(),
                              constants.CATEGORY_DIGIT: char.isdecimal(), constants.CATEGORY_NOT_DIGIT: not char.isdecimal(),
                              constants.CATEGORY_WORD: char.isalnum() or char == "_", constants.CATEGORY_NOT_WORD: not (char.isalnum() or char == "_")}
                return predicates.get(value, True)
            if op == constants.IN:
                negate = bool(value and value[0][0] == constants.NEGATE)
                hit = any((low <= ord(char) <= high) if item == constants.RANGE else accepts((item, entry), char)
                          for item, entry in value if item != constants.NEGATE
                          for low, high in ([entry] if item == constants.RANGE else [(0, 0)]))
                return not hit if negate else hit
            return True

        def whitespace_atom(atom, negate=False):
            op, value = atom
            if op == constants.CATEGORY:
                return value == (constants.CATEGORY_NOT_SPACE if negate else constants.CATEGORY_SPACE)
            if op == constants.IN and len(value) == 1:
                return whitespace_atom(value[0], negate)
            if op == constants.IN and len(value) == 2 and value[0][0] == constants.NEGATE:
                return whitespace_atom(value[1], not negate)
            return False

        # Multiple ambiguous repetitions can cause polynomial backtracking even
        # without nesting, e.g. a*a*b. Require a provably distinct delimiter or
        # whitespace/non-whitespace boundary before running on user text.
        separator = any(op == constants.LITERAL and not accepts(first, chr(value))
                        for op, value in flattened[indices[0] + 1:indices[1]])
        disjoint = (whitespace_atom(first) and whitespace_atom(second, True)) or (whitespace_atom(first, True) and whitespace_atom(second))
        if not separator and not disjoint:
            raise ValueError("两个重复项必须以明确固定分隔符或空白/非空白边界区分，避免回溯卡顿。")
    expression = re.compile(pattern)
    if expression.search("") is not None:
        raise ValueError("正则不能匹配空文本。")
    return expression


def _rules(options):
    rows = options.get("rules", [])
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError("请填写1至100组核查字段。")
    kind = options.get("match_kind", "keyword")
    if kind not in {"keyword", "regex"}:
        raise ValueError("未知的字段定位方式。")
    result, names = [], set()
    for row in rows:
        if not isinstance(row, dict) or not all(isinstance(row.get(key, ""), str) for key in ("name", "pattern", "expected")):
            raise ValueError("每组规则的字段名、定位规则和预期值应为文本。")
        name, pattern, expected = row.get("name", "").strip(), row.get("pattern", ""), row.get("expected", "")
        if not name or name in names or not pattern.strip():
            raise ValueError("字段名不能重复或为空，定位规则不能为空。")
        if len(name) > 200 or len(expected) > 30000 or len(pattern) > 500:
            raise ValueError("字段名最多200字符，规则最多500字符，预期值最多30000字符。")
        names.add(name)
        result.append((name, _safe_regex(pattern) if kind == "regex" else pattern, expected))
    return result


def validate(mode, options, sources=None):
    if mode not in TOOL_SPECS or not isinstance(options, dict):
        raise ValueError("未知的文档核查工具或设置格式无效。")
    _bool(options, "ignore_whitespace", True)
    _bool(options, "recursive", mode != "word_chapters")
    if not isinstance(options.get("password", ""), str):
        raise ValueError("PDF密码应为文本。")
    if mode == "document_compare" and sources is not None and len(sources) != 2:
        raise ValueError("文档版本比对请添加恰好两份文件，旧版在前、新版在后。")
    if mode == "material_consistency":
        _rules(options)
    if mode == "word_chapters":
        if options.get("action", "split") not in {"split", "merge"}:
            raise ValueError("未知的章节处理方式。")
        value = options.get("heading_level", 1)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 9:
            raise ValueError("标题级别应为1至9的整数。")
        _bool(options, "page_break", True)


def _inputs(mode, sources, folder, options):
    files = expand_inputs(sources, TOOL_SPECS[mode]["extensions"], options.get("recursive", mode != "word_chapters"), exclude_root=folder)
    if not files:
        raise ValueError("没有找到该功能支持的文件，请检查所选文件和结果目录。")
    if len(files) > 1000:
        raise ValueError("一次最多处理1000份材料，请缩小范围。")
    if mode == "document_compare" and len(files) != 2:
        raise ValueError("文档版本比对需要恰好两份文件。")
    return files


def _check_size(path):
    if path.stat().st_size > _MAX_FILE:
        raise ValueError("单文件超过100 MB，请先缩小文件。")


def _read_units(path, options, notices, cancel, max_units=_MAX_UNITS, allow_empty=False):
    _check_size(path)
    result, total = [], 0
    initial = len(notices)
    with contextlib.closing(_iter_units(path, options, notices=notices)) as units:
        for position, text in units:
            ensure_cancel(cancel)
            total += len(text)
            if len(result) >= max_units or total > _MAX_TEXT:
                raise ValueError(f"文字超过{max_units}个单元或200万字符上限，请分批处理。")
            result.append((position, text))
    if path.suffix.lower() == ".pdf" and any("疑似扫描页" in note for note in notices[initial:]):
        raise ValueError("PDF含疑似扫描页，无法完整核查文字；本工具不执行OCR，请选择可搜索的电子PDF。")
    if not result and not allow_empty:
        raise ValueError("未提取到可核查文字；空白文档或扫描PDF不支持本次核查。")
    return result


def _inline(old, new):
    if max(len(old), len(new)) > 5000:
        return "<del>" + html.escape(old) + "</del>", "<ins>" + html.escape(new) + "</ins>"
    left, right = [], []
    for tag, a, b, c, d in difflib.SequenceMatcher(None, old, new, autojunk=True).get_opcodes():
        x, y = html.escape(old[a:b]), html.escape(new[c:d])
        left.append(x if tag == "equal" else "<del>" + x + "</del>")
        right.append(y if tag == "equal" else "<ins>" + y + "</ins>")
    return "".join(left), "".join(right)


def _cell_parts(prefix, old, new):
    # Preserve long page/paragraph text without exceeding Excel's cell limit.
    count = max(1, (max(len(old), len(new)) + 29999) // 30000)
    for index in range(count):
        row = list(prefix)
        if count > 1:
            row[0] += f"（原文片段{index + 1}/{count}）"
        yield row + [old[index * 30000:(index + 1) * 30000], new[index * 30000:(index + 1) * 30000]]


def _compare(files, folder, options, progress, cancel):
    warnings = []
    old, new = [_read_units(path, options, warnings, cancel, allow_empty=True) for path in files]
    ignore = options.get("ignore_whitespace", True)
    left_keys = [_normalize(text, ignore) for _, text in old]
    right_keys = [_normalize(text, ignore) for _, text in new]
    rows = [["类型", "旧版位置", "新版位置", "旧版原文", "新版原文"]]
    view, count = [], 0
    for tag, a, b, c, d in difflib.SequenceMatcher(None, left_keys, right_keys, autojunk=True).get_opcodes():
        ensure_cancel(cancel)
        for index in range(max(b - a, d - c)):
            li, ri = a + index, c + index
            lp, lt = old[li] if li < b else ("", "")
            rp, rt = new[ri] if ri < d else ("", "")
            status = "相同" if tag == "equal" else ("新增" if li >= b else "删除" if ri >= d else "修改")
            if status != "相同":
                count += 1
                rows.extend(_cell_parts([status, lp, rp], lt, rt))
            lhtml, rhtml = _inline(lt, rt) if status == "修改" else (html.escape(lt), html.escape(rt))
            view.append(f'<tr class="s{tag}"><td>{status}</td><td>{html.escape(lp)}</td><td>{lhtml}</td><td>{html.escape(rp)}</td><td>{rhtml}</td></tr>')
        notify(progress, b + d, len(old) + len(new), "生成文字对照")
    ensure_cancel(cancel)
    target = unique_path(folder, "文档版本对照.html")
    page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>文档版本对照</title>'
    page += '<style>body{font:14px "Microsoft YaHei",sans-serif;margin:28px;color:#27334b}table{border-collapse:collapse;width:100%;table-layout:fixed}th,td{border:1px solid #dbe2eb;padding:10px;vertical-align:top;white-space:pre-wrap;overflow-wrap:anywhere}th{background:#eef2ff}td:first-child{width:50px}.sinsert{background:#effaf1}.sdelete{background:#fff0f0}.sreplace{background:#fffbeb}del{background:#ffd6d6}ins{background:#c9f0d1;text-decoration:none}td:nth-child(2),td:nth-child(4){width:100px}</style>'
    page += f'<h1>文档版本对照</h1><p>旧版：{html.escape(str(files[0]))}<br>新版：{html.escape(str(files[1]))}</p>'
    page += f'<p>发现{count}个文字差异单元；忽略空白：{"是" if ignore else "否"}。只比较提取文字，不比较图片、版式或语义。移动内容可能显示为删除及新增。</p>'
    page += '<table><thead><tr><th>类型</th><th>旧版位置</th><th>旧版原文</th><th>新版位置</th><th>新版原文</th></tr></thead><tbody>' + "".join(view) + '</tbody></table></html>'
    atomic_write(target, lambda p: p.write_text(page, encoding="utf-8"))
    report = unique_path(folder, "文档版本差异.xlsx")
    write_report_book(report, {"文字差异": rows, "比较说明": [["项目", "值"], ["旧版", str(files[0])], ["新版", str(files[1])], ["差异单元", count], ["忽略空白", ignore], ["比较范围", "提取文字；不含图片、版式、语义判断"], *[["提示", x] for x in warnings]]})
    result = make_result([target, report], count, warnings)
    result["data_outputs"] = [str(target), str(report)]
    return result


def _matches(text, expression, kind):
    if kind == "regex":
        if len(text) > 4096:
            raise ValueError("单行超过4096字符，超出安全正则范围；请改用关键词或先将材料分行。")
        for match in expression.finditer(text):
            value = match.group(1) if expression.groups else match.group(0)
            yield "" if value is None else value
    else:
        start = 0
        while True:
            found = text.find(expression, start)
            if found < 0:
                break
            yield text[found + len(expression):].lstrip(" \t：:")
            start = found + len(expression)


def _consistency(files, folder, options, progress, cancel):
    rules = _rules(options)
    kind, ignore = options.get("match_kind", "keyword"), options.get("ignore_whitespace", True)
    warnings, errors, failed, hits = [], [], set(), []
    values = defaultdict(list)
    for index, path in enumerate(files, 1):
        ensure_cancel(cancel)
        try:
            units = _read_units(path, options, warnings, cancel)
            pending, pending_values = [], defaultdict(list)
            for position, text in units:
                for line_index, line in enumerate(text.splitlines(), 1):
                    ensure_cancel(cancel)
                    if len(line) > 30000:
                        raise ValueError("单行文字超过30000字符，请先拆分文本。")
                    loc = position + (f" 内第{line_index}行" if "\n" in text else "")
                    for name, expression, expected in rules:
                        for value in _matches(line, expression, kind):
                            if len(hits) + len(pending) >= _MAX_HITS:
                                raise ValueError("命中总数超过50000条上限，请缩小材料或细化规则。")
                            pending.append([str(path), name, value, loc, line])
                            pending_values[(str(path), name)].append(value)
            hits.extend(pending)
            for key, extracted in pending_values.items():
                values[key].extend(extracted)
        except CancelledError:
            raise
        except Exception as exc:
            failed.add(str(path))
            errors.append(f"{path}：{exc}")
        notify(progress, index, len(files), f"核查：{path.name}")
    findings, anomalies = [["来源文件", "字段名", "结论", "不同取值数量", "提取值", "预期值"]], 0
    for name, expression, expected in rules:
        all_values = {_normalize(value, ignore) for (path, field), entries in values.items() if field == name for value in entries if _normalize(value, ignore)}
        for path in files:
            ensure_cancel(cancel)
            entries = values.get((str(path), name), [])
            distinct = list(dict.fromkeys(entries))
            normalized = {_normalize(value, ignore) for value in entries}
            reasons = []
            if str(path) in failed:
                reasons.append("未核查（读取失败）")
            elif not entries:
                reasons.append("缺失")
            else:
                if "" in normalized:
                    reasons.append("空值")
                if len(normalized) > 1:
                    reasons.append("文件内多值")
                if expected and any(value != _normalize(expected, ignore) for value in normalized):
                    reasons.append("不符预期")
                if len(all_values) > 1:
                    reasons.append("跨材料不一致")
            if reasons:
                anomalies += 1
            joined = "\n".join(distinct)
            # Individual occurrences remain complete on the all-hits sheet.
            if len(joined) > 30000:
                joined = "取值较多，请按来源文件和字段筛选“全部命中”查看完整值。"
            findings.append([str(path), name, "；".join(reasons) if reasons else "一致", len(normalized), joined, expected])
    ensure_cancel(cancel)
    target = unique_path(folder, "跨材料一致性核查.xlsx")
    write_report_book(target, {"核查结论": findings, "全部命中": [["来源文件", "字段名", "提取值", "来源位置", "原文"], *hits],
                              "核查规则": [["字段名", "定位方式", "定位规则", "预期值"], *[[name, kind, expression.pattern if kind == "regex" else expression, expected] for name, expression, expected in rules]],
                              "读取提示": [["类型", "说明"], *[["提示", x] for x in warnings], *[["异常", x] for x in errors]],
                              "范围说明": [["项目", "说明"], ["检查方式", "每个字段在每份材料中核查；仅按明确规则提取，不判断语义。"], ["命中数量", len(hits)], ["异常字段文件组合", anomalies], ["忽略空白", ignore], ["失败材料", len(failed)], ["图片文字", "不识别；扫描PDF明确报错，不视为核查通过。"]]})
    result = make_result([target], len(hits), warnings, errors)
    result["data_outputs"] = [str(target)]
    return result


def _q(name):
    return "{" + _W + "}" + name


def _docx_safety(document):
    body = document.element.body
    tags = {element.tag for element in body.iter()}
    reasons = []
    if len(list(body.iter(_q("sectPr")))) > 1:
        reasons.append("复杂分节")
    if tags & {_q("headerReference"), _q("footerReference")}:
        reasons.append("页眉或页脚")
    features = {"脚注或尾注": {"footnoteReference", "endnoteReference"},
                "批注或修订": {"commentReference", "commentRangeStart", "commentRangeEnd", "ins", "del", "moveFrom", "moveTo", "pPrChange", "rPrChange", "tblPrChange", "sectPrChange"},
                "域或交叉引用": {"fldSimple", "instrText"},
                "嵌入对象或外部正文": {"object", "altChunk"}}
    for label, names in features.items():
        if tags & {_q(name) for name in names}:
            reasons.append(label)
    if reasons:
        raise ValueError("为完整保留内容，暂不处理含" + "、".join(reasons) + "的DOCX；请另存副本整理后重试，原件未修改。")
    if any(rel.is_external and rel.reltype.endswith("/image") for rel in document.part.rels.values()):
        raise ValueError("文档含外部链接图片，本地离线无法保证图片完整；请先在Word中嵌入图片后重试。")
    if len(body) > 30000:
        raise ValueError("正文块超过30000个，请按较小范围整理。")


def _load_docx(path):
    from docx import Document
    _check_size(path)
    _zip_check(path)
    document = Document(path)
    _docx_safety(document)
    return document


def _heading_level(element, styles):
    if element.tag != _q("p"):
        return None
    ppr = element.find(_q("pPr"))
    style_id = "Normal"
    if ppr is not None:
        outline = ppr.find(_q("outlineLvl"))
        if outline is not None:
            return int(outline.get(_q("val"), "9")) + 1
        style = ppr.find(_q("pStyle"))
        if style is not None:
            style_id = style.get(_q("val"), "Normal")
    seen = set()
    while style_id and style_id not in seen:
        seen.add(style_id)
        style = styles.get(style_id)
        if style is None:
            break
        ppr = style.find(_q("pPr"))
        if ppr is not None:
            outline = ppr.find(_q("outlineLvl"))
            if outline is not None:
                return int(outline.get(_q("val"), "9")) + 1
        name = style.find(_q("name"))
        match = re.fullmatch(r"(?:heading\s*|标题\s*)([1-9])", name.get(_q("val"), "") if name is not None else style_id, re.I)
        if match:
            return int(match.group(1))
        parent = style.find(_q("basedOn"))
        style_id = parent.get(_q("val")) if parent is not None else None
    return None


def _audit_relationships(document):
    """Validate every referenced document relationship before committing output."""
    names = {str(part.partname) for part in document.part.package.parts}
    for part in document.part.package.parts:
        for rel in part.rels.values():
            if not rel.is_external and str(rel.target_part.partname) not in names:
                raise ValueError("文档关系未完整保存，未输出不完整文件。")
    for element in document.element.iter():
        for attribute, value in element.attrib.items():
            if attribute.startswith("{" + _R + "}") and value not in document.part.rels:
                raise ValueError("正文图片或链接关系不存在，未输出不完整文件。")


def _save_docx(document, target):
    from docx import Document
    _audit_relationships(document)
    def writer(staged):
        document.save(staged)
        # Re-open the actual ZIP so a bad relationship is caught before publish.
        reopened = Document(staged)
        _audit_relationships(reopened)
    atomic_write(target, writer)


def _split_word(path, folder, options, records, outputs, cancel):
    document = _load_docx(path)
    body = document.element.body
    blocks = [node for node in body if node.tag != _q("sectPr")]
    styles = {node.get(_q("styleId")): node for node in document.styles.element if node.tag == _q("style")}
    level = options.get("heading_level", 1)
    starts = [index for index, node in enumerate(blocks) if _heading_level(node, styles) == level]
    if not starts:
        raise ValueError(f"没有找到第{level}级Word标题，请设置标题样式或大纲级别后重试。")
    bounds = ([0] if starts[0] else []) + starts + [len(blocks)]
    if len(bounds) - 1 > 200:
        raise ValueError("一次拆分最多生成200份Word，请提高拆分级别或缩小文档。")
    chapter_of = {index: number for number, (start, end) in enumerate(zip(bounds, bounds[1:])) for index in range(start, end)}
    bookmark_chapter = {node.get(_q("name")): chapter_of[index]
                        for index, block in enumerate(blocks) for node in block.iter(_q("bookmarkStart"))}
    for index, block in enumerate(blocks):
        for link in block.iter(_q("hyperlink")):
            anchor = link.get(_q("anchor"))
            if anchor and bookmark_chapter.get(anchor) != chapter_of[index]:
                raise ValueError("文档含跨章节或无效的书签链接；拆分会使链接失效，请在副本中整理后重试。")
    for number, (start, end) in enumerate(zip(bounds, bounds[1:]), 1):
        ensure_cancel(cancel)
        title = "前言" if start == 0 and starts[0] else "".join(blocks[start].itertext()).strip()
        # w:t only prevents python-docx's XML text properties duplicating words.
        if title != "前言":
            title = "".join(node.text or "" for node in blocks[start].iter(_q("t"))).strip() or f"章节{number}"
        copy = deepcopy(document)
        copied_blocks = [node for node in copy.element.body if node.tag != _q("sectPr")]
        for index, node in enumerate(copied_blocks):
            if index < start or index >= end:
                copy.element.body.remove(node)
        # Removed chapters must not leave their unused pictures/charts attached.
        referenced = {value for node in copy.element.iter() for attribute, value in node.attrib.items()
                      if attribute.startswith("{" + _R + "}")}
        for rid, relation in list(copy.part.rels.items()):
            if rid not in referenced and relation.reltype.rsplit("/", 1)[-1] in {"image", "chart", "diagramData", "diagramLayout", "diagramColors", "diagramQuickStyle", "hyperlink"}:
                del copy.part.rels[rid]
        target = unique_path(folder, f"{path.stem}_{number:03d}_{safe_name(title, limit=65)}.docx")
        _save_docx(copy, target)
        outputs.append(str(target))
        records.append([str(path), f"正文块{start + 1}至{end}", "已拆分", str(target), title])


class _PartCloner:
    def __init__(self, package, prefix):
        self.package, self.prefix, self.cache = package, prefix, {}
        self.names = {str(part.partname) for part in package.parts}

    def clone(self, part):
        from docx.opc.part import Part
        from docx.opc.packuri import PackURI
        identity = id(part)
        if identity in self.cache:
            return self.cache[identity]
        original = Path(str(part.partname))
        number = 1
        while True:
            candidate = str(original.parent).replace("\\", "/") + f"/{self.prefix}_{number}_{original.name}"
            if candidate not in self.names:
                break
            number += 1
        self.names.add(candidate)
        new = Part(PackURI(candidate), part.content_type, part.blob, self.package)
        self.cache[identity] = new
        for rel in part.rels.values():
            target = rel.target_ref if rel.is_external else self.clone(rel.target_part)
            new.rels.add_relationship(rel.reltype, target, rel.rId, rel.is_external)
        return new

    def relationships(self, source, destination, elements):
        mapping = {}
        for element in elements:
            for descendant in element.iter():
                for attribute, value in list(descendant.attrib.items()):
                    if not attribute.startswith("{" + _R + "}"):
                        continue
                    if value not in source.rels:
                        raise ValueError("来源正文包含无效的图片或链接关系。")
                    if value not in mapping:
                        relation = source.rels[value]
                        target = relation.target_ref if relation.is_external else self.clone(relation.target_part)
                        mapping[value] = destination.relate_to(target, relation.reltype, relation.is_external)
                    descendant.set(attribute, mapping[value])


def _remap_xml(elements, styles, numbers, abstract=None, pictures=None):
    style_tags = {_q(x) for x in ("pStyle", "rStyle", "tblStyle", "basedOn", "next", "link", "numStyleLink", "styleLink")}
    for element in elements:
        for node in element.iter():
            value = node.get(_q("val"))
            if node.tag in style_tags and value in styles:
                node.set(_q("val"), styles[value])
            elif node.tag == _q("numId") and value in numbers:
                node.set(_q("val"), numbers[value])
            elif node.tag == _q("abstractNumId") and abstract and value in abstract:
                node.set(_q("val"), abstract[value])
            elif node.tag == _q("lvlPicBulletId") and pictures and value in pictures:
                node.set(_q("val"), pictures[value])


def _merge_import(destination, source, number):
    from docx.oxml import OxmlElement
    prefix = f"qj{number}"
    cloner = _PartCloner(destination.part.package, prefix)
    source_styles = [node for node in source.styles.element if node.tag == _q("style")]
    used = {node.get(_q("styleId")) for node in destination.styles.element if node.tag == _q("style")}
    style_map = {}
    for node in source_styles:
        old = node.get(_q("styleId"))
        new = prefix + "_" + old
        while new in used:
            new = "_" + new
        style_map[old], used = new, used | {new}
    source_num, dest_num = source.part.numbering_part, destination.part.numbering_part
    num_maps = []
    for tag, attribute in (("num", "numId"), ("abstractNum", "abstractNumId"), ("numPicBullet", "numPicBulletId")):
        existing = [int(node.get(_q(attribute))) for node in dest_num.element if node.tag == _q(tag)]
        next_value = max(existing, default=0) + 1
        originals = [node for node in source_num.element if node.tag == _q(tag)]
        num_maps.append({node.get(_q(attribute)): str(next_value + index) for index, node in enumerate(originals)})
    numbers, abstracts, pictures = num_maps
    imported_numbers = [deepcopy(node) for node in source_num.element if node.tag in {_q("num"), _q("abstractNum"), _q("numPicBullet")}]
    for node in imported_numbers:
        attribute, mapping = ("numId", numbers) if node.tag == _q("num") else (("abstractNumId", abstracts) if node.tag == _q("abstractNum") else ("numPicBulletId", pictures))
        node.set(_q(attribute), mapping[node.get(_q(attribute))])
    _remap_xml(imported_numbers, style_map, numbers, abstracts, pictures)
    cloner.relationships(source_num, dest_num, imported_numbers)
    # OOXML ordering is numPicBullet, abstractNum, num. Insert in that order.
    for tag in ("numPicBullet", "abstractNum", "num"):
        for node in imported_numbers:
            if node.tag != _q(tag):
                continue
            if tag == "num":
                dest_num.element.append(node)
            else:
                anchor = next((child for child in dest_num.element if child.tag == (_q("abstractNum") if tag == "numPicBullet" else _q("num"))), None)
                if anchor is None:
                    dest_num.element.append(node)
                else:
                    anchor.addprevious(node)
    imported_styles = [deepcopy(node) for node in source_styles]
    for node in imported_styles:
        node.set(_q("styleId"), style_map[node.get(_q("styleId"))])
        node.attrib.pop(_q("default"), None)
        label = node.find(_q("name"))
        if label is not None:
            label.set(_q("val"), f"文档{number}·" + label.get(_q("val"), "样式"))
    _remap_xml(imported_styles, style_map, numbers, abstracts, pictures)
    for node in imported_styles:
        destination.styles.element.append(node)
    blocks = [deepcopy(node) for node in source.element.body if node.tag != _q("sectPr")]
    # An implicit Normal style must resolve to the source's Normal, not the base.
    normal = next((node.get(_q("styleId")) for node in source_styles if node.get(_q("type")) == "paragraph" and node.get(_q("default")) == "1"), "Normal")
    table_default = next((node.get(_q("styleId")) for node in source_styles if node.get(_q("type")) == "table" and node.get(_q("default")) == "1"), None)
    for block in blocks:
        for p in block.iter(_q("p")):
            ppr = p.get_or_add_pPr()
            if ppr.find(_q("pStyle")) is None and normal in style_map:
                style = OxmlElement("w:pStyle")
                style.set(_q("val"), normal)
                ppr.insert(0, style)
        if table_default:
            for table in block.iter(_q("tbl")):
                properties = table.find(_q("tblPr"))
                if properties is not None and properties.find(_q("tblStyle")) is None:
                    style = OxmlElement("w:tblStyle")
                    style.set(_q("val"), table_default)
                    properties.insert(0, style)
    _remap_xml(blocks, style_map, numbers, abstracts, pictures)
    cloner.relationships(source.part, destination.part, blocks)
    # Bookmark IDs/names are document-scoped. Rename imported anchors together.
    bookmark_names, bookmark_ids = {}, {}
    existing_ids = [int(node.get(_q("id"))) for node in destination.element.iter(_q("bookmarkStart")) if str(node.get(_q("id"), "")).isdigit()]
    next_id = max(existing_ids, default=0) + 1
    for block in blocks:
        for node in block.iter(_q("bookmarkStart")):
            original_id, original_name = node.get(_q("id")), node.get(_q("name"), "")
            bookmark_ids[original_id] = str(next_id)
            next_id += 1
            bookmark_names[original_name] = prefix + "_" + original_name
            node.set(_q("id"), bookmark_ids[original_id])
            node.set(_q("name"), bookmark_names[original_name])
    for block in blocks:
        for node in block.iter():
            if node.tag == _q("bookmarkEnd") and node.get(_q("id")) in bookmark_ids:
                node.set(_q("id"), bookmark_ids[node.get(_q("id"))])
            if node.tag == _q("hyperlink") and node.get(_q("anchor")) in bookmark_names:
                node.set(_q("anchor"), bookmark_names[node.get(_q("anchor"))])
    # Word drawing IDs must be unique within the assembled document.
    drawing_id = 1 + max([int(node.get("id")) for node in destination.element.iter() if node.tag.endswith("}docPr") and str(node.get("id", "")).isdigit()], default=0)
    for block in blocks:
        for node in block.iter():
            if node.tag.endswith("}docPr"):
                node.set("id", str(drawing_id))
                drawing_id += 1
        destination.element.body.insert_element_before(block, "w:sectPr")


def _chapters(files, folder, options, progress, cancel):
    records, outputs, errors, warnings = [], [], [], []
    if options.get("action", "split") == "split":
        for index, path in enumerate(files, 1):
            ensure_cancel(cancel)
            try:
                _split_word(path, folder, options, records, outputs, cancel)
            except CancelledError:
                raise
            except Exception as exc:
                errors.append(f"{path}：{exc}")
                records.append([str(path), "", "失败", "", str(exc)])
            notify(progress, index, len(files), f"拆分：{path.name}")
    else:
        if len(files) < 2:
            raise ValueError("合并请添加至少两份DOCX，按队列顺序执行。")
        if len(files) > 100 or sum(path.stat().st_size for path in files) > 200 * 1024 * 1024:
            raise ValueError("合并最多100份文档且文件合计不超过200 MB，请分批处理。")
        # Preflight all sources before changing anything or publishing partial merge.
        documents = []
        total, expanded = 0, 0
        for path in files:
            ensure_cancel(cancel)
            with zipfile.ZipFile(path) as archive:
                expanded += sum(item.file_size for item in archive.infolist())
            if expanded > 512 * 1024 * 1024:
                raise ValueError("合并文档展开后合计超过512 MB，请分批处理。")
            document = _load_docx(path)
            total += len(document.element.body)
            if total > 30000:
                raise ValueError("合并最多100份文档且正文合计不超过30000块，请分批处理。")
            documents.append(document)
        merged = documents[0]
        for index, document in enumerate(documents[1:], 2):
            ensure_cancel(cancel)
            if options.get("page_break", True):
                merged.add_page_break()
            _merge_import(merged, document, index)
            notify(progress, index, len(files), f"合并：{files[index - 1].name}")
        ensure_cancel(cancel)
        target = unique_path(folder, "Word合并结果.docx")
        _save_docx(merged, target)
        outputs.append(str(target))
        records.extend([str(path), f"队列第{index}份", "已合并", str(target), ""] for index, path in enumerate(files, 1))
        warnings.append("合并使用第一份文档的页面设置与主题；主题字体和颜色、长表跨页及复杂排版请打开成稿复核。")
    ensure_cancel(cancel)
    report = unique_path(folder, "Word章节处理清单.xlsx")
    write_report_book(report, {"处理清单": [["来源文件", "来源位置", "结果", "输出", "说明"], *records],
                             "使用提示": [["类型", "说明"], ["说明", "输出可编辑DOCX，原件保留。"], *[["提示", x] for x in warnings], *[["异常", x] for x in errors]]})
    result = make_result([*outputs, str(report)], len(outputs), warnings, errors)
    result["data_outputs"] = outputs
    return result


def run(mode, sources, folder, options, progress=None, cancel=None):
    validate(mode, options, sources)
    ensure_cancel(cancel)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    files = _inputs(mode, sources, folder, options)
    if mode == "document_compare":
        return _compare(files, folder, options, progress, cancel)
    if mode == "material_consistency":
        return _consistency(files, folder, options, progress, cancel)
    return _chapters(files, folder, options, progress, cancel)


def build_example(mode, folder):
    from docx import Document
    from openpyxl import Workbook
    if mode not in TOOL_SPECS:
        raise ValueError("未知的示例工具。")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if mode == "document_compare":
        sources = []
        for name, amount, extra in (("旧版", "100", ""), ("新版", "120", "增加阶段验收。")):
            document = Document()
            document.add_heading("青禾项目说明（虚构示例）", 1)
            document.add_paragraph("项目名称：青禾项目")
            document.add_paragraph(f"预算：{amount}万元")
            if extra:
                document.add_paragraph(extra)
            table = document.add_table(rows=1, cols=2)
            table.cell(0, 0).text = "负责人"
            table.cell(0, 1).text = "示例人员甲"
            path = unique_path(folder, name + ".docx")
            atomic_write(path, document.save)
            sources.append(str(path))
        return sources, {"ignore_whitespace": True, "password": ""}
    if mode == "material_consistency":
        document = Document()
        document.add_heading("青禾项目报告（虚构示例）", 1)
        document.add_paragraph("项目名称：青禾项目")
        document.add_paragraph("项目编号：QH-001")
        first = unique_path(folder, "项目报告.docx")
        atomic_write(first, document.save)
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["项目名称", "青和项目"])
        sheet.append(["项目编号", "QH-001"])
        second = unique_path(folder, "申请信息.xlsx")
        try:
            atomic_write(second, workbook.save)
        finally:
            workbook.close()
        return [str(first), str(second)], {"rules": [{"name": "项目名称", "pattern": "项目名称", "expected": "青禾项目"}, {"name": "项目编号", "pattern": "项目编号", "expected": ""}], "match_kind": "keyword", "ignore_whitespace": True, "recursive": True, "password": ""}
    from PIL import Image
    image = unique_path(folder, "示例图.png")
    atomic_write(image, lambda path: Image.new("RGB", (160, 80), "#5c6bc0").save(path))
    document = Document()
    document.add_paragraph("本文件为虚构练习材料，可按一级标题拆分。")
    for index in (1, 2):
        document.add_heading(f"第{index}章 示例事项", 1)
        document.add_paragraph(f"这是第{index}章的可编辑正文。")
        document.add_picture(str(image))
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "材料"
        table.cell(0, 1).text = "已准备"
    path = unique_path(folder, "章节练习.docx")
    atomic_write(path, document.save)
    return [str(path)], {"action": "split", "heading_level": 1, "page_break": True, "recursive": False}
