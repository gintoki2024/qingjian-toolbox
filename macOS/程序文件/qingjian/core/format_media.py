"""Small CPU-only image/vector converters and bundled executable discovery."""
from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .common import ensure_cancel

IMAGES = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff', '.gif', '.ico', '.avif', '.jp2', '.j2k', '.heic', '.heif'}
IMAGE_OUTPUTS = {'.jpg', '.png', '.webp', '.bmp', '.tiff', '.gif', '.ico', '.avif', '.jp2'}


def support_root():
    candidates = [Path(__file__).resolve().parents[2] / 'support', Path(sys.executable).resolve().parent / 'support']
    if getattr(sys, '_MEIPASS', None):
        candidates.append(Path(sys._MEIPASS) / 'support')
    return next((p for p in candidates if p.is_dir()), candidates[0])


def _engine_relatives(relative):
    """Keep the component-root contract stable across native payloads."""
    if sys.platform == 'darwin':
        return {
            '7zip/7z.exe': ('7zip/7zz', '7zip/7z'),
            'Ghostscript/bin/gswin64c.exe': ('Ghostscript/bin/gs',),
            'OFD/runtime/bin/java.exe': ('OFD/runtime/bin/java', 'OFD/runtime/Contents/Home/bin/java'),
        }.get(str(relative).replace('\\', '/'), (relative,))
    return (relative,)


def engine(relative):
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or '..' in parts:
        raise RuntimeError('离线转换组件路径无效。')
    root = support_root() / parts[0]
    # Source startup supplies only complete, hash-checked components. The
    # portable build always uses its own payload and ignores outside overrides.
    if not getattr(sys, 'frozen', False):
        try:
            configured = json.loads(os.environ.get('QINGJIAN_NATIVE_COMPONENTS', '{}'))
            selected = configured.get(parts[0]) if isinstance(configured, dict) else None
            if isinstance(selected, str) and Path(selected).is_absolute():
                root = Path(selected)
        except ValueError:
            pass
    for candidate in _engine_relatives(relative):
        target = root.joinpath(*Path(candidate).parts[1:])
        if target.is_file():
            if sys.platform == 'darwin' and target.suffix != '.jar' and not os.access(target, os.X_OK):
                raise RuntimeError('转换组件没有执行权限，请重新运行源码启动器检查：' + str(target))
            return target
    raise RuntimeError('缺少离线转换组件，请重新运行源码启动器自动补齐，或重新解压完整运行包：' + str(relative))


def run_engine(args, cancel=None, timeout=180, cwd=None):
    """Run an explicit bundled executable without shell or inherited DLL paths."""
    from .conversion import _attach_windows_job, _resume_windows_process, _close_windows_job, _terminate_process_tree
    args = [str(a) for a in args]
    env = os.environ.copy()
    for key in ('JAVA_TOOL_OPTIONS', '_JAVA_OPTIONS', 'JDK_JAVA_OPTIONS', 'CLASSPATH'):
        env.pop(key, None)
    if os.name == 'nt':
        windows = Path(os.environ.get('WINDIR', 'C:/Windows'))
        system_paths = [str(windows / 'System32'), str(windows)]
    else:
        system_paths = ['/usr/bin', '/bin', '/usr/sbin', '/sbin']
        # Never let external library injection select another conversion
        # runtime. The selected component keeps its own relative rpaths.
        for key in ('DYLD_LIBRARY_PATH', 'DYLD_INSERT_LIBRARIES', 'LD_LIBRARY_PATH', 'LD_PRELOAD'):
            env.pop(key, None)
    env['PATH'] = os.pathsep.join([str(Path(args[0]).parent), *system_paths])
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004) if os.name == 'nt' else 0
    process = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags,
                               start_new_session=os.name != 'nt')
    job = None
    try:
        job = _attach_windows_job(process)
        if os.name == 'nt':
            if job is None:
                raise RuntimeError('无法隔离转换进程，请检查运行权限。')
            _resume_windows_process(process)
        deadline = time.monotonic() + timeout
        while True:
            ensure_cancel(cancel)
            if time.monotonic() > deadline:
                raise TimeoutError('转换超时，已停止本次任务。')
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            detail = (stderr or stdout).decode('utf-8', errors='replace')[-1500:]
            raise RuntimeError('转换引擎处理失败：' + detail)
        return stdout.decode('utf-8', errors='replace')
    except BaseException:
        _terminate_process_tree(process)
        raise
    finally:
        _close_windows_job(job)


def _ghostscript_resource_options(executable):
    """Point the privately extracted MacTeX engine at its own resources.

    GS_LIB locates initialization scripts but does not relocate Ghostscript's
    compiled-in Resource and ICC defaults. Do not read a system installation's
    resources or silently use another installed version.
    """
    if sys.platform != 'darwin':
        return []
    versions = Path(executable).parent.parent / 'share/ghostscript'
    try:
        candidates = [base for base in versions.iterdir()
                      if base.is_dir() and not base.is_symlink()
                      and (base / 'Resource/Init/gs_init.ps').is_file()
                      and (base / 'iccprofiles/default_rgb.icc').is_file()]
    except OSError as exc:
        raise RuntimeError('Ghostscript 本地资源不完整，请重新运行源码启动器检查。') from exc
    if len(candidates) != 1:
        raise RuntimeError('Ghostscript 本地资源缺失或版本不唯一，请重新运行源码启动器检查。')
    base = candidates[0]
    return ['-sGenericResourceDir=' + str(base / 'Resource') + '/',
            '-sICCProfilesDir=' + str(base / 'iccprofiles') + '/']


def image_convert(source, target, options=None):
    """Convert every page/frame; static outputs return one file per frame."""
    from PIL import Image, ImageOps, ImageColor
    from .format_office import _number
    import pillow_heif
    pillow_heif.register_heif_opener()
    options = dict(options or {})
    source, target = Path(source), Path(target)
    if os.path.lexists(target):
        raise FileExistsError('输出图片已存在，未覆盖：' + target.name)
    quality = _number(options.get('quality', 92), '图片质量', 1, 100)
    max_edge = _number(options.get('max_edge', 0), '图片最长边', 0, 30000)
    try:
        background_color = ImageColor.getcolor(options.get('jpeg_background', '#ffffff'), 'RGB')
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError('JPG 背景色无效，请填写 #ffffff 等颜色值。') from exc
    cancel = options.get('_cancel')
    cancel = cancel.is_set if hasattr(cancel, 'is_set') else cancel
    ensure_cancel(cancel)
    frames, durations, warnings, outputs = [], [], [], []
    output = target.suffix.lower()
    if output not in IMAGE_OUTPUTS | {'.jpeg', '.tif'}:
        raise ValueError('不支持此图片输出格式。')
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(source) as image:
            count = getattr(image, 'n_frames', 1)
            if count > 1000:
                raise ValueError('多帧图片超过 1000 帧，请先拆分。')
            keep_frames = output in {'.tif', '.tiff', '.gif', '.webp'}
            destinations = [target if index == 0 else target.with_name(f'{target.stem}_{index + 1:03d}{target.suffix}')
                            for index in range(1 if keep_frames else count)]
            for destination in destinations:
                if os.path.lexists(destination):
                    raise FileExistsError('输出图片已存在，未覆盖：' + destination.name)
            total_pixels = 0
            for index in range(count):
                ensure_cancel(cancel)
                image.seek(index)
                frame = ImageOps.exif_transpose(image).convert('RGBA')
                if max_edge:
                    frame.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                total_pixels += frame.width * frame.height
                if total_pixels > 250_000_000:
                    frame.close()
                    raise ValueError('图片总像素超过 2.5 亿，请缩小最长边或拆分后转换。')
                if output in {'.jpg', '.jpeg', '.bmp', '.jp2'}:
                    background = Image.new('RGB', frame.size, background_color)
                    background.paste(frame, mask=frame.getchannel('A'))
                    frame.close()
                    frame = background
                frames.append(frame)
                durations.append(image.info.get('duration', 100))
            if count > 1 and not keep_frames:
                warnings.append(f'原文件含 {count} 帧/页，已逐帧分别输出 {count} 张静态图片，未丢弃后续内容。')
            settings = {}
            if output in {'.jpg', '.jpeg', '.webp', '.avif'}:
                settings['quality'] = quality
            if output in {'.jpg', '.jpeg'}:
                settings['subsampling'] = 0
            if len(frames) > 1 and keep_frames:
                settings.update(save_all=True, append_images=frames[1:])
                if output in {'.gif', '.webp'}:
                    settings.update(duration=durations, loop=image.info.get('loop', 0))
            if output == '.ico':
                settings['sizes'] = [(16,16), (32,32), (48,48), (64,64), (128,128), (256,256)]
                warnings.append('ICO 按标准图标尺寸生成，最大 256×256。')
            for index, destination in enumerate(destinations):
                ensure_cancel(cancel)
                # Exclusive creation never replaces an existing output.
                with destination.open('x+b') as stream:
                    outputs.append(destination)
                    frames[0 if keep_frames else index].save(
                        stream, format=Image.registered_extensions()[output], **settings)
            ensure_cancel(cancel)
    except BaseException:
        for path in outputs:
            path.unlink(missing_ok=True)
        raise
    finally:
        for frame in frames:
            frame.close()
    return {'changes': count, 'outputs': [str(path) for path in outputs], 'warnings': warnings}


def vector_convert(source, target, options=None):
    options = options or {}
    cancel = options.get('_cancel')
    ext, out = source.suffix.lower(), target.suffix.lower()
    ensure_cancel(cancel)
    if ext in {'.ps', '.eps'}:
        gs = engine('Ghostscript/bin/gswin64c.exe')
        device = 'pdfwrite' if out == '.pdf' else 'pngalpha'
        native_target = target if out == '.pdf' else target.with_name(target.stem + '.render-%04d.png')
        run_engine([gs, *_ghostscript_resource_options(gs), '-dSAFER', '-dBATCH', '-dNOPAUSE', '-dEPSCrop', '-r144', '-sDEVICE=' + device,
                    '-sOutputFile=' + str(native_target), source], cancel)
        if out == '.pdf':
            outputs = [str(target)]
        else:
            rendered = sorted(target.parent.glob(target.stem + '.render-*.png'))
            if not rendered:
                raise ValueError('转换引擎未生成图片。')
            outputs = []
            for index, page in enumerate(rendered, 1):
                ensure_cancel(cancel)
                destination = target if index == 1 else target.with_name(f'{target.stem}_{index:03d}{target.suffix}')
                if out == '.png':
                    page.replace(destination)
                else:
                    image_convert(page, destination, options)
                outputs.append(str(destination))
        return {'changes': len(outputs), 'outputs': outputs, 'warnings': []}
    if ext == '.svgz':
        with gzip.open(source, 'rb') as stream:
            data = stream.read(32 * 1024 * 1024 + 1)
    else:
        data = source.read_bytes()
    if len(data) > 32 * 1024 * 1024:
        raise ValueError('SVG 内容过大，请先简化图形。')
    import re
    if re.search(br'<!\s*(?:DOCTYPE|ENTITY)', data, re.I):
        raise ValueError('不读取带外部实体的 SVG。')
    # Remote references are never fetched; local file references are likewise
    # disallowed to keep a dropped drawing from reading unrelated documents.
    from xml.etree import ElementTree as ET
    root = ET.fromstring(data)
    for node in root.iter():
        for key, value in node.attrib.items():
            if key.rsplit('}', 1)[-1] == 'href' and not value.startswith(('#', 'data:')):
                raise ValueError('SVG 引用了外部资源，请先嵌入图片再转换。')
    if out == '.svg':
        target.write_bytes(data)
    elif out == '.svgz':
        target.write_bytes(gzip.compress(data, mtime=0))
    else:
        from PySide6.QtCore import QByteArray, QRectF
        from PySide6.QtGui import QImage, QPainter, QPdfWriter, QPageSize
        from PySide6.QtSvg import QSvgRenderer
        renderer = QSvgRenderer(QByteArray(data))
        if not renderer.isValid():
            raise ValueError('无法读取此 SVG 图形。')
        size = renderer.defaultSize()
        if size.width() <= 0 or size.height() <= 0 or size.width() * size.height() > 64000000:
            raise ValueError('SVG 尺寸无效或超过 6400 万像素。')
        if out == '.pdf':
            from PySide6.QtCore import QSizeF
            writer = QPdfWriter(str(target))
            writer.setResolution(96)
            writer.setPageSize(QPageSize(QSizeF(size.width() * 25.4 / 96, size.height() * 25.4 / 96), QPageSize.Unit.Millimeter))
            from PySide6.QtCore import QMarginsF
            writer.setPageMargins(QMarginsF(0, 0, 0, 0))
            painter = QPainter(writer)
            try:
                renderer.render(painter, QRectF(0, 0, writer.width(), writer.height()))
            finally:
                painter.end()
        else:
            image = QImage(size, QImage.Format.Format_ARGB32)
            image.fill(0)
            painter = QPainter(image)
            try:
                renderer.render(painter)
            finally:
                painter.end()
            intermediate = target if out == '.png' else target.with_suffix('.render.png')
            if not image.save(str(intermediate), 'PNG'):
                raise ValueError('无法写入图像。')
            if intermediate != target:
                image_convert(intermediate, target, options)
    ensure_cancel(cancel)
    return {'changes': 1, 'outputs': [str(target)], 'warnings': []}
