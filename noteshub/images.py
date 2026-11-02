"""Bounded decoding of PNG/JPEG uploads; no OCR or network calls."""

import io
import warnings

from PIL import Image, UnidentifiedImageError


def validate_image(raw, settings):
    if not raw or len(raw) > settings.max_image_bytes:
        raise ValueError("图片不能为空，且不能超过 5 MiB。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in {"PNG", "JPEG"} or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("只支持单帧 PNG 或 JPEG 图片，请转换后重试。")
                width, height = image.size
                if min(width, height) < 1 or max(width, height) > settings.max_image_side or width * height > settings.max_image_pixels:
                    raise ValueError("图片不能超过 1200 万像素，且任一边不能超过 6000 像素。")
                fmt = image.format
                image.verify()
            # verify() alone does not decode JPEG scan data or all PNG pixels.
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
        return ("image/png", "png", width, height) if fmt == "PNG" else ("image/jpeg", "jpg", width, height)
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("图片损坏、格式不支持或尺寸过大，请重新选择 PNG/JPEG 图片。") from exc
