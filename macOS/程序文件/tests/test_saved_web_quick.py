"""Real offline saved-page conversions used by the home-page quick channel."""

from __future__ import annotations

import hashlib
import io
import shutil
import tempfile
import unittest
import uuid
import zipfile
from email import encoders, policy
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from unittest.mock import patch

from docx import Document
from PIL import Image

from qingjian.core.web_ops import webpage_to_word


class SavedWebQuickTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.gettempdir()) / ("qingjian-saved-web-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.target = self.root / "结果.docx"
        image = io.BytesIO()
        Image.new("RGB", (32, 20), "red").save(image, "PNG")
        self.image = image.getvalue()

    def tearDown(self):
        shutil.rmtree(self.root)

    def archive(self, html, *, suffix=".mhtml", charset="utf-8", images=(), start="", extra=None):
        message = MIMEMultipart("related")
        message["Snapshot-Content-Location"] = "https://example.test/articles/page.html"
        if start:
            message.set_param("start", start)
        part = MIMEText(html, "html", charset)
        part["Content-Location"] = "https://example.test/articles/page.html"
        part["Content-ID"] = "<page-root>"
        message.attach(part)
        for location, cid, content in images:
            picture = MIMEImage(content, _subtype="png")
            if location:
                picture["Content-Location"] = location
            if cid:
                picture["Content-ID"] = "<" + cid + ">"
            message.attach(picture)
        if extra is not None:
            extra(message)
        source = self.root / ("已保存网页" + suffix)
        source.write_bytes(message.as_bytes(policy=policy.SMTP))
        return source

    def convert(self, source, options=None, target=None):
        original = hashlib.sha256(source.read_bytes()).hexdigest()
        with patch("qingjian.core.web_ops.requests.Session", side_effect=AssertionError("saved pages must stay offline")):
            result = webpage_to_word(source, target or self.target, options or {})
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original)
        self.assertFalse(list(self.root.glob(".qingjian-web-*")))
        return result

    def text(self, target=None):
        return "\n".join(paragraph.text for paragraph in Document(target or self.target).paragraphs)

    def test_html_files_directory_keeps_picture_text_table_and_originals(self):
        folder = self.root / "已保存网页_files"
        folder.mkdir()
        picture = folder / "图 1.png"
        picture.write_bytes(self.image)
        source = self.root / "已保存网页.html"
        source.write_text('<meta charset="utf-8"><article><h1>办公资料</h1><p>图片前</p>'
                          '<img src="已保存网页_files/图%201.png"><p>图片后</p>'
                          '<table><tr><td>名称</td><td>数值</td></tr><tr><td>甲</td><td>8</td></tr></table></article>', encoding="utf-8")
        result = self.convert(source)
        self.assertEqual(result["warnings"], [])
        document = Document(self.target)
        self.assertEqual(len(document.inline_shapes), 1)
        self.assertEqual(document.tables[0].cell(1, 1).text, "8")
        self.assertEqual(picture.read_bytes(), self.image)
        self.assertIn("图片前\n\n图片后", self.text())

    def test_html_external_and_escaping_files_are_not_read(self):
        source = self.root / "local.htm"
        source.write_text('<meta charset="utf-8"><article><p>静态正文</p><img src="../secret.png">'
                          '<img src="https://example.test/live.png"><img src="file:///C:/secret.png"></article>', encoding="utf-8")
        result = self.convert(source)
        self.assertEqual(len(result["warnings"]), 3)
        self.assertEqual(len(Document(self.target).inline_shapes), 0)

    def test_mhtml_base64_html_and_absolute_content_location(self):
        source = self.archive('<article><h1>离线文章</h1><p>中文正文</p><img src="images/photo.png">'
                              '<p>结尾</p></article>',
                              images=[("https://example.test/articles/images/photo.png", "", self.image)])
        result = self.convert(source)
        self.assertEqual(result["warnings"], [])
        self.assertIn("离线文章\n中文正文", self.text())
        self.assertEqual(len(Document(self.target).inline_shapes), 1)
        with zipfile.ZipFile(self.target) as archive:
            data = archive.read(next(name for name in archive.namelist() if name.startswith("word/media/")))
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.getpixel((0, 0)), (255, 0, 0))

    def test_mht_cid_and_chinese_mime_charset_without_meta(self):
        source = self.archive('<article><h1>已保存通知</h1><p>中文编码正确</p>'
                              '<img src="cid:picture%40saved"></article>', suffix=".mht", charset="gb18030",
                              images=[("", "picture@saved", self.image)])
        result = self.convert(source)
        self.assertEqual(result["warnings"], [])
        self.assertIn("中文编码正确", self.text())
        self.assertEqual(len(Document(self.target).inline_shapes), 1)

    def test_utf8_bom_and_encoded_chinese_content_location(self):
        source = self.archive('<article><p>归档正文</p><img src="插图/图%201.png"></article>',
                              images=[("插图/图 1.png", "", self.image)])
        source.write_bytes(b"\xef\xbb\xbf" + source.read_bytes())
        result = self.convert(source)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(len(Document(self.target).inline_shapes), 1)

    def test_quoted_printable_html_with_resource_query_and_fragment(self):
        def quoted(message):
            part = message.get_payload()[0]
            content = '<article><p>通知正文</p><img src="./img.png?size=2#display"></article>'.encode("utf-8")
            del part["Content-Transfer-Encoding"]
            part.set_payload(content)
            encoders.encode_quopri(part)
        source = self.archive("placeholder", images=[("https://example.test/articles/img.png?size=2", "", self.image)], extra=quoted)
        self.assertEqual(self.convert(source)["warnings"], [])
        self.assertIn("通知正文", self.text())
        self.assertEqual(len(Document(self.target).inline_shapes), 1)

    def test_related_start_selects_main_page_instead_of_frame(self):
        def prepend_frame(message):
            frame = MIMEText("<p>不是主页面</p>", "html", "utf-8")
            frame["Content-ID"] = "<frame>"
            message.get_payload().insert(0, frame)
        source = self.archive("<article><p>这是主页面</p></article>", start="<page-root>", extra=prepend_frame)
        self.convert(source)
        self.assertIn("这是主页面", self.text())
        self.assertNotIn("不是主页面", self.text())

    def test_nested_alternative_with_related_start_is_supported(self):
        def nest(message):
            html = message.get_payload()[0]
            alternative = MIMEMultipart("alternative")
            alternative["Content-ID"] = "<alternative-root>"
            alternative.attach(MIMEText("纯文本备用", "plain", "utf-8"))
            alternative.attach(html)
            message.set_payload([alternative, *message.get_payload()[1:]])
        source = self.archive('<article><p>嵌套正文</p><img src="cid:embedded"></article>',
                              start="<alternative-root>", images=[("", "embedded", self.image)], extra=nest)
        self.assertEqual(self.convert(source)["warnings"], [])
        self.assertIn("嵌套正文", self.text())
        self.assertNotIn("纯文本备用", self.text())
        self.assertEqual(len(Document(self.target).inline_shapes), 1)

    def test_html_base_href_resolves_archive_image_only(self):
        source = self.archive('<base href="https://cdn.test/images/"><article><p>正文</p><img src="one.png"></article>',
                              images=[("https://cdn.test/images/one.png", "", self.image)])
        self.assertEqual(self.convert(source)["warnings"], [])
        self.assertEqual(len(Document(self.target).inline_shapes), 1)

    def test_relative_html_location_honors_archive_content_base(self):
        def relative(message):
            message["Content-Base"] = "https://example.test/articles/"
            part = message.get_payload()[0]
            part.replace_header("Content-Location", "page.html")
        source = self.archive('<article><p>正文</p><img src="one.png"></article>',
                              images=[("https://example.test/articles/one.png", "", self.image)], extra=relative)
        self.assertEqual(self.convert(source)["warnings"], [])
        self.assertEqual(len(Document(self.target).inline_shapes), 1)

    def test_archive_missing_images_never_fall_back_to_network_or_disk(self):
        (self.root / "same-folder.png").write_bytes(self.image)
        source = self.archive('<article><p>正文保留</p><img src="same-folder.png">'
                              '<img src="https://example.test/missing.png"><img src="file:///C:/secret.png">'
                              '<img src="cid:missing"></article>')
        result = self.convert(source)
        self.assertEqual(len(result["warnings"]), 4)
        self.assertTrue(all("存档外" in warning for warning in result["warnings"]))
        self.assertEqual(len(Document(self.target).inline_shapes), 0)

    def test_embedded_image_location_is_lookup_only_not_file_access(self):
        source = self.archive('<article><p>归档图</p><img src="file:///C:/not-real/image.png"></article>',
                              images=[("file:///C:/not-real/image.png", "", self.image)])
        self.assertEqual(self.convert(source)["warnings"], [])
        self.assertEqual(len(Document(self.target).inline_shapes), 1)

    def test_scripts_objects_and_frames_do_not_enter_document(self):
        source = self.archive('<article><p>可见正文</p><script>不执行脚本</script>'
                              '<iframe src="https://example.test/frame">框架内容</iframe>'
                              '<object>对象内容</object><style>样式内容</style></article>')
        self.convert(source)
        text = self.text()
        self.assertIn("可见正文", text)
        for excluded in ("不执行脚本", "框架内容", "对象内容", "样式内容"):
            self.assertNotIn(excluded, text)

    def test_existing_output_is_preserved(self):
        source = self.archive("<p>正文</p>")
        self.target.write_bytes(b"existing output")
        with self.assertRaises(FileExistsError):
            self.convert(source)
        self.assertEqual(self.target.read_bytes(), b"existing output")

    def test_save_failure_cleans_temporary_document(self):
        source = self.archive("<p>正文</p>")
        before = source.read_bytes()
        with patch("docx.document.Document.save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.convert(source)
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.root.glob(".qingjian-web-*")))
        self.assertEqual(source.read_bytes(), before)

    def test_missing_html_and_malformed_boundary_fail_without_output(self):
        for content in (b"MIME-Version: 1.0\r\nContent-Type: image/png\r\n\r\nnot html",
                        b'MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary="missing"\r\n\r\ninvalid'):
            with self.subTest(content=content):
                source = self.root / "bad.mhtml"
                source.write_bytes(content)
                with self.assertRaises(ValueError):
                    self.convert(source)
                self.assertFalse(self.target.exists())

    def test_invalid_base64_does_not_silently_drop_corrupt_content(self):
        def corrupt(message):
            message.get_payload()[0].set_payload("not#base64")
        source = self.archive("<p>正文</p>", extra=corrupt)
        with self.assertRaisesRegex(ValueError, "编码"):
            self.convert(source)
        self.assertFalse(self.target.exists())

    def test_duplicate_resource_location_with_different_content_fails(self):
        other = io.BytesIO()
        Image.new("RGB", (32, 20), "blue").save(other, "PNG")
        source = self.archive('<p>正文</p><img src="photo.png">', images=[
            ("photo.png", "", self.image), ("photo.png", "", other.getvalue())])
        with self.assertRaisesRegex(ValueError, "不同资源"):
            self.convert(source)
        self.assertFalse(self.target.exists())

    def test_identical_duplicate_alias_is_reused(self):
        source = self.archive('<p>正文</p><img src="cid:one"><img src="photo.png">', images=[
            ("photo.png", "one", self.image), ("photo.png", "one", self.image)])
        self.assertEqual(self.convert(source)["warnings"], [])
        self.assertEqual(len(Document(self.target).inline_shapes), 2)

    def test_declared_html_charset_must_decode(self):
        def invalid_charset(message):
            part = message.get_payload()[0]
            part.set_param("charset", "not-a-real-character-encoding")
        source = self.archive("<p>正文</p>", extra=invalid_charset)
        with self.assertRaisesRegex(ValueError, "字符编码"):
            self.convert(source)
        self.assertFalse(self.target.exists())

    def test_archive_raw_decoded_html_and_picture_limits(self):
        source = self.archive('<p>正文</p><img src="cid:one">', images=[("", "one", self.image)])
        for constant, value, expected in (("_ARCHIVE_LIMIT", 40, "42 MB"),
                                          ("_PAGE_LIMIT", 20, "30 MB"),
                                          ("_HTML_LIMIT", 3, "5 MB"),
                                          ("_IMAGE_LIMIT", 3, "12 MB")):
            with self.subTest(constant=constant), patch("qingjian.core.web_ops." + constant, value):
                with self.assertRaisesRegex(ValueError, expected):
                    self.convert(source)
                self.assertFalse(self.target.exists())

    def test_archive_resource_count_limit(self):
        source = self.archive("<p>正文</p>", images=[("", str(number), self.image) for number in range(5)])
        with patch("qingjian.core.web_ops._ARCHIVE_PARTS", 3):
            with self.assertRaisesRegex(ValueError, "资源数量"):
                self.convert(source)
        self.assertFalse(self.target.exists())

    def test_nested_archive_depth_limit(self):
        def nest(message):
            child = message.get_payload()[0]
            for _ in range(20):
                parent = MIMEMultipart("related")
                parent.attach(child)
                child = parent
            message.set_payload([child])
        source = self.archive("<p>正文</p>", extra=nest)
        with self.assertRaisesRegex(ValueError, "嵌套层级"):
            self.convert(source)
        self.assertFalse(self.target.exists())

    def test_image_option_and_cap_work_for_archives(self):
        source = self.archive('<p>正文</p><img src="cid:one"><img src="cid:one">', images=[("", "one", self.image)])
        first = self.root / "one.docx"
        result = self.convert(source, {"max_images": 1}, first)
        self.assertEqual(len(Document(first).inline_shapes), 1)
        self.assertTrue(any("图片数量" in warning for warning in result["warnings"]))
        result = self.convert(source, {"include_images": False})
        self.assertEqual(result["warnings"], [])
        self.assertEqual(len(Document(self.target).inline_shapes), 0)


if __name__ == "__main__":
    unittest.main()
