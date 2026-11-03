from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docx import Document
from openpyxl import Workbook, load_workbook
from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter

from qingjian.core.document_ops import run_documents


class DocumentOpsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.folder = self.root / "outputs"

    def tearDown(self):
        self.temporary.cleanup()

    def csv(self, rows, name="data.csv"):
        path = self.inputs / name
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            csv.writer(stream).writerows(rows)
        return path

    def template(self, text="您好 {{姓名}}，编号 {{编号}}", rich=False):
        path = self.inputs / "template.docx"
        document = Document()
        if rich:
            paragraph = document.add_paragraph()
            paragraph.add_run("您好 {{姓").bold = True
            paragraph.add_run("名}}，编号 {{编号}}")
            document.add_table(1, 1).cell(0, 0).text = "部门：{{部门}}"
            document.sections[0].header.paragraphs[0].text = "{{姓名}}"
            document.sections[0].footer.paragraphs[0].text = "{{编号}}"
            picture = self.inputs / "logo.png"
            Image.new("RGB", (20, 20), "blue").save(picture)
            document.add_paragraph().add_run().add_picture(str(picture))
        else:
            document.add_paragraph(text)
        document.save(path)
        return path

    def pdf(self, name, pages=3, password=None):
        path = self.inputs / name
        with PdfWriter() as writer:
            for index in range(pages):
                page = writer.add_blank_page(width=100 + 10 * index, height=200 + 10 * index)
                if index == 1:
                    page.rotate(90)
            writer.add_outline_item("First chapter", 0)
            if password:
                writer.encrypt(password)
            with path.open("wb") as stream:
                writer.write(stream)
        return path

    def reports(self, result):
        paths = [Path(path) for path in result["outputs"]]
        self.assertTrue(any(path.suffix == ".xlsx" for path in paths))
        self.assertTrue(any(path.suffix == ".json" for path in paths))
        self.assertTrue(all(path.is_file() for path in paths))
        self.assertTrue(all(Path(path).suffix not in {".xlsx", ".json"} for path in result["data_outputs"]))
        report = next(path for path in paths if path.suffix == ".json")
        return json.loads(report.read_text(encoding="utf-8"))

    def test_template_preserves_cross_run_format_tables_headers_footers_and_images(self):
        template = self.template(rich=True)
        source = self.csv([["姓名", "编号", "部门"], ["张三", "00001234567890123456", "战略规划"]])
        original = template.read_bytes(), source.read_bytes()
        result = run_documents("template_fill", [source], self.folder, {"template_path": str(template)})
        self.assertFalse(result["errors"], result["errors"])
        self.assertEqual(result["changes"], 1)
        document = Document(result["data_outputs"][0])
        self.assertIn("张三", document.paragraphs[0].text)
        self.assertIn("00001234567890123456", document.paragraphs[0].text)
        self.assertTrue(document.paragraphs[0].runs[0].bold)
        self.assertIn("战略规划", document.tables[0].cell(0, 0).text)
        self.assertEqual(document.sections[0].header.paragraphs[0].text, "张三")
        self.assertEqual(document.sections[0].footer.paragraphs[0].text, "00001234567890123456")
        self.assertEqual(len(document.inline_shapes), 1)
        self.assertEqual((template.read_bytes(), source.read_bytes()), original)
        self.reports(result)

    def test_template_missing_fields_error_and_blank_policy(self):
        template = self.template("{{姓名}} - {{部门}}")
        source = self.csv([["姓名"], ["李四"]])
        result = run_documents("template_fill", [source], self.folder, {"template_path": str(template)})
        self.assertFalse(result["data_outputs"])
        self.assertTrue(any("部门" in error for error in result["errors"]))
        report = self.reports(result)
        self.assertEqual(report["records"][0]["原始行或页"], 2)
        blank = run_documents("template_fill", [source], self.folder / "blank", {
            "template_path": str(template), "missing_policy": "blank"})
        self.assertFalse(blank["errors"])
        self.assertEqual(Document(blank["data_outputs"][0]).paragraphs[0].text, "李四 - ")
        self.assertTrue(blank["warnings"])

    def test_template_rejects_jinja_code_attribute_access_and_expressions(self):
        source = self.csv([["姓名"], ["客户"]])
        for text in ("{{7 * 7}}", "{{姓名.__class__}}", "{% for x in data %}{{姓名}}{% endfor %}", "{{__import__('os')}}"):
            with self.subTest(text=text):
                template = self.template(text)
                result = run_documents("template_fill", [source], self.folder, {"template_path": str(template)})
                self.assertTrue(result["errors"])
                self.assertFalse(result["data_outputs"])

    def test_template_excel_text_ids_zero_format_and_uncached_formula(self):
        source = self.inputs / "data.xlsx"
        book = Workbook()
        sheet = book.active
        sheet.append(["姓名", "编号"])
        sheet.append(["客户甲", "00001234567890123456"])
        sheet.append(["客户乙", 123])
        sheet["B3"].number_format = "000000"
        book.save(source); book.close()
        template = self.template()
        result = run_documents("template_fill", [source], self.folder, {"template_path": str(template)})
        self.assertFalse(result["errors"], result["errors"])
        self.assertIn("00001234567890123456", Document(result["data_outputs"][0]).paragraphs[0].text)
        self.assertIn("000123", Document(result["data_outputs"][1]).paragraphs[0].text)
        book = load_workbook(source)
        book.active["B2"] = "=1+2"
        book.save(source); book.close()
        formula = run_documents("template_fill", [source], self.folder / "formula", {
            "template_path": str(template), "missing_policy": "blank"})
        self.assertTrue(any("公式" in error for error in formula["errors"]))
        self.assertEqual(len(formula["data_outputs"]), 1)

    def test_template_accepts_xlsm_as_read_only_data_without_changing_source(self):
        source = self.inputs / "macro-enabled.xlsm"
        book = Workbook()
        book.active.append(["姓名", "编号"])
        book.active.append(["宏表客户", "000123"])
        book.save(source); book.close()
        original = source.read_bytes()
        result = run_documents("template_fill", [source], self.folder, {"template_path": str(self.template())})
        self.assertFalse(result["errors"], result["errors"])
        self.assertEqual(len(result["data_outputs"]), 1)
        self.assertIn("000123", Document(result["data_outputs"][0]).paragraphs[0].text)
        self.assertTrue(any("XLSM" in warning and "不执行" in warning for warning in result["warnings"]))
        self.assertEqual(source.read_bytes(), original)

    def test_recursive_switch_is_respected_by_all_document_tools(self):
        nested = self.inputs / "nested"
        nested.mkdir()
        template = self.template("{{姓名}}")
        self.csv([["姓名"], ["顶层客户"]])
        (nested / "nested.csv").write_text("姓名\n子目录客户\n", encoding="utf-8")
        root_pdf = self.pdf("root.pdf", pages=1)
        nested_pdf = self.pdf("nested-temporary.pdf", pages=1)
        nested_pdf.rename(nested / "nested.pdf")
        Image.new("RGB", (20, 20), "red").save(self.inputs / "root.png")
        Image.new("RGB", (20, 20), "blue").save(nested / "nested.png")
        # The rich template helper is not used here, so there are exactly two
        # images, two PDFs and two data files in the source tree.
        for recursive in (False, True):
            for mode, options in (
                ("template_fill", {"template_path": str(template)}),
                ("pdf_pages", {"action": "merge"}),
                ("image_pack", {"action": "compress"}),
            ):
                with self.subTest(mode=mode, recursive=recursive):
                    result = run_documents(mode, [self.inputs], self.folder / (mode + str(recursive)),
                                           {**options, "recursive": recursive})
                    self.assertFalse(result["errors"], result["errors"])
                    expected = 2 if recursive else 1
                    if mode == "pdf_pages":
                        self.assertEqual(len(PdfReader(result["data_outputs"][0]).pages), expected)
                    else:
                        self.assertEqual(len(result["data_outputs"]), expected)

    def test_template_names_cannot_escape_folder_and_duplicate_names_are_unique(self):
        template = self.template("{{姓名}}")
        source = self.csv([["姓名"], ["../../CON:客户"], ["../../CON:客户"]])
        result = run_documents("template_fill", [source], self.folder, {
            "template_path": str(template), "filename_template": "{姓名}"})
        self.assertFalse(result["errors"])
        paths = [Path(path) for path in result["data_outputs"]]
        self.assertEqual(len(set(paths)), 2)
        self.assertTrue(all(path.parent == self.folder.resolve() for path in paths))

    def test_template_pdf_failure_does_not_publish_half_a_row(self):
        template = self.template("{{姓名}}")
        source = self.csv([["姓名"], ["客户"]])
        with mock.patch("qingjian.core.conversion.convert_word", side_effect=RuntimeError("Engine test failure")):
            result = run_documents("template_fill", [source], self.folder, {"template_path": str(template), "output_pdf": True})
        self.assertTrue(result["errors"])
        self.assertFalse(result["data_outputs"])
        self.assertFalse(list(self.folder.glob("*.docx")))

    def test_template_preserves_multiline_data_and_tab_characters(self):
        template = self.template("内容：{{说明}}")
        source = self.csv([["姓名", "说明"], ["客户", "第一行\n第二行\t编号0001"]])
        result = run_documents("template_fill", [source], self.folder, {"template_path": str(template)})
        self.assertFalse(result["errors"], result["errors"])
        self.assertEqual(Document(result["data_outputs"][0]).paragraphs[0].text, "内容：第一行\n第二行\t编号0001")

    def test_template_also_generates_pdf_with_configured_engine(self):
        engine = os.environ.get("QINGJIAN_LIBREOFFICE")
        if not engine:
            self.skipTest("Set QINGJIAN_LIBREOFFICE to test template DOCX and PDF together.")
        template = self.template("离线模板：{{姓名}} / {{编号}}")
        source = self.csv([["姓名", "编号"], ["测试客户", "000123"]])
        result = run_documents("template_fill", [source], self.folder, {
            "template_path": str(template), "output_pdf": True, "libreoffice_path": engine})
        self.assertFalse(result["errors"], result["errors"])
        self.assertEqual(len(result["data_outputs"]), 2)
        pdf = next(path for path in result["data_outputs"] if path.endswith(".pdf"))
        self.assertEqual(len(PdfReader(pdf).pages), 1)

    def test_pdf_merge_preserves_queue_order_rotation_sizes_and_outlines(self):
        first = self.pdf("first.pdf", pages=2)
        second = self.pdf("second.pdf", pages=1)
        result = run_documents("pdf_pages", [second, first], self.folder, {"action": "merge"})
        self.assertFalse(result["errors"], result["errors"])
        merged = PdfReader(result["data_outputs"][0])
        self.assertEqual([float(page.mediabox.width) for page in merged.pages], [100, 100, 110])
        self.assertEqual([page.rotation for page in merged.pages], [0, 0, 90])
        self.assertGreaterEqual(len(merged.outline), 2)
        self.assertEqual(result["changes"], 3)
        self.reports(result)

    def test_pdf_extract_and_reorder_allow_descending_and_repeated_pages(self):
        source = self.pdf("source.pdf", pages=4)
        for action in ("extract", "reorder"):
            with self.subTest(action=action):
                result = run_documents("pdf_pages", [source], self.folder / action,
                                       {"action": action, "pages": "4-3,1,2,2"})
                self.assertFalse(result["errors"], result["errors"])
                pages = PdfReader(result["data_outputs"][0]).pages
                self.assertEqual([float(page.mediabox.width) for page in pages], [130, 120, 100, 110, 110])
                self.assertEqual([page.rotation for page in pages], [0, 0, 0, 90, 90])

    def test_pdf_split_has_exact_page_groups(self):
        source = self.pdf("source.pdf", pages=5)
        result = run_documents("pdf_pages", [source], self.folder, {"action": "split", "split_every": 2})
        self.assertFalse(result["errors"])
        self.assertEqual([len(PdfReader(path).pages) for path in result["data_outputs"]], [2, 2, 1])
        self.assertEqual(result["changes"], 5)

    def test_pdf_invalid_ranges_and_password_error_never_silently_drop_pages(self):
        source = self.pdf("source.pdf", pages=3)
        for specification in ("", "0", "4", "1,,2", "-1", "3-0"):
            result = run_documents("pdf_pages", [source], self.folder / ("range" + str(len(specification))),
                                   {"action": "extract", "pages": specification})
            self.assertTrue(result["errors"])
            self.assertFalse(result["data_outputs"])
        encrypted = self.pdf("encrypted.pdf", pages=1, password="secret")
        bad = run_documents("pdf_pages", [source, encrypted], self.folder / "wrong", {"action": "merge", "password": "wrong"})
        self.assertTrue(bad["errors"])
        self.assertFalse(bad["data_outputs"])
        self.assertTrue(any("没有输出缺页" in warning for warning in bad["warnings"]))
        good = run_documents("pdf_pages", [source, encrypted], self.folder / "right", {"action": "merge", "password": "secret"})
        self.assertFalse(good["errors"])
        self.assertEqual(len(PdfReader(good["data_outputs"][0]).pages), 4)

    def test_cancel_keeps_complete_results_and_writes_reports(self):
        source = self.pdf("source.pdf", pages=5)
        calls = 0
        def cancel():
            nonlocal calls
            calls += 1
            return calls >= 6
        result = run_documents("pdf_pages", [source], self.folder, {"action": "split"}, cancel=cancel)
        self.assertGreater(len(result["data_outputs"]), 0)
        self.assertLess(len(result["data_outputs"]), 5)
        self.assertTrue(any("已停止" in warning for warning in result["warnings"]))
        self.reports(result)

    def test_compress_keeps_transparency_clears_exif_and_does_not_enlarge(self):
        source = self.inputs / "transparent.png"
        image = Image.new("RGBA", (120, 80), (0, 0, 0, 0))
        ImageDraw.Draw(image).rectangle((30, 20, 90, 60), fill="red")
        exif = image.getexif(); exif[270] = "private description"
        image.save(source, exif=exif); image.close()
        original = source.read_bytes()
        result = run_documents("image_pack", [source], self.folder, {
            "action": "compress", "output_format": "png", "max_width": 60, "max_height": 60})
        self.assertFalse(result["errors"], result["errors"])
        with Image.open(result["data_outputs"][0]) as converted:
            self.assertEqual(converted.size, (60, 40))
            self.assertEqual(converted.mode, "RGBA")
            self.assertEqual(converted.getpixel((0, 0))[3], 0)
            self.assertFalse(converted.getexif())
        self.assertEqual(source.read_bytes(), original)
        small = run_documents("image_pack", [source], self.folder / "small", {"max_width": 1000, "max_height": 1000})
        with Image.open(small["data_outputs"][0]) as converted:
            self.assertEqual(converted.size, (120, 80))

    def test_compress_corrects_exif_orientation_and_jpeg_uses_white_background(self):
        source = self.inputs / "oriented.jpg"
        image = Image.new("RGB", (80, 40), "blue")
        exif = image.getexif(); exif[274] = 6
        image.save(source, exif=exif); image.close()
        result = run_documents("image_pack", [source], self.folder, {"output_format": "jpg"})
        self.assertFalse(result["errors"])
        with Image.open(result["data_outputs"][0]) as converted:
            self.assertEqual(converted.size, (40, 80))
            self.assertFalse(converted.getexif())
        transparent = self.inputs / "transparent.png"
        Image.new("RGBA", (20, 20), (0, 0, 0, 0)).save(transparent)
        result = run_documents("image_pack", [transparent], self.folder / "white", {"output_format": "jpg"})
        with Image.open(result["data_outputs"][0]) as converted:
            self.assertTrue(all(channel >= 250 for channel in converted.getpixel((0, 0))))

    def test_compress_honors_target_kb_and_reports_unattainable_target(self):
        source = self.inputs / "noise.png"
        image = Image.effect_noise((400, 400), 100).convert("RGB")
        image.save(source); image.close()
        result = run_documents("image_pack", [source], self.folder, {
            "output_format": "png", "target_kb": 5, "max_width": 400, "max_height": 400})
        self.assertFalse(result["errors"], result["errors"])
        self.assertLessEqual(Path(result["data_outputs"][0]).stat().st_size, 5 * 1024)
        with mock.patch("qingjian.core.document_ops._encode_image", return_value=b"x" * 2048):
            impossible = run_documents("image_pack", [source], self.folder / "impossible", {"target_kb": 1})
        self.assertTrue(impossible["errors"])
        self.assertFalse(impossible["data_outputs"])
        self.assertTrue(any("未达到" in error for error in impossible["errors"]))

    def test_image_pdf_keeps_queue_order_page_count_and_first_frame_notice(self):
        red = self.inputs / "red.png"
        blue = self.inputs / "blue.jpg"
        multi = self.inputs / "multi.tiff"
        Image.new("RGB", (100, 80), "red").save(red)
        Image.new("RGB", (80, 100), "blue").save(blue)
        first, second = Image.new("RGB", (100, 100), "green"), Image.new("RGB", (100, 100), "yellow")
        first.save(multi, save_all=True, append_images=[second]); first.close(); second.close()
        result = run_documents("image_pack", [blue, red, multi], self.folder, {
            "action": "pdf", "pdf_page_size": "A4", "pdf_landscape": True})
        self.assertFalse(result["errors"], result["errors"])
        pdf = PdfReader(result["data_outputs"][0])
        self.assertEqual(len(pdf.pages), 3)
        self.assertTrue(all(float(page.mediabox.width) > float(page.mediabox.height) for page in pdf.pages))
        blue_image, red_image = pdf.pages[0].images[0].image, pdf.pages[1].images[0].image
        self.assertGreater(blue_image.getpixel((10, 10))[2], 200)
        self.assertGreater(red_image.getpixel((10, 10))[0], 200)
        self.assertTrue(any("第一帧" in warning for warning in result["warnings"]))
        self.reports(result)

    def test_image_pdf_image_paper_and_invalid_image_do_not_drop_a_page(self):
        source = self.inputs / "wide.png"
        Image.new("RGB", (200, 100), "red").save(source)
        result = run_documents("image_pack", [source], self.folder, {"action": "pdf", "pdf_page_size": "image"})
        self.assertFalse(result["errors"])
        page = PdfReader(result["data_outputs"][0]).pages[0]
        self.assertAlmostEqual(float(page.mediabox.width) / float(page.mediabox.height), 2)
        broken = self.inputs / "broken.png"
        broken.write_bytes(b"not an image")
        failed = run_documents("image_pack", [source, broken], self.folder / "broken", {"action": "pdf"})
        self.assertTrue(failed["errors"])
        self.assertFalse(failed["data_outputs"])

    def test_reports_write_formula_looking_strings_as_literal_text(self):
        template = self.template("{{姓名}}")
        source = self.csv([["姓名"], ["=HYPERLINK(1)"]])
        result = run_documents("template_fill", [source], self.folder, {"template_path": str(template)})
        self.assertFalse(result["errors"])
        report = next(Path(path) for path in result["outputs"] if path.endswith(".xlsx"))
        book = load_workbook(report, data_only=False)
        try:
            self.assertTrue(all(cell.data_type != "f" for sheet in book for row in sheet for cell in row))
        finally:
            book.close()


if __name__ == "__main__":
    unittest.main()
