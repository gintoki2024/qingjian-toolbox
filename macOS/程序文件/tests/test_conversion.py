"""Conversion tests; use unittest discovery or pytest from the project root."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docx import Document
from PIL import Image, ImageDraw
from reportlab.lib.pdfencrypt import StandardEncryption
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from qingjian.core.conversion import convert_word, find_libreoffice, pdf_to_word
from qingjian.core import conversion as conversion_module


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def make_pdf(self, filename="source.pdf", *, scan=False, pages=1, password=None):
        path = self.root / filename
        encryption = StandardEncryption(password, ownerPassword="owner-password") if password else None
        pdf = canvas.Canvas(str(path), pagesize=(595.28, 841.89), encrypt=encryption)
        for index in range(pages):
            width, height = (595.28, 841.89) if index % 2 == 0 else (792, 612)
            pdf.setPageSize((width, height))
            if scan:
                image = Image.new("RGB", (800, 1000), "white")
                ImageDraw.Draw(image).text((40, 40), "Scanned page only", fill="black")
                pdf.drawImage(ImageReader(image), 0, 0, width=width, height=height)
                image.close()
            else:
                pdf.drawString(45, height - 55, f"Quarterly summary page {index + 1}")
                pdf.drawString(45, height - 80, "Editable text above the table.")
                top = height - 120
                for column in (45, 190, 335):
                    pdf.line(column, top, column, top - 75)
                for row in range(4):
                    pdf.line(45, top - 25 * row, 335, top - 25 * row)
                for row, values in enumerate((("Metric", "Value"), ("Revenue", "123"), ("Profit", "45"))):
                    for column, value in enumerate(values):
                        pdf.drawString(51 + column * 145, top - 17 - row * 25, value)
            pdf.showPage()
        pdf.save()
        return path

    @unittest.skipUnless(os.name == "nt", "Windows LibreOffice profile paths")
    def test_word_pdf_in_deep_selected_runtime_without_changing_drive(self):
        engine = os.environ.get("QINGJIAN_LIBREOFFICE")
        if not engine:
            self.skipTest("Set QINGJIAN_LIBREOFFICE for real deep-profile conversion.")
        base = self.root / ("部署环境_" + "x" * max(1, 129 - len(str(self.root)) - 6))
        temporary, inputs = base / "temp", base / "inputs"
        temporary.mkdir(parents=True)
        inputs.mkdir()
        self.assertGreaterEqual(len(str(temporary)), 130)
        document = Document()
        document.add_paragraph("深目录中保持原部署盘的转换检查。")
        source, target = inputs / "source.docx", inputs / "result.pdf"
        document.save(source)
        original = source.read_bytes()
        with mock.patch.dict(os.environ, {key: str(temporary) for key in ("TEMP", "TMP", "TMPDIR")}), \
             mock.patch.object(conversion_module.tempfile, "gettempdir", return_value=str(temporary)):
            result = convert_word(source, target, {"libreoffice_path": engine, "timeout": 90})
        self.assertGreater(result["changes"], 0)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(list(temporary.iterdir()), [])
        import pypdfium2
        with pypdfium2.PdfDocument(target) as pdf:
            self.assertGreater(len(pdf), 0)

    def test_editable_text_and_table_without_duplicate_table_text(self):
        source = self.make_pdf()
        target = self.root / "editable.docx"
        result = pdf_to_word(source, target, {"pdf_mode": "text"})
        document = Document(target)
        paragraphs = "\n".join(paragraph.text for paragraph in document.paragraphs)
        self.assertIn("Quarterly summary", paragraphs)
        self.assertEqual(len(document.tables), 1)
        cells = [cell.text for row in document.tables[0].rows for cell in row.cells]
        self.assertEqual(cells, ["Metric", "Value", "Revenue", "123", "Profit", "45"])
        self.assertNotIn("Revenue", paragraphs)
        self.assertEqual(result["changes"], 1)
        self.assertTrue(any("重排" in warning for warning in result["warnings"]))

    def test_appearance_keeps_one_image_and_section_per_mixed_size_page(self):
        target = self.root / "appearance.docx"
        result = pdf_to_word(self.make_pdf(pages=3), target, {"pdf_mode": "appearance", "dpi": 96})
        document = Document(target)
        self.assertEqual(result["changes"], 3)
        self.assertEqual(len(document.inline_shapes), 3)
        self.assertEqual(len(document.sections), 3)
        self.assertEqual(len(document.paragraphs), 3)
        self.assertTrue(all(paragraph._p.xpath(".//w:drawing") for paragraph in document.paragraphs))
        self.assertLess(document.sections[0].page_width, document.sections[0].page_height)
        self.assertGreater(document.sections[1].page_width, document.sections[1].page_height)
        for shape, section in zip(document.inline_shapes, document.sections):
            self.assertLessEqual(shape.width, section.page_width - section.left_margin - section.right_margin)
            self.assertLess(shape.height, section.page_height - section.top_margin - section.bottom_margin)

    def test_scanned_pages_fall_back_to_page_images(self):
        source = self.make_pdf(scan=True, pages=2)
        target = self.root / "scan.docx"
        result = pdf_to_word(source, target, {"pdf_mode": "text", "dpi": 96})
        document = Document(target)
        self.assertEqual(len(document.inline_shapes), 2)
        self.assertEqual(len(document.sections), 2)
        self.assertEqual(len(document.paragraphs), 2)
        self.assertEqual(sum("未检测到可提取文字" in warning for warning in result["warnings"]), 2)

    def test_extracts_image_between_text_blocks(self):
        source = self.root / "illustrated.pdf"
        pdf = canvas.Canvas(str(source), pagesize=(400, 500))
        pdf.drawString(30, 450, "Before image")
        image = Image.new("RGB", (100, 50), "steelblue")
        pdf.drawImage(ImageReader(image), 30, 300, width=160, height=80)
        image.close()
        pdf.drawString(30, 260, "After image")
        pdf.save()
        target = self.root / "illustrated.docx"
        pdf_to_word(source, target, {"pdf_mode": "text", "dpi": 96})
        document = Document(target)
        self.assertEqual(len(document.inline_shapes), 1)
        events = []
        for paragraph in document.paragraphs:
            events.append("image" if paragraph._p.xpath(".//w:drawing") else paragraph.text)
        self.assertEqual(events, ["Before image", "image", "After image"])

    def test_respects_pdf_visible_crop_box(self):
        source = self.root / "cropped.pdf"
        pdf = canvas.Canvas(str(source), pagesize=(500, 600))
        pdf.setCropBox((50, 100, 450, 500))
        pdf.drawString(70, 450, "Visible content")
        pdf.drawString(70, 550, "Hidden printer note")
        image = Image.new("RGB", (80, 80), "steelblue")
        pdf.drawImage(ImageReader(image), 100, 250, width=80, height=80)
        image.close()
        pdf.save()
        target = self.root / "cropped.docx"
        pdf_to_word(source, target, {"pdf_mode": "text", "dpi": 96})
        document = Document(target)
        paragraphs = "\n".join(paragraph.text for paragraph in document.paragraphs)
        self.assertIn("Visible content", paragraphs)
        self.assertNotIn("Hidden printer note", paragraphs)
        self.assertEqual(len(document.inline_shapes), 1)
        self.assertAlmostEqual(document.sections[0].page_width.pt, 400, delta=0.1)
        self.assertAlmostEqual(document.sections[0].page_height.pt, 400, delta=0.1)

    def test_wrong_password_leaves_no_output_and_right_password_works(self):
        source = self.make_pdf(password="secret")
        target = self.root / "protected.docx"
        with self.assertRaisesRegex(ValueError, "密码"):
            pdf_to_word(source, target, {"password": "wrong"})
        self.assertFalse(target.exists())
        result = pdf_to_word(source, target, {"password": "secret"})
        self.assertEqual(result["changes"], 1)
        self.assertTrue(target.is_file())

    def test_rejects_invalid_options(self):
        source = self.make_pdf()
        for options in ({"pdf_mode": "unknown"}, {"pdf_mode": {}}, {"dpi": 0}, {"dpi": 72.5}, {"dpi": 601}, {"dpi": True}, {"password": 123}):
            with self.subTest(options=options):
                target = self.root / "invalid.docx"
                with self.assertRaises(ValueError):
                    pdf_to_word(source, target, options)
                self.assertFalse(target.exists())

    def test_existing_output_and_source_are_protected(self):
        source = self.make_pdf()
        original = source.read_bytes()
        target = self.root / "existing.docx"
        target.write_bytes(b"do not replace")
        with self.assertRaises(FileExistsError):
            pdf_to_word(source, target, {})
        self.assertEqual(target.read_bytes(), b"do not replace")
        self.assertEqual(source.read_bytes(), original)
        with self.assertRaises(ValueError):
            pdf_to_word(source, source, {})

    def test_invalid_pdf_and_failed_save_leave_no_partial_output(self):
        bad = self.root / "invalid.pdf"
        bad.write_bytes(b"not a PDF")
        target = self.root / "failed.docx"
        with self.assertRaises(ValueError):
            pdf_to_word(bad, target, {})
        source = self.make_pdf()
        with mock.patch("docx.document.Document.save", side_effect=OSError("Disk test failure")):
            with self.assertRaises(OSError):
                pdf_to_word(source, target, {})
        self.assertFalse(target.exists())
        self.assertFalse(list(self.root.glob(".qj-*")))

    def test_bundled_libreoffice_precedes_environment_override(self):
        bundled = self.root / "bundle/engines/LibreOffice/program/soffice.com"
        bundled.parent.mkdir(parents=True)
        bundled.write_bytes(b"test engine")
        other = self.root / "other.exe"
        other.write_bytes(b"test engine")
        with mock.patch("qingjian.core.conversion._bundled_roots", return_value=[self.root / "bundle"]):
            with mock.patch.dict(os.environ, {"QINGJIAN_LIBREOFFICE": str(other)}):
                self.assertEqual(find_libreoffice(), bundled.resolve())

    def test_word_conversion_needs_an_engine_and_valid_timeout(self):
        source = self.root / "source.docx"
        document = Document()
        document.add_paragraph("A local conversion test")
        document.save(source)
        target = self.root / "output.pdf"
        with self.assertRaises(ValueError):
            convert_word(source, target, {"timeout": 0})
        with self.assertRaises(FileNotFoundError):
            convert_word(source, target, {"libreoffice_path": str(self.root / "missing.exe")})
        self.assertFalse(target.exists())

    def test_word_timeout_cleans_only_its_own_process(self):
        source = self.root / "source.docx"
        document = Document()
        document.add_paragraph("Timeout cleanup test")
        document.save(source)
        engine = self.root / "soffice.exe"
        engine.write_bytes(b"mock engine")
        target = self.root / "output.pdf"
        process = mock.Mock(pid=345678)
        process.communicate.side_effect = subprocess.TimeoutExpired([str(engine)], 0.01)
        with mock.patch("qingjian.core.conversion.subprocess.Popen", return_value=process):
            with mock.patch("qingjian.core.conversion._attach_windows_job", return_value=(mock.Mock(), 123)):
                with mock.patch("qingjian.core.conversion._resume_windows_process"):
                    with mock.patch("qingjian.core.conversion._close_windows_job"):
                        with mock.patch("qingjian.core.conversion._terminate_process_tree") as cleanup:
                            with self.assertRaisesRegex(TimeoutError, "已结束本次转换进程"):
                                convert_word(source, target, {"libreoffice_path": str(engine), "timeout": 0.01})
        self.assertGreaterEqual(cleanup.call_count, 1)
        self.assertTrue(all(call.args == (process,) for call in cleanup.call_args_list))
        self.assertFalse(target.exists())
        self.assertFalse(list(self.root.glob(".qj-*")))

    def _mock_word_process(self, source):
        process = mock.Mock(pid=345678, returncode=0)
        process.communicate.return_value = (b"conversion complete", b"")

        def launch(command, **kwargs):
            if os.name == "nt":
                self.assertTrue(kwargs["creationflags"] & 0x00000004)
            converted = Path(command[command.index("--outdir") + 1])
            shutil.copyfile(source, converted / source.name)
            return process

        return launch

    def _make_word_fixture(self):
        source = self.root / "source.docx"
        document = Document()
        document.add_paragraph("Profile cleanup regression test")
        document.save(source)
        engine = self.root / "soffice.exe"
        engine.write_bytes(b"mock engine")
        return source, engine

    def test_word_publishes_only_after_process_exit_and_profile_cleanup(self):
        source, engine = self._make_word_fixture()
        target = self.root / "complete.docx"
        events = []
        cleanup_directory = conversion_module._cleanup_conversion_directory
        commit_output = conversion_module._commit_output

        def cleanup(path, parent):
            self.assertEqual(events, ["job exited"])
            cleanup_directory(path, parent)
            self.assertFalse(path.exists())
            events.append("profile removed")

        def commit(staged, destination):
            self.assertEqual(events, ["job exited", "profile removed"])
            self.assertFalse(destination.exists())
            commit_output(staged, destination)
            events.append("published")

        with mock.patch("qingjian.core.conversion.subprocess.Popen", side_effect=self._mock_word_process(source)):
            with mock.patch("qingjian.core.conversion._attach_windows_job", return_value=(mock.Mock(), 123)):
                with mock.patch("qingjian.core.conversion._resume_windows_process"):
                    with mock.patch("qingjian.core.conversion._close_windows_job", side_effect=lambda job: events.append("job exited")):
                        with mock.patch("qingjian.core.conversion._cleanup_conversion_directory", side_effect=cleanup):
                            with mock.patch("qingjian.core.conversion._commit_output", side_effect=commit):
                                convert_word(source, target, {"libreoffice_path": str(engine)})
        self.assertEqual(events, ["job exited", "profile removed", "published"])
        self.assertTrue(target.is_file())
        self.assertFalse(list(self.root.glob(".qingjian-output-*")))

    def test_profile_permission_failure_does_not_publish_the_output(self):
        source, engine = self._make_word_fixture()
        target = self.root / "must-not-exist.docx"
        denied = PermissionError("Real profile access denied")
        denied.winerror = 5
        with mock.patch("qingjian.core.conversion.subprocess.Popen", side_effect=self._mock_word_process(source)):
            with mock.patch("qingjian.core.conversion._attach_windows_job", return_value=(mock.Mock(), 123)):
                with mock.patch("qingjian.core.conversion._resume_windows_process"):
                    with mock.patch("qingjian.core.conversion._close_windows_job"):
                        with mock.patch("qingjian.core.conversion._cleanup_conversion_directory", side_effect=denied):
                            with self.assertRaises(PermissionError):
                                convert_word(source, target, {"libreoffice_path": str(engine)})
        self.assertFalse(target.exists())
        self.assertFalse(list(self.root.glob(".qingjian-output-*")))

    def test_profile_cleanup_retries_transient_windows_errors_only(self):
        path = self.root / ".qingjian-convert-retry"
        path.mkdir()
        (path / "profile.txt").write_text("complete", encoding="utf-8")
        busy = OSError("Directory briefly busy")
        busy.winerror = 145
        sharing = OSError("Sharing violation")
        sharing.winerror = 32
        actual_rmtree = shutil.rmtree
        attempts = []

        def remove(directory):
            attempts.append(directory)
            if len(attempts) == 1:
                raise busy
            if len(attempts) == 2:
                raise sharing
            actual_rmtree(directory)

        with mock.patch("qingjian.core.conversion.shutil.rmtree", side_effect=remove):
            with mock.patch("qingjian.core.conversion.time.sleep"):
                conversion_module._cleanup_conversion_directory(path, self.root)
        self.assertEqual(len(attempts), 3)
        self.assertFalse(path.exists())
        path.mkdir()
        denied = PermissionError("Real access denied")
        denied.winerror = 5
        with mock.patch("qingjian.core.conversion.shutil.rmtree", side_effect=denied) as remove:
            with mock.patch("qingjian.core.conversion.time.sleep") as sleep:
                with self.assertRaises(PermissionError):
                    conversion_module._cleanup_conversion_directory(path, self.root)
        remove.assert_called_once()
        sleep.assert_not_called()

    def test_profile_cleanup_refuses_unrelated_directories(self):
        unrelated = self.root / "my-documents"
        unrelated.mkdir()
        with self.assertRaises(ValueError):
            conversion_module._cleanup_conversion_directory(unrelated, self.root)
        self.assertTrue(unrelated.exists())

    @unittest.skipUnless(os.name == "nt", "Windows extended-length paths")
    def test_profile_cleanup_handles_real_paths_longer_than_max_path(self):
        path = self.root / ".qingjian-convert-long-path"
        folder = path / "profile" / "cache"
        extended_folder = Path("\\\\?\\" + str(folder.resolve()))
        extended_folder.mkdir(parents=True)
        filename = folder / ("opencl-cache-" + "x" * 180 + ".bin")
        self.assertGreater(len(str(filename)), 260)
        with open("\\\\?\\" + str(filename.resolve()), "wb") as stream:
            stream.write(b"long-path regression")
        conversion_module._cleanup_conversion_directory(path, self.root)
        self.assertFalse(path.exists())

    @unittest.skipUnless(os.name == "nt", "Windows Job Object API")
    def test_windows_job_waits_for_all_owned_processes_to_exit(self):
        kernel = mock.Mock()
        kernel.TerminateJobObject.return_value = True
        counts = iter((2, 1, 0))

        def query(handle, information_class, pointer, size, returned_length):
            pointer._obj.ActiveProcesses = next(counts)
            return True

        kernel.QueryInformationJobObject.side_effect = query
        with mock.patch("qingjian.core.conversion.time.sleep") as sleep:
            conversion_module._close_windows_job((kernel, 123))
        kernel.TerminateJobObject.assert_called_once_with(123, 1)
        self.assertEqual(kernel.QueryInformationJobObject.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        kernel.CloseHandle.assert_called_once_with(123)

    @unittest.skipUnless(os.name == "nt", "Windows Job Object API")
    def test_windows_job_wait_timeout_is_reported(self):
        kernel = mock.Mock()
        kernel.TerminateJobObject.return_value = True

        def query(handle, information_class, pointer, size, returned_length):
            pointer._obj.ActiveProcesses = 1
            return True

        kernel.QueryInformationJobObject.side_effect = query
        with mock.patch("qingjian.core.conversion.time.monotonic", side_effect=(0.0, 0.2)):
            with self.assertRaises(TimeoutError):
                conversion_module._close_windows_job((kernel, 123), timeout=0.1)
        kernel.CloseHandle.assert_called_once_with(123)

    def test_libreoffice_docx_to_pdf_when_configured(self):
        engine = os.environ.get("QINGJIAN_LIBREOFFICE")
        if not engine:
            self.skipTest("Set QINGJIAN_LIBREOFFICE to run the LibreOffice integration test.")
        source = self.root / "word.docx"
        document = Document()
        document.add_paragraph("Offline conversion integration test")
        document.save(source)
        original = source.read_bytes()
        target = self.root / "word.pdf"
        result = convert_word(source, target, {"libreoffice_path": engine})
        self.assertGreaterEqual(result["changes"], 1)
        self.assertTrue(target.read_bytes().startswith(b"%PDF-"))
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(list(self.root.glob(".qj-*")))

    def test_libreoffice_appearance_roundtrip_has_no_blank_pages_when_configured(self):
        engine = os.environ.get("QINGJIAN_LIBREOFFICE")
        if not engine:
            self.skipTest("Set QINGJIAN_LIBREOFFICE to check the rendered Word page count.")
        source = self.make_pdf(pages=3)
        word = self.root / "page-images.docx"
        pdf_to_word(source, word, {"pdf_mode": "appearance", "dpi": 96})
        rendered = self.root / "roundtrip.pdf"
        result = convert_word(word, rendered, {"libreoffice_path": engine})
        self.assertEqual(result["changes"], 3)
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(str(source)) as original, pdfium.PdfDocument(str(rendered)) as converted:
            for index in range(3):
                original_page, converted_page = original[index], converted[index]
                try:
                    for original_size, converted_size in zip(original_page.get_size(), converted_page.get_size()):
                        self.assertAlmostEqual(original_size, converted_size, delta=1)
                finally:
                    original_page.close()
                    converted_page.close()

    def test_libreoffice_fresh_profiles_are_removed_before_return_when_configured(self):
        engine = os.environ.get("QINGJIAN_LIBREOFFICE")
        if not engine:
            self.skipTest("Set QINGJIAN_LIBREOFFICE for repeated fresh-profile conversions.")
        source = self.root / "repeat.docx"
        document = Document()
        document.add_paragraph("Repeated isolated LibreOffice profile conversion")
        document.save(source)
        for index in range(3):
            target = self.root / f"repeat-{index + 1}.pdf"
            result = convert_word(source, target, {"libreoffice_path": engine})
            self.assertEqual(result["changes"], 1)
            self.assertTrue(target.read_bytes().startswith(b"%PDF-"))
            self.assertFalse(list(self.root.glob(".qj-*")))
            self.assertFalse(list(self.root.glob(".qingjian-output-*")))


if __name__ == "__main__":
    unittest.main()
