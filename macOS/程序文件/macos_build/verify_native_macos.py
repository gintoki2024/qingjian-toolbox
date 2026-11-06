"""Real, offline native conversion acceptance using the packaged Mac Python.

Each case gets a fresh subprocess. PDFium is never imported in the coordinator,
and no two cases run concurrently. Fixtures are small, fictional and generated
locally. This module does not prepare/download components or modify the bundle.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import zipfile


CASES = (
    "7zip_roundtrip", "7zip_encrypted_rejected",
    "ghostscript_ps_pdf", "ghostscript_eps_png", "ghostscript_ps_pages",
    "ofd_text_roundtrip", "ofd_pdf_roundtrip", "ofd_png_pages", "ofd_image_jpg",
)


def deny_network() -> None:
    def blocked(*args, **kwargs):
        raise RuntimeError("原生转换离线验收禁止 Python 网络连接和域名查询。")
    socket.create_connection = blocked
    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    socket.getaddrinfo = blocked


def _write_report(report: Path, value: dict) -> None:
    report.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".native-report-", suffix=".json",
                                         dir=report.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temporary, report)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _configure(project: Path) -> dict:
    if sys.platform != "darwin" or platform.system() != "Darwin":
        raise RuntimeError("必须在真实 macOS 中使用完整包内的 Python 验收。")
    runtime = (project / "Python").resolve()
    if (not sys.flags.isolated or not sys.flags.no_user_site
            or Path(sys.prefix).resolve() != runtime
            or not Path(sys.executable).resolve().is_relative_to(runtime)):
        raise RuntimeError("原生验收必须使用完整包的 Python/bin/python3 -I，不能借用系统或用户 site。")
    marker = json.loads((project / "macos-package.json").read_text(encoding="utf-8"))
    if marker.get("mode") != "offline" or marker.get("architecture") != platform.machine():
        raise RuntimeError("完整包标记或处理器架构不一致。")
    sys.path.insert(0, str(project))
    from startup_macos_native import check_native
    state = check_native(project, project, architecture=platform.machine())
    if not state.get("ready"):
        raise RuntimeError("包内原生组件校验失败：" + str(state.get("missing")))
    if not all(Path(value).resolve().is_relative_to(project) for value in state["components"].values()):
        raise RuntimeError("原生验收不得使用完整包外的组件。")
    os.environ.update(state["env"])
    from qingjian.core.format_media import engine
    paths = {name: engine(relative).resolve() for name, relative in (
        ("7zip", "7zip/7zz"), ("Ghostscript", "Ghostscript/bin/gs"),
        ("Java", "OFD/runtime/bin/java"), ("OFD_bridge", "OFD/bridge.jar"))}
    if not all(path.is_relative_to(project) for path in paths.values()):
        raise RuntimeError("实际调用的原生程序不在完整包内。")
    return {"executable": sys.executable, "executables": {key: str(path) for key, path in paths.items()},
            "relative_executables": {key: path.relative_to(project).as_posix() for key, path in paths.items()}}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_image(path: Path, *, expected_size=None) -> dict:
    from PIL import Image, ImageChops
    with Image.open(path) as image:
        image.load()
        if not (0 < image.width <= 10000 and 0 < image.height <= 10000):
            raise AssertionError("生成图片的尺寸无效。")
        if expected_size is not None and image.size != expected_size:
            raise AssertionError(f"生成图片尺寸不符：{image.size} != {expected_size}")
        rgba = image.convert("RGBA")
        rgb = Image.new("RGB", image.size, "white")
        rgb.paste(rgba, mask=rgba.getchannel("A"))
        if ImageChops.difference(rgb, Image.new("RGB", rgb.size, "white")).getbbox() is None:
            raise AssertionError("生成图片是空白页。")
        return {"name": path.name, "width": image.width, "height": image.height,
                "bytes": path.stat().st_size, "sha256": _sha(path)}


def _make_pdf(path: Path) -> None:
    from reportlab.pdfgen.canvas import Canvas
    canvas = Canvas(str(path), pagesize=(240, 180))
    for index, color in enumerate(((.85, .15, .1), (.1, .2, .85)), 1):
        canvas.setFillColorRGB(*color)
        canvas.rect(20, 20, 80, 40, fill=1, stroke=0)
        canvas.setFillColorRGB(0, 0, 0)
        canvas.setFont("Helvetica", 12)
        canvas.drawString(15, 140, "QINGJIAN_NATIVE_PAGE_" + str(index))
        canvas.showPage()
    canvas.save()


def _assert_pdf(path: Path, *, pages=2, text=None, colored_pages=False) -> dict:
    from pypdf import PdfReader
    reader = PdfReader(path)
    if reader.is_encrypted or len(reader.pages) != pages:
        raise AssertionError("生成 PDF 加密状态或页数不符合预期。")
    if text is not None and text not in "\n".join(page.extract_text() or "" for page in reader.pages):
        raise AssertionError("生成 PDF 缺少预期的虚构原文。")
    if colored_pages:
        # Import PDFium only inside this case's dedicated subprocess.
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(path) as document:
            for index in range(len(document)):
                page = document[index]
                bitmap = page.render(scale=1)
                try:
                    image = bitmap.to_pil().convert("RGB")
                    dominant = 0 if index == 0 else 2
                    colors = image.resize((80, 80)).getdata()
                    matching = sum(color[dominant] >= 150
                                   and max(color[channel] for channel in range(3) if channel != dominant) <= 110
                                   for color in colors)
                    if matching < 3:
                        raise AssertionError("OFD 往返 PDF 的页内颜色内容不符。")
                finally:
                    bitmap.close()
                    page.close()
    return {"name": path.name, "pages": len(reader.pages), "bytes": path.stat().st_size,
            "sha256": _sha(path), "content_checked": bool(text is not None or colored_pages)}


def _archive_case(name: str, folder: Path, cancel) -> dict:
    from qingjian.core import format_archive
    from qingjian.core.format_media import engine, run_engine
    payload = {"材料说明.txt": "青简虚构材料\nQINGJIAN_ARCHIVE_2026\n".encode(),
               "nested/data.csv": "项目,数量\n虚构项目,3\n".encode()}
    source = folder / "fictional.zip"
    with zipfile.ZipFile(source, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for key, value in payload.items():
            archive.writestr(key, value)
    original = _sha(source)
    options = {"_cancel": cancel.is_set}
    if name == "7zip_encrypted_rejected":
        content = folder / "fixture-content"
        content.mkdir()
        (content / "fictional.txt").write_text("Fictional protected archive", encoding="utf-8")
        encrypted = folder / "encrypted.7z"
        run_engine([engine("7zip/7zz"), "a", "-t7z", "-pQingjianFixtureOnly2026", "-mhe=off",
                    encrypted, "fictional.txt"], cancel.is_set, timeout=60, cwd=content)
        destination = folder / "must-not-exist"
        before = _sha(encrypted)
        try:
            format_archive.convert(encrypted, destination, {**options, "extract": True})
        except ValueError as exc:
            if "加密" not in str(exc):
                raise
        else:
            raise AssertionError("加密 7z 未按界面能力范围拒绝。")
        assert not destination.exists() and _sha(encrypted) == before
        return {"direction": "真实加密7z → 明确拒绝解压", "encrypted_archive_generated_by_packaged_7zip": True,
                "source_unchanged": True, "unpublished_result": True}
    seven, returned, extracted = folder / "converted.7z", folder / "returned.zip", folder / "extracted"
    format_archive.convert(source, seven, options)
    seven_before = _sha(seven)
    format_archive.convert(seven, returned, options)
    with zipfile.ZipFile(returned) as archive:
        files = {info.filename: archive.read(info) for info in archive.infolist() if not info.is_dir()}
        assert files == payload, "ZIP/7z 往返文件名或字节内容变化"
    format_archive.convert(seven, extracted, {**options, "extract": True})
    files = {path.relative_to(extracted).as_posix(): path.read_bytes() for path in extracted.rglob("*") if path.is_file()}
    assert files == payload and _sha(source) == original and _sha(seven) == seven_before
    return {"directions": ["ZIP→7z", "7z→ZIP", "7z→文件夹"], "files": len(payload),
            "exact_bytes_and_filenames": True, "source_unchanged": True}


def _ghostscript_case(name: str, folder: Path, cancel) -> dict:
    from qingjian.core.format_media import vector_convert
    eps = name == "ghostscript_eps_png"
    source = folder / ("fictional.eps" if eps else "fictional.ps")
    content = "%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 180 120\n" if eps else "%!PS-Adobe-3.0\n%%Pages: 2\n"
    content += "<< /PageSize [180 120] >> setpagedevice\n/Helvetica findfont 12 scalefont setfont\n"
    for index in range(1, 2 if eps else 3):
        content += ("0.15 0.35 0.7 setrgbcolor 20 20 60 30 rectfill\n0 0 0 setrgbcolor\n"
                    f"10 90 moveto (QINGJIAN_PS_PAGE_{index}) show showpage\n")
    source.write_text(content + "%%EOF\n", encoding="ascii")
    original = _sha(source)
    target = folder / ("result.pdf" if name == "ghostscript_ps_pdf" else "result.png")
    result = vector_convert(source, target, {"_cancel": cancel.is_set})
    if target.suffix == ".pdf":
        detail = _assert_pdf(target, text="QINGJIAN_PS_PAGE_1")
    else:
        outputs = [Path(value) for value in result["outputs"]]
        assert len(outputs) == (1 if eps else 2), "PostScript 多页未完整输出"
        detail = {"images": [_assert_image(path, expected_size=(360, 240)) for path in outputs]}
    assert _sha(source) == original
    return {"direction": ("EPS" if eps else "PS") + "→" + target.suffix[1:].upper(),
            "source_unchanged": True, **detail}


def _ofd_case(name: str, folder: Path, cancel) -> dict:
    from qingjian.core.format_ofd import convert, validate_ofd
    options = {"_cancel": cancel.is_set, "dpi": 72}
    document = folder / "fictional.ofd"
    if name == "ofd_text_roundtrip":
        source = folder / "fictional.txt"
        source.write_text("青简虚构材料\n材料核查 2026 年\nQINGJIAN_OFD_OFFLINE_123\n", encoding="utf-8")
        original = _sha(source)
        convert(source, document, options)
        validate_ofd(document, options)
        ofd_original = _sha(document)
        target = folder / "returned.txt"
        convert(document, target, options)
        value = target.read_text(encoding="utf-8")
        assert "青简虚构材料" in value and "QINGJIAN_OFD_OFFLINE_123" in value
        assert _sha(source) == original and _sha(document) == ofd_original
        return {"direction": "TXT→OFD→TXT", "chinese_and_ascii_text_preserved": True,
                "source_unchanged": True, "text_bytes": target.stat().st_size}
    if name == "ofd_image_jpg":
        from PIL import Image
        source = folder / "fictional.png"
        image = Image.new("RGB", (240, 160), "#2c6fb3")
        image.save(source)
        original = _sha(source)
        convert(source, document, options)
        validate_ofd(document, options)
        ofd_original = _sha(document)
        result = convert(document, folder / "returned.jpg", options)
        assert len(result["outputs"]) == 1
        target = Path(result["outputs"][0])
        detail = _assert_image(target)
        with Image.open(target) as image:
            colors = image.convert("RGB").resize((80, 80)).getdata()
            assert sum(blue > red + 30 and green > red for red, green, blue in colors) >= 3, "OFD/JPG 图像颜色内容未保留"
        assert _sha(source) == original and _sha(document) == ofd_original
        return {"direction": "PNG→OFD→JPG", "source_unchanged": True, "color_content_checked": True, **detail}
    source = folder / "fictional.pdf"
    _make_pdf(source)
    original = _sha(source)
    convert(source, document, options)
    validate_ofd(document, options)
    ofd_original = _sha(document)
    if name == "ofd_pdf_roundtrip":
        target = folder / "returned.pdf"
        convert(document, target, options)
        detail = _assert_pdf(target, colored_pages=True)
        direction = "PDF→OFD→PDF"
    else:
        result = convert(document, folder / "page.png", options)
        outputs = [Path(value) for value in result["outputs"]]
        assert len(outputs) == 2, "OFD 转 PNG 未输出两页"
        detail = {"images": [_assert_image(path) for path in outputs]}
        direction = "PDF→OFD→PNG"
    assert _sha(source) == original and _sha(document) == ofd_original
    return {"direction": direction, "source_unchanged": True, **detail}


def _worker(project: Path, report: Path, name: str) -> dict:
    result = {"name": name, "ok": False, "platform": sys.platform,
              "python_network_denied": True, "cancel_interface": "threading.Event.is_set"}
    cancel = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: cancel.set())
    started = time.monotonic()
    try:
        deny_network()
        result.update(_configure(project))
        with tempfile.TemporaryDirectory(prefix="fictional-native-", dir=report.parent) as temporary:
            folder = Path(temporary)
            if name.startswith("7zip_"):
                result["detail"] = _archive_case(name, folder, cancel)
            elif name.startswith("ghostscript_"):
                result["detail"] = _ghostscript_case(name, folder, cancel)
            else:
                result["detail"] = _ofd_case(name, folder, cancel)
        result["ok"] = True
    except Exception:
        result["error"] = traceback.format_exc()
    result["seconds"] = round(time.monotonic() - started, 2)
    _write_report(report, result)
    return result


def run(project: Path, report: Path) -> dict:
    """Return a real conversion report; non-Mac hosts are explicitly unsupported."""
    project, report = Path(project).resolve(), Path(report).absolute()
    result = {"passed": False, "unsupported": False, "platform": sys.platform,
              "architecture": platform.machine(), "executable": sys.executable, "checks": [],
              "python_network_denied": False, "pdfium_processes_separated": False}
    if sys.platform != "darwin" or platform.system() != "Darwin":
        result.update(unsupported=True, reason="当前系统不支持真实 Mac 原生转换验收，未执行或伪造转换结果。")
        _write_report(report, result)
        return result
    report.parent.mkdir(parents=True, exist_ok=True)
    try:
        deny_network()
        result.update(_configure(project))
        result.update(python_network_denied=True, pdfium_processes_separated=True)
        with tempfile.TemporaryDirectory(prefix="native-case-records-", dir=report.parent) as temporary:
            evidence = Path(temporary)
            for name in CASES:
                child_report = evidence / (name + ".json")
                environment = os.environ.copy()
                environment.update({"QINGJIAN_OFFLINE": "1", "PYTHONNOUSERSITE": "1"})
                command = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--project", str(project),
                           "--report", str(child_report), "--worker", name]
                child = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                         start_new_session=True)
                try:
                    stdout, stderr = child.communicate(timeout=240)
                except subprocess.TimeoutExpired:
                    # The worker's SIGTERM handler sets the same cancellation
                    # event passed to core converters, which reap their groups.
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(child.pid, signal.SIGTERM)
                    try:
                        stdout, stderr = child.communicate(timeout=20)
                    except subprocess.TimeoutExpired:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(child.pid, signal.SIGKILL)
                        stdout, stderr = child.communicate(timeout=10)
                    row = {"name": name, "ok": False, "error": "原生转换验收超时，已请求取消并结束测试进程。"}
                else:
                    row = json.loads(child_report.read_text(encoding="utf-8")) if child_report.is_file() else {
                        "name": name, "ok": False, "error": "原生验收子进程没有写入结果。"}
                row["returncode"] = child.returncode
                row["ok"] = row.get("ok") is True and child.returncode == 0
                if not row["ok"]:
                    row["stdout_tail"], row["stderr_tail"] = stdout[-4000:], stderr[-4000:]
                result["checks"].append(row)
                _write_report(report, result)
        result["passed"] = len(result["checks"]) == len(CASES) and all(row["ok"] for row in result["checks"])
    except Exception:
        result["error"] = traceback.format_exc()
    _write_report(report, result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--worker", choices=CASES)
    arguments = parser.parse_args()
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    if arguments.worker:
        outcome = _worker(arguments.project.resolve(), arguments.report.absolute(), arguments.worker)
        raise SystemExit(0 if outcome["ok"] else 1)
    outcome = run(arguments.project, arguments.report)
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    raise SystemExit(0 if outcome["passed"] else 2 if outcome["unsupported"] else 1)
