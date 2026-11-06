"""Offline OFD conversions through the bundled Java runtime and OFDRW bridge."""
from __future__ import annotations

import os
import posixpath
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from .common import ensure_cancel
from .conversion import _conversion_workspace
from .format_media import engine, image_convert, run_engine, IMAGES
from .text_ops import _ordinary_path, read_text_file

MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_XML_BYTES = 16 * 1024 * 1024
MAX_PAGES = 1000
_PATH_ELEMENTS = {"DocRoot", "PublicRes", "DocumentRes", "Annotations", "Attachments", "Signatures",
                  "FontFile", "MediaFile", "Thumbnail", "FileLoc", "PageAnnot", "Signature", "SignedValue"}
_PATH_ATTRIBUTES = {"BaseLoc", "FileLoc", "FontFile", "MediaFile"}


def targets_for(extension):
    extension = str(extension).lower()
    if not extension.startswith("."):
        extension = "." + extension
    if extension == ".ofd":
        return frozenset({".pdf", ".txt", ".png", ".jpg", ".jpeg"})
    if extension in IMAGES | {".pdf", ".txt"}:
        return frozenset({".ofd"})
    return frozenset()


def _check(options):
    cancel = options.get("_cancel")
    ensure_cancel(cancel.is_set if hasattr(cancel, "is_set") else cancel)


def _reference(reference, member):
    value = unquote(reference.strip()).replace("\\", "/")
    if not value:
        return
    if "\x00" in value or ":" in value or value.startswith("//"):
        raise ValueError("OFD 包含外部资源地址，未读取该资源。")
    # OFD's leading slash refers to the archive root, not the machine root.
    resolved = posixpath.normpath(value.lstrip("/")) if value.startswith("/") else posixpath.normpath(posixpath.join(posixpath.dirname(member), value))
    if resolved == ".." or resolved.startswith("../"):
        raise ValueError("OFD 内部资源路径越界，已停止转换。")


def validate_ofd(source, options=None):
    """Validate archive paths, bounded XML and references before Java opens it."""
    options = options or {}
    signed = False
    try:
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > 30000 or sum(item.file_size for item in entries) > MAX_ARCHIVE_BYTES:
                raise ValueError("OFD 展开超过 512 MB 或 30000 个部件，请先拆分。")
            seen = set()
            if not any(item.filename == "OFD.xml" for item in entries):
                raise ValueError("不是有效的 OFD 文件：缺少 OFD.xml。")
            for item in entries:
                _check(options)
                path = PurePosixPath(item.filename)
                raw_parts = item.filename.rstrip("/").split("/")
                if (path.is_absolute() or any(part in {"", ".", ".."} for part in raw_parts)
                        or ".." in path.parts or "\\" in item.orig_filename or "\x00" in item.orig_filename
                        or ":" in item.filename or item.filename.casefold() in seen
                        or stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1
                        or any(part.endswith((" ", ".")) or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
                               for part in path.parts)):
                    raise ValueError("OFD 内部路径不安全、重复、包含链接或文件已加密。")
                seen.add(item.filename.casefold())
                if item.file_size > MAX_MEMBER_BYTES:
                    raise ValueError("OFD 单个内部部件超过 64 MB，请先拆分。")
                signed = signed or bool(re.search(r"(?:^|/)(?:signs|signatures?)(?:/|\.)", item.filename, re.I))
                if not item.filename.lower().endswith(".xml"):
                    continue
                if item.file_size > MAX_XML_BYTES:
                    raise ValueError("OFD XML 部件超过 16 MB。")
                data = archive.read(item)
                # Strip NUL code-unit padding for the same check on UTF-16/32.
                if re.search(br"<!\s*(?:DOCTYPE|ENTITY)", data.replace(b"\x00", b""), re.I):
                    raise ValueError("不读取包含 DTD 或外部实体的 OFD。")
                root = ET.fromstring(data)
                page_count = sum(1 for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "Page")
                if page_count > MAX_PAGES:
                    raise ValueError("OFD 页数超过 1000 页，请先拆分。")
                for node in root.iter():
                    name = node.tag.rsplit("}", 1)[-1]
                    if name in _PATH_ELEMENTS and node.text and not list(node):
                        _reference(node.text, item.filename)
                    for key, value in node.attrib.items():
                        if key.rsplit("}", 1)[-1] in _PATH_ATTRIBUTES:
                            _reference(value, item.filename)
    except (zipfile.BadZipFile, ET.ParseError, RuntimeError) as exc:
        raise ValueError("OFD 内部文件损坏或无法读取。") from exc
    return {"signed": signed}


def _java(mode, source, target, temporary, options):
    java = engine("OFD/runtime/bin/java.exe")
    bridge = engine("OFD/bridge.jar")
    lib = bridge.parent / "lib" / "*"
    javatemp = temporary / "java-temp"
    javatemp.mkdir(exist_ok=True)
    cancel = options.get("_cancel")
    cancel = cancel.is_set if hasattr(cancel, "is_set") else cancel
    result = run_engine([java, "-Xmx512m", "-Djava.awt.headless=true", "-Dfile.encoding=UTF-8",
                         "-Djava.io.tmpdir=" + str(javatemp), "-Duser.home=" + str(javatemp),
                         "-Djavax.xml.accessExternalDTD=", "-Djavax.xml.accessExternalSchema=",
                         "-cp", str(bridge) + os.pathsep + str(lib), "local.qingjian.OFDBridge",
                         mode, source, target], cancel, timeout=300, cwd=temporary)
    if "QINGJIAN_OFD_OK" not in result or not target.is_file():
        raise RuntimeError("OFD 转换引擎没有生成完整结果。")


def _publish(staged, target, options):
    _check(options)
    # Exclusive creation protects both old results and the input even when an
    # unrelated application creates a file while conversion is in progress.
    created = False
    try:
        with staged.open("rb") as source, target.open("xb") as output:
            created = True
            for block in iter(lambda: source.read(1024 * 1024), b""):
                _check(options)
                output.write(block)
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise
    return str(target)


def convert(source, target, options=None):
    options = dict(options or {})
    if hasattr(options.get("_cancel"), "is_set"):
        options["_cancel"] = options["_cancel"].is_set
    _check(options)
    source, target = Path(source).expanduser().absolute(), Path(target).expanduser().absolute()
    _ordinary_path(source)
    _ordinary_path(target)
    if not source.is_file():
        raise FileNotFoundError("找不到输入文件：" + source.name)
    if source.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("单个输入文件超过 512 MB，请先拆分。")
    if os.path.lexists(target):
        raise FileExistsError("输出文件已存在，未覆盖：" + target.name)
    ext, output = source.suffix.lower(), target.suffix.lower()
    if output not in targets_for(ext):
        raise ValueError("不支持此 OFD 转换方向：" + ext + " → " + output)
    target.parent.mkdir(parents=True, exist_ok=True)
    warnings = ["转换生成新的归档副本，不保留原电子签名效力；复杂图形和字体可能变化。"]
    changes = 1
    if ext == ".ofd":
        validate_ofd(source, options)
    elif ext == ".pdf":
        from pypdf import PdfReader
        with PdfReader(source) as reader:
            if reader.is_encrypted:
                raise ValueError("PDF 已加密，请先用合法密码另存为未加密副本。")
            if not 0 < len(reader.pages) <= MAX_PAGES:
                raise ValueError("PDF 没有页面或超过 1000 页，请先拆分。")
    with _conversion_workspace(target.parent) as temporary:
        staged = temporary / ("result" + output)
        produced = [staged]
        if ext == ".ofd":
            if output in {".pdf", ".txt"}:
                _java("ofd-" + output[1:], source, staged, temporary, options)
                if output == ".txt":
                    warnings.append("仅提取 OFD 中已有的文字，未进行 OCR；图片页和字形轮廓页可能没有文本。")
                    if not staged.read_text(encoding="utf-8", errors="replace").strip():
                        warnings.append("此 OFD 未提取到可复制文字。")
            else:
                from .format_office import convert as office_convert
                pdf = temporary / "visual.pdf"
                _java("ofd-pdf", source, pdf, temporary, options)
                visual = office_convert(pdf, staged, options)
                produced = [Path(item) for item in visual["outputs"]]
                changes = visual.get("changes", len(produced))
                warnings.extend(visual.get("warnings", []))
        elif ext == ".pdf":
            _java("pdf-ofd", source, staged, temporary, options)
            warnings.append("PDF 附件与交互功能不复制到 OFD。")
        elif ext == ".txt":
            normalized = temporary / "text-utf8.txt"
            text = read_text_file(source, warnings)
            normalized.write_text(text, encoding="utf-8")
            _java("txt-ofd", normalized, staged, temporary, options)
        else:
            normalized = temporary / "image.png"
            image_result = image_convert(source, normalized, options)
            warnings.extend(image_result.get("warnings", []))
            _java("image-ofd", normalized, staged, temporary, options)
        _check(options)
        destinations = [target if number == 1 else target.with_name(f"{target.stem}_{number:03d}{target.suffix}")
                        for number in range(1, len(produced) + 1)]
        for file, destination in zip(produced, destinations):
            if not file.is_file() or os.path.lexists(destination):
                raise FileExistsError("结果不完整或输出已存在，未覆盖：" + destination.name)
        outputs = [_publish(file, destination, options) for file, destination in zip(produced, destinations)]
    return {"changes": changes, "outputs": outputs, "warnings": list(dict.fromkeys(warnings))}
