"""Offline format conversion for documents, sheets, slides and lightweight EPUB.

The public adapter deliberately advertises directions, not a Cartesian product
of extensions.  It never downloads an engine, executes document macros or OCRs
pages.  ``outputs`` includes every produced page image so a caller can publish
an entire job from its private staging directory.
"""
from __future__ import annotations

import base64
import contextlib
import csv
import html
import io
import os
import posixpath
import re
import shutil
import stat
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from .common import atomic_write, ensure_cancel
from . import conversion
from .text_ops import _ordinary_path, read_text_file


WORD_TYPES = frozenset({".doc", ".docx", ".rtf", ".odt"})
SHEET_TYPES = frozenset({".xls", ".xlsx", ".ods", ".csv", ".tsv"})
SLIDE_TYPES = frozenset({".ppt", ".pptx", ".odp"})
WEB_TYPES = frozenset({".html", ".htm", ".xhtml", ".mht", ".mhtml"})
TEXT_TYPES = frozenset({".txt", ".md", ".markdown"})
IMAGE_TYPES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".avif", ".ico", ".jp2", ".j2k", ".heic", ".heif"})
PDF_TARGETS = frozenset({".docx", ".txt", ".xlsx", ".csv", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".pptx"})
MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_HTML_BYTES = 40 * 1024 * 1024
MAX_PAGES = 1000
MAX_RENDER_PIXELS = 250_000_000

_FILTERS = {
    "word": {".doc": "doc:MS Word 97", ".docx": "docx:Office Open XML Text",
             ".rtf": "rtf:Rich Text Format", ".odt": "odt:writer8", ".pdf": "pdf:writer_pdf_Export"},
    "sheet": {".xls": "xls:MS Excel 97", ".xlsx": "xlsx:Calc MS Excel 2007 XML",
              ".ods": "ods:calc8", ".pdf": "pdf:calc_pdf_Export"},
    "slide": {".ppt": "ppt:MS PowerPoint 97", ".pptx": "pptx:Impress MS PowerPoint 2007 XML",
              ".odp": "odp:impress8", ".pdf": "pdf:impress_pdf_Export"},
}


def targets_for(source_ext: str) -> frozenset[str]:
    """Return directions implemented here; image-to-image lives elsewhere."""
    ext = str(source_ext).lower()
    if ext and not ext.startswith(".") and ext not in {"url", "http", "https"}:
        ext = "." + ext
    if ext in WORD_TYPES:
        result = WORD_TYPES | {".pdf", ".txt", ".html", ".md", ".epub"}
    elif ext in SHEET_TYPES:
        result = SHEET_TYPES | {".pdf", ".html"}
    elif ext in SLIDE_TYPES:
        result = SLIDE_TYPES | {".pdf", ".png", ".jpg", ".jpeg", ".docx", ".txt"}
    elif ext == ".pdf":
        result = PDF_TARGETS
    elif ext in WEB_TYPES or ext in {"url", "http", "https"}:
        result = {".docx", ".pdf", ".txt", ".md", ".html", ".epub"}
    elif ext in TEXT_TYPES:
        result = {".docx", ".pdf", ".txt", ".md", ".html", ".epub"}
    elif ext == ".epub":
        result = {".docx", ".pdf", ".txt", ".html", ".md"}
    elif ext in IMAGE_TYPES:
        result = {".pdf", ".docx", ".pptx"}
    else:
        result = set()
    return frozenset(result - {ext})


def supports(source_ext: str, target_ext: str) -> bool:
    source_ext = str(source_ext).lower()
    if source_ext and not source_ext.startswith(".") and source_ext not in {"url", "http", "https"}:
        source_ext = "." + source_ext
    target_ext = str(target_ext).lower()
    if not target_ext.startswith("."):
        target_ext = "." + target_ext
    # The hub exposes this through a separate UTF-8 action, rather than a
    # confusing same-format choice among the normal format targets.
    if source_ext == target_ext and source_ext in TEXT_TYPES:
        return True
    return target_ext in targets_for(source_ext)


def _check(options):
    cancel = options.get("_cancel")
    ensure_cancel(cancel.is_set if hasattr(cancel, "is_set") else cancel)


def _number(value, label, lower, upper):
    if isinstance(value, bool):
        raise ValueError(f"{label}必须为整数。")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}必须为整数。") from exc
    if not lower <= number <= upper or number != int(number):
        raise ValueError(f"{label}必须为 {lower} 至 {upper} 的整数。")
    return int(number)


def _source(path):
    path = Path(path).expanduser().absolute()
    _ordinary_path(path)
    if not path.is_file():
        raise FileNotFoundError(f"找不到输入文件：{path.name}")
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("单个输入文件超过 512 MB，请拆分后转换。")
    return path


@contextlib.contextmanager
def _archive(path):
    """Read ZIP packages without extracting untrusted member paths."""
    with zipfile.ZipFile(path) as package:
        entries = package.infolist()
        if len(entries) > 30000 or sum(item.file_size for item in entries) > MAX_ARCHIVE_BYTES:
            raise ValueError("文档内部文件超过 30000 个或展开大小超过 512 MB。")
        seen = set()
        for item in entries:
            name = PurePosixPath(item.filename)
            if (name.is_absolute() or ".." in name.parts or "\\" in item.orig_filename or "\x00" in item.orig_filename
                    or ":" in item.filename or item.filename.casefold() in seen
                    or stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1):
                raise ValueError("文档内部文件路径重复、越界、包含链接或已加密。")
            if item.file_size > MAX_MEMBER_BYTES:
                raise ValueError("文档内部单个部件超过 64 MB。")
            seen.add(item.filename.casefold())
        yield package


def _xml(data):
    if len(data) > MAX_MEMBER_BYTES or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("XML 部件过大或包含不支持的 DTD/实体。")
    from lxml import etree
    try:
        root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
    except etree.XMLSyntaxError as exc:
        raise ValueError("文档的 XML 部件无效或已损坏。") from exc
    if root.getroottree().docinfo.doctype:
        raise ValueError("XML 部件包含不支持的 DTD/实体。")
    return root


def _office_safety(path):
    if path.suffix.lower() not in {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}:
        return
    with _archive(path) as package:
        for item in package.infolist():
            name = item.filename.lower()
            if name.endswith("vbaproject.bin") or "/externallinks/" in name or name.startswith(("basic/", "scripts/")):
                raise ValueError("文档含宏或外部数据链接，请另存为不含这些内容的本地副本。")
            if name.endswith(".rels"):
                for relation in _xml(package.read(item)):
                    if relation.get("TargetMode", "").lower() == "external" and not relation.get("Type", "").endswith("/hyperlink"):
                        raise ValueError("文档包含外部图片或数据链接，请先将资源保存在文件内。")
            elif name == "content.xml":
                for node in _xml(package.read(item)).iter():
                    href = node.get("{http://www.w3.org/1999/xlink}href", "")
                    if href and node.tag.rsplit("}", 1)[-1] in {"image", "object", "object-ole", "section-source", "table-source"}:
                        if urlsplit(href).scheme or href.startswith(("/", "\\", "../")):
                            raise ValueError("文档包含外部图片或数据引用，请先嵌入这些资源。")


def _lo(source, target, family, options):
    _check(options)
    _office_safety(source)
    # Fresh Writer/Calc/Impress profiles may spend over two minutes building
    # font and extension caches on a cold Windows machine. The cancellation
    # predicate is still polled every 250 ms by the subprocess helper.
    engine_options = {"timeout": 240, **options}
    result = conversion._convert_office(
        source, target, engine_options, {source.suffix.lower()}, {target.suffix.lower()},
        filter_name=_FILTERS[family][target.suffix.lower()],
    )
    _check(options)
    if target.suffix.lower() in {".xlsx", ".pptx", ".odt", ".ods", ".odp"}:
        with _archive(target) as package:
            required = {".xlsx": "xl/workbook.xml", ".pptx": "ppt/presentation.xml"}.get(target.suffix.lower(), "content.xml")
            if required not in package.namelist():
                raise ValueError("转换结果缺少必要的文档结构。")
    return result


def _publish(staged, target, options):
    _check(options)
    atomic_write(target, lambda temporary: shutil.copyfile(staged, temporary))
    return target


def _html_document(title, body):
    return ('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<title>{html.escape(title)}</title><style>body{{font-family:Arial,"Microsoft YaHei",sans-serif;'
            'line-height:1.7;max-width:960px;margin:2em auto;padding:0 1em}table{border-collapse:collapse}'
            'td,th{border:1px solid #cbd5e1;padding:6px 10px}img{max-width:100%;height:auto}'
            'pre{white-space:pre-wrap}h1,h2,h3{line-height:1.4}</style></head><body>' + body + '</body></html>')


def _image_data_uri(data):
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(data)) as picture:
        if picture.width * picture.height > 50_000_000:
            raise ValueError("单张插图超过 5000 万像素。")
        image = ImageOps.exif_transpose(picture).convert("RGBA")
        try:
            buffer = io.BytesIO()
            image.save(buffer, "PNG")
        finally:
            image.close()
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _docx_html(path, options, warnings):
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    from docx.oxml.ns import qn
    _office_safety(path)
    document = Document(path)
    image_count = 0

    def inline(node):
        nonlocal image_count
        _check(options)
        local = node.tag.rsplit("}", 1)[-1]
        if local == "t":
            return html.escape(node.text or "")
        if local == "tab":
            return "&#9;"
        if local in {"br", "cr"}:
            return "<br/>"
        if local == "blip":
            rid = node.get(qn("r:embed"))
            if rid and rid in document.part.related_parts:
                part = document.part.related_parts[rid]
                try:
                    data_uri = _image_data_uri(part.blob)
                    image_count += 1
                    return f'<img src="{data_uri}" alt="文档插图 {image_count}"/>'
                except (ValueError, OSError):
                    warnings.append("部分矢量或特殊图片无法导出，已保留其余内容。")
            return ""
        value = "".join(inline(child) for child in node if child.tag.rsplit("}", 1)[-1] not in {"rPr", "pPr"})
        if local == "r":
            props = node.find(qn("w:rPr"))
            if props is not None:
                for name, tag in (("b", "strong"), ("i", "em")):
                    setting = props.find(qn("w:" + name))
                    if setting is not None and setting.get(qn("w:val"), "true") not in {"0", "false", "off"}:
                        value = f"<{tag}>{value}</{tag}>"
        return value

    def paragraph(element):
        item = Paragraph(element, document)
        content = inline(element)
        name = item.style.name if item.style is not None else ""
        match = re.search(r"(?:Heading|标题)\s*([1-6])", name, re.I)
        tag = "h" + match.group(1) if match else "p"
        if name.lower().startswith("list bullet"):
            content = "• " + content
        return f"<{tag}>{content}</{tag}>"

    body = []
    for element in document.element.body:
        _check(options)
        local = element.tag.rsplit("}", 1)[-1]
        if local == "p":
            body.append(paragraph(element))
        elif local == "tbl":
            table = Table(element, document)
            body.append("<table>")
            for row in table.rows:
                body.append("<tr>" + "".join("<td>" + "<br/>".join(inline(p._p) for p in cell.paragraphs) + "</td>" for cell in row.cells) + "</tr>")
            body.append("</table>")
    warnings.append("已保留正文、标题、表格和可读取插图；页眉页脚、批注、分页与复杂版式不带入重排格式。")
    return _html_document(document.core_properties.title or path.stem, "\n".join(body))


def _clean_soup(content):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(content, "html.parser")
    for tag in list(soup.select("script,style,iframe,object,embed,form,link,meta,base")):
        tag.decompose()
    for tag in soup.find_all(True):
        for key in list(tag.attrs):
            if key.lower().startswith("on") or key.lower() in {"srcset", "style"}:
                del tag[key]
        if tag.name == "a" and urlsplit(str(tag.get("href", ""))).scheme.lower() not in {"", "http", "https", "mailto"}:
            tag.attrs.pop("href", None)
    return soup


def _html_text(content):
    soup = _clean_soup(content)
    body = soup.body or soup
    for br in body.find_all("br"):
        br.replace_with("\n")
    for table in body.find_all("table"):
        rows = ["\t".join(cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"], recursive=False)) for row in table.find_all("tr")]
        table.replace_with("\n" + "\n".join(rows) + "\n")
    for block in body.find_all(["p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "li", "pre", "blockquote"]):
        block.insert_after("\n")
    text = "\n".join(line.strip() for line in body.get_text("", strip=False).splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def _html_markdown(content, warnings):
    from bs4 import NavigableString
    soup = _clean_soup(content)
    def render(node, depth=0):
        if isinstance(node, NavigableString):
            return str(node)
        name = node.name
        if name == "table":
            rows = [[re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).replace("|", "\\|") for cell in row.find_all(["td", "th"], recursive=False)] for row in node.find_all("tr")]
            if not rows:
                return ""
            columns = max(map(len, rows))
            formatted = ["| " + " | ".join(row + [""] * (columns - len(row))) + " |" for row in rows]
            formatted.insert(1, "| " + " | ".join(["---"] * columns) + " |")
            return "\n\n" + "\n".join(formatted) + "\n\n"
        if name == "img":
            warnings.append("Markdown 仅记录插图说明；需要保留嵌入图片时请选择 HTML、Word 或 EPUB。")
            return "[图片：" + str(node.get("alt", "插图")) + "]"
        value = "".join(render(child, depth + 1) for child in node.children)
        if name and re.fullmatch("h[1-6]", name):
            return "\n\n" + "#" * int(name[1]) + " " + value.strip() + "\n\n"
        if name == "br":
            return "  \n"
        if name in {"strong", "b"}:
            return "**" + value + "**"
        if name in {"em", "i"}:
            return "*" + value + "*"
        if name == "a" and node.get("href"):
            return "[" + value + "](" + str(node["href"]).replace(")", "%29") + ")"
        if name == "pre":
            fence = "`" * max(3, max((len(x) + 1 for x in re.findall(r"`+", value)), default=3))
            return "\n\n" + fence + "\n" + value.rstrip() + "\n" + fence + "\n\n"
        if name == "code" and getattr(node.parent, "name", None) != "pre":
            return "`" + value.replace("`", "\\`") + "`"
        if name == "li":
            prefix = "1. " if getattr(node.parent, "name", None) == "ol" else "- "
            return "\n" + prefix + value.strip()
        if name in {"p", "div", "section", "article", "blockquote", "ul", "ol"}:
            return "\n\n" + value.strip() + "\n\n"
        return value
    return re.sub(r"\n{3,}", "\n\n", render(soup.body or soup)).strip() + "\n"


def _text_html(source, options, warnings):
    text = read_text_file(source, warnings)
    _check(options)
    if source.suffix.lower() in {".md", ".markdown"}:
        from markdown_it import MarkdownIt
        body = MarkdownIt("commonmark", {"html": False}).enable("table").render(text)
        # Resolve local Markdown pictures before the HTML is staged elsewhere.
        from bs4 import BeautifulSoup
        from .web_ops import _Reader
        soup = BeautifulSoup(body, "html.parser")
        reader = _Reader(source, 20)
        reader.local_root = source.parent.resolve()
        try:
            for image in soup.find_all("img"):
                _check(options)
                try:
                    image["src"] = _image_data_uri(reader.picture(str(image.get("src", ""))))
                except (ValueError, OSError) as exc:
                    warnings.append("Markdown 插图未嵌入：" + str(exc))
                    image.replace_with("[图片：" + str(image.get("alt", "插图")) + "]")
        finally:
            reader.close()
        body = str(soup)
    else:
        body = "\n".join("<p>" + html.escape(line) + "</p>" for line in text.splitlines())
    return _html_document(source.stem, body)


def _epub_member(base, href):
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc or "\\" in href:
        raise ValueError("EPUB 引用了存档外部的内容。")
    relative = unquote(parsed.path)
    value = posixpath.normpath(posixpath.join(posixpath.dirname(base), relative))
    if value.startswith(("../", "/")) or value == ".." or ":" in value:
        raise ValueError("EPUB 的资源路径越界。")
    return value


def _epub_html(source, options, warnings):
    from bs4 import BeautifulSoup
    with _archive(source) as package:
        names = set(package.namelist())
        if "META-INF/encryption.xml" in names:
            encryption = _xml(package.read("META-INF/encryption.xml"))
            if list(encryption):
                raise ValueError("EPUB 含加密或受保护的资源；本工具仅转换未加密电子书。")
        if "META-INF/container.xml" not in names:
            raise ValueError("文件缺少 EPUB 容器信息。")
        container = _xml(package.read("META-INF/container.xml"))
        roots = [node.get("full-path", "") for node in container.iter() if node.tag.endswith("}rootfile") or node.tag == "rootfile"]
        if not roots:
            raise ValueError("EPUB 未指定内容清单。")
        opf_name = _epub_member("", roots[0])
        opf = _xml(package.read(opf_name))
        manifest = {}
        spine = []
        title = source.stem
        for node in opf.iter():
            local = node.tag.rsplit("}", 1)[-1]
            if local == "item":
                manifest[node.get("id")] = (_epub_member(opf_name, node.get("href", "")), node.get("media-type", ""))
            elif local == "itemref" and node.get("linear", "yes") != "no":
                spine.append(node.get("idref"))
            elif local == "title" and node.text:
                title = node.text
        if not spine or len(spine) > 2000:
            raise ValueError("EPUB 没有正文阅读顺序或章节超过 2000 个。")
        chapters = []
        total = 0
        for chapter_id in spine:
            _check(options)
            if chapter_id not in manifest:
                raise ValueError("EPUB 正文章节清单不完整。")
            chapter, media = manifest[chapter_id]
            if media not in {"application/xhtml+xml", "text/html"}:
                warnings.append("已跳过非文字章节：" + chapter)
                continue
            data = package.read(chapter)
            total += len(data)
            if total > MAX_HTML_BYTES:
                raise ValueError("EPUB 正文超过 40 MB，请拆分后转换。")
            soup = _clean_soup(data)
            for image in soup.find_all("img"):
                _check(options)
                try:
                    resource = _epub_member(chapter, str(image.get("src", "")))
                    if resource not in names:
                        raise ValueError("缺少图片资源")
                    image["src"] = _image_data_uri(package.read(resource))
                except (ValueError, KeyError, OSError) as exc:
                    warnings.append("EPUB 插图未嵌入：" + str(exc))
                    image.replace_with("[图片：" + str(image.get("alt", "插图")) + "]")
            body = soup.body or soup
            chapters.append("<section>" + "".join(str(child) for child in body.children) + "</section>")
        if not chapters:
            raise ValueError("EPUB 中没有可转换的 HTML 正文章节。")
        warnings.append("电子书按阅读顺序重排正文与插图；交互内容、原书分页和复杂样式不保留。")
        return _html_document(title, "\n".join(chapters))


def _write_epub(content, target, options, warnings):
    """Create a small standards-based EPUB 3 using only ZIP, XML and XHTML."""
    from bs4 import NavigableString
    soup = _clean_soup(content)
    title = soup.title.get_text(strip=True) if soup.title else target.stem
    ns = "http://www.w3.org/1999/xhtml"
    epub_ns = "http://www.idpf.org/2007/ops"
    ET.register_namespace("", ns)
    root = ET.Element("{" + ns + "}html", {"lang": "zh-CN"})
    head = ET.SubElement(root, "{" + ns + "}head")
    ET.SubElement(head, "{" + ns + "}title").text = title
    ET.SubElement(head, "{" + ns + "}meta", {"charset": "utf-8"})
    body = ET.SubElement(root, "{" + ns + "}body")
    images = []
    allowed = {"p", "div", "section", "article", "span", "h1", "h2", "h3", "h4", "h5", "h6", "b", "strong", "i", "em", "u", "s", "br", "hr", "ul", "ol", "li", "blockquote", "pre", "code", "table", "thead", "tbody", "tfoot", "tr", "td", "th", "a", "img", "sup", "sub"}

    def append_text(parent, text):
        if len(parent):
            parent[-1].tail = (parent[-1].tail or "") + text
        else:
            parent.text = (parent.text or "") + text

    def append(parent, node):
        _check(options)
        if isinstance(node, NavigableString):
            append_text(parent, str(node))
            return
        if not getattr(node, "name", None):
            return
        name = node.name if node.name in allowed else "span"
        attrs = {key: str(node[key]) for key in ("alt", "colspan", "rowspan", "title") if node.has_attr(key)}
        if name == "a":
            href = str(node.get("href", ""))
            if urlsplit(href).scheme in {"http", "https", "mailto"}:
                attrs["href"] = href
        if name == "img":
            value = str(node.get("src", ""))
            if not value.startswith("data:image/") or ";base64," not in value:
                warnings.append("EPUB 中未嵌入未下载的外部图片。")
                return
            try:
                raw = base64.b64decode(value.partition(",")[2], validate=True)
                normalized = _image_data_uri(raw)
                data = base64.b64decode(normalized.partition(",")[2])
            except (ValueError, OSError) as exc:
                raise ValueError("EPUB 插图编码无效。") from exc
            name_in_zip = f"Images/image{len(images) + 1:04d}.png"
            images.append((name_in_zip, data))
            attrs["src"] = name_in_zip
            attrs.setdefault("alt", "插图")
        element = ET.SubElement(parent, "{" + ns + "}" + name, attrs)
        for child in node.children:
            append(element, child)

    for node in (soup.body or soup).children:
        append(body, node)
    chapter = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    identifier = "urn:uuid:" + str(uuid.uuid4())
    image_manifest = "".join(f'<item id="image{i}" href="{name}" media-type="image/png"/>' for i, (name, _) in enumerate(images, 1))
    opf = ('<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:identifier id="book-id">{identifier}</dc:identifier><dc:title>{html.escape(title)}</dc:title><dc:language>zh-CN</dc:language>'
           f'<meta property="dcterms:modified">{datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}</meta></metadata>'
           '<manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>'
           '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>' + image_manifest + '</manifest>'
           '<spine><itemref idref="chapter"/></spine></package>')
    nav = ('<?xml version="1.0" encoding="utf-8"?><html xmlns="' + ns + '" xmlns:epub="' + epub_ns + '"><head><title>目录</title></head>'
           '<body><nav epub:type="toc"><h1>目录</h1><ol><li><a href="chapter.xhtml">' + html.escape(title) + '</a></li></ol></nav></body></html>')
    container = '<?xml version="1.0" encoding="utf-8"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        package.writestr("META-INF/container.xml", container)
        package.writestr("OEBPS/content.opf", opf)
        package.writestr("OEBPS/chapter.xhtml", chapter)
        package.writestr("OEBPS/nav.xhtml", nav)
        for name, data in images:
            _check(options)
            package.writestr("OEBPS/" + name, data)


def _html_output(content, target, options, warnings, temporary):
    ext = target.suffix.lower()
    _check(options)
    if ext == ".html":
        target.write_text(content, encoding="utf-8")
    elif ext in {".txt", ".md"}:
        text = _html_text(content) if ext == ".txt" else _html_markdown(content, warnings)
        encoding = options.get("encoding", "utf-8")
        if encoding not in {"utf-8", "utf-8-sig", "utf-16", "gb18030"}:
            raise ValueError("输出编码仅支持 UTF-8、UTF-8 BOM、UTF-16 或 GB18030。")
        target.write_text(text, encoding=encoding)
    elif ext == ".epub":
        _write_epub(content, target, options, warnings)
    else:
        from .web_ops import webpage_to_word
        local_html = temporary / ("body-" + uuid.uuid4().hex[:8] + ".html")
        local_html.write_text(content, encoding="utf-8")
        docx = target if ext == ".docx" else temporary / ("body-" + uuid.uuid4().hex[:8] + ".docx")
        result = webpage_to_word(local_html, docx, {**options, "max_images": 200})
        warnings.extend(result.get("warnings", []))
        if ext == ".pdf":
            result = _lo(docx, target, "word", options)
            warnings.extend(result.get("warnings", []))


def _sheet_xlsx(source, temporary, options, warnings):
    from openpyxl import Workbook
    if source.suffix.lower() == ".xlsx":
        _office_safety(source)
        return source
    target = temporary / ("sheet-" + uuid.uuid4().hex[:8] + ".xlsx")
    if source.suffix.lower() in {".csv", ".tsv"}:
        content = read_text_file(source, warnings)
        delimiter = "\t" if source.suffix.lower() == ".tsv" else ","
        book = Workbook(write_only=True)
        sheet = book.create_sheet("数据")
        from openpyxl.cell import WriteOnlyCell
        for number, row in enumerate(csv.reader(io.StringIO(content), delimiter=delimiter), 1):
            _check(options)
            if number > 1_048_576 or len(row) > 16384:
                raise ValueError("文本表格超过 Excel 行数或列数上限。")
            cells = []
            for value in row:
                if len(value) > 32767:
                    raise ValueError("单个单元格超过 Excel 的 32767 字符上限。")
                cell = WriteOnlyCell(sheet, value=value)
                cell.data_type = "s"  # No formula evaluation and no loss of leading zeros.
                cells.append(cell)
            sheet.append(cells)
        book.save(target)
        book.close()
        warnings.append("CSV/TSV 按文本值导入，保留前导零；公式样式字符串不会执行。")
    else:
        _lo(source, target, "sheet", options)
    return target


def _sheet_dimensions(sheet, options, *, row_limit=1_048_576, column_limit=16384, cell_limit=None):
    """Write-only XLSX files legitimately omit the worksheet dimension tag."""
    if sheet.max_row is not None and sheet.max_column is not None:
        return sheet.max_row, sheet.max_column
    rows = columns = 0
    for rows, row in enumerate(sheet.iter_rows(), 1):
        _check(options)
        columns = max(columns, len(row))
        if rows > row_limit or columns > column_limit or cell_limit and rows * columns > cell_limit:
            break
    return rows, columns


def _sheet_convert(source, target, temporary, options, warnings):
    from openpyxl import load_workbook
    ext = target.suffix.lower()
    xlsx = _sheet_xlsx(source, temporary, options, warnings)
    if ext in {".xls", ".ods", ".pdf"}:
        if ext == ".xls":
            book = load_workbook(xlsx, read_only=True, data_only=False, keep_links=False)
            try:
                for sheet in book:
                    rows, columns = _sheet_dimensions(sheet, options, row_limit=65536, column_limit=256)
                    if rows > 65536 or columns > 256:
                        raise ValueError("源工作表超过旧 XLS 的 65536 行或 256 列限制，请选择 XLSX/ODS，避免截断数据。")
            finally:
                book.close()
            warnings.append("XLS 为旧版格式；新函数、图表和较新的表格特性可能不兼容，转换后请复核。")
        return _lo(xlsx, target, "sheet", options).get("changes", 1)
    if ext == ".xlsx":
        shutil.copyfile(xlsx, target)
        return 1
    book = load_workbook(xlsx, read_only=True, data_only=True, keep_links=False)
    formulas = load_workbook(xlsx, read_only=True, data_only=False, keep_links=False)
    try:
        selected = options.get("sheet")
        if selected and selected not in book.sheetnames:
            raise ValueError("未找到指定工作表：" + str(selected))
        sheets = [book[str(selected)]] if selected else list(book.worksheets)
        if ext in {".csv", ".tsv"}:
            sheet = sheets[0]
            if len(sheets) > 1:
                warnings.append(f"{ext.upper()[1:]} 只保存第一个工作表“{sheet.title}”；其余 {len(sheets) - 1} 个工作表未带入。需要完整工作簿请选择 XLSX 或 ODS。")
            with target.open("x", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream, delimiter="\t" if ext == ".tsv" else ",")
                formula_rows = formulas[sheet.title].iter_rows()
                count = 0
                for row in sheet.iter_rows():
                    _check(options)
                    original = next(formula_rows)
                    values = []
                    for cell, formula in zip(row, original):
                        value = cell.value
                        if formula.data_type == "f" and value is None:
                            value = "'" + str(formula.value)
                            warnings.append("部分公式没有已计算值，已作为带前导单引号的文本导出；请在表格软件重新计算后另存再转换以取得数值。")
                        elif isinstance(value, str) and value.startswith(("=", "+", "@")):
                            value = "'" + value
                            warnings.append("可能被表格软件当作公式的文本已加前导单引号，防止打开时执行。")
                        values.append(value)
                    writer.writerow(values)
                    count += 1
            return count
        body = []
        count = 0
        for sheet in sheets:
            rows, columns = _sheet_dimensions(sheet, options, cell_limit=1_000_000)
            if rows * columns > 1_000_000:
                raise ValueError("单张工作表超过 100 万个单元格，不适合导出为网页，请缩小范围后重试。")
            body.append("<h2>" + html.escape(sheet.title) + "</h2><table>")
            formula_rows = formulas[sheet.title].iter_rows()
            for row in sheet.iter_rows(values_only=True):
                _check(options)
                original = next(formula_rows)
                values = []
                for value, formula in zip(row, original):
                    if value is None and formula.data_type == "f":
                        value = formula.value
                        warnings.append("部分公式没有已计算值，HTML 中显示公式原文；请重新计算并保存工作簿后转换以取得数值。")
                    values.append(value)
                body.append("<tr>" + "".join("<td>" + html.escape("" if value is None else str(value)) + "</td>" for value in values) + "</tr>")
                count += 1
            body.append("</table>")
        warnings.append("HTML 保留各工作表的表格数值；不保留图表、公式计算与 Excel 交互功能。")
        target.write_text(_html_document(source.stem, "\n".join(body)), encoding="utf-8")
        return count
    finally:
        book.close()
        formulas.close()


def _slide_text(source, target, temporary, options, warnings):
    from pptx import Presentation
    from docx import Document
    pptx = source
    if source.suffix.lower() != ".pptx":
        pptx = temporary / "slides.pptx"
        _lo(source, pptx, "slide", options)
    _office_safety(pptx)
    presentation = Presentation(pptx)
    document = Document() if target.suffix.lower() == ".docx" else None
    lines = []

    def shapes_text(shapes):
        for shape in shapes:
            _check(options)
            if hasattr(shape, "shapes"):
                yield from shapes_text(shape.shapes)
            elif shape.has_table:
                for row in shape.table.rows:
                    yield "\t".join(cell.text for cell in row.cells)
            elif shape.has_text_frame and shape.text.strip():
                yield shape.text

    for number, slide in enumerate(presentation.slides, 1):
        _check(options)
        title = f"第 {number} 页"
        lines.append(title)
        if document:
            document.add_heading(title, 1)
        for text in shapes_text(slide.shapes):
            lines.append(text)
            if document:
                document.add_paragraph(text)
        lines.append("")
    if document:
        document.save(target)
    else:
        target.write_text("\n".join(lines), encoding="utf-8")
    warnings.append("已提取每页文字及表格；图表内部标签、图片、动画和讲稿备注不转为正文。")
    return len(presentation.slides)


def _pdf_text(source, target, options, warnings):
    from pypdf import PdfReader
    reader = PdfReader(source)
    try:
        if reader.is_encrypted and not reader.decrypt(options.get("password") or ""):
            raise ValueError("PDF 密码不正确或尚未填写。")
        if not 1 <= len(reader.pages) <= MAX_PAGES:
            raise ValueError(f"PDF 应有 1 至 {MAX_PAGES} 页。")
        with target.open("x", encoding="utf-8") as stream:
            for number, page in enumerate(reader.pages, 1):
                _check(options)
                text = page.extract_text() or ""
                if not text.strip():
                    warnings.append(f"第 {number} 页没有可提取文字；扫描页不进行 OCR。")
                stream.write(text.rstrip() + "\n\n")
        return len(reader.pages)
    finally:
        reader.close()


def _pdf_tables(source, target, options, warnings):
    import pdfplumber
    from openpyxl import Workbook
    book = Workbook() if target.suffix.lower() == ".xlsx" else None
    if book:
        book.remove(book.active)
    tables = []
    with pdfplumber.open(source, password=options.get("password") or None) as pdf:
        if not 1 <= len(pdf.pages) <= MAX_PAGES:
            raise ValueError(f"PDF 应有 1 至 {MAX_PAGES} 页。")
        for number, page in enumerate(pdf.pages, 1):
            _check(options)
            for index, rows in enumerate(page.extract_tables() or [], 1):
                _check(options)
                if not rows or not any(any(cell for cell in row) for row in rows):
                    continue
                tables.append((number, index, rows))
    if not tables:
        raise ValueError("未找到可提取的带文字表格。扫描图片不做 OCR；无边框或复杂表格请转 Word 后复核。")
    if book:
        for page, number, rows in tables:
            sheet = book.create_sheet(f"第{page}页_表{number}")
            for row in rows:
                _check(options)
                sheet.append(row)
                for cell in sheet[sheet.max_row]:
                    if isinstance(cell.value, str):
                        cell.data_type = "s"
        book.save(target)
        book.close()
    else:
        with target.open("x", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            maximum = max(len(row) for _, _, rows in tables for row in rows)
            writer.writerow(["来源页码", "表格序号", "表内行号"] + [f"列{i}" for i in range(1, maximum + 1)])
            for page, number, rows in tables:
                for row_number, row in enumerate(rows, 1):
                    _check(options)
                    safe = [("'" + value if isinstance(value, str) and value.startswith(("=", "+", "@")) else value) for value in row]
                    writer.writerow([page, number, row_number] + safe + [""] * (maximum - len(row)))
        warnings.append("CSV 已附带来源页码、表格序号和表内行号；不同表格顺序合并到同一文件。")
    warnings.append("PDF 表格提取依赖原文字层与表格线，合并单元格及复杂表头需要核对。")
    return len(tables)


def _pdf_images(source, temporary, options):
    dpi = _number(options.get("dpi", 144), "图片清晰度", 72, 300)
    rendered = []
    total = 0
    with conversion._open_pdf(source, options.get("password")) as pdf:
        if len(pdf) > MAX_PAGES:
            raise ValueError(f"PDF 超过 {MAX_PAGES} 页，请分批转换。")
        for index in range(len(pdf)):
            _check(options)
            page = pdf[index]
            try:
                width, height = page.get_size()
                total += int(width * height * (dpi / 72) ** 2)
                if total > MAX_RENDER_PIXELS:
                    raise ValueError("本批页面总像素超过 2.5 亿，请降低清晰度或拆分文件后转换。")
                path = temporary / f"page-{index + 1:04d}.png"
                conversion._render_page(page, path, dpi)
                rendered.append(path)
            finally:
                page.close()
    return rendered


def _images_document(images, target, options, warnings):
    from PIL import Image, ImageOps
    pictures = []
    total = 0
    with conversion._conversion_workspace(target.parent) as temporary:
        for source in images:
            _check(options)
            if source.suffix.lower() in {".heic", ".heif"}:
                try:
                    import pillow_heif
                    pillow_heif.register_heif_opener()
                except ImportError as exc:
                    raise ValueError("HEIC/HEIF 需要安装图片格式组件，请重新运行源码启动器完成自检。") from exc
            with Image.open(source) as original:
                frames = getattr(original, "n_frames", 1) if source.suffix.lower() in {".tif", ".tiff"} else 1
                if frames == 1 and getattr(original, "n_frames", 1) > 1:
                    warnings.append(f"{source.name} 为动图，文档只收录第一帧。")
                if len(pictures) + frames > 500:
                    raise ValueError("图片文档一次最多收录 500 页。")
                for index in range(frames):
                    _check(options)
                    original.seek(index)
                    total += original.width * original.height
                    if total > MAX_RENDER_PIXELS or original.width * original.height > 50_000_000:
                        raise ValueError("图片总像素超过 2.5 亿或单张超过 5000 万，请分批处理。")
                    fixed = ImageOps.exif_transpose(original)
                    try:
                        rgba = fixed.convert("RGBA")
                        try:
                            rgb = Image.new("RGB", rgba.size, "white")
                            rgb.paste(rgba, mask=rgba.getchannel("A"))
                            path = temporary / f"picture-{len(pictures) + 1:04d}.png"
                            rgb.save(path, "PNG")
                            pictures.append((path, rgb.size))
                            rgb.close()
                        finally:
                            rgba.close()
                    finally:
                        if fixed is not original:
                            fixed.close()
        if not pictures:
            raise ValueError("没有可以放入文档的图片。")
        ext = target.suffix.lower()
        if ext == ".pdf":
            from reportlab.pdfgen.canvas import Canvas
            from reportlab.lib.utils import ImageReader
            canvas = Canvas(str(target), pagesize=(595.276, 841.890))
            for picture, (width, height) in pictures:
                _check(options)
                page_w, page_h = (841.890, 595.276) if width > height else (595.276, 841.890)
                canvas.setPageSize((page_w, page_h))
                scale = min((page_w - 40) / width, (page_h - 40) / height)
                canvas.drawImage(ImageReader(str(picture)), (page_w - width * scale) / 2, (page_h - height * scale) / 2,
                                 width=width * scale, height=height * scale)
                canvas.showPage()
            canvas.save()
        elif ext == ".docx":
            from docx import Document
            document = Document()
            previous = None
            for index, (picture, (width, height)) in enumerate(pictures):
                _check(options)
                section = document.sections[0] if index == 0 else conversion._next_section(document, previous)
                page_w, page_h = (841.890, 595.276) if width > height else (595.276, 841.890)
                conversion._configure_section(section, page_w, page_h, 20)
                previous = conversion._add_page_image(document, picture, section)
            document.save(target)
        else:
            _pictures_pptx([picture for picture, _ in pictures], target, options)
            warnings.append("每页是一张图片，文字不会变为可编辑的 PPT 文本框。")
    return len(pictures)


def _pictures_pptx(images, target, options):
    from PIL import Image
    from pptx import Presentation
    from pptx.util import Inches
    presentation = Presentation()
    with Image.open(images[0]) as first:
        ratio = first.width / first.height
    presentation.slide_width = Inches(10)
    presentation.slide_height = Inches(max(1, min(56, 10 / ratio)))
    width, height = presentation.slide_width, presentation.slide_height
    for image in images:
        _check(options)
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        with Image.open(image) as picture:
            scale = min(width / picture.width, height / picture.height)
            item_width, item_height = int(picture.width * scale), int(picture.height * scale)
        slide.shapes.add_picture(str(image), (width - item_width) // 2, (height - item_height) // 2,
                                 width=item_width, height=item_height)
    presentation.save(target)


def _pdf_visual(source, target, temporary, options, warnings):
    from PIL import Image
    images = _pdf_images(source, temporary, options)
    ext = target.suffix.lower()
    outputs = []
    if ext == ".pptx":
        _pictures_pptx(images, target, options)
        warnings.append("PPT 每页为 PDF 页面图片；保持可视内容，文字不可逐字编辑。")
        outputs = [target]
    elif ext in {".tif", ".tiff"}:
        with contextlib.ExitStack() as stack:
            frames = [stack.enter_context(Image.open(path)) for path in images]
            frames[0].save(target, "TIFF", save_all=True, append_images=frames[1:], compression="tiff_deflate")
        outputs = [target]
    else:
        for number, path in enumerate(images, 1):
            _check(options)
            output = target if number == 1 else target.with_name(f"{target.stem}_{number:03d}{target.suffix}")
            if output.exists():
                raise FileExistsError("页面输出已存在：" + output.name)
            if ext == ".png":
                shutil.copyfile(path, output)
            else:
                with Image.open(path) as picture:
                    picture.save(output, "JPEG", quality=_number(options.get("quality", 92), "图片质量", 1, 100))
            outputs.append(output)
    return len(images), outputs


def convert(source: Path | str, target: Path, options: dict | None = None) -> dict:
    """Convert to new file(s); target suffix selects the documented direction.

    ``_cancel`` may be a threading.Event or a predicate. ``_sources`` contains
    the ordered image inputs for one combined PDF/Word/PPTX. All output files
    are complete before they are returned; the integration layer should use a
    private staging directory when publishing a multi-file job transactionally.
    """
    options = dict(options or {})
    _check(options)
    target = Path(target).expanduser().absolute()
    _ordinary_path(target)
    if os.path.lexists(target):
        raise FileExistsError("输出文件已存在，未覆盖：" + target.name)
    is_url = isinstance(source, str) and urlsplit(source).scheme.lower() in {"http", "https"}
    source_ext = "url" if is_url else Path(source).suffix.lower()
    if not supports(source_ext, target.suffix):
        raise ValueError(f"不支持此转换方向：{source_ext} → {target.suffix.lower()}")
    if not is_url:
        source = _source(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    warnings = []
    changes = 1
    with conversion._conversion_workspace(target.parent) as temporary:
        staged = temporary / ("result" + target.suffix.lower())
        produced = [staged]
        ext = target.suffix.lower()
        if source_ext == ext and source_ext in TEXT_TYPES:
            encoding = options.get("encoding", "utf-8")
            if encoding not in {"utf-8", "utf-8-sig", "utf-16", "gb18030"}:
                raise ValueError("输出编码仅支持 UTF-8、UTF-8 BOM、UTF-16 或 GB18030。")
            text = read_text_file(source, warnings)
            _check(options)
            staged.write_text(text, encoding=encoding, newline="")
            changes = len(text.splitlines())
            warnings.append(f"已按 {encoding.upper()} 保存；保留原文字、空白、换行和 Markdown 标记。")
        elif source_ext in IMAGE_TYPES:
            images = [_source(item) for item in options.get("_sources", [source])]
            if not images or any(image.suffix.lower() not in IMAGE_TYPES for image in images):
                raise ValueError("图片文档需要至少一个支持的图片文件。")
            changes = _images_document(images, staged, options, warnings)
        elif source_ext == ".pdf":
            if ext == ".docx":
                result = conversion.pdf_to_word(source, staged, options)
                changes = result.get("changes", 1)
                warnings.extend(result.get("warnings", []))
            elif ext == ".txt":
                changes = _pdf_text(source, staged, options, warnings)
            elif ext in {".xlsx", ".csv"}:
                changes = _pdf_tables(source, staged, options, warnings)
            else:
                changes, produced = _pdf_visual(source, staged, temporary, options, warnings)
        elif source_ext in SHEET_TYPES:
            changes = _sheet_convert(source, staged, temporary, options, warnings)
        elif source_ext in SLIDE_TYPES:
            if ext in {".docx", ".txt"}:
                changes = _slide_text(source, staged, temporary, options, warnings)
            elif ext in {".png", ".jpg", ".jpeg"}:
                pdf = temporary / "slides.pdf"
                _lo(source, pdf, "slide", options)
                changes, produced = _pdf_visual(pdf, staged, temporary, options, warnings)
            else:
                changes = _lo(source, staged, "slide", options).get("changes", 1)
        elif source_ext in WORD_TYPES and ext in WORD_TYPES | {".pdf"}:
            changes = _lo(source, staged, "word", options).get("changes", 1)
        else:
            if is_url or source_ext in WEB_TYPES:
                from .web_ops import webpage_to_word
                intermediate = staged if ext == ".docx" else temporary / "web.docx"
                result = webpage_to_word(source, intermediate, options)
                warnings.extend(result.get("warnings", []))
                changes = result.get("changes", 1)
                _check(options)
                if ext == ".pdf":
                    changes = _lo(intermediate, staged, "word", options).get("changes", changes)
                elif ext != ".docx":
                    content = _docx_html(intermediate, options, warnings)
                    _html_output(content, staged, options, warnings, temporary)
            else:
                if source_ext in WORD_TYPES:
                    docx = source
                    if source_ext != ".docx":
                        docx = temporary / "body.docx"
                        _lo(source, docx, "word", options)
                    content = _docx_html(docx, options, warnings)
                elif source_ext == ".epub":
                    content = _epub_html(source, options, warnings)
                else:
                    content = _text_html(source, options, warnings)
                _html_output(content, staged, options, warnings, temporary)
        _check(options)
        destinations = [target if number == 1 else target.with_name(f"{target.stem}_{number:03d}{target.suffix}") for number in range(1, len(produced) + 1)]
        for result, destination in zip(produced, destinations):
            if not result.is_file():
                raise RuntimeError("转换未生成完整结果。")
            if os.path.lexists(destination):
                raise FileExistsError("输出文件已存在，未覆盖：" + destination.name)
        outputs = [_publish(result, destination, options) for result, destination in zip(produced, destinations)]
    return {"changes": changes, "warnings": list(dict.fromkeys(warnings)), "outputs": outputs}
