"""Read-only search results retain source evidence without output artefacts."""
from __future__ import annotations

from pathlib import Path

import pytest

from qingjian.core.search_session import (
    keyword_ranges, search_documents, validate_search_options,
)


def test_searches_real_word_and_text_with_locations_and_no_outputs(tmp_path):
    from docx import Document

    word = tmp_path / "公文.docx"
    document = Document()
    document.add_paragraph("这段不命中")
    document.add_paragraph("项目 Alpha 已通过审核。")
    document.add_table(rows=1, cols=1).cell(0, 0).text = "表格内 alpha 事项"
    document.save(word)
    text = tmp_path / "记录.txt"
    text.write_text("不相关\nALPHA 进展\n", encoding="utf-8")
    originals = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    emitted, progress = [], []
    report = search_documents([word, text], {"query": "alpha"},
                              lambda *args: progress.append(args), on_file=emitted.append)
    assert report.hit_count == 3
    assert report.scanned_files == report.total_files == 2
    assert report.files == emitted
    assert [hit.location for hit in report.files[0].hits] == ["正文 第2段", "正文 表格1 第1行"]
    assert report.files[1].hits[0].location == "第2行"
    assert report.files[0].hits[0].text == "项目 Alpha 已通过审核。"
    assert progress[-1][:2] == (2, 2)
    assert originals == {path.name: path.read_bytes() for path in tmp_path.iterdir()}


def test_recursive_scope_deduplicates_actual_supported_files(tmp_path):
    nested = tmp_path / "子目录"
    nested.mkdir()
    main = tmp_path / "事项.txt"
    main.write_text("Alpha", encoding="utf-8")
    (nested / "子事项.md").write_text("Alpha", encoding="utf-8")
    (tmp_path / "不支持.doc").write_text("Alpha", encoding="utf-8")
    (tmp_path / "~$暂存.txt").write_text("Alpha", encoding="utf-8")
    top = search_documents([tmp_path], {"query": "alpha", "recursive": False})
    assert top.total_files == top.hit_count == 1
    all_files = search_documents([tmp_path, main, nested], {"query": "alpha"})
    assert all_files.total_files == all_files.hit_count == 2


def test_all_keywords_must_occur_in_one_original_unit_and_are_literal(tmp_path):
    path = tmp_path / "literal.txt"
    path.write_text("alpha\nbeta\nALPHA beta a.b\naXb\n", encoding="utf-8")
    report = search_documents([path], {"query": "alpha\nbeta\nalpha", "match_mode": "all"})
    assert report.queries == ("alpha", "beta")
    assert report.hit_count == 1
    assert report.files[0].hits[0].location == "第3行"
    assert search_documents([path], {"query": "a.b"}).hit_count == 1
    assert search_documents([path], {"query": "alpha", "case_sensitive": True}).hit_count == 1


@pytest.mark.parametrize("options", [
    {}, {"query": " \n "}, {"query": 4}, {"query": "甲", "match_mode": "regex"},
    {"query": "甲", "case_sensitive": "false"}, {"query": "甲", "recursive": 1},
    {"query": "甲", "password": 123}, {"query": "\n".join(str(i) for i in range(101))},
])
def test_invalid_options_fail_before_file_reads(options, monkeypatch):
    monkeypatch.setattr("qingjian.core.search_session.expand_inputs",
                        lambda *args, **kwargs: pytest.fail("must validate before scanning"))
    with pytest.raises(ValueError):
        search_documents([], options)


def test_unicode_highlight_ranges_use_original_characters():
    text = "😀 Straße STRASSE <img>"
    ranges = keyword_ranges(text, ("STRASSE",), False)
    assert [text[start:end] for start, end in ranges] == ["Straße", "STRASSE"]
    assert keyword_ranges("ababa", ("aba", "bab"), True) == ((0, 5),)
    assert keyword_ranges("a.b aXb", ("a.b",), False) == ((0, 3),)
    assert len(keyword_ranges("x " * 1000, ("x",), False)) == 256


def test_a_corrupt_file_does_not_hide_other_results(tmp_path):
    bad = tmp_path / "损坏.docx"
    bad.write_bytes(b"not a Word zip")
    valid = tmp_path / "正常.txt"
    valid.write_text("关键词", encoding="utf-8")
    report = search_documents([bad, valid], {"query": "关键词"})
    assert report.error_count == 1
    assert report.errors[0].path == bad.resolve()
    assert "有效" in report.errors[0].message or "损坏" in report.errors[0].message
    assert report.hit_count == 1
    assert report.files[0].path == valid.resolve()
    assert report.scanned_files == 2
    assert not report.fatal


def test_cancel_keeps_already_found_hits_and_closes_reader(tmp_path, monkeypatch):
    path = tmp_path / "大文件.txt"
    path.write_text("fake", encoding="utf-8")
    state = {"cancel": False, "closed": False}

    def units(*_args):
        try:
            yield "第1段", "关键词一"
            state["cancel"] = True
            yield "第2段", "关键词二"
        finally:
            state["closed"] = True

    monkeypatch.setattr("qingjian.core.search_session.iter_text_units", units)
    report = search_documents([path], {"query": "关键词"}, cancel=lambda: state["cancel"])
    assert report.cancelled and report.hit_count == 1
    assert report.files[0].partial
    assert state["closed"]
    assert not report.errors


def test_cancel_during_directory_enumeration_does_not_read_documents(tmp_path, monkeypatch):
    (tmp_path / "子目录").mkdir()
    state = {"cancel": False}

    def progress(_done, _total, message):
        if "已找到" in message:
            state["cancel"] = True

    monkeypatch.setattr("qingjian.core.search_session.iter_text_units",
                        lambda *_args: pytest.fail("cancelled enumeration must not open a document"))
    report = search_documents([tmp_path], {"query": "甲"}, progress, lambda: state["cancel"])
    assert state["cancel"]
    assert report.cancelled
    assert report.scanned_files == 0


def test_hit_limit_reports_incomplete_scope_and_keeps_bounded_results(tmp_path, monkeypatch):
    path = tmp_path / "记录.txt"
    path.write_text("fake", encoding="utf-8")
    closed = []

    def units(*_args):
        try:
            for number in range(100):
                yield f"第{number}段", "hello alpha"
        finally:
            closed.append(True)

    monkeypatch.setattr("qingjian.core.search_session.iter_text_units", units)
    report = search_documents([path], {"query": "alpha"}, max_hits=2)
    assert report.limited and report.hit_count == 2
    assert len(report.files[0].hits) == 2 and report.files[0].partial
    assert any("后续" in warning for warning in report.warnings)
    assert closed


def test_long_preview_contains_match_and_character_budget_is_explicit(tmp_path):
    path = tmp_path / "长行.txt"
    path.write_text("无" * 10000 + "关键词" + "后" * 10000, encoding="utf-8")
    report = search_documents([path], {"query": "关键词"}, preview_characters=100,
                              max_characters=100)
    hit = report.files[0].hits[0]
    assert len(hit.text) == report.stored_characters == 100
    assert "关键词" in hit.text
    assert hit.truncated and report.preview_truncated_count == 1 and report.limited
    assert any("片段" in warning for warning in report.warnings)


def test_real_pdf_excel_and_local_html_preserve_source_position(tmp_path):
    from openpyxl import Workbook
    from reportlab.pdfgen.canvas import Canvas

    pdf = tmp_path / "通知.pdf"
    canvas = Canvas(str(pdf))
    canvas.drawString(20, 800, "Alpha project")
    canvas.save()
    spreadsheet = tmp_path / "清单.xlsx"
    book = Workbook()
    book.active.title = "任务"
    book.active.append(["Alpha", "project"])
    book.save(spreadsheet)
    book.close()
    html = tmp_path / "本地网页.html"
    html.write_text('<p>Alpha project</p><script>Alpha hidden</script><img src="https://invalid.example/a">', encoding="utf-8")
    originals = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    report = search_documents([pdf, spreadsheet, html], {"query": "Alpha"})
    assert report.error_count == 0
    assert report.hit_count == 3
    assert report.files[0].hits[0].location == "第1页"
    assert "工作表 [任务] 第1行" == report.files[1].hits[0].location
    assert "文本块1" in report.files[2].hits[0].location
    assert report.files[2].hits[0].text == "Alpha project"
    assert originals == {path.name: path.read_bytes() for path in tmp_path.iterdir()}


def test_empty_scope_is_understandable(tmp_path):
    report = search_documents([tmp_path], {"query": "不存在"})
    assert report.total_files == report.hit_count == 0
    assert "没有支持检索" in report.warnings[0]
