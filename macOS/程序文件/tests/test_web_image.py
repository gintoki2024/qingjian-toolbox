"""Focused regression tests for local HTML conversion and image cropping."""

from __future__ import annotations

import base64
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from docx import Document
from docx.oxml.ns import qn
from PIL import Image

from qingjian.core.image_ops import crop_image
from qingjian.core.web_ops import webpage_to_word


class _Scratch:
    """Use inherited Windows ACLs, including inside restricted test runners."""

    def __init__(self):
        self.path = Path(tempfile.gettempdir()) / ("qingjian-test-" + uuid.uuid4().hex)
        self.path.mkdir()
        self.name = str(self.path)

    def cleanup(self):
        shutil.rmtree(self.path)


class ImageCropTests(unittest.TestCase):
    def setUp(self):
        self.temporary = _Scratch()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.png"
        Image.new("RGB", (200, 100), "red").save(self.source)

    def tearDown(self):
        self.temporary.cleanup()

    def test_margins_and_preserved_input(self):
        before = self.source.read_bytes()
        result = crop_image(self.source, self.root / "crop.png", {
            "mode": "margins", "left": 10, "right": 20, "top": 10, "bottom": 0})
        with Image.open(self.root / "crop.png") as image:
            self.assertEqual(image.size, (140, 90))
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(result["changes"], 1)

    def test_ratio_and_anchor(self):
        image = Image.new("RGB", (100, 50), "blue")
        image.paste("red", (0, 0, 50, 50))
        image.save(self.source)
        crop_image(self.source, self.root / "anchored.png", {
            "mode": "ratio", "ratio_w": 1, "ratio_h": 1, "anchor": "top-left"})
        with Image.open(self.root / "anchored.png") as actual:
            self.assertEqual(actual.size, (50, 50))
            self.assertEqual(actual.getpixel((49, 25)), (255, 0, 0))

    def test_size_shrinks_but_never_enlarges(self):
        crop_image(self.source, self.root / "small.png", {"mode": "size", "width": 50, "height": 50})
        with Image.open(self.root / "small.png") as actual:
            self.assertEqual(actual.size, (50, 50))
        result = crop_image(self.source, self.root / "large.png", {"mode": "size", "width": 500, "height": 500})
        with Image.open(self.root / "large.png") as actual:
            self.assertEqual(actual.size, (100, 100))
        self.assertTrue(any("禁止放大" in warning for warning in result["warnings"]))

    def test_transparency_and_white_jpeg_background(self):
        Image.new("RGBA", (20, 20), (10, 20, 30, 0)).save(self.source)
        crop_image(self.source, self.root / "alpha.png", {"format": "png"})
        with Image.open(self.root / "alpha.png") as actual:
            self.assertEqual(actual.getpixel((10, 10))[3], 0)
        crop_image(self.source, self.root / "white.jpg", {"format": "jpg"})
        with Image.open(self.root / "white.jpg") as actual:
            self.assertEqual(actual.mode, "RGB")
            self.assertTrue(all(value >= 250 for value in actual.getpixel((10, 10))))

    def test_exif_rotation_and_removal(self):
        exif = Image.Exif()
        exif[274] = 6
        exif[270] = "private camera description"
        source = self.root / "camera.jpg"
        Image.new("RGB", (60, 40), "red").save(source, exif=exif)
        crop_image(source, self.root / "upright.jpg", {})
        with Image.open(self.root / "upright.jpg") as actual:
            self.assertEqual(actual.size, (40, 60))
            self.assertEqual(len(actual.getexif()), 0)

    def test_multipage_first_frame_warning(self):
        source = self.root / "multi.tiff"
        Image.new("RGB", (30, 20), "red").save(source, save_all=True, append_images=[Image.new("RGB", (30, 20), "blue")])
        result = crop_image(source, self.root / "first.png", {"format": "png"})
        self.assertTrue(result["warnings"])
        with Image.open(self.root / "first.png") as actual:
            self.assertEqual(actual.getpixel((0, 0)), (255, 0, 0))

    def test_reject_invalid_parameters_without_output(self):
        invalid = [{"mode": "unknown"}, {"left": -1}, {"right": 50}, {"left": float("nan")},
                   {"mode": "ratio", "ratio_w": 0}, {"mode": "ratio", "ratio_h": "inf"},
                   {"mode": "size", "width": 0}, {"mode": "size", "height": 2.5},
                   {"anchor": "outside"}, {"format": "exe"}, {"quality": 101}, {"quality": True}]
        for options in invalid:
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    crop_image(self.source, self.root / "invalid.png", options)
                self.assertFalse((self.root / "invalid.png").exists())

    def test_empty_crop_rejected(self):
        Image.new("RGB", (2, 2), "red").save(self.source)
        with self.assertRaises(ValueError):
            crop_image(self.source, self.root / "empty.png", {"left": 49.9, "right": 49.9})

    def test_does_not_overwrite_input_or_existing_output(self):
        with self.assertRaises(ValueError):
            crop_image(self.source, self.source, {})
        target = self.root / "existing.png"
        target.write_bytes(b"keep me")
        with self.assertRaises(FileExistsError):
            crop_image(self.source, target, {})
        self.assertEqual(target.read_bytes(), b"keep me")


class WebToWordTests(unittest.TestCase):
    def setUp(self):
        self.temporary = _Scratch()
        self.root = Path(self.temporary.name)
        self.source = self.root / "article.html"
        self.target = self.root / "article.docx"
        Image.new("RGB", (40, 20), "red").save(self.root / "photo.png")

    def tearDown(self):
        self.temporary.cleanup()

    def html(self, content):
        self.source.write_text('<!doctype html><html><head><meta charset="utf-8"><title>网页标题</title></head><body>' + content + "</body></html>", encoding="utf-8")

    def test_document_order_styles_table_and_image(self):
        self.html('<nav>不应导出的导航</nav><article><h1>测试文章</h1><p>前文<strong>加粗</strong></p><h2>小节</h2>'
                  '<p>图片前<img src="photo.png">图片后</p><table><tr><th>公司</th><th>数值</th></tr>'
                  '<tr><td>甲</td><td>42</td></tr></table><p>表格后</p><ul><li>第一项</li><li>第二项</li></ul>'
                  '<div class="ads">不应导出的广告</div><script>throw Error("never execute")</script></article>')
        original = self.source.read_bytes()
        result = webpage_to_word(self.source, self.target, {})
        self.assertFalse(any("插图未能导入" in warning for warning in result["warnings"]))
        document = Document(self.target)
        order = []
        for element in document.element.body:
            if element.tag == qn("w:p"):
                if element.findall(".//" + qn("w:drawing")):
                    order.append("[图片]")
                else:
                    order.append("".join(node.text or "" for node in element.iter(qn("w:t"))))
            elif element.tag == qn("w:tbl"):
                order.append("[表格]")
        self.assertEqual(order[:9], ["测试文章", "前文加粗", "小节", "图片前", "[图片]", "图片后", "[表格]", "表格后", "第一项"])
        self.assertEqual(document.tables[0].cell(1, 1).text, "42")
        self.assertEqual(len(document.inline_shapes), 1)
        self.assertEqual(document.styles["Title"].font.name, "宋体")
        self.assertEqual(str(document.styles["Title"].font.color.rgb), "000000")
        self.assertEqual(self.source.read_bytes(), original)
        self.assertTrue(document.paragraphs[1].runs[1].bold)
        self.assertFalse(any("导航" in paragraph.text or "广告" in paragraph.text for paragraph in document.paragraphs))

    def test_local_html_never_uses_network_and_rejects_escape(self):
        self.html('<article><p>正文</p><img src="https://example.com/a.png"><img src="../outside.png"><img src="file:///C:/secret.png"></article>')
        with patch("qingjian.core.web_ops.requests.Session", side_effect=AssertionError("local HTML must never connect")):
            result = webpage_to_word(self.source, self.target, {})
        self.assertEqual(len(result["warnings"]), 3)
        self.assertEqual(len(Document(self.target).inline_shapes), 0)

    def test_data_uri_image_supported_without_network(self):
        encoded = base64.b64encode((self.root / "photo.png").read_bytes()).decode()
        self.html(f'<article><p>正文</p><img src="data:image/png;base64,{encoded}"></article>')
        with patch("qingjian.core.web_ops.requests.Session", side_effect=AssertionError("no network")):
            result = webpage_to_word(self.source, self.target, {})
        self.assertFalse(result["warnings"])
        self.assertEqual(len(Document(self.target).inline_shapes), 1)

    def test_image_cap_and_disabled_images(self):
        self.html('<article><p>正文</p><img src="photo.png"><img src="photo.png"></article>')
        result = webpage_to_word(self.source, self.target, {"max_images": 1})
        self.assertEqual(len(Document(self.target).inline_shapes), 1)
        self.assertTrue(result["warnings"])
        second = self.root / "no-images.docx"
        result = webpage_to_word(self.source, second, {"include_images": False})
        self.assertEqual(len(Document(second).inline_shapes), 0)
        self.assertEqual(result["warnings"], [])

    def test_login_challenge_and_empty_dynamic_page_do_not_succeed(self):
        for content in ('<main><h1>安全验证</h1><p>请完成验证后继续访问</p></main>',
                        '<div id="app"></div><script>loadArticle()</script>',
                        '<h1>账号登录</h1><form><input type="password"></form>'):
            with self.subTest(content=content):
                self.html(content)
                with self.assertRaises(ValueError):
                    webpage_to_word(self.source, self.target, {})
                self.assertFalse(self.target.exists())

    def test_short_dynamic_page_warns(self):
        self.html('<main><p>部分正文</p></main><script>loadMore()</script>')
        result = webpage_to_word(self.source, self.target, {})
        self.assertTrue(any("动态" in warning for warning in result["warnings"]))

    def test_existing_output_kept_and_failed_save_is_atomic(self):
        self.html("<main><p>正文</p></main>")
        self.target.write_bytes(b"original output")
        with self.assertRaises(FileExistsError):
            webpage_to_word(self.source, self.target, {})
        self.assertEqual(self.target.read_bytes(), b"original output")
        self.target.unlink()
        with patch("docx.document.Document.save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                webpage_to_word(self.source, self.target, {})
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.root.glob(".qingjian-web-*")), [])

    def test_invalid_options_and_schemes(self):
        self.html("<p>正文</p>")
        for options in ({"timeout": 0}, {"timeout": "nan"}, {"max_images": -1}, {"max_images": 201}, {"include_images": "false"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                webpage_to_word(self.source, self.target, options)
        with self.assertRaises(ValueError):
            webpage_to_word("file:///C:/secret.html", self.target, {})

    def test_http_content_type_checked(self):
        response = _FakeResponse(b"%PDF-1.7", "application/pdf")
        with patch("qingjian.core.web_ops.requests.Session", return_value=_FakeSession(response)):
            with self.assertRaisesRegex(ValueError, "不是 HTML"):
                webpage_to_word("https://example.com/article", self.target, {})
        self.assertFalse(self.target.exists())

    def test_http_html_and_disallowed_image_url(self):
        response = _FakeResponse('<html><head><meta charset="utf-8"><title>HTTP文章</title></head><body><article><p>网页正文</p><img src="file:///secret.png"></article></body></html>'.encode(), "text/html")
        session = _FakeSession(response)
        with patch("qingjian.core.web_ops.requests.Session", return_value=session):
            result = webpage_to_word("https://example.com/article", self.target, {})
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(result["warnings"])
        self.assertEqual(Document(self.target).paragraphs[1].text, "网页正文")
        self.assertTrue(session.closed)

    def test_table_inline_image_order(self):
        self.html('<article><p>正文</p><table><tr><td>前<img src="photo.png">后</td></tr></table></article>')
        webpage_to_word(self.source, self.target, {})
        cell = Document(self.target).tables[0].cell(0, 0)
        self.assertEqual([paragraph.text for paragraph in cell.paragraphs], ["前", "", "后"])
        self.assertTrue(cell.paragraphs[1]._p.findall(".//" + qn("w:drawing")))

    def test_http_redirect_limit_and_size_limit(self):
        redirect = _FakeResponse(b"", "text/html")
        redirect.status_code = 302
        redirect.headers["Location"] = "/another"
        session = _FakeSession(redirect)
        with patch("qingjian.core.web_ops.requests.Session", return_value=session):
            with self.assertRaisesRegex(ValueError, "重定向"):
                webpage_to_word("https://example.com/article", self.target, {})
        self.assertEqual(len(session.calls), 5)
        self.assertTrue(all(call[1]["allow_redirects"] is False for call in session.calls))
        response = _FakeResponse(b"<p>hello</p>", "text/html")
        response.headers["Content-Length"] = str(6 * 1024 * 1024)
        with patch("qingjian.core.web_ops.requests.Session", return_value=_FakeSession(response)):
            with self.assertRaisesRegex(ValueError, "上限"):
                webpage_to_word("https://example.com/article", self.target, {})
        self.assertFalse(self.target.exists())


class _FakeResponse:
    def __init__(self, content, content_type):
        self.content = content
        self.status_code = 200
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(content))}
        self.url = "https://example.com/article"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        yield self.content

    def close(self):
        return None


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.headers = {}
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

    def close(self):
        self.closed = True


if __name__ == "__main__":
    unittest.main()
