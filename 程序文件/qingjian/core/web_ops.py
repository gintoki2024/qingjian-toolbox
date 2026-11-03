"""Convert ordinary HTML articles into local DOCX without executing web code."""

from __future__ import annotations

import base64
import binascii
from email import policy
from email.parser import BytesParser
import io
import math
import os
import re
import tempfile
import time
import warnings as python_warnings
from pathlib import Path
from urllib.parse import unquote, unquote_to_bytes, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from PIL import Image, ImageOps, UnidentifiedImageError


_HTML_LIMIT = 5 * 1024 * 1024
_IMAGE_LIMIT = 12 * 1024 * 1024
_PAGE_LIMIT = 30 * 1024 * 1024
_ARCHIVE_LIMIT = 42 * 1024 * 1024
_ARCHIVE_PARTS = 400
_LOCAL_SUFFIXES = {".html", ".htm", ".xhtml", ".mht", ".mhtml"}
_BLOCKS = {"p", "div", "section", "article", "main", "header", "figure", "figcaption",
           "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "table", "blockquote", "pre", "hr"}
_AD = re.compile(r"(?:^|[-_\s])(ads?|advert(?:isement)?|banner|promo)(?:$|[-_\s])|广告", re.I)


def _integer(value, name: str, lower: int, upper: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name}必须为整数。")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name}必须为整数。") from exc
    if not math.isfinite(number) or not number.is_integer() or not lower <= number <= upper:
        raise ValueError(f"{name}必须为 {lower} 至 {upper} 之间的整数。")
    return int(number)


def _http_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise ValueError("网页地址只支持 http:// 或 https:// 链接。")
    if parsed.username or parsed.password:
        raise ValueError("请使用不含账号或密码的网页链接。")
    return value


def _archive_key(value: str) -> str:
    """Normalize an archive lookup key without opening a URL or a local path."""
    value = value.strip()
    if value.lower().startswith("cid:"):
        return "cid:" + unquote(value[4:]).strip("<>")
    parsed = urlsplit(value)
    return parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower(),
                           path=unquote(parsed.path), fragment="").geturl()


def _content_id(part) -> str:
    return str(part.get("Content-ID", "")).strip().strip("<>")


def _archive_parts(message):
    """Bound traversal independently of the email parser's multipart helpers."""
    pending, nodes = [(message, 0)], []
    while pending:
        part, depth = pending.pop()
        nodes.append(part)
        if len(nodes) > _ARCHIVE_PARTS or depth > 16:
            raise ValueError("网页存档的资源数量或嵌套层级超过上限（400 个资源、16 层）。")
        if part.is_multipart():
            pending.extend((child, depth + 1) for child in reversed(part.get_payload()))
    return nodes


class _Reader:
    def __init__(self, source: str | Path, timeout: int):
        self.source = source
        self.timeout = timeout
        self.deadline = time.monotonic() + min(120, max(30, timeout * 3))
        self.used = 0
        self.session = None
        self.base_url = ""
        self.local_root = None
        self.cache: dict[str, bytes] = {}
        self.archive_resources: dict[str, bytes] | None = None

    def _download(self, url: str, limit: int, html: bool = False) -> bytes:
        _http_url(url)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("网页处理已达到总时间上限，请减少图片数量后重试。")
        if self.session is None:
            self.session = requests.Session()
            self.session.max_redirects = 4
            self.session.headers.update({"User-Agent": "QingjianLocalToolbox/1.0 (HTML to Word)",
                                         "Accept": "text/html,application/xhtml+xml,image/*;q=0.8"})
        try:
            headers = {"Referer": self.base_url} if self.base_url and not html else {}
            response = self._response(url, headers)
            with response:
                response.raise_for_status()
                _http_url(response.url)
                if html:
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    if content_type not in {"text/html", "application/xhtml+xml"}:
                        raise ValueError("该链接返回的内容不是 HTML 网页，请提供文章页面链接。")
                    self.base_url = response.url
                advertised = response.headers.get("Content-Length")
                try:
                    advertised_length = int(advertised) if advertised else 0
                except (TypeError, ValueError, OverflowError):
                    advertised_length = 0
                if advertised_length > limit or self.used + advertised_length > _PAGE_LIMIT:
                    raise ValueError("网页或图片文件超过大小上限。")
                chunks, length = [], 0
                for chunk in response.iter_content(chunk_size=65536):
                    if time.monotonic() > self.deadline:
                        raise ValueError("网页处理已达到总时间上限，请减少图片数量后重试。")
                    if not chunk:
                        continue
                    length += len(chunk)
                    if length > limit or self.used + length > _PAGE_LIMIT:
                        raise ValueError("网页或图片超过大小上限（单张 12 MB，整页 30 MB）。")
                    chunks.append(chunk)
                self.used += length
                return b"".join(chunks)
        except requests.TooManyRedirects as exc:
            raise ValueError("网页重定向次数过多，请使用文章的最终链接。") from exc
        except requests.Timeout as exc:
            raise ValueError("网页连接超时，请检查网络或稍后重试。") from exc
        except requests.HTTPError as exc:
            code = getattr(exc.response, "status_code", "")
            if code in {401, 403}:
                raise ValueError("网页要求登录、授权或安全验证，无法直接转换。") from exc
            raise ValueError(f"网页访问失败（HTTP {code}）。") from exc
        except requests.RequestException as exc:
            raise ValueError("网页访问失败，请检查链接、网络和证书。") from exc

    def _response(self, url: str, headers: dict):
        """Count redirects against the same page-wide deadline."""
        for redirect in range(5):
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("网页处理已达到总时间上限，请减少图片数量后重试。")
            response = self.session.get(url, timeout=(min(self.timeout, remaining), min(self.timeout, remaining)),
                                        stream=True, headers=headers, allow_redirects=False)
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("Location")
            response_url = response.url or url
            response.close()
            if redirect == 4:
                raise ValueError("网页重定向次数过多，请使用文章的最终链接。")
            if not location:
                raise ValueError("网页返回了没有目标地址的重定向。")
            url = _http_url(urljoin(response_url, location))
        raise ValueError("网页重定向次数过多。")

    def html(self) -> bytes | str:
        if isinstance(self.source, str) and urlsplit(self.source).scheme.lower() in {"http", "https"}:
            return self._download(_http_url(self.source), _HTML_LIMIT, html=True)
        path = Path(self.source)
        if isinstance(self.source, str) and re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", self.source):
            raise ValueError("仅支持 http(s) 网页链接或本地 HTML、MHTML 网页存档。")
        if not path.is_file():
            raise FileNotFoundError(f"找不到本地网页文件：{path.name}")
        if path.suffix.lower() not in _LOCAL_SUFFIXES:
            raise ValueError("本地网页请使用 .html、.htm、.xhtml、.mht 或 .mhtml 格式。")
        self.local_root = path.resolve().parent
        if path.suffix.lower() in {".mht", ".mhtml"}:
            return self._archive_html(path)
        if path.stat().st_size > _HTML_LIMIT:
            raise ValueError("本地 HTML 超过 5 MB 上限。")
        with path.open("rb") as stream:
            raw = stream.read(_HTML_LIMIT + 1)
        if len(raw) > _HTML_LIMIT:
            raise ValueError("本地 HTML 超过 5 MB 上限。")
        self.used = len(raw)
        return raw

    def _archive_html(self, path: Path) -> bytes | str:
        """Read browser MHTML in memory; embedded locations are lookup keys only."""
        self.archive_resources = {}
        if path.stat().st_size > _ARCHIVE_LIMIT:
            raise ValueError("MHTML 网页存档超过 42 MB 上限。")
        with path.open("rb") as stream:
            raw = stream.read(_ARCHIVE_LIMIT + 1)
        if len(raw) > _ARCHIVE_LIMIT:
            raise ValueError("MHTML 网页存档超过 42 MB 上限。")
        # Reject unbounded MIME structures before constructing email objects.
        # Browser archives use one delimiter line per part plus closing lines.
        delimiters = re.finditer(rb"(?m)^--[^\r\n]*(?:\r)?$", raw)
        if sum(1 for _ in delimiters) > _ARCHIVE_PARTS * 2:
            raise ValueError("网页存档的资源数量超过上限（400 个资源）。")
        try:
            message = BytesParser(policy=policy.default).parsebytes(raw.removeprefix(b"\xef\xbb\xbf"))
            parts = _archive_parts(message)
        except (RecursionError, TypeError, IndexError) as exc:
            raise ValueError("MHTML 网页存档结构无效，请在浏览器中重新另存为网页。") from exc
        leaves = [part for part in parts if not part.is_multipart()]
        html_parts = [part for part in leaves if part.get_content_type() in {"text/html", "application/xhtml+xml"}]
        if not html_parts:
            raise ValueError("MHTML 网页存档中没有 HTML 正文。")
        html_part = html_parts[0]
        start = str(message.get_param("start", "", header="content-type")).strip().strip("<>")
        if start:
            root_part = next((part for part in parts if _content_id(part) == start), None)
            if root_part is not None:
                html_part = next((part for part in _archive_parts(root_part)
                                  if not part.is_multipart() and part.get_content_type() in
                                  {"text/html", "application/xhtml+xml"}), html_part)
        archive_base = str(message.get("Content-Base", "") or
                           message.get("Content-Location", "") or
                           message.get("Snapshot-Content-Location", "")).strip()
        html_location = str(html_part.get("Content-Location", "")).strip()
        self.base_url = urljoin(archive_base, html_location) if html_location else archive_base
        if html_part.get("Content-Base"):
            self.base_url = urljoin(self.base_url, str(html_part["Content-Base"]).strip())
        html_content = b""
        for part in leaves:
            content = part.get_payload(decode=True)
            if content is None:
                raise ValueError("MHTML 网页存档包含无法解码的资源。")
            if part.defects:
                raise ValueError("MHTML 网页存档的资源编码或边界无效，请重新另存为网页。")
            if self.used + len(content) > _PAGE_LIMIT:
                raise ValueError("网页存档解码后超过整页 30 MB 上限。")
            self.used += len(content)
            if part is html_part:
                if len(content) > _HTML_LIMIT:
                    raise ValueError("MHTML 网页正文超过 5 MB 上限。")
                html_content = content
            if not part.get_content_type().startswith("image/"):
                continue
            if len(content) > _IMAGE_LIMIT:
                raise ValueError("网页存档中的单张图片超过 12 MB 上限。")
            location = str(part.get("Content-Location", "")).strip()
            cid = _content_id(part)
            keys = []
            if location:
                resource_base = urljoin(self.base_url, str(part.get("Content-Base", "")).strip())
                keys.extend((_archive_key(location), _archive_key(urljoin(resource_base, location))))
            if cid:
                keys.append(_archive_key("cid:" + cid))
            for key in keys:
                previous = self.archive_resources.get(key)
                if previous is not None and previous != content:
                    raise ValueError("网页存档中同一图片地址对应不同资源，请重新另存为网页。")
                self.archive_resources[key] = content
        if any(part.defects for part in parts):
            raise ValueError("MHTML 网页存档的 MIME 边界无效，请重新另存为网页。")
        charset = html_part.get_content_charset()
        if charset:
            try:
                return html_content.decode(charset)
            except (LookupError, UnicodeError) as exc:
                raise ValueError("MHTML 网页正文的字符编码无效，请重新另存为网页。") from exc
        return html_content

    def picture(self, value: str) -> bytes:
        if value in self.cache:
            return self.cache[value]
        if value.lower().startswith("data:"):
            if len(value) > _IMAGE_LIMIT * 4 // 3 + 4096:
                raise ValueError("内嵌图片超过 12 MB 上限。")
            header, separator, payload = value.partition(",")
            if not separator or not header.lower().startswith("data:image/"):
                raise ValueError("内嵌内容不是图片。")
            try:
                content = base64.b64decode(re.sub(r"\s+", "", payload), validate=True) if ";base64" in header.lower() else unquote_to_bytes(payload)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("内嵌图片编码无效。") from exc
            self._consume(content)
        elif self.archive_resources is not None:
            keys = (_archive_key(value), _archive_key(urljoin(self.base_url, value)))
            content = next((self.archive_resources[key] for key in keys if key in self.archive_resources), None)
            if content is None:
                raise ValueError("网页存档未包含该图片，已阻止联网或读取存档外文件。")
        elif self.local_root is not None:
            parsed = urlsplit(value)
            if parsed.scheme or parsed.netloc or value.startswith(("/", "\\")):
                raise ValueError("本地 HTML 仅允许读取同目录内的相对图片，已阻止外部地址。")
            relative = unquote(parsed.path)
            candidate = (self.local_root / relative).resolve()
            if not candidate.is_relative_to(self.local_root):
                raise ValueError("图片路径超出 HTML 所在目录，已阻止读取。")
            if not candidate.is_file():
                raise ValueError("本地图片不存在。")
            if candidate.stat().st_size > _IMAGE_LIMIT:
                raise ValueError("本地图片超过 12 MB 上限。")
            with candidate.open("rb") as stream:
                content = stream.read(_IMAGE_LIMIT + 1)
            self._consume(content)
        else:
            content = self._download(_http_url(urljoin(self.base_url, value)), _IMAGE_LIMIT)
        self.cache[value] = content
        return content

    def _consume(self, content: bytes):
        if len(content) > _IMAGE_LIMIT or self.used + len(content) > _PAGE_LIMIT:
            raise ValueError("图片超过大小上限（单张 12 MB，整页 30 MB）。")
        self.used += len(content)

    def close(self):
        if self.session:
            self.session.close()
        self.cache.clear()
        if self.archive_resources is not None:
            self.archive_resources.clear()


def _font(style):
    style.font.name = "宋体"
    style.font.color.rgb = RGBColor(0, 0, 0)
    rpr = style.element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.insert(0, fonts)
    fonts.set(qn("w:eastAsia"), "宋体")


def _text(node: Tag) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


class _Renderer:
    def __init__(self, document, reader, include_images, max_images, title):
        self.document = document
        self.reader = reader
        self.include_images = include_images
        self.max_images = max_images
        self.title = title
        self.changes = 0
        self.notices = []
        self.image_attempts = 0
        self.skipped_title = False

    def notice(self, message):
        if message not in self.notices:
            self.notices.append(message)

    def paragraph(self, container=None, style=None):
        if container is not None and container.paragraphs:
            last = container.paragraphs[-1]
            if not last.text and not last._p.findall(".//" + qn("w:drawing")):
                if style:
                    last.style = style
                return last
        destination = self.document if container is None else container
        return destination.add_paragraph(style=style)

    def image(self, node, container=None):
        if not self.include_images:
            return
        if self.image_attempts >= self.max_images:
            self.notice(f"图片数量超过 {self.max_images} 张，超出部分已跳过。")
            return
        self.image_attempts += 1
        value = next((str(node.get(key)).strip() for key in ("data-src", "data-original", "data-actualsrc", "src") if node.get(key)), "")
        if not value and node.get("srcset"):
            value = str(node["srcset"]).split(",")[0].strip().split(" ")[0]
        if not value:
            self.notice("部分图片没有可读取的地址，已跳过。")
            return
        try:
            raw = self.reader.picture(value)
            with python_warnings.catch_warnings():
                python_warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as original:
                    if original.width * original.height > 40_000_000:
                        raise ValueError("图片超过 4000 万像素。")
                    if getattr(original, "n_frames", 1) > 1:
                        self.notice("动态或多页插图仅保留第一帧。")
                    picture = ImageOps.exif_transpose(original)
                    picture = picture.convert("RGBA" if "A" in picture.getbands() or "transparency" in picture.info else "RGB")
                    width, height = picture.size
                    converted = io.BytesIO()
                    picture.info.clear()
                    picture.save(converted, format="PNG")
            converted.seek(0)
            maximum_width = 6.1 if container is None else 1.5
            scale = min(1 / 96, maximum_width / width, 8.4 / height)
            paragraph = self.paragraph(container)
            paragraph.add_run().add_picture(converted, width=Inches(width * scale), height=Inches(height * scale))
            self.changes += 1
        except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError,
                Image.DecompressionBombWarning) as exc:
            label = "内嵌图片" if value.startswith("data:") else value[:100]
            self.notice(f"插图未能导入：{label}（{exc}）")

    def inline(self, node, style=None, level=0, container=None):
        segments = []

        def collect(current, bold=False, italic=False):
            if isinstance(current, Comment):
                return
            if isinstance(current, NavigableString):
                value = re.sub(r"\s+", " ", str(current))
                if value:
                    segments.append(("text", value, bold, italic))
                return
            if not isinstance(current, Tag):
                return
            if current.name == "img":
                segments.append(("image", current, False, False))
            elif current.name == "br":
                segments.append(("text", "\n", bold, italic))
            elif current.name not in {"ul", "ol", "table"}:
                boundary = current.name in _BLOCKS
                if boundary and segments:
                    segments.append(("text", "\n", bold, italic))
                for child in current.children:
                    collect(child, bold or current.name in {"b", "strong"}, italic or current.name in {"i", "em"})
                if boundary:
                    segments.append(("text", "\n", bold, italic))

        for child in node.children:
            collect(child)
        paragraph = None
        for kind, value, bold, italic in segments:
            if kind == "image":
                self.image(value, container)
                paragraph = None
                continue
            if paragraph is None:
                value = value.lstrip()
                if not value:
                    continue
                paragraph = self.paragraph(container, style)
                if level:
                    paragraph.paragraph_format.left_indent = Inches(level * 0.25)
                self.changes += 1
            run = paragraph.add_run(value)
            run.bold, run.italic = bold, italic

    def table(self, node):
        rows = [row for row in node.find_all("tr") if row.find_parent("table") is node]
        if len(rows) > 200:
            rows = rows[:200]
            self.notice("表格超过 200 行，已保留前 200 行。")
        cells = [row.find_all(["th", "td"], recursive=False) for row in rows]
        columns = max((len(row) for row in cells), default=0)
        if not columns:
            return
        if columns > 30:
            columns = 30
            self.notice("表格超过 30 列，已保留前 30 列。")
        table = self.document.add_table(rows=len(rows), cols=columns)
        table.style = "Table Grid"
        self.changes += 1
        for i, row in enumerate(cells):
            for j, cell in enumerate(row[:columns]):
                destination = table.cell(i, j)
                self.inline(cell, container=destination)
                if cell.name == "th":
                    for paragraph in destination.paragraphs:
                        for run in paragraph.runs:
                            run.bold = True

    def block(self, node, level=0):
        name = node.name
        if name == "table":
            self.table(node)
        elif name in {"ul", "ol"}:
            for item in node.find_all("li", recursive=False):
                self.inline(item, "List Number" if name == "ol" else "List Bullet", level)
                for nested in item.find_all(["ul", "ol"], recursive=False):
                    self.block(nested, level + 1)
        elif name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            text = _text(node)
            if name == "h1" and not self.skipped_title and text == self.title:
                self.skipped_title = True
                return
            if text:
                heading = self.document.add_heading(text, level=min(3, int(name[1])))
                for run in heading.runs:
                    run.font.color.rgb = RGBColor(0, 0, 0)
                self.changes += 1
        elif name == "img":
            self.image(node)
        elif name in {"p", "figcaption", "pre"}:
            self.inline(node)
        elif name == "hr":
            return
        else:
            self.container(node, level)

    def container(self, node, level=0):
        pending = []

        def flush():
            if not pending:
                return
            temporary = BeautifulSoup("<p></p>", "html.parser").p
            for child in pending:
                temporary.append(child.__copy__())
            self.inline(temporary)
            pending.clear()

        for child in list(node.children):
            if isinstance(child, Comment):
                continue
            if isinstance(child, Tag) and child.name in _BLOCKS | {"img"}:
                flush()
                self.block(child, level)
            elif isinstance(child, Tag) and child.find(list(_BLOCKS)) is not None:
                flush()
                self.container(child, level)
            else:
                pending.append(child)
        flush()


def webpage_to_word(source: str | Path, target: Path, options: dict) -> dict:
    """Convert a public HTML article or a local HTML/MHTML page to a new DOCX.

    Local HTML is processed without network access and may only reference local
    pictures within its own directory tree or data URIs. JavaScript/CSS are never
    executed. MHTML resources are decoded in memory and only embedded images
    are read; missing resources never trigger a network request or disk lookup.
    HTTP downloads have per-request and overall limits.
    """
    target = Path(target)
    if target.exists():
        raise FileExistsError(f"输出文件已存在，已保留：{target.name}")
    if isinstance(source, Path) or (isinstance(source, str) and not source.lower().startswith(("http://", "https://"))):
        if Path(source).resolve() == target.resolve():
            raise ValueError("输出文件不能与原 HTML 文件相同。")
    timeout = _integer(options.get("timeout", 20), "超时时间", 1, 120)
    max_images = _integer(options.get("max_images", 40), "最大图片数", 0, 200)
    include_images = options.get("include_images", True)
    if not isinstance(include_images, bool):
        raise ValueError("是否包含图片必须为布尔值。")
    reader = _Reader(source, timeout)
    temp = None
    try:
        html = reader.html()
        soup = BeautifulSoup(html, "html.parser")
        if reader.archive_resources is not None:
            base = soup.find("base", href=True)
            if base is not None:
                reader.base_url = urljoin(reader.base_url, str(base["href"]))
        had_scripts = bool(soup.find("script"))
        original_text = _text(soup)
        challenge = re.search(r"请先登录|登录后(?:阅读|查看)|请完成验证|安全验证|访问过于频繁|verify you are human|access denied|just a moment|authentication required|enable javascript and cookies", original_text, re.I)
        login_title = bool(soup.title and re.search(r"^(?:用户)?登[录陸]|^sign[ -]?in|^log[ -]?in", _text(soup.title), re.I))
        password_form = soup.find("input", attrs={"type": re.compile("^password$", re.I)})
        if (challenge or login_title or password_form) and len(original_text) < 600:
            raise ValueError("页面要求登录或安全验证，无法直接提取文章。请先在浏览器另存为 HTML 后导入。")
        for element in list(soup.select("script,style,noscript,nav,footer,aside,form,button,input,select,textarea,iframe,object,embed,link,meta")):
            element.decompose()
        for element in list(soup.find_all(True)):
            if element.parent is None:
                continue
            identity = " ".join([str(element.get("id", "")), " ".join(element.get("class", []))])
            hidden = str(element.get("style", "")).replace(" ", "").lower()
            if _AD.search(identity) or element.has_attr("hidden") or "display:none" in hidden or "visibility:hidden" in hidden:
                element.decompose()
        content = soup.select_one("#js_content") or soup.find("article") or soup.find("main") or soup.find("body") or soup
        content_text = _text(content)
        if not content_text and not content.find("img"):
            raise ValueError("页面中没有可提取正文，可能需要登录、验证或依赖 JavaScript 动态加载。")
        first_heading = content.find("h1")
        title = _text(first_heading) if first_heading else (_text(soup.title) if soup.title else "网页文章")
        title = title[:200] or "网页文章"
        document = Document()
        for style_name in ("Normal", "Title", "Subtitle", "Heading 1", "Heading 2", "Heading 3", "List Bullet", "List Number"):
            _font(document.styles[style_name])
        document.styles["Normal"].font.size = Pt(11)
        document.core_properties.title = title
        document.add_heading(title, 0)
        renderer = _Renderer(document, reader, include_images, max_images, title)
        if had_scripts and len(content_text) < 100:
            renderer.notice("页面正文较少，可能包含依赖 JavaScript 的动态内容；仅导出已获得的静态内容。")
        renderer.container(content)
        if renderer.changes == 0:
            raise ValueError("未获得可导出的正文或图片，请检查网页内容。")
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix=".qingjian-web-", suffix=".docx", dir=target.parent)
        os.close(handle)
        temp = Path(name)
        document.save(temp)
        if os.name == "nt":
            os.rename(temp, target)
        else:
            os.link(temp, target)
            temp.unlink()
        return {"changes": renderer.changes, "warnings": renderer.notices}
    finally:
        reader.close()
        if temp is not None and temp.exists():
            temp.unlink()
