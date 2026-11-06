"""Offline HTML/JSON reports and safe official-template output."""
from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

from .common import atomic_write


_LABELS = {
    "index": "段落", "paragraph": "段落", "paragraph_index": "段落", "level": "层级",
    "text": "标题摘要", "title": "标题摘要", "reason": "说明", "message": "说明",
    "rule": "识别依据", "matched_by": "识别依据", "kind": "类别", "type": "类别",
    "expected": "预期", "actual": "当前", "location": "位置", "suggestion": "建议",
    "count": "数量", "heading_counts": "各级标题", "paragraphs": "段落数",
    "tables": "表格数", "removed_blank_paragraphs": "删除空白段落",
    "split_paragraphs": "换行拆段", "changed_paragraphs": "调整段落",
    "cleaned_paragraphs": "清理文字段落", "changed_parts": "调整文档部件", "headings": "识别标题数",
    "heading_levels": "各级标题数", "source": "识别依据", "font": "字体", "size": "字号",
    "sequence": "序号类型", "number": "序号", "number_scope": "上级序号范围",
}


def _text(value):
    if value is None or value == "":
        return "—"
    if isinstance(value, dict):
        return "；".join(f"{_LABELS.get(str(k), str(k))}：{_text(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return "、".join(_text(v) for v in value)
    if value is True:
        return "是"
    if value is False:
        return "否"
    return str(value)


def report_html(report):
    audit = report["检查结果"]
    pieces = ["<!doctype html><html lang='zh-CN'><meta charset='utf-8'>",
              "<meta name='viewport' content='width=device-width, initial-scale=1'>",
              "<title>青简 · 公文检查报告</title><style>",
              "body{margin:0;background:#f4f5fb;color:#26304a;font:15px/1.8 'Microsoft YaHei',sans-serif}",
              "main{max-width:1080px;margin:32px auto;padding:0 24px 40px}h1{font-size:28px;margin:0}",
              ".meta{color:#7c859c;font-size:13px;overflow-wrap:anywhere}.card{background:white;border:1px solid #e4e7f2;",
              "border-radius:14px;padding:20px 24px;margin-top:16px}h2{font-size:18px;margin:0 0 10px}",
              "li{margin:5px 0;overflow-wrap:anywhere}ul{padding-left:22px}.quiet{color:#8991a5}",
              ".pill{display:inline-block;background:#eeefff;color:#5755d7;padding:3px 12px;border-radius:8px}",
              "@media print{body{background:white}.card{break-inside:avoid}main{margin:0;padding:0}}</style><main>",
              f"<h1>{html.escape(report['报告类型'])}</h1>",
              f"<p class='pill'>{html.escape(report['公文种类'])}</p>",
              f"<div class='meta'>时间：{html.escape(report['时间'])}<br>源文件：{html.escape(report['源文件'])}"]
    if report.get("输出文档"):
        pieces.append(f"<br>输出文档：{html.escape(report['输出文档'])}")
    pieces.append("</div>")
    sections = [("处理统计", audit.get("counts", {})),
                ("格式预检", audit.get("preflight", [])),
                ("识别到的标题", audit.get("headings", [])),
                ("处理后复核", audit.get("postflight", [])),
                ("纠错与内容提示", audit.get("corrections", [])),
                ("提示", report.get("提示", []))]
    for title, values in sections:
        pieces.append(f"<section class='card'><h2>{html.escape(title)}</h2>")
        if isinstance(values, dict):
            values = [f"{_LABELS.get(str(key), str(key))}：{_text(value)}" for key, value in values.items()]
        if values:
            pieces.append("<ul>" + "".join("<li>" + html.escape(_text(value)) + "</li>" for value in values) + "</ul>")
        else:
            message = "仅检查任务未修改文档。" if title == "处理后复核" and report.get("仅检查") else "当前规则未检出项目。"
            pieces.append(f"<p class='quiet'>{message}</p>")
        pieces.append("</section>")
    pieces.append("<p class='meta'>本报告保存在本机。纠错为规则提示，未自动替换；标题摘要和检查位置用于人工复核。"
                  + html.escape(audit.get("preset_note", "")) + "</p></main></html>")
    return "".join(pieces)


def write_official_reports(source, target, result, options, *, check_only=False):
    """Reports use processed text only; callers run redaction before formatting."""
    from .. import __version__

    target = Path(target)
    stem = target.stem + ("_公文检查" if check_only else "_公文排版报告")
    json_path, html_path = target.with_name(stem + ".json"), target.with_name(stem + ".html")
    report = {"工具": "青简工具箱 " + __version__, "时间": datetime.now().astimezone().isoformat(timespec="seconds"),
              "报告类型": "公文检查报告" if check_only else "公文排版报告", "仅检查": check_only,
              "公文种类": options.get("doc_type", "通知"), "源文件": str(source),
              "输出文档": "" if check_only else str(target), "处理统计": result.get("changes", 0),
              "检查结果": result.get("audit", {}), "提示": result.get("warnings", [])}
    # The readable report can still be delivered if the machine has no browser;
    # both files are self-contained and use no network resources.
    atomic_write(json_path, lambda staged: staged.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"))
    atomic_write(html_path, lambda staged: staged.write_text(report_html(report), encoding="utf-8"))
    return [str(html_path), str(json_path)]


def save_official_template(target, options, title, issuer, date, doc_type):
    from .official_ops import create_official_template

    target = Path(target)
    if target.suffix.lower() != ".docx":
        raise ValueError("公文样板请保存为 .docx 文件。")
    if not title.strip():
        raise ValueError("请填写样板标题。")
    document = create_official_template(options, title.strip(), issuer.strip(), date.strip(), doc_type)
    atomic_write(target, document.save)
    return target
