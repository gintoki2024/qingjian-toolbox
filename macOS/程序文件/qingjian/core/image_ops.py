"""Batch-friendly image cropping with no overwrite and no EXIF leakage."""

from __future__ import annotations

import math
import os
import tempfile
import warnings as python_warnings
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


_FORMATS = {"PNG", "JPEG", "WEBP", "BMP", "TIFF"}
_ANCHORS = {"center", "top-left", "top", "bottom"}


def _number(value, name: str, minimum=None, maximum=None) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name}必须为数字。")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name}必须为数字。") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name}必须为有限数字。")
    if minimum is not None and result < minimum:
        raise ValueError(f"{name}不能小于 {minimum}。")
    if maximum is not None and result > maximum:
        raise ValueError(f"{name}不能大于 {maximum}。")
    return result


def _integer(value, name: str, minimum=1, maximum=None) -> int:
    number = _number(value, name, minimum, maximum)
    if not number.is_integer():
        raise ValueError(f"{name}必须为整数。")
    return int(number)


def _ratio_box(size: tuple[int, int], ratio: float, anchor: str):
    width, height = size
    if width / height > ratio:
        crop_width, crop_height = max(1, min(width, round(height * ratio))), height
    else:
        crop_width, crop_height = width, max(1, min(height, round(width / ratio)))
    x = 0 if anchor == "top-left" else (width - crop_width) // 2
    y = (height - crop_height) // 2
    if anchor in {"top-left", "top"}:
        y = 0
    elif anchor == "bottom":
        y = height - crop_height
    return x, y, x + crop_width, y + crop_height


def _publish(temp: Path, target: Path):
    """Publish without replacing a file created by another process."""
    if os.name == "nt":
        os.rename(temp, target)  # Windows rename refuses an existing destination.
    else:
        os.link(temp, target)
        temp.unlink()


def crop_image(source: Path, target: Path, options: dict) -> dict:
    """Crop an image, remove EXIF, and save atomically.

    ``size`` first crops to the requested aspect ratio and then scales down to
    fit the requested pixel size. It never enlarges a small source; in that case
    the returned warning gives the actual (smaller) output dimensions.
    Animated WebP/APNG and multipage TIFF are reduced to their first frame.
    """
    source, target = Path(source), Path(target)
    if not source.is_file():
        raise FileNotFoundError(f"找不到图片：{source.name}")
    if source.resolve() == target.resolve():
        raise ValueError("输出文件不能与原图片相同。")
    if target.exists():
        raise FileExistsError(f"输出文件已存在，已保留：{target.name}")

    mode = options.get("mode", "margins")
    if mode not in {"margins", "ratio", "size"}:
        raise ValueError("裁剪模式必须为 margins、ratio 或 size。")
    anchor = options.get("anchor", "center")
    if anchor not in _ANCHORS:
        raise ValueError("裁剪位置必须为 center、top-left、top 或 bottom。")
    output_format = str(options.get("format", "original")).lower()
    if output_format not in {"original", "png", "jpg"}:
        raise ValueError("输出格式必须为 original、png 或 jpg。")
    quality = _integer(options.get("quality", 92), "图片质量", 1, 100)
    margins = None
    desired_size = None
    ratio = None
    if mode == "margins":
        margins = [_number(options.get(key, 0), label, 0, 49.9)
                   for key, label in (("left", "左边比例"), ("top", "上边比例"),
                                      ("right", "右边比例"), ("bottom", "下边比例"))]
    elif mode == "ratio":
        ratio_w = _number(options.get("ratio_w", 1), "宽比例")
        ratio_h = _number(options.get("ratio_h", 1), "高比例")
        if ratio_w <= 0 or ratio_h <= 0:
            raise ValueError("宽、高比例必须大于 0。")
        ratio = ratio_w / ratio_h
        if not math.isfinite(ratio) or ratio <= 0:
            raise ValueError("宽高比例超出可处理范围。")
    else:
        desired_size = (_integer(options.get("width", 800), "目标宽度", 1, 100000),
                        _integer(options.get("height", 800), "目标高度", 1, 100000))
        ratio = desired_size[0] / desired_size[1]

    notices: list[str] = []
    try:
        with python_warnings.catch_warnings():
            python_warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as original:
                actual_format = original.format
                if actual_format not in _FORMATS:
                    raise ValueError("不支持该图片格式，请使用 PNG、JPG、WebP、BMP 或 TIFF。")
                if original.width * original.height > 80_000_000:
                    raise ValueError("图片超过 8000 万像素，请先缩小后再处理。")
                if getattr(original, "n_frames", 1) > 1:
                    notices.append("原图片包含多帧或多页，本次仅处理第一帧。")
                original.seek(0)
                image = ImageOps.exif_transpose(original)
                image.load()
                if "A" in image.getbands() or "transparency" in image.info:
                    image = image.convert("RGBA")
                else:
                    image = image.copy()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise ValueError("无法读取图片，文件可能损坏、尺寸过大或格式不受支持。") from exc

    width, height = image.size
    if margins is not None:
        left, top, right, bottom = margins
        box = (round(width * left / 100), round(height * top / 100),
               width - round(width * right / 100), height - round(height * bottom / 100))
    else:
        box = _ratio_box(image.size, ratio, anchor)
    if not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height):
        raise ValueError("裁剪区域为空或超出原图，请减小裁剪比例。")
    result = image.crop(box)
    if desired_size:
        scale = min(1.0, desired_size[0] / result.width, desired_size[1] / result.height)
        resized = (max(1, round(result.width * scale)), max(1, round(result.height * scale)))
        if resized != result.size:
            result = result.resize(resized, Image.Resampling.LANCZOS)
        if result.width < desired_size[0] or result.height < desired_size[1]:
            notices.append(f"原图较小，已禁止放大；实际输出 {result.width}×{result.height} 像素。")

    save_format = actual_format if output_format == "original" else {"png": "PNG", "jpg": "JPEG"}[output_format]
    if save_format == "JPEG":
        if "A" in result.getbands():
            background = Image.new("RGB", result.size, "white")
            background.paste(result, mask=result.getchannel("A"))
            result = background
        elif result.mode != "RGB":
            result = result.convert("RGB")
    elif save_format in {"PNG", "WEBP", "BMP"} and result.mode not in {"1", "L", "LA", "P", "RGB", "RGBA"}:
        result = result.convert("RGB")
    result.info.clear()  # Do not copy camera EXIF, GPS or source metadata.
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".qingjian-image-", suffix=target.suffix, dir=target.parent)
    os.close(handle)
    temp = Path(temporary)
    try:
        save_options = {"quality": quality} if save_format in {"JPEG", "WEBP"} else {}
        if save_format == "JPEG":
            save_options["optimize"] = True
        result.save(temp, format=save_format, **save_options)
        _publish(temp, target)
    finally:
        if temp.exists():
            temp.unlink()
        result.close()
        image.close()
    return {"changes": 1, "warnings": notices}
