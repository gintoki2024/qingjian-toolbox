"""Rule-based spreadsheet checks, conservative lookup, and print-copy layout.

No formula is executed here. Audits and lookups use saved Excel caches; PDF
export receives a static, macro-free copy with external workbook links removed.
"""
from __future__ import annotations

import ast
import io
import posixpath
import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation, localcontext
from html import escape
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter, quote_sheetname, range_boundaries

from . import conversion, excel_ops
from .common import CancelledError, atomic_write, ensure_cancel, expand_inputs, make_result, notify, unique_path

_N = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_MAX_CELLS = 2_000_000
_MAX_CHECKS = 500_000
_REF = re.compile(r"\{([^{}]+)\}|\[([^\[\]]+)\]")
_CELL = re.compile(r"\$?([A-Za-z]{1,3})\$?([1-9][0-9]{0,6})\Z")
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


def _field(key, title, kind="text", default="", **extra):
    return dict(key=key, title=title, kind=kind, default=default, **extra)


TOOL_SPECS = {
    "excel_audit": dict(title="数字勾稽与计算检查", nav="数字勾稽检查", group="表格处理", extensions={".xlsx"},
        description="按自定义规则检查行内合计、比例和指定单元格关系，定位数字与公式异常。",
        note="行内规则如 {合计} = {收入}+{补贴}；单元格规则如 [B8] = SUM([B2:B7])，左右分别填写。仅用已保存的公式缓存，不重算；空值、错误值和缺缓存单独报告。",
        fields=[_field("sheet", "工作表名称", placeholder="留空读取第一张可见页"),
                _field("header_row", "表头所在行", "int", 1, minimum=1, maximum=1000),
                _field("rules", "勾稽规则", "rules", [], columns=[("name", "规则名称"), ("left", "左侧算式"), ("right", "右侧算式")]),
                _field("numeric_tolerance", "数值允许误差", "float", 0.01, minimum=0, maximum=1e12, decimals=6),
                _field("recursive", "包含子文件夹", "bool", True)]),
    "excel_lookup": dict(title="多表匹配与字段回填", nav="多表匹配回填", group="表格处理", extensions={".xlsx", ".xlsm", ".csv", ".tsv"},
        description="按联合主键把参考表中的多个字段回填到目标表，保留冲突与未匹配清单。",
        note="队列第一份为目标表，第二份为参考表。重复键、空键不强行匹配；默认只填空值。结果为标准值表，不保留原样式、公式、图表或宏，原文件不改动。",
        fields=[_field("key_mappings", "主键对应关系", "rules", [], columns=[("target", "目标表列名"), ("source", "参考表列名")]),
                _field("fill_mappings", "回填字段对应关系", "rules", [], columns=[("target", "目标列名（可新增）"), ("source", "参考表列名")]),
                _field("sheet_target", "目标工作表"), _field("sheet_reference", "参考工作表"),
                _field("header_row", "表头所在行", "int", 1, minimum=1, maximum=1000),
                _field("trim_keys", "主键忽略首尾空白", "bool", True),
                _field("overwrite", "允许覆盖冲突原值（仍记录原值）", "bool", False)]),
    "excel_print": dict(title="表格打印排版与PDF", nav="表格打印排版", group="表格处理", extensions={".xlsx"},
        description="批量生成设置纸张、方向、重复表头和页边距的打印副本，可同时导出PDF。",
        note="仅处理XLSX，保留原件和公式缓存。PDF使用已保存的静态值，不运行宏或更新外链；缺公式缓存、含活动连接或不兼容结构时明确提示。",
        fields=[_field("sheet", "工作表名称", placeholder="留空调整所有可见工作表"),
                _field("paper", "纸张", "choice", "A4", choices=[("A4", "A4"), ("A3", "A3"), ("Letter", "LETTER")]),
                _field("orientation", "方向", "choice", "portrait", choices=[("纵向", "portrait"), ("横向", "landscape")]),
                _field("fit_width", "缩放为一页宽（高度不限）", "bool", True),
                _field("repeat_rows", "每页重复表头行", "text", "1:1", placeholder="例如 1:2；留空取消"),
                _field("print_area", "打印区域", "text", "", placeholder="例如 A1:H60；留空保留原设置"),
                _field("margin_cm", "四边页边距（厘米）", "float", 1.5, minimum=0, maximum=5, decimals=2),
                _field("output_pdf", "同时生成PDF", "bool", False),
                _field("recursive", "包含子文件夹", "bool", True)]),
}

GUIDES = {
    "excel_audit": dict(intro="把需要成立的数字关系写成规则，逐条核验并保留来源位置。",
        quick="① 添加XLSX　② 填写左右算式　③ 查看核验明细与公式异常。",
        steps=["第一行有表头时保留默认表头行；可指定工作表。", "每条规则填写名称、左侧和右侧。{列名}逐行检查；[B8]、SUM([B2:B7])整表检查一次。", "支持加减乘除、括号、SUM、ABS、ROUND，例如 {增长率}=({本期}/{上期}-1)*100。", "先查看异常和公式检查页。通过只表示保存的数值符合规则，不保证缓存最新。"],
        terms=[("允许误差", "左右差值绝对值小于等于此值即通过；使用十进制计算。"), ("空值与缺缓存", "均记录异常，不当作0。请在Excel重算保存后再核验。"), ("SUM区域", "区域内每格都须有合法数字，含空格也会提示。")],
        example="示例包含正确合计、错误合计和缺失金额；两条规则同时检查行内合计和单元格总计。"),
    "excel_lookup": dict(intro="根据能唯一识别记录的字段，把参考表中的信息补到目标表。",
        quick="① 目标表在前、参考表在后　② 配置主键与回填字段　③ 查看回填结果和异常。",
        steps=["添加恰好两份文件，用上移/下移调整顺序。", "主键对应关系可添加多行，例如目标“员工编号”对应参考“工号”；多行组成联合主键。", "回填字段每行选择目标列和参考列。目标列可填写新名称。", "默认保留已有内容；勾选允许覆盖后也会完整记录原值。重复或空键、未匹配、冲突分别列出。"],
        terms=[("主键类型", "文本001与数字1不同，文本1与数字1也不自动混同；先统一源表类型。"), ("冲突", "目标已有非空内容且与参考不同，默认不覆盖。"), ("标准值表", "导出数据及审计清单，原表不修改；不保留公式、样式、宏、图形。")],
        example="示例用编号补部门和电话，包含未匹配、重复参考编号和已有值冲突。"),
    "excel_print": dict(intro="把常见打印设置批量写入副本，避免逐个打开调整。",
        quick="① 添加XLSX　② 选择纸张与表头　③ 打开打印副本或PDF确认分页。",
        steps=["留空工作表名称时调整所有可见页，隐藏页不改。", "按需要选横向、一页宽、重复表头行，如1:2。打印区域留空沿用原值。", "开始后生成独立打印副本，原文件始终保留。", "需要PDF时勾选同时生成PDF。使用已保存的公式值；复杂图形、字体和分页仍应通过预览核对。"],
        terms=[("一页宽", "所有列缩为一页，行数不限；列很多时字号会变小。"), ("静态PDF", "不重算公式、不刷新外链；源表请先重算保存。"), ("XLS兼容", "旧XLS、XLSM请先另存为无宏XLSX；活动连接、嵌入对象不用于PDF导出。")],
        example="示例包含80条记录，默认A4横向、一页宽、重复第一行。"),
}


def _integer(value, label, lower=1, upper=1000):
    if isinstance(value, bool):
        raise ValueError(label + "必须为整数。")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(label + "必须为整数。") from exc
    if str(result) != str(value).strip() or not lower <= result <= upper:
        raise ValueError(f"{label}必须在{lower}至{upper}之间。")
    return result


def _decimal(value, label="数值"):
    if value is None or isinstance(value, str) and not value.strip():
        raise ValueError(label + "为空，未按0处理。")
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(label + "不是合法数字。")
    text = str(value).strip()
    if len(text) > 256 or not _NUMBER.fullmatch(text):
        raise ValueError(label + "不是合法数字：" + text[:100])
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(label + "不是合法数字。") from exc
    if not number.is_finite() or number and abs(number.adjusted()) > 100:
        raise ValueError(label + "超出有限数值范围。")
    return number


def _range(value):
    parts = str(value).split(":")
    if len(parts) not in {1, 2} or any(not _CELL.fullmatch(part) for part in parts):
        raise ValueError("单元格引用仅支持A1或A1:B20，不支持跨表、外链及整列区域。")
    left, top, right, bottom = range_boundaries(str(value).upper())
    if not 1 <= left <= right <= 16384 or not 1 <= top <= bottom <= 1048576:
        raise ValueError("单元格区域超出Excel范围或顺序颠倒。")
    if (right - left + 1) * (bottom - top + 1) > 100_000:
        raise ValueError("每个SUM区域最多100000个单元格。")
    return left, top, right, bottom


class _Expression:
    def __init__(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 2048:
            raise ValueError("算式须填写1至2048个字符。")
        self.refs = {}
        kinds = set()
        def replace(match):
            key = "_v" + str(len(self.refs))
            kind = "row" if match.group(1) is not None else "cell"
            value = (match.group(1) or match.group(2)).strip()
            if not value:
                raise ValueError("引用列名或单元格不能为空。")
            if kind == "cell":
                _range(value)
            self.refs[key] = kind, value
            kinds.add(kind)
            return key
        self.text = _REF.sub(replace, text)
        if "{" in self.text or "}" in self.text or "[" in self.text or "]" in self.text:
            raise ValueError("引用括号不完整。")
        if len(kinds) > 1:
            raise ValueError("同一条规则不能混用{列名}与[单元格]引用。")
        self.kind = next(iter(kinds), None)
        try:
            self.tree = ast.parse(self.text, mode="eval")
        except (SyntaxError, RecursionError) as exc:
            raise ValueError("算式格式不正确。") from exc
        nodes = list(ast.walk(self.tree))
        if len(nodes) > 160:
            raise ValueError("算式过于复杂，请拆成多条规则。")
        allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult, ast.Div,
                   ast.UAdd, ast.USub, ast.Constant, ast.Name, ast.Load, ast.Call)
        for node in nodes:
            if not isinstance(node, allowed):
                raise ValueError("算式仅支持数字、引用、加减乘除、SUM、ABS和ROUND。")
            if isinstance(node, ast.Name) and node.id not in self.refs and node.id not in {"SUM", "ABS", "ROUND"}:
                raise ValueError("未知算式名称：" + node.id)
            if isinstance(node, ast.Constant):
                if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                    raise ValueError("算式常量仅支持有限数字。")
                _decimal(ast.get_source_segment(self.text, node), "常量")
            if isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name) or node.func.id not in {"SUM", "ABS", "ROUND"} or node.keywords:
                    raise ValueError("仅支持SUM、ABS和ROUND函数。")
                if not 1 <= len(node.args) <= 30:
                    raise ValueError("函数参数数量须在1至30之间。")

    def evaluate(self, resolver):
        def scalar(value):
            if isinstance(value, (list, tuple)):
                raise ValueError("区域引用只能用于SUM函数。")
            return value
        def visit(node):
            if isinstance(node, ast.Expression):
                return visit(node.body)
            if isinstance(node, ast.Constant):
                return _decimal(ast.get_source_segment(self.text, node), "常量")
            if isinstance(node, ast.Name):
                if node.id not in self.refs:
                    raise ValueError("函数名必须带括号。")
                return resolver(*self.refs[node.id])
            if isinstance(node, ast.UnaryOp):
                value = scalar(visit(node.operand))
                return value if isinstance(node.op, ast.UAdd) else -value
            if isinstance(node, ast.BinOp):
                left, right = scalar(visit(node.left)), scalar(visit(node.right))
                if isinstance(node.op, ast.Add):
                    result = left + right
                elif isinstance(node.op, ast.Sub):
                    result = left - right
                elif isinstance(node.op, ast.Mult):
                    result = left * right
                else:
                    if not right:
                        raise ValueError("除数为0。")
                    result = left / right
                return _decimal(result, "算式结果")
            values = [visit(arg) for arg in node.args]
            if node.func.id == "SUM":
                result = sum((item for value in values for item in (value if isinstance(value, tuple) else (value,))), Decimal(0))
            elif node.func.id == "ABS":
                if len(values) != 1:
                    raise ValueError("ABS须填写1个参数。")
                result = abs(scalar(values[0]))
            else:
                if len(values) != 2:
                    raise ValueError("ROUND须填写数值和小数位数2个参数。")
                places = scalar(values[1])
                if places != places.to_integral_value() or not -20 <= places <= 20:
                    raise ValueError("ROUND小数位数须为-20至20的整数。")
                # Excel's ROUND uses half-away-from-zero for decimal ties.
                from decimal import ROUND_HALF_UP
                result = scalar(values[0]).quantize(Decimal(1).scaleb(-int(places)), rounding=ROUND_HALF_UP)
            return _decimal(result, "算式结果")
        try:
            with localcontext() as context:
                context.prec = 110
                return scalar(visit(self.tree))
        except (InvalidOperation, OverflowError, ZeroDivisionError) as exc:
            raise ValueError("算式无法进行有限十进制计算。") from exc


def _rules(options):
    values = options.get("rules", [])
    if not isinstance(values, list) or not 1 <= len(values) <= 100:
        raise ValueError("请填写1至100条勾稽规则。")
    result = []
    for index, value in enumerate(values, 1):
        if not isinstance(value, dict):
            raise ValueError("勾稽规则格式无效。")
        name = value.get("name", "")
        if not isinstance(name, str) or len(name) > 200:
            raise ValueError("规则名称最多200个字符。")
        left, right = _Expression(value.get("left")), _Expression(value.get("right"))
        kinds = {item.kind for item in (left, right) if item.kind}
        if len(kinds) > 1:
            raise ValueError("同一规则左右不能混用行内与单元格引用。")
        result.append((name.strip() or f"规则{index}", left, right, next(iter(kinds), "cell"), value))
    return result


def _mappings(options, key):
    values = options.get(key, [])
    if not isinstance(values, list) or not 1 <= len(values) <= 100:
        raise ValueError("请填写1至100行主键关系和回填关系。")
    result = []
    for value in values:
        if not isinstance(value, dict) or any(not isinstance(value.get(k), str) or not value[k].strip() for k in ("target", "source")):
            raise ValueError("每行对应关系均须填写目标列名和参考列名。")
        result.append((value["target"].strip(), value["source"].strip()))
    if len({target for target, _source in result}) != len(result):
        raise ValueError("同一组关系中的目标列名不能重复。")
    return result


def validate(mode, options, sources=None):
    if mode not in TOOL_SPECS or not isinstance(options, dict):
        raise ValueError("表格工具或设置格式无效。")
    for field in TOOL_SPECS[mode]["fields"]:
        key, kind = field["key"], field["kind"]
        value = options.get(key, field["default"])
        if kind == "bool" and not isinstance(value, bool):
            raise ValueError(field["title"] + "必须为开关值。")
        if kind == "text" and not isinstance(value, str):
            raise ValueError(field["title"] + "必须为文字。")
        if kind == "choice" and value not in {value for _title, value in field["choices"]}:
            raise ValueError(field["title"] + "包含未知选项。")
    if mode in {"excel_audit", "excel_lookup"}:
        _integer(options.get("header_row", 1), "表头行")
    if mode == "excel_audit":
        _rules(options)
        tolerance = _decimal(options.get("numeric_tolerance", 0.01), "允许误差")
        if not 0 <= tolerance <= Decimal("1e12"):
            raise ValueError("允许误差须在0至1e12之间。")
    elif mode == "excel_lookup":
        keys, fills = _mappings(options, "key_mappings"), _mappings(options, "fill_mappings")
        if {name for name, _value in keys} & {name for name, _value in fills}:
            raise ValueError("回填字段不能覆盖主键列。")
        if sources is not None and (len(sources) != 2 or any(not Path(path).is_file() for path in sources)):
            raise ValueError("请添加恰好两份文件：第一份目标表，第二份参考表。")
    else:
        repeat = options.get("repeat_rows", "1:1").strip()
        if repeat:
            match = re.fullmatch(r"([1-9][0-9]{0,6}):([1-9][0-9]{0,6})", repeat)
            if not match or not 1 <= int(match[1]) <= int(match[2]) <= 1048576:
                raise ValueError("重复表头填写1:1或1:2，且起始行不大于结束行。")
        if options.get("print_area", "").strip():
            _range(options["print_area"].strip())
        margin = _decimal(options.get("margin_cm", 1.5), "页边距")
        if not 0 <= margin <= 5:
            raise ValueError("页边距须在0至5厘米之间。")


def _result(outputs, changes, warnings=(), errors=(), data_outputs=()):
    result = make_result(outputs, changes, warnings, errors)
    result["data_outputs"] = [str(path) for path in data_outputs]
    return result


def _input_files(sources, folder, extensions, recursive=True):
    result, seen = [], set()
    for source in sources:
        path = Path(source)
        if path.is_file() and path.suffix.lower() not in extensions:
            raise ValueError("不支持输入格式：" + path.suffix)
        files = expand_inputs([path], extensions, recursive, exclude_root=folder if path.is_dir() else None)
        for file in files:
            identity = str(file).casefold()
            if identity not in seen:
                seen.add(identity)
                result.append(file)
    if not result:
        raise ValueError("没有可处理的表格文件。")
    return result


def _sheet_dimensions(sheet):
    if not sheet.max_row or not sheet.max_column:
        raise ValueError("工作表未提供有效范围，请用Excel另存为标准XLSX。")
    if sheet.max_row * sheet.max_column > _MAX_CELLS:
        raise ValueError("工作表范围超过200万个单元格，请删除空白格式区域或拆分处理。")


def _audit_one(path, options, details, formula_issues, summary, warnings, cancel):
    excel_ops._check_xlsx(path)
    rules = _rules(options)
    original = load_workbook(path, data_only=False, read_only=True, keep_links=False)
    cached = None
    try:
        cached = load_workbook(path, data_only=True, read_only=True, keep_links=False)
        name = options.get("sheet", "").strip()
        if not name:
            name = next((sheet.title for sheet in original if sheet.sheet_state == "visible"), "")
        if name not in original.sheetnames:
            raise ValueError("找不到指定或可见工作表。")
        sheet, values = original[name], cached[name]
        _sheet_dimensions(sheet)
        row_rules = sum(rule[3] == "row" for rule in rules)
        header_row = _integer(options.get("header_row", 1), "表头行")
        if max(0, sheet.max_row - header_row) * row_rules + len(rules) > _MAX_CHECKS:
            raise ValueError("核验超过50万次，请缩小范围或减少规则。")
        cells, originals = {}, {}
        for row_index, (row, saved) in enumerate(zip(sheet.iter_rows(), values.iter_rows()), 1):
            if row_index % 128 == 0:
                ensure_cancel(cancel)
            for column_index, (cell, cache) in enumerate(zip(row, saved), 1):
                coordinate = f"{get_column_letter(column_index)}{row_index}"
                if cell.value is not None or cache.value is not None:
                    cells[coordinate], originals[coordinate] = cache, cell
                problem = ""
                if cell.data_type == "f":
                    warnings.add("公式仅使用已保存的缓存，未重新计算；请先在Excel重算并保存，缓存可能过期。")
                    if cache.value is None:
                        problem = "公式缺少缓存"
                    elif cache.data_type == "e":
                        problem = "公式缓存为Excel错误值"
                    if re.search(r"\[[^\]]+\][^!]*!", str(cell.value)):
                        problem = (problem + "；" if problem else "") + "外部工作簿公式，未刷新链接"
                elif cache.data_type == "e":
                    problem = "Excel错误值"
                if problem:
                    formula_issues.append([str(path), name, coordinate, problem, str(cell.value or ""), excel_ops._display(cache.value)])
        with ZipFile(path) as package:
            if any(info.filename.startswith("xl/externalLinks/") for info in package.infolist()):
                formula_issues.append([str(path), name, "工作簿", "存在外部链接，未访问或刷新", "", ""])
        header_values = [cells.get(f"{get_column_letter(column)}{header_row}") for column in range(1, sheet.max_column + 1)]
        header = [excel_ops._display(cell.value).strip() if cell else "" for cell in header_values]
        counts = Counter(value for value in header if value)
        columns = {value: index + 1 for index, value in enumerate(header) if value and counts[value] == 1}
        tolerance = _decimal(options.get("numeric_tolerance", 0.01))
        for rule_name, left, right, kind, raw in rules:
            refs = [ref for expression in (left, right) for ref in expression.refs.values()]
            missing = [value for refkind, value in refs if refkind == "row" and value not in columns]
            if missing:
                details.append([str(path), name, "表头", rule_name, "规则异常", "", "", "", "引用列缺失或重名：" + "，".join(dict.fromkeys(missing)), raw["left"], raw["right"]])
                summary["规则异常"] += 1
                continue
            row_numbers = range(header_row + 1, sheet.max_row + 1) if kind == "row" else (None,)
            for row in row_numbers:
                if row and row % 128 == 0:
                    ensure_cancel(cancel)
                if row and not any(f"{get_column_letter(column)}{row}" in originals for column in range(1, sheet.max_column + 1)):
                    continue
                def numeric(coordinate):
                    ensure_cancel(cancel)
                    origin, saved = originals.get(coordinate), cells.get(coordinate)
                    if origin is not None and origin.data_type == "f" and (saved is None or saved.value is None):
                        raise ValueError(coordinate + "公式缺少缓存，未按0处理。")
                    if saved is not None and saved.data_type == "e":
                        raise ValueError(coordinate + "含Excel错误值：" + str(saved.value))
                    return _decimal(saved.value if saved else None, coordinate)
                def resolve(refkind, value):
                    if refkind == "row":
                        return numeric(f"{get_column_letter(columns[value])}{row}")
                    col1, row1, col2, row2 = _range(value)
                    if ":" not in value:
                        return numeric(f"{get_column_letter(col1)}{row1}")
                    return tuple(numeric(f"{get_column_letter(column)}{number}") for number in range(row1, row2 + 1) for column in range(col1, col2 + 1))
                a = b = difference = ""
                message = ""
                try:
                    a, b = left.evaluate(resolve), right.evaluate(resolve)
                    with localcontext() as context:
                        context.prec = 110
                        difference = a - b
                    status = "通过" if abs(difference) <= tolerance else "不一致"
                except ValueError as exc:
                    status, message = "数值异常", str(exc)
                summary[status] += 1
                details.append([str(path), name, row if row else "单元格规则", rule_name, status, str(a), str(b), str(difference), message, raw["left"], raw["right"]])
    finally:
        original.close()
        if cached is not None:
            cached.close()


def _audit(files, folder, options, progress, cancel):
    details = [["来源文件", "工作表", "行号或范围", "规则名称", "结果", "左侧值", "右侧值", "差额（左减右）", "异常说明", "左侧算式", "右侧算式"]]
    issues = [["来源文件", "工作表", "单元格", "问题", "原公式或值", "保存的缓存"]]
    summary, warnings, errors = Counter(), set(), []
    for index, path in enumerate(files, 1):
        ensure_cancel(cancel)
        notify(progress, index - 1, len(files) + 1, "核验 " + path.name)
        # Stage rows locally: a failed workbook never contributes partial results.
        current_details, current_issues, current_summary = [], [], Counter()
        try:
            _audit_one(path, options, current_details, current_issues, current_summary, warnings, cancel)
            if len(details) + len(current_details) > _MAX_CHECKS + 1 or len(issues) + len(current_issues) > _MAX_CHECKS + 1:
                raise ValueError("本批报告超过50万条，请分批处理；该文件未计入结果。")
            details.extend(current_details)
            issues.extend(current_issues)
            summary.update(current_summary)
        except CancelledError:
            raise
        except Exception as exc:
            errors.append(path.name + "：" + str(exc))
    if len(details) == 1 and len(issues) == 1:
        return _result([], 0, sorted(warnings), errors or ["所选工作表没有可核验的数据。"])
    report = unique_path(folder, "数字勾稽核验报告.xlsx")
    intro = [["项目", "数量或说明"], *[[key, summary[key]] for key in ("通过", "不一致", "数值异常", "规则异常")], ["公式和外链问题", len(issues) - 1], ["读取失败文件", len(errors)], ["注意", "只核对保存的数值；公式不重新计算，空值不当0。"]]
    excel_ops._write_book(report, {"核验摘要": intro, "核验明细": details, "公式与外链检查": issues, "读取错误": [["错误"], *[[error] for error in errors]]}, cancel=cancel)
    notify(progress, len(files) + 1, len(files) + 1, "核验完成")
    return _result([report], summary["不一致"] + summary["数值异常"] + summary["规则异常"] + len(issues) - 1, sorted(warnings), errors)


def _lookup(files, folder, options, progress, cancel):
    keys, fills = _mappings(options, "key_mappings"), _mappings(options, "fill_mappings")
    target_tables, warnings1 = excel_ops._load_tables(files[0], options.get("sheet_target", "").strip(), options.get("header_row", 1), cancel=cancel)
    reference_tables, warnings2 = excel_ops._load_tables(files[1], options.get("sheet_reference", "").strip(), options.get("header_row", 1), cancel=cancel)
    target, reference = target_tables[0], reference_tables[0]
    if len(target.rows) * max(1, len(fills)) > _MAX_CHECKS:
        raise ValueError("回填检查超过50万次，请分批处理。")
    missing_target = [column for column, _source in keys if column not in target.headers]
    missing_reference = [column for column in [source for _target, source in keys + fills] if column not in reference.headers]
    if missing_target or missing_reference:
        raise ValueError("列名缺失：" + "；".join(["目标表：" + "，".join(missing_target)] if missing_target else []) + ("；参考表：" + "，".join(dict.fromkeys(missing_reference)) if missing_reference else ""))
    def key(row, side):
        values = [row[target_column if side == "目标表" else source_column] for target_column, source_column in keys]
        if any(excel_ops._blank(value) for value in values):
            return None
        return tuple(excel_ops._atom(value, options.get("trim_keys", True)) for value in values)
    indexes = {}
    anomalies = [["类型", "来源表", "来源行", "主键值", "说明"]]
    for side, table in (("目标表", target), ("参考表", reference)):
        index = defaultdict(list)
        for number, row in enumerate(table.rows, 1):
            if number % 128 == 0:
                ensure_cancel(cancel)
            identity = key(row, side)
            if identity is None:
                anomalies.append(["空主键", side, row.source_row, "", "未进行匹配"])
            else:
                index[identity].append(row)
        for identity, rows in index.items():
            if len(rows) > 1:
                for row in rows:
                    anomalies.append(["重复主键", side, row.source_row, " | ".join(excel_ops._display(item[1]) for item in identity), f"该键共{len(rows)}条，未强行匹配"])
        indexes[side] = index
    headers = target.headers + [column for column, _source in fills if column not in target.headers]
    audit = [["目标来源行", "参考来源行", "目标列", "参考列", "处理结果", "原值", "参考值", "最终值"]]
    unmatched = [["目标来源行", "主键值", "说明"]]
    conflicts = [audit[0][:]]
    output_rows, changes, matched = [headers], 0, 0
    for index, row in enumerate(target.rows, 1):
        if index % 128 == 0:
            ensure_cancel(cancel)
            notify(progress, index, len(target.rows) + 1, "匹配回填")
        values = dict(row)
        identity = key(row, "目标表")
        candidates = indexes["参考表"].get(identity, []) if identity is not None else []
        if identity is not None and len(indexes["目标表"][identity]) == 1:
            if not candidates:
                unmatched.append([row.source_row, " | ".join(excel_ops._display(item[1]) for item in identity), "参考表未找到该键"])
            elif len(candidates) == 1:
                matched += 1
                other = candidates[0]
                for target_column, source_column in fills:
                    old, new = values.get(target_column), other[source_column]
                    if excel_ops._blank(new):
                        status = "参考值为空，保留原值"
                    elif excel_ops._atom(old) == excel_ops._atom(new):
                        status = "已有相同值"
                    elif excel_ops._blank(old):
                        values[target_column] = new
                        changes += 1
                        status = "已填入空值"
                    else:
                        status = "冲突，保留原值"
                        if options.get("overwrite", False):
                            values[target_column] = new
                            changes += 1
                            status = "冲突，按设置覆盖"
                        conflicts.append([row.source_row, other.source_row, target_column, source_column, status, old, new, values.get(target_column)])
                    audit.append([row.source_row, other.source_row, target_column, source_column, status, old, new, values.get(target_column)])
        output_rows.append([values.get(header) for header in headers])
    ensure_cancel(cancel)
    report = unique_path(folder, files[0].stem + "_回填结果.xlsx")
    summary = [["项目", "数量或说明"], ["目标文件", str(files[0])], ["参考文件", str(files[1])], ["目标记录", len(target.rows)], ["唯一匹配记录", matched], ["实际修改单元格", changes], ["未匹配记录", len(unmatched) - 1], ["键异常记录", len(anomalies) - 1], ["冲突单元格", len(conflicts) - 1], ["结果类型", "标准值表，原件不改动；主键不跨文本/数字类型自动混同。"]]
    excel_ops._write_book(report, {"回填结果": output_rows, "处理摘要": summary, "回填明细": audit, "原值冲突": conflicts, "未匹配": unmatched, "主键异常": anomalies}, primary=["回填结果"], cancel=cancel)
    notify(progress, len(target.rows) + 1, len(target.rows) + 1, "回填完成")
    warnings = list(dict.fromkeys([excel_ops._STANDARD_WARNING, *warnings1, *warnings2]))
    return _result([report], changes, warnings, data_outputs=[report])


def _package_parts(path):
    excel_ops._check_xlsx(path)
    with ZipFile(path) as package:
        names = package.namelist()
        content = package.read("[Content_Types].xml")
        if any("vbaproject" in name.casefold() or "macrosheets/" in name.casefold() for name in names) or b"macroEnabled" in content:
            raise ValueError("该XLSX含宏或宏表，请先另存为无宏的标准XLSX。")
        workbook = ET.fromstring(package.read("xl/workbook.xml"))
        relations = ET.fromstring(package.read("xl/_rels/workbook.xml.rels"))
        targets = {relation.get("Id"): relation.get("Target") for relation in relations if relation.get("TargetMode") != "External"}
        sheets = []
        for index, sheet in enumerate(workbook.find(_N + "sheets")):
            target = targets.get(sheet.get(_REL + "id"), "")
            part = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
            if part not in names or not part.startswith("xl/worksheets/"):
                raise ValueError("工作簿含不支持的图表页或工作表关系，未改写文件。")
            sheets.append((index, sheet.get("name"), sheet.get("state", "visible"), part))
    return workbook, sheets


def _xml(root, original):
    """Keep namespace prefixes referenced by mc:Ignorable and extension values.

    ElementTree otherwise drops unused xmlns declarations even when attribute
    *values* refer to them. Keep the original root declarations on rewritten
    parts so modern Excel extensions remain structurally valid.
    """
    namespaces = []
    for event, value in ET.iterparse(io.BytesIO(original), events=("start-ns", "start")):
        if event == "start":
            break
        prefix, uri = value
        namespaces.append((prefix, uri))
        if not re.fullmatch(r"ns[0-9]+", prefix):
            ET.register_namespace(prefix, uri)
    result = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    declaration_end = result.find(b"?>") + 2
    root_end = result.find(b">", declaration_end)
    if result[root_end - 1:root_end] == b"/":
        root_end -= 1
    start = result[declaration_end:root_end]
    additions = []
    for prefix, uri in namespaces:
        attribute = "xmlns" + (":" + prefix if prefix else "")
        if re.search(rb"\s" + re.escape(attribute.encode()) + rb"=", start) is None:
            additions.append(f' {attribute}="{escape(uri, quote=True)}"'.encode())
    return result[:root_end] + b"".join(additions) + result[root_end:]


_SHEET_ORDER = ["sheetPr", "dimension", "sheetViews", "sheetFormatPr", "cols", "sheetData", "sheetCalcPr", "sheetProtection", "protectedRanges", "scenarios", "autoFilter", "sortState", "dataConsolidate", "customSheetViews", "mergeCells", "phoneticPr", "conditionalFormatting", "dataValidations", "hyperlinks", "printOptions", "pageMargins", "pageSetup", "headerFooter", "rowBreaks", "colBreaks", "customProperties", "cellWatches", "ignoredErrors", "smartTags", "drawing", "legacyDrawing", "legacyDrawingHF", "picture", "oleObjects", "controls", "webPublishItems", "tableParts", "extLst"]


def _element(parent, tag):
    element = parent.find(_N + tag)
    if element is None:
        element = ET.Element(_N + tag)
        position = _SHEET_ORDER.index(tag)
        for index, child in enumerate(parent):
            name = child.tag.removeprefix(_N)
            if name in _SHEET_ORDER and _SHEET_ORDER.index(name) > position:
                parent.insert(index, element)
                break
        else:
            parent.append(element)
    return element


def _layout_copy(source, target, options, cancel):
    workbook, sheets = _package_parts(source)
    selected = [(index, name, state, part) for index, name, state, part in sheets if name == options.get("sheet", "").strip()] if options.get("sheet", "").strip() else [entry for entry in sheets if entry[2] == "visible"]
    if not selected:
        raise ValueError("没有找到指定或可见工作表。")
    replacements = {}
    titles = workbook.find(_N + "definedNames")
    if titles is None:
        titles = ET.Element(_N + "definedNames")
        # definedNames belongs after sheets and before calcPr/pivotCaches.
        position = list(workbook).index(workbook.find(_N + "sheets")) + 1
        for index, child in enumerate(workbook):
            if child.tag in {_N + "functionGroups", _N + "externalReferences"}:
                position = index + 1
        workbook.insert(position, titles)
    with ZipFile(source) as package:
        for index, name, _state, part in selected:
            ensure_cancel(cancel)
            original_xml = package.read(part)
            root = ET.fromstring(original_xml)
            sheet_properties = _element(root, "sheetPr")
            page_properties = sheet_properties.find(_N + "pageSetUpPr")
            if page_properties is None:
                page_properties = ET.SubElement(sheet_properties, _N + "pageSetUpPr")
            page_properties.set("fitToPage", "1" if options.get("fit_width", True) else "0")
            page = _element(root, "pageSetup")
            page.set("paperSize", {"A4": "9", "A3": "8", "LETTER": "1"}[options.get("paper", "A4")])
            page.set("orientation", options.get("orientation", "portrait"))
            if options.get("fit_width", True):
                page.set("fitToWidth", "1")
                page.set("fitToHeight", "0")
                page.attrib.pop("scale", None)
            else:
                page.attrib.pop("fitToWidth", None)
                page.attrib.pop("fitToHeight", None)
            margins = _element(root, "pageMargins")
            for side in ("left", "right", "top", "bottom"):
                margins.set(side, str(_decimal(options.get("margin_cm", 1.5)) / Decimal("2.54")))
            margins.attrib.setdefault("header", "0.3")
            margins.attrib.setdefault("footer", "0.3")
            repeat = options.get("repeat_rows", "1:1").strip()
            area = options.get("print_area", "").strip()
            repeated_columns = []
            for node in list(titles):
                if node.get("localSheetId") == str(index) and (node.get("name") == "_xlnm.Print_Titles" or area and node.get("name") == "_xlnm.Print_Area"):
                    if node.get("name") == "_xlnm.Print_Titles":
                        repeated_columns.extend(re.findall(r"(?:'(?:[^']|'')*'|[^!,]+)!\$?[A-Za-z]+:\$?[A-Za-z]+", node.text or ""))
                    titles.remove(node)
            if repeat or repeated_columns:
                ranges = repeated_columns[:]
                if repeat:
                    first, last = repeat.split(":")
                    ranges.insert(0, f"{quote_sheetname(name)}!${first}:${last}")
                node = ET.SubElement(titles, _N + "definedName", {"name": "_xlnm.Print_Titles", "localSheetId": str(index)})
                node.text = ",".join(ranges)
            if area:
                left, top, right, bottom = _range(area)
                node = ET.SubElement(titles, _N + "definedName", {"name": "_xlnm.Print_Area", "localSheetId": str(index)})
                node.text = f"{quote_sheetname(name)}!${get_column_letter(left)}${top}:${get_column_letter(right)}${bottom}"
            replacements[part] = _xml(root, original_xml)
        replacements["xl/workbook.xml"] = _xml(workbook, package.read("xl/workbook.xml"))
        def writer(staged):
            with ZipFile(staged, "w", ZIP_DEFLATED) as output:
                for info in package.infolist():
                    ensure_cancel(cancel)
                    output.writestr(info, replacements[info.filename] if info.filename in replacements else package.read(info.filename))
            # Parse the staged result before its visible filename is published.
            check = load_workbook(staged, read_only=True, data_only=False, keep_links=False)
            try:
                for sheet in check:
                    _sheet_dimensions(sheet)
            finally:
                check.close()
            ensure_cancel(cancel)
        atomic_write(target, writer)
    return len(selected)


def _static_pdf_input(source, target, cancel):
    """Freeze formulas to saved caches and remove all external link sources."""
    _package_parts(source)
    with ZipFile(source) as package, ZipFile(target, "w", ZIP_DEFLATED) as output:
        names = package.namelist()
        blocked = ("xl/connections", "xl/queryTables/", "xl/embeddings/", "xl/activeX/", "xl/pivotCache/", "xl/model/", "customXml/")
        if any(name.startswith(blocked) for name in names):
            raise ValueError("PDF导出不支持活动数据连接、查询、数据模型、透视缓存或嵌入对象；请先另存为静态值工作簿。打印副本仍已保留。")
        remove = {name for name in names if name.startswith("xl/externalLinks/") or name == "xl/calcChain.xml"}
        for info in package.infolist():
            ensure_cancel(cancel)
            if info.filename in remove:
                continue
            data = package.read(info.filename)
            if info.filename.endswith(".rels"):
                root = ET.fromstring(data)
                for relation in list(root):
                    kind = relation.get("Type", "").rsplit("/", 1)[-1]
                    if kind in {"externalLink", "calcChain"}:
                        root.remove(relation)
                    elif relation.get("TargetMode") == "External":
                        if kind == "hyperlink":
                            root.remove(relation)
                        else:
                            raise ValueError("PDF导出检测到外部对象关系，已停止；请将链接图片等嵌入工作簿后重试。")
                data = _xml(root, data)
            elif info.filename == "[Content_Types].xml":
                root = ET.fromstring(data)
                for item in list(root):
                    if item.get("PartName", "").lstrip("/") in remove:
                        root.remove(item)
                data = _xml(root, data)
            elif info.filename == "xl/workbook.xml":
                root = ET.fromstring(data)
                for name in ("externalReferences", "calcPr"):
                    child = root.find(_N + name)
                    if child is not None:
                        root.remove(child)
                definitions = root.find(_N + "definedNames")
                if definitions is not None:
                    for definition in list(definitions):
                        if definition.get("name") not in {"_xlnm.Print_Area", "_xlnm.Print_Titles"} and not _safe_defined_name(definition.text or ""):
                            raise ValueError("PDF导出检测到动态命名公式或外部命名范围（" + definition.get("name", "") + "），请先另存为静态值工作簿；打印副本仍已保留。")
                data = _xml(root, data)
            elif info.filename.startswith("xl/worksheets/") and info.filename.endswith(".xml"):
                root = ET.fromstring(data)
                count = 0
                for cell in root.iter(_N + "c"):
                    count += 1
                    if count % 128 == 0:
                        ensure_cancel(cancel)
                    formula = cell.find(_N + "f")
                    if formula is not None:
                        cache = cell.find(_N + "v")
                        if cache is None or cache.text is None:
                            raise ValueError(f"{info.filename}的{cell.get('r')}公式缺少缓存，无法离线生成可靠PDF；请用Excel重算保存。")
                        if cell.get("t") == "e":
                            raise ValueError(f"{info.filename}的{cell.get('r')}公式缓存为错误值，请先修复。")
                        cell.remove(formula)
                hyperlinks = root.find(_N + "hyperlinks")
                if hyperlinks is not None:
                    root.remove(hyperlinks)
                data = _xml(root, data)
            elif info.filename.startswith("xl/tables/") and info.filename.endswith(".xml"):
                root = ET.fromstring(data)
                for parent in root.iter():
                    for child in list(parent):
                        if child.tag in {_N + "calculatedColumnFormula", _N + "totalsRowFormula"}:
                            parent.remove(child)
                data = _xml(root, data)
            output.writestr(info, data)
    check = load_workbook(target, read_only=True, data_only=False, keep_links=False)
    try:
        for sheet in check:
            _sheet_dimensions(sheet)
            for row in sheet:
                ensure_cancel(cancel)
                if any(cell.data_type == "f" for cell in row):
                    raise ValueError("静态导出副本仍含公式，已停止PDF转换。")
    finally:
        check.close()


def _safe_defined_name(value):
    """Preserve local range/constant names without evaluating named formulas."""
    value = value.strip().removeprefix("=").strip()
    if _NUMBER.fullmatch(value) or value in {"TRUE", "FALSE"} or re.fullmatch(r'"(?:[^"]|"")*"', value):
        return True
    sheet = r"(?:'(?:[^']|'')*'|[^'!+*/(),\[\]]+)"
    cell = r"\$?[A-Za-z]{1,3}\$?[1-9][0-9]{0,6}"
    column = r"\$?[A-Za-z]{1,3}"
    row = r"\$?[1-9][0-9]{0,6}"
    area = rf"(?:{cell}(?::{cell})?|{column}:{column}|{row}:{row})"
    term = rf"{sheet}!{area}"
    return re.fullmatch(rf"{term}(?:,{term})*", value) is not None


def _export_pdf(source, target, options, cancel):
    target = Path(target)
    with conversion._conversion_workspace(target.parent) as temporary:
        static = temporary / "静态打印副本.xlsx"
        _static_pdf_input(source, static, cancel)
        ensure_cancel(cancel)
        return conversion.convert_office_to_pdf(static, target, {**options, "_cancel": cancel})


def _print(files, folder, options, progress, cancel):
    outputs, data_outputs, errors, changes = [], [], [], 0
    warnings = ["打印副本保留原件；复杂图形、字体、页眉页脚及分页请用打印预览核对。"]
    if options.get("output_pdf", False):
        warnings.append("PDF使用Excel保存的静态公式值；不重算公式、不刷新外链，原文件缓存过期时请先重算保存。")
    for index, path in enumerate(files, 1):
        ensure_cancel(cancel)
        notify(progress, index - 1, len(files), "打印排版 " + path.name)
        try:
            target = unique_path(folder, path.stem + "_打印副本.xlsx")
            changes += _layout_copy(path, target, options, cancel)
            outputs.append(target)
            data_outputs.append(target)
            if options.get("output_pdf", False):
                pdf = unique_path(folder, path.stem + "_打印.pdf")
                _export_pdf(target, pdf, options, cancel)
                outputs.append(pdf)
        except CancelledError:
            raise
        except Exception as exc:
            errors.append(path.name + "：" + str(exc))
    notify(progress, len(files), len(files), "打印排版完成")
    return _result(outputs, changes, warnings, errors, data_outputs)


def run(mode, sources, folder, options, progress=None, cancel=None):
    validate(mode, options, sources)
    ensure_cancel(cancel)
    folder = Path(folder)
    files = _input_files(sources, folder, TOOL_SPECS[mode]["extensions"], options.get("recursive", True))
    if mode == "excel_audit":
        return _audit(files, folder, options, progress, cancel)
    if mode == "excel_lookup":
        if len(files) != 2:
            raise ValueError("目标表和参考表须为两份不同的文件。")
        return _lookup(files, folder, options, progress, cancel)
    return _print(files, folder, options, progress, cancel)


def build_example(mode, folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    def book(name, rows):
        path = unique_path(folder, name)
        workbook = Workbook()
        try:
            workbook.active.title = "数据"
            for row in rows:
                workbook.active.append(row)
            sheet = workbook.active
            for column in range(1, sheet.max_column + 1):
                width = max(sum(2 if ord(character) > 255 else 1 for character in str(sheet.cell(row, column).value or "")) for row in range(1, sheet.max_row + 1))
                sheet.column_dimensions[get_column_letter(column)].width = max(14, min(42, width + 4))
            for row in sheet:
                for cell in row:
                    cell.font = Font(name="微软雅黑", size=10, bold=cell.row == 1, color="FFFFFF" if cell.row == 1 else "24352F")
                    cell.alignment = Alignment(vertical="center", wrap_text=True)
                    cell.fill = PatternFill("solid", fgColor="287D70" if cell.row == 1 else "F0F6F4" if cell.row % 2 == 0 else "FFFFFF")
                sheet.row_dimensions[row[0].row].height = 25
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            atomic_write(path, workbook.save)
        finally:
            workbook.close()
        return str(path)
    if mode == "excel_audit":
        source = book("金额核验示例.xlsx", [["编号", "收入", "补贴", "合计"], ["001", 100, 20, 120], ["002", 200, 30, 250], ["003", None, 10, 10], ["总计", 300, 60, 380]])
        return [source], {"sheet": "数据", "header_row": 1, "rules": [{"name": "行内合计", "left": "{合计}", "right": "{收入}+{补贴}"}, {"name": "单元格总计", "left": "[D5]", "right": "SUM([D2:D4])"}], "numeric_tolerance": 0.01, "recursive": True}
    if mode == "excel_lookup":
        target = book("01_目标名单.xlsx", [["编号", "姓名", "部门"], ["001", "张三", ""], ["002", "李四", "原部门"], ["003", "王五", ""], ["004", "赵六", ""], ["", "待核实", ""]])
        reference = book("02_参考名册.xlsx", [["工号", "所属部门", "电话"], ["001", "综合部", "010-12345678"], ["002", "业务部", "010-87654321"], ["004", "财务部", "010-11111111"], ["004", "人事部", "010-22222222"]])
        return [target, reference], {"key_mappings": [{"target": "编号", "source": "工号"}], "fill_mappings": [{"target": "部门", "source": "所属部门"}, {"target": "电话", "source": "电话"}], "header_row": 1, "trim_keys": True, "overwrite": False}
    if mode == "excel_print":
        source = book("打印排版示例.xlsx", [["编号", "姓名", "部门", "金额", "说明"], *[[f"{number:03d}", "示例员工", "综合部", number * 100, "用于打印排版练习"] for number in range(1, 81)]])
        return [source], {"paper": "A4", "orientation": "landscape", "fit_width": True, "repeat_rows": "1:1", "print_area": "A1:E81", "margin_cm": 1.5, "output_pdf": False, "recursive": True}
    raise ValueError("未知表格工具。")
