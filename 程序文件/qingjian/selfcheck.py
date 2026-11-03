"""Offline checks executable from the frozen application without Python installed."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path


def run(report_path: Path) -> int:
    from docx import Document
    from PIL import Image
    import pypdfium2
    from .batch import dispatch
    from .core.conversion import find_libreoffice

    report_path.parent.mkdir(parents=True, exist_ok=True)
    result = {"frozen": bool(getattr(sys, "frozen", False)), "python": sys.version,
              "executable": sys.executable, "checks": [], "passed": False}

    def check(name, function):
        try:
            detail = function()
            result["checks"].append({"name": name, "ok": True, "detail": detail})
        except Exception:
            result["checks"].append({"name": name, "ok": False, "error": traceback.format_exc()})
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="qingjian_check_", dir=report_path.parent) as temp:
        folder = Path(temp)
        doc = Document()
        doc.add_heading("青简离线功能检查", 0)
        p = doc.add_paragraph()
        p.add_run("联系电话 138")
        p.add_run("12345678，测试替换内容。")
        doc.add_table(2, 2).cell(0, 0).text = "基础表格"
        source = folder / "input.docx"
        doc.save(source)

        def word():
            target = folder / "word.docx"
            info = dispatch("word", str(source), target,
                            {"format_enabled": True, "mask_phone": True,
                             "replacements": [{"find": "测试替换", "replace": "处理后的", "regex": False}]})
            text = "".join(p.text for p in Document(target).paragraphs)
            assert "13812345678" not in text and "处理后的" in text
            return info
        check("word_cross_run_mask_replace", word)

        def image():
            path = folder / "image.png"
            Image.new("RGB", (640, 480), "#287d70").save(path)
            target = folder / "crop.png"
            info = dispatch("image", str(path), target, {"mode": "ratio", "ratio_w": 1, "ratio_h": 1})
            with Image.open(target) as cropped:
                assert cropped.size == (480, 480)
            return info
        check("image_crop", image)

        def html():
            path = folder / "article.html"
            path.write_text('<html><meta charset="utf-8"><title>本地网页</title>'
                            '<article><h1>离线网页检查</h1><p>LOCAL_HTML_OK 中文正文。</p>'
                            '<table><tr><td>A</td><td>B</td></tr></table></article></html>', encoding="utf-8")
            target = folder / "web.docx"
            info = dispatch("web", str(path), target, {"include_images": True})
            converted = Document(target)
            assert any("LOCAL_HTML_OK" in p.text for p in converted.paragraphs)
            assert len(converted.tables) == 1
            return info
        check("local_html_to_word", html)

        engine = find_libreoffice()
        result["libreoffice"] = str(engine) if engine else None
        pdf = folder / "converted.pdf"

        def word_pdf():
            assert engine, "Bundled LibreOffice not found"
            info = dispatch("convert", str(source), pdf, {"timeout": 120})
            with pypdfium2.PdfDocument(pdf) as loaded:
                assert len(loaded) == 1
                page = loaded[0]
                bitmap = page.render(scale=1)
                image = bitmap.to_pil()
                image.save(report_path.parent / "转换检查.png")
                bitmap.close()
                page.close()
            return info
        check("word_to_pdf_bundled_engine", word_pdf)

        def pdf_word(mode):
            target = folder / f"pdf_{mode}.docx"
            info = dispatch("convert", str(pdf), target, {"pdf_mode": mode, "dpi": 100})
            converted = Document(target)
            if mode == "appearance":
                assert len(converted.inline_shapes) == 1
            else:
                assert "138" in "".join(p.text for p in converted.paragraphs)
            return info
        check("pdf_to_editable_word", lambda: pdf_word("text"))
        check("pdf_to_appearance_word", lambda: pdf_word("appearance"))

        # The v0.2 checks use the same public dispatch path as the UI.  All
        # fixtures, including rename/undo inputs, belong to this private folder.
        from openpyxl import Workbook, load_workbook
        from pypdf import PdfReader, PdfWriter
        from .tools import execute_tool
        from .core.excel_ops import read_table
        from .core.file_ops import plan_organize

        fixtures = folder / "fixtures"
        fixtures.mkdir()
        generated = folder / "new_tool_results"
        generated.mkdir()

        def save_book(path, rows):
            workbook = Workbook()
            for row in rows:
                workbook.active.append(row)
            workbook.save(path)
            workbook.close()
            return path

        def book_rows(path, sheet=None):
            workbook = load_workbook(path, read_only=True, data_only=True)
            try:
                page = workbook[sheet] if sheet else workbook.worksheets[0]
                return list(page.iter_rows(values_only=True))
            finally:
                workbook.close()

        def execute(mode, inputs, label, options):
            detail = execute_tool(mode, inputs, generated / label, options)
            assert not detail.get("errors"), detail.get("errors")
            assert detail.get("outputs"), f"{mode} did not produce an output/report"
            assert all(Path(path).is_file() for path in detail["outputs"])
            return detail

        headers = ["姓名", "编号", "部门", "金额"]
        table_a = save_book(fixtures / "table_a.xlsx", [headers,
            ["甲", "0001", "A", 10], ["乙", "0002", "B", 20]])
        table_b = save_book(fixtures / "table_b.xlsx", [headers, ["丙", "0003", "A", 30]])
        table_new = save_book(fixtures / "table_new.xlsx", [headers,
            ["甲", "0001", "A", 12], ["丙", "0003", "A", 30]])
        dirty = save_book(fixtures / "dirty.xlsx", [headers,
            [" 客户甲 ", "0001", "A", "1,200.50"], [None, None, None, None],
            [" 客户甲 ", "0001", "A", "1,200.50"], ["客户乙", "0002", "B", "25"]])
        clean_options = {"trim_text": True, "remove_blank_rows": True,
                         "deduplicate": True, "key_columns": ["编号"],
                         "number_columns": ["金额"], "text_columns": ["编号"]}

        def excel_merge():
            detail = execute("excel_merge", [table_a, table_b], "excel_merge", {"action": "merge"})
            names, rows, _ = read_table(Path(detail["data_outputs"][0]))
            assert set(headers).issubset(names) and len(rows) == 3
            assert [row["编号"] for row in rows] == ["0001", "0002", "0003"]
            return detail
        check("excel_merge_values_and_ids", excel_merge)

        def excel_split():
            detail = execute("excel_merge", [table_a, table_b], "excel_split",
                             {"action": "split", "split_column": "部门"})
            assert len(detail["data_outputs"]) == 2
            groups = [read_table(Path(path))[1] for path in detail["data_outputs"]]
            assert sorted(len(rows) for rows in groups) == [1, 2]
            assert all(len({row["部门"] for row in rows}) == 1 for rows in groups)
            return detail
        check("excel_split_by_column", excel_split)

        def excel_clean():
            before = dirty.read_bytes()
            detail = execute("excel_clean", [dirty], "excel_clean", clean_options)
            _, rows, _ = read_table(Path(detail["data_outputs"][0]))
            assert len(rows) == 2 and rows[0]["姓名"] == "客户甲"
            assert rows[0]["编号"] == "0001" and float(rows[0]["金额"]) == 1200.5
            assert dirty.read_bytes() == before
            return detail
        check("excel_clean_trim_dedupe_numeric", excel_clean)

        def excel_compare():
            detail = execute("excel_compare", [table_a, table_new], "excel_compare", {"key_columns": ["编号"]})
            workbook = Path(detail["outputs"][0])
            assert len(book_rows(workbook, "新增记录")) == 2
            assert len(book_rows(workbook, "删除记录")) == 2
            assert len(book_rows(workbook, "字段修改")) == 2
            return detail
        check("excel_compare_added_removed_modified", excel_compare)

        template = fixtures / "template.docx"
        template_doc = Document()
        template_p = template_doc.add_paragraph()
        template_p.add_run("客户：{{姓").bold = True
        template_p.add_run("名}}；编号：{{编号}}")
        template_doc.add_table(1, 1).cell(0, 0).text = "部门：{{部门}}"
        template_doc.sections[0].header.paragraphs[0].text = "{{姓名}}"
        template_doc.save(template)
        template_options = {"template_path": str(template), "filename_template": "{序号:03d}_{姓名}",
                            "missing_policy": "error", "output_pdf": False}

        def template_fill():
            before = template.read_bytes()
            detail = execute("template_fill", [table_a], "template_fill", template_options)
            assert len(detail["data_outputs"]) == 2
            first = Document(detail["data_outputs"][0])
            assert "客户：甲；编号：0001" == first.paragraphs[0].text
            assert first.paragraphs[0].runs[0].bold
            assert first.tables[0].cell(0, 0).text == "部门：A"
            assert first.sections[0].header.paragraphs[0].text == "甲"
            assert template.read_bytes() == before
            return detail
        check("template_fill_cross_run_and_table", template_fill)

        def extract_fields():
            detail = execute("extract_fields", [source], "extract_fields", {
                "fields": [{"name": "手机", "pattern": r"(1[3-9]\d{9})"}], "include_tables": True})
            rows = book_rows(detail["outputs"][0], "字段提取")
            assert any("13812345678" == str(value) for row in rows[1:] for value in row)
            assert len(book_rows(detail["outputs"][0], "识别表格")) > 1
            return detail
        check("extract_fields_and_tables_with_provenance", extract_fields)

        pages_a, pages_b = fixtures / "pages_a.pdf", fixtures / "pages_b.pdf"
        with PdfWriter() as writer:
            for index in range(3):
                page = writer.add_blank_page(width=100 + index * 10, height=200 + index * 10)
                if index == 1:
                    page.rotate(90)
            with pages_a.open("wb") as stream:
                writer.write(stream)
        with PdfWriter() as writer:
            writer.add_blank_page(width=300, height=400)
            with pages_b.open("wb") as stream:
                writer.write(stream)

        def pdf_pages():
            merged = execute("pdf_pages", [pages_b, pages_a], "pdf_merge", {"action": "merge"})
            merged_path = Path(merged["data_outputs"][0])
            loaded = PdfReader(merged_path)
            assert [float(page.mediabox.width) for page in loaded.pages] == [300, 100, 110, 120]
            assert loaded.pages[2].rotation == 90
            extracted = execute("pdf_pages", [merged_path], "pdf_extract", {"action": "extract", "pages": "4-3,1,1"})
            loaded = PdfReader(extracted["data_outputs"][0])
            assert [float(page.mediabox.width) for page in loaded.pages] == [120, 110, 300, 300]
            split = execute("pdf_pages", [pages_a], "pdf_split", {"action": "split", "split_every": 2})
            assert [len(PdfReader(path).pages) for path in split["data_outputs"]] == [2, 1]
            reordered = execute("pdf_pages", [pages_a], "pdf_reorder", {"action": "reorder", "pages": "3,2,1"})
            assert [float(page.mediabox.width) for page in PdfReader(reordered["data_outputs"][0]).pages] == [120, 110, 100]
            return {"merge": merged, "extract": extracted, "split": split, "reorder": reordered}
        check("pdf_pages_merge_extract_split_reorder", pdf_pages)

        alpha = fixtures / "transparent.png"
        with Image.new("RGBA", (120, 80), (20, 90, 160, 100)) as image_fixture:
            image_fixture.save(alpha)

        def image_pack():
            compressed = execute("image_pack", [alpha], "image_compress", {
                "action": "compress", "output_format": "png", "max_width": 60,
                "max_height": 60, "target_kb": 4})
            with Image.open(compressed["data_outputs"][0]) as converted:
                assert converted.size == (60, 40) and converted.mode == "RGBA"
                assert not converted.getexif()
            assert Path(compressed["data_outputs"][0]).stat().st_size <= 4 * 1024
            combined = execute("image_pack", [folder / "image.png", alpha], "image_pdf", {
                "action": "pdf", "pdf_page_size": "A4", "pdf_landscape": True})
            loaded = PdfReader(combined["data_outputs"][0])
            assert len(loaded.pages) == 2
            assert all(float(page.mediabox.width) > float(page.mediabox.height) for page in loaded.pages)
            return {"compress": compressed, "pdf": combined}
        check("image_pack_transparency_size_and_pdf", image_pack)

        def content_search():
            detail = execute("content_search", [source, table_a], "content_search", {
                "query": "13812345678\n0001", "match_mode": "any", "case_sensitive": False})
            assert detail["changes"] >= 2
            rows = book_rows(detail["outputs"][0])
            text = "\n".join(str(value) for row in rows for value in row)
            assert "13812345678" in text and "0001" in text
            return detail
        check("content_search_word_and_excel", content_search)

        duplicate_a, duplicate_b = fixtures / "duplicate_a.txt", fixtures / "duplicate_b.bin"
        duplicate_a.write_bytes(b"SAME_BYTES_FOR_OFFLINE_CHECK")
        duplicate_b.write_bytes(duplicate_a.read_bytes())

        def find_duplicates():
            detail = execute("find_duplicates", [duplicate_a, duplicate_b], "find_duplicates", {})
            assert detail["changes"] == 1
            assert len(book_rows(detail["outputs"][0], "重复文件")) == 3
            assert duplicate_a.read_bytes() == duplicate_b.read_bytes()
            return detail
        check("find_duplicates_hash_only_no_delete", find_duplicates)

        materials = fixtures / "materials"
        materials.mkdir()
        (materials / "required.txt").write_text("present", encoding="utf-8")
        save_book(materials / "material.xlsx", [["姓名", "必填字段"], ["客户", None]])

        def check_materials():
            detail = execute("check_materials", [materials], "check_materials", {
                "required_files": ["required.txt", "missing.txt"], "required_columns": ["姓名", "必填字段"]})
            assert detail["changes"] >= 2
            rows = book_rows(detail["outputs"][0], "文件缺漏")
            assert any("missing.txt" in row and "缺漏" in row for row in rows)
            assert any("必填字段为空" in row for row in book_rows(detail["outputs"][0], "必填字段异常"))
            return detail
        check("check_materials_missing_files_and_values", check_materials)

        def file_organize_copy():
            target_folder = generated / "file_copy"
            options = {"action": "copy", "group_by": "extension", "filename_template": "{序号:03d}_{原名}"}
            inputs = [duplicate_a, duplicate_b]
            plan = plan_organize(inputs, target_folder, options)
            assert all(Path(row["source"]).is_file() and not Path(row["target"]).exists() for row in plan)
            detail = execute("file_organize", inputs, "file_copy", {**options, "_plan": plan})
            assert len(detail["data_outputs"]) == 2
            for row in plan:
                assert Path(row["source"]).read_bytes() == Path(row["target"]).read_bytes()
                assert Path(row["target"]).parent.name in {"txt", "bin"}
            return detail
        check("file_organize_preview_and_copy", file_organize_copy)

        def file_organize_rename_undo():
            rename_folder = fixtures / "rename_inputs"
            rename_folder.mkdir()
            inputs = [rename_folder / "old-a.txt", rename_folder / "old-b.txt"]
            for index, path in enumerate(inputs):
                path.write_text(f"CONTENT-{index}", encoding="utf-8")
            originals = [path.read_bytes() for path in inputs]
            target_folder = generated / "file_rename"
            options = {"action": "rename", "group_by": "none", "filename_template": "renamed_{序号:02d}_{原名}"}
            plan = plan_organize(inputs, target_folder, options)
            assert all(path.exists() for path in inputs)
            renamed = execute("file_organize", inputs, "file_rename", {**options, "_plan": plan})
            assert not any(path.exists() for path in inputs)
            assert [Path(path).read_bytes() for path in renamed["data_outputs"]] == originals
            manifest = next(Path(path) for path in renamed["outputs"] if Path(path).suffix.lower() == ".json")
            restored = execute("undo_rename", [manifest], "file_undo", {})
            assert [path.read_bytes() for path in inputs] == originals
            assert not any(Path(row["target"]).exists() for row in plan)
            return {"rename": renamed, "undo": restored}
        check("file_organize_in_place_rename_and_undo", file_organize_rename_undo)

        def workflow():
            detail = execute("workflow", [dirty], "workflow", {"archive": True, "stop_on_error": True,
                "steps": [{"tool": "excel_clean", "options": clean_options},
                          {"tool": "template_fill", "options": template_options}]})
            assert len(detail["data_outputs"]) == 2
            assert all(Path(path).suffix.lower() == ".docx" for path in detail["data_outputs"])
            texts = [Document(path).paragraphs[0].text for path in detail["data_outputs"]]
            assert any("客户甲" in text and "0001" in text for text in texts)
            assert any("客户乙" in text and "0002" in text for text in texts)
            from zipfile import ZipFile
            archive = next(Path(path) for path in detail["outputs"] if Path(path).suffix.lower() == ".zip")
            with ZipFile(archive) as packed:
                assert len(packed.namelist()) == 2 and all(name.endswith(".docx") for name in packed.namelist())
            return detail
        check("workflow_excel_clean_to_template_and_archive", workflow)

        # v0.3 exercises the same public entry points used by the new controls.
        official_source = fixtures / "official.docx"
        official_doc = Document()
        for text in ("关于自检工作的通知", "一、总体安排", "（一）具体要求", "1.落实任务", "（1）办理时限"):
            official_doc.add_paragraph(text)
        contact = official_doc.add_paragraph("联系人张三，电话138")
        contact.add_run("12345678。")
        official_doc.save(official_source)
        source_bytes = official_source.read_bytes()

        def official_format():
            from docx.shared import Pt
            target = folder / "official-result.docx"
            info = dispatch("word", str(official_source), target, {
                "official_enabled": True, "official": {"page_numbers": "outside"},
                "mask_phone": True, "mask_phone_replacement": "[联系电话]",
                "custom_sensitive_rules": [{"find": "张三", "replace": "张先生"}]})
            loaded = Document(target)
            assert official_source.read_bytes() == source_bytes
            assert loaded.paragraphs[0].runs[0].font.size == Pt(22)
            assert loaded.paragraphs[-1].runs[0].font.name == "仿宋"
            assert loaded.paragraphs[-1].paragraph_format.line_spacing == Pt(28)
            assert "张先生" in loaded.paragraphs[-1].text and "[联系电话]" in loaded.paragraphs[-1].text
            for path in info["reports"]:
                assert "13812345678" not in Path(path).read_text(encoding="utf-8")
            return {"reports": len(info["reports"]), "source_unchanged": True}
        check("official_format_four_levels_and_custom_redaction", official_format)

        def official_inspect():
            target = folder / "check-only.docx"
            info = dispatch("word", str(official_source), target, {
                "official_enabled": True, "official": {"operation": "check"}})
            assert not target.exists() and official_source.read_bytes() == source_bytes
            assert Path(info["primary_output"]).suffix == ".html"
            assert all(Path(path).is_file() for path in info["reports"])
            return {"reports": len(info["reports"]), "changes": info["changes"]}
        check("official_read_only_inspection_report", official_inspect)

        def official_template():
            from .core.official_reports import save_official_template
            target = folder / "会议纪要样板.docx"
            save_official_template(target, {}, "自检会议纪要", "示例单位", "2026年10月29日", "会议纪要")
            loaded = Document(target)
            assert loaded.paragraphs[0].text == "自检会议纪要"
            assert loaded.paragraphs[-2].text == "示例单位"
            assert not any("〔" in paragraph.text and "号" in paragraph.text for paragraph in loaded.paragraphs)
            return {"template": "会议纪要", "editable": True}
        check("official_template_without_document_number", official_template)

        def txt_rules():
            source = fixtures / "text-gb18030.txt"
            source.write_bytes("张三电话13812345678\r\n删除这行\r\n\r\n".encode("gb18030"))
            original = source.read_bytes()
            target = folder / "text-result.txt"
            info = dispatch("word", str(source), target, {
                "custom_sensitive_rules": [{"find": "张三", "replace": "张先生"}],
                "mask_phone": True, "mask_phone_replacement": "[联系电话]",
                "delete_paragraphs": ["删除"], "remove_blank_paragraphs": True})
            assert target.read_text(encoding="utf-8") == "张先生电话[联系电话]\n"
            assert source.read_bytes() == original
            return info
        check("txt_encoding_custom_rules_and_safe_output", txt_rules)

        def export_text_and_images():
            image_path = fixtures / "embedded.png"
            Image.new("RGB", (32, 24), "#5353d7").save(image_path)
            document = Document()
            document.add_paragraph("EXPORT_TEXT_OK")
            document.add_picture(str(image_path))
            source = fixtures / "image-document.docx"
            document.save(source)
            original = source.read_bytes()
            images = execute("document_export", [source], "export_images", {"action": "images"})
            assert len(images["data_outputs"]) == 1
            assert Path(images["data_outputs"][0]).read_bytes() == image_path.read_bytes()
            text = execute("document_export", [source, pdf], "export_text", {"action": "text"})
            assert len(text["data_outputs"]) == 2
            assert any("EXPORT_TEXT_OK" in Path(path).read_text(encoding="utf-8") for path in text["data_outputs"])
            assert source.read_bytes() == original
            return {"images": len(images["data_outputs"]), "text_files": len(text["data_outputs"])}
        check("document_plain_text_and_original_images", export_text_and_images)

        def ppt_template():
            import hashlib
            from zipfile import ZipFile
            runtime = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
            template = runtime / "示例材料" / "PPT模板示例.pptx"
            assert template.is_file(), "随包PPT模板示例缺失"
            before = hashlib.sha256(template.read_bytes()).hexdigest()
            data = save_book(fixtures / "ppt-rows.xlsx", [["姓名", "部门", "编号", "内容"],
                ["自检甲", "综合部", "0001", "事项甲"], ["自检乙", "业务部", "0002", "事项乙"]])
            detail = execute("template_fill", [data], "ppt_templates", {
                "template_path": str(template), "filename_template": "{序号:03d}_{姓名}"})
            assert len(detail["data_outputs"]) == 2
            for path in detail["data_outputs"]:
                assert Path(path).suffix == ".pptx"
                with ZipFile(path) as archive:
                    assert archive.testzip() is None
                    text = "".join(archive.read(name).decode("utf-8") for name in archive.namelist()
                                   if name.startswith("ppt/slides/") and name.endswith(".xml"))
                    assert "自检" in text and "{{" not in text
            assert hashlib.sha256(template.read_bytes()).hexdigest() == before
            return {"generated_presentations": len(detail["data_outputs"]), "template_unchanged": True}
        check("pptx_template_text_table_groups_and_safe_output", ppt_template)

        def remembered_redaction():
            from .redaction_memory import RedactionMemoryStore
            library_path = fixtures / "redaction-library.json"
            rules = [{"find": f"MEMORY_{index:04d}", "replace": f"匿名{index:04d}"} for index in range(200)]
            store = RedactionMemoryStore(library_path)
            group = store.save_group("离线自检规则", rules)
            store.set_restore_group(group["id"])
            restored = RedactionMemoryStore(library_path)
            assert restored.restore_id == group["id"]
            assert restored.find_group(group["id"])["rules"] == rules
            source = fixtures / "remembered-rules.txt"
            original = " ".join(rule["find"] for rule in rules)
            source.write_text(original, encoding="utf-8")
            target = folder / "remembered-result.txt"
            dispatch("word", str(source), target, {"custom_sensitive_rules": restored.find_group(group["id"])["rules"]})
            assert target.read_text(encoding="utf-8") == " ".join(rule["replace"] for rule in rules)
            assert source.read_text(encoding="utf-8") == original
            assert restored.delete_group(group["id"])
            assert RedactionMemoryStore(library_path).restore_id == ""
            return {"remembered_rules": len(rules), "restored_after_reload": True, "original_unchanged": True}
        check("redaction_memory_reload_and_bulk_replacement", remembered_redaction)

        def notebook_reminder():
            from datetime import datetime, timedelta
            from .notebook_store import NotebookStore
            now = datetime(2026, 10, 29, 9, 0, 0)
            path = fixtures / "notebook.json"
            store = NotebookStore(path)
            task = store.upsert_task({"title": "离线提醒检查", "body": "只使用检查目录的示例事项", "priority": "important",
                                      "date": "2026-10-29", "remind_at": "2026-10-29T09:05:00",
                                      "repeat": {"kind": "daily", "interval": 1}, "lead_minutes": 5}, now=now)
            alerts = store.collect_due(now)
            assert len(alerts) == 1 and alerts[0]["task_id"] == task["id"]
            store.snooze(alerts[0]["id"], 10, now=now)
            restored = NotebookStore(path)
            assert restored.collect_due(now + timedelta(minutes=9)) == []
            due = restored.collect_due(now + timedelta(minutes=10))
            assert len(due) == 1 and due[0]["id"] == alerts[0]["id"]
            restored.acknowledge(due[0]["id"], now=now + timedelta(minutes=10))
            assert not restored.find_task(task["id"])["completed"]
            assert restored.find_task(task["id"])["next_due"] == "2026-10-30T09:05:00"
            restored.complete_task(task["id"], True, now=now + timedelta(minutes=10))
            assert NotebookStore(path).find_task(task["id"])["completed"]
            return {"persistent_snooze": True, "repeat_advanced": True, "completed": True}
        check("notebook_persistent_snooze_and_repeat", notebook_reminder)

        def perpetual_calendar():
            from datetime import date
            from .notebook_calendar import calendar_info
            new_year = calendar_info(date(2025, 1, 29))
            assert new_year["lunar_month"] == "正月" and new_year["lunar_day"] == "初一"
            assert calendar_info(date(2025, 2, 3))["term"] == "立春"
            return {"offline_lunar_and_solar_terms": True, "year_range": [1900, 2100]}
        check("offline_perpetual_lunar_calendar", perpetual_calendar)

        # Exercise every new tool through the same dispatcher used by the UI.
        from .feature_registry import SPECS as office_specs
        from .guides import build_example
        from .tools import execute_tool
        import copy
        import hashlib
        for mode, spec in office_specs.items():
            def office_check(mode=mode, spec=spec):
                area = folder / ("office_" + mode)
                example = area / "example"
                example.mkdir(parents=True)
                sources, values = build_example(mode, example)
                options = {field["key"]: copy.deepcopy(field["default"]) for field in spec["fields"]}
                options.update(values)
                before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in example.rglob("*") if path.is_file()}
                detail = execute_tool(mode, sources, area / "result", options)
                assert not detail.get("errors"), detail
                assert detail["outputs"] and all(Path(path).is_file() for path in detail["outputs"])
                assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == value for path, value in before.items())
                return {"outputs": len(detail["outputs"]), "data_outputs": len(detail.get("data_outputs", [])),
                        "inputs_unchanged": True, "changes": detail["changes"]}
            check("office_" + mode, office_check)

        # The same immediate-conversion path is exercised in the frozen app.
        from .quick_actions import ACTIONS, run_quick_action
        quick_sources = {"to_pdf": source, "pdf_to_word": pdf,
                         "web_to_word": folder / "article.html", "to_text": source,
                         "image_to_jpg": folder / "image.png", "image_to_png": folder / "image.png"}
        for action_id in ACTIONS:
            def quick_check(action_id=action_id):
                original = quick_sources[action_id]
                before = original.read_bytes()
                detail = run_quick_action(action_id, [str(original)], folder / "unused_url_downloads")
                assert not detail.get("errors"), detail
                assert len(detail["outputs"]) == 1
                output = Path(detail["outputs"][0])
                assert output.is_file() and output.parent == original.parent
                assert output.resolve() != original.resolve() and original.read_bytes() == before
                assert not (folder / "unused_url_downloads").exists()
                return {"source_unchanged": True, "same_folder": True, "output": output.name}
            check("quick_" + action_id, quick_check)

        def mhtml_check():
            from email.message import EmailMessage
            message = EmailMessage()
            message.set_type("multipart/related")
            html_part = EmailMessage()
            html_part.set_content('<html><title>离线归档检查</title><article><p>MHTML_OFFLINE_OK</p><img src="cid:local-picture"></article></html>', subtype="html", charset="utf-8")
            html_part["Content-Location"] = "https://offline.invalid/article.html"
            message.attach(html_part)
            image_part = EmailMessage()
            image_part.set_content((folder / "image.png").read_bytes(), maintype="image", subtype="png")
            image_part["Content-ID"] = "<local-picture>"
            message.attach(image_part)
            archived = folder / "saved-page.mhtml"
            archived.write_bytes(message.as_bytes())
            before = archived.read_bytes()
            detail = run_quick_action("web_to_word", [str(archived)], folder / "unused_url_downloads")
            assert not detail.get("errors"), detail
            loaded = Document(detail["outputs"][0])
            assert any("MHTML_OFFLINE_OK" in paragraph.text for paragraph in loaded.paragraphs)
            assert len(loaded.inline_shapes) == 1 and archived.read_bytes() == before
            return {"offline_embedded_image": True, "source_unchanged": True}
        check("local_mhtml_embedded_image_to_word", mhtml_check)

        from .conversion_checks import run as check_conversions
        check_conversions(folder, check)

    result["passed"] = all(entry["ok"] for entry in result["checks"])
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["passed"] else 1
