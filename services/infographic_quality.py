# -*- coding: utf-8 -*-
"""Production quality contracts for generated WB product images.

The image model is allowed to create a background.  It is not a source of
truth for the product or for text.  A publishable image therefore uses an
unchanged foreground layer and a deterministic text overlay.  Generative
edits remain review-only even when the provider returned a valid file.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import threading
import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageStat

WB_WIDTH = 900
WB_HEIGHT = 1200
WB_SIZE = (WB_WIDTH, WB_HEIGHT)

_rembg_session = None
_rembg_session_lock = threading.Lock()


class ImageQualityError(ValueError):
    """The input cannot safely be turned into a publishable product image."""


@dataclass(frozen=True)
class CompositeResult:
    image_bytes: bytes
    metadata: Dict[str, Any]
    protection_mask_bytes: Optional[bytes] = None


def _sha256(*parts: bytes) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part)
    return digest.hexdigest()


def _open_image(data: bytes, label: str) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        return image
    except Exception as exc:  # noqa: BLE001 - normalized domain error
        raise ImageQualityError(f"{label}: изображение не декодируется") from exc


def fit_background_to_canvas(
    background_bytes: bytes,
    size: Tuple[int, int] = WB_SIZE,
) -> Image.Image:
    """Cover-crop a generated background to the exact WB canvas."""
    background = _open_image(background_bytes, "Фон").convert("RGB")
    return ImageOps.fit(
        background,
        size,
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    ).convert("RGBA")


def canonicalize_image(
    image_bytes: bytes,
    size: Tuple[int, int] = WB_SIZE,
    *,
    image_format: str = "PNG",
) -> bytes:
    """Decode and cover-crop an arbitrary provider response to the contract."""
    image = _open_image(image_bytes, "Изображение").convert("RGB")
    if image.size != size:
        image = ImageOps.fit(
            image,
            size,
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
    output = io.BytesIO()
    image.save(output, format=image_format, optimize=image_format.upper() == "PNG")
    return output.getvalue()


def evaluate_background_text(
    background_bytes: bytes,
    *,
    ocr: Optional[Callable[[Image.Image], str]] = None,
) -> Dict[str, Any]:
    """Require an OCR-backed no-text check before unattended publication.

    If OCR is unavailable the result is deliberately inconclusive, not a pass.
    This prevents a negative prompt from being mistaken for a verified property.
    """
    image = _open_image(background_bytes, "Фон").convert("RGB")
    if ocr is None:
        try:
            import pytesseract
        except ImportError:
            return {
                "checked": False,
                "pass": None,
                "detected_text": "",
                "reason": "pytesseract недоступен",
            }
        if shutil.which("tesseract") is None:
            return {
                "checked": False,
                "pass": None,
                "detected_text": "",
                "reason": "tesseract недоступен",
            }
        ocr = lambda value: pytesseract.image_to_string(value, lang="eng+rus")
    try:
        raw_text = ocr(image) or ""
    except Exception as exc:  # noqa: BLE001
        return {
            "checked": False,
            "pass": None,
            "detected_text": "",
            "reason": f"OCR error: {exc}",
        }
    normalized = " ".join(str(raw_text).split()).strip()
    return {
        "checked": True,
        "pass": not bool(normalized),
        "detected_text": normalized[:300],
        "reason": "" if not normalized else "model background contains text-like glyphs",
    }


def _default_cutout(product_bytes: bytes) -> bytes:
    global _rembg_session
    try:
        from rembg import new_session, remove
    except ImportError as exc:
        raise ImageQualityError(
            "Не установлен rembg: безопасный foreground-композит недоступен"
        ) from exc
    if _rembg_session is None:
        with _rembg_session_lock:
            if _rembg_session is None:
                model = os.environ.get("INFOGRAPHIC_REMBG_MODEL", "u2net")
                _rembg_session = new_session(model)
    return remove(product_bytes, session=_rembg_session)


def extract_foreground(
    product_bytes: bytes,
    cutout: Optional[Callable[[bytes], Any]] = None,
) -> Tuple[Image.Image, Dict[str, Any]]:
    """Extract and tightly crop a foreground, failing closed on bad mattes."""
    source_image = _open_image(product_bytes, "Исходное фото")
    source = source_image.convert("RGBA")
    source_alpha = source.getchannel("A")
    has_meaningful_source_alpha = source_alpha.getextrema()[0] < 250
    if cutout is None and has_meaningful_source_alpha:
        removed = source.copy()
        mask_assurance = "source_alpha"
    else:
        remover = cutout or _default_cutout
        try:
            removed = remover(product_bytes)
        except ImageQualityError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ImageQualityError("Не удалось отделить товар от фона") from exc
        mask_assurance = "automated_unreviewed"

    if isinstance(removed, Image.Image):
        foreground = removed.convert("RGBA")
    elif isinstance(removed, (bytes, bytearray)):
        foreground = _open_image(bytes(removed), "Foreground").convert("RGBA")
    else:
        raise ImageQualityError("Foreground extractor вернул неподдерживаемый тип")

    if foreground.size != source.size:
        raise ImageQualityError("Foreground extractor изменил размер исходного фото")
    # rembg отвечает только за alpha matte. RGB всегда берём из исходника, чтобы
    # сегментатор физически не мог отретушировать товар, лицо, принт или цвет.
    foreground = Image.merge(
        "RGBA",
        (*source.convert("RGB").split(), foreground.getchannel("A")),
    )
    alpha = foreground.getchannel("A")
    bbox = alpha.getbbox()
    if not bbox:
        raise ImageQualityError("Foreground пустой")
    full_histogram = alpha.histogram()
    full_alpha_sum = sum(
        value * count for value, count in enumerate(full_histogram)
    )
    canvas_coverage = full_alpha_sum / max(255 * source.width * source.height, 1)
    if canvas_coverage > 0.98:
        raise ImageQualityError(
            "Foreground mask почти полностью непрозрачна: фон, вероятно, не удалён"
        )
    foreground = foreground.crop(bbox)
    alpha = foreground.getchannel("A")

    histogram = alpha.histogram()
    alpha_sum = sum(value * count for value, count in enumerate(histogram))
    coverage = alpha_sum / max(255 * foreground.width * foreground.height, 1)
    if coverage < 0.03:
        raise ImageQualityError("Foreground mask слишком разрежена")

    rgba = foreground.tobytes()
    metadata = {
        "source_sha256": _sha256(product_bytes),
        "source_size": list(source.size),
        "source_mode": source.mode,
        "rgb_source": "original_bytes_decoded_no_retouch",
        "cutout_bbox": list(bbox),
        "cutout_size": list(foreground.size),
        "alpha_coverage": round(coverage, 4),
        "source_canvas_alpha_coverage": round(canvas_coverage, 4),
        # RGB preservation is cryptographically auditable, but an automated
        # alpha matte can still cut off an edge.  Do not conflate these two
        # guarantees: the matte remains review-only until separately approved.
        "mask_assurance": mask_assurance,
        "cutout_sha256": _sha256(
            f"{foreground.width}x{foreground.height}".encode("ascii"), rgba
        ),
    }
    return foreground, metadata


def _scaled_foreground(
    foreground: Image.Image,
    canvas_size: Tuple[int, int],
    *,
    top_reserved_ratio: float,
    bottom_safe_ratio: float,
    max_width_ratio: float,
) -> Tuple[Image.Image, int, int]:
    width, height = canvas_size
    available_height = height * (1.0 - top_reserved_ratio - bottom_safe_ratio)
    max_width = width * max_width_ratio
    scale = min(
        max_width / max(foreground.width, 1),
        available_height / max(foreground.height, 1),
    )
    if not 0 < scale < 20:
        raise ImageQualityError("Некорректный масштаб foreground")
    target = (
        max(1, round(foreground.width * scale)),
        max(1, round(foreground.height * scale)),
    )
    resized = foreground.resize(target, Image.Resampling.LANCZOS)
    x = (width - resized.width) // 2
    y = height - round(height * bottom_safe_ratio) - resized.height
    if x < 0 or y < round(height * top_reserved_ratio):
        raise ImageQualityError("Foreground не помещается в безопасную область")
    return resized, x, y


def _add_product_shadows(
    canvas: Image.Image,
    foreground: Image.Image,
    x: int,
    y: int,
) -> Image.Image:
    """Add shadows behind the foreground without changing its pixels."""
    width, height = canvas.size
    alpha = foreground.getchannel("A")

    drop_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    drop_mask = Image.new("L", canvas.size, 0)
    reduced_alpha = alpha.point(lambda value: round(value * 0.22))
    drop_mask.paste(reduced_alpha, (x + 9, y + 14))
    drop_mask = drop_mask.filter(ImageFilter.GaussianBlur(18))
    drop_layer.paste((0, 0, 0, 255), (0, 0, width, height), drop_mask)
    canvas = Image.alpha_composite(canvas, drop_layer)

    contact_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    contact_mask = Image.new("L", canvas.size, 0)
    draw = ImageDraw.Draw(contact_mask)
    contact_width = max(24, round(foreground.width * 0.58))
    contact_height = max(8, min(28, round(foreground.height * 0.025)))
    center_x = x + foreground.width // 2
    bottom = min(height - 1, y + foreground.height + 5)
    draw.ellipse(
        (
            center_x - contact_width // 2,
            bottom - contact_height // 2,
            center_x + contact_width // 2,
            bottom + contact_height // 2,
        ),
        fill=110,
    )
    contact_mask = contact_mask.filter(ImageFilter.GaussianBlur(10))
    contact_layer.paste((0, 0, 0, 255), (0, 0, width, height), contact_mask)
    return Image.alpha_composite(canvas, contact_layer)


def _encode_png(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _protection_mask(mask: Image.Image) -> bytes:
    """Encode an edit mask: transparent background, protected foreground.

    A small dilation protects antialiased product edges from provider edits.
    The original foreground is still restored locally after generation.
    """
    expanded = mask.filter(ImageFilter.MaxFilter(7))
    rgba = Image.new("RGBA", mask.size, (255, 255, 255, 0))
    rgba.putalpha(expanded)
    return _encode_png(rgba)


def compose_identity_preserving(
    background_bytes: bytes,
    product_bytes: bytes,
    *,
    cutout: Optional[Callable[[bytes], Any]] = None,
    size: Tuple[int, int] = WB_SIZE,
    top_reserved_ratio: float = 0.22,
    bottom_safe_ratio: float = 0.055,
    max_width_ratio: float = 0.8,
    mask_verified: Optional[bool] = None,
) -> CompositeResult:
    """Build a final scene while never sending the product through a model."""
    if not 0 <= top_reserved_ratio < 0.7:
        raise ImageQualityError("Некорректная верхняя safe-zone")
    if not 0 <= bottom_safe_ratio < 0.3:
        raise ImageQualityError("Некорректная нижняя safe-zone")
    if not 0.2 <= max_width_ratio <= 0.95:
        raise ImageQualityError("Некорректная допустимая ширина товара")

    canvas = fit_background_to_canvas(background_bytes, size)
    foreground, metadata = extract_foreground(product_bytes, cutout=cutout)
    foreground, x, y = _scaled_foreground(
        foreground,
        size,
        top_reserved_ratio=top_reserved_ratio,
        bottom_safe_ratio=bottom_safe_ratio,
        max_width_ratio=max_width_ratio,
    )
    rendered_foreground_sha256 = _sha256(
        f"{foreground.width}x{foreground.height}".encode("ascii"),
        foreground.tobytes(),
    )
    canvas = _add_product_shadows(canvas, foreground, x, y)
    canvas.alpha_composite(foreground, (x, y))

    mask = Image.new("L", size, 0)
    mask.paste(foreground.getchannel("A"), (x, y), foreground.getchannel("A"))

    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="PNG", optimize=True)
    result_bytes = output.getvalue()
    resolved_mask_verified = (
        metadata.get("mask_assurance") == "source_alpha"
        if mask_verified is None
        else bool(mask_verified)
    )
    metadata.update({
        "identity_mode": "pixel_preserved_composite",
        "mask_verified": resolved_mask_verified,
        "allowed_foreground_transforms": ["alpha_extract", "lanczos_resize", "translate"],
        "rendered_foreground_sha256": rendered_foreground_sha256,
        "rendered_foreground_size": list(foreground.size),
        "placement": {"x": x, "y": y},
        "canvas_size": list(size),
        "output_sha256": _sha256(result_bytes),
    })
    return CompositeResult(result_bytes, metadata, _protection_mask(mask))


def compose_multi_identity_preserving(
    background_bytes: bytes,
    product_images: Sequence[bytes],
    *,
    cutout: Optional[Callable[[bytes], Any]] = None,
    size: Tuple[int, int] = WB_SIZE,
    top_reserved_ratio: float = 0.16,
    bottom_safe_ratio: float = 0.04,
) -> CompositeResult:
    """Arrange several untouched product cutouts on one generated background.

    Every foreground RGB channel comes from its corresponding source image;
    only alpha extraction, resize and translation are allowed. The same layout
    can be used both for a reference canvas and for the final local restore.
    """
    sources = list(product_images)
    if not 2 <= len(sources) <= 10:
        raise ImageQualityError("Общий макет поддерживает от 2 до 10 исходных фото")
    if not 0 <= top_reserved_ratio < 0.5 or not 0 <= bottom_safe_ratio < 0.2:
        raise ImageQualityError("Некорректная safe-zone общего макета")

    canvas = fit_background_to_canvas(background_bytes, size)
    protection = Image.new("L", size, 0)
    foregrounds = [extract_foreground(value, cutout=cutout) for value in sources]
    count = len(foregrounds)
    columns = 2 if count <= 4 else 3
    rows = math.ceil(count / columns)
    width, height = size
    content_top = round(height * top_reserved_ratio)
    content_bottom = height - round(height * bottom_safe_ratio)
    cell_width = width / columns
    cell_height = (content_bottom - content_top) / rows
    placements = []
    rendered_hashes = []
    source_metadata = []

    last_row_count = count - columns * (rows - 1)
    for index, (foreground, metadata) in enumerate(foregrounds):
        row = index // columns
        column = index % columns
        row_items = last_row_count if row == rows - 1 else columns
        row_offset = (columns - row_items) * cell_width / 2
        max_width = cell_width * 0.78
        max_height = cell_height * 0.78
        scale = min(
            max_width / max(foreground.width, 1),
            max_height / max(foreground.height, 1),
        )
        if not 0 < scale < 20:
            raise ImageQualityError("Некорректный масштаб фото в общем макете")
        target = (
            max(1, round(foreground.width * scale)),
            max(1, round(foreground.height * scale)),
        )
        rendered = foreground.resize(target, Image.Resampling.LANCZOS)
        cell_left = row_offset + column * cell_width
        x = round(cell_left + (cell_width - rendered.width) / 2)
        y = round(content_top + row * cell_height + (cell_height - rendered.height) / 2)
        if x < 0 or y < content_top or x + rendered.width > width or y + rendered.height > content_bottom:
            raise ImageQualityError("Фото не помещается в общий макет")
        rendered_hash = _sha256(
            f"{rendered.width}x{rendered.height}".encode("ascii"),
            rendered.tobytes(),
        )
        canvas = _add_product_shadows(canvas, rendered, x, y)
        canvas.alpha_composite(rendered, (x, y))
        protection.paste(
            rendered.getchannel("A"),
            (x, y),
            rendered.getchannel("A"),
        )
        rendered_hashes.append(rendered_hash)
        placements.append({"x": x, "y": y, "width": rendered.width, "height": rendered.height})
        source_metadata.append({
            **metadata,
            "rendered_foreground_sha256": rendered_hash,
            "rendered_foreground_size": list(rendered.size),
            "placement": placements[-1],
        })

    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="PNG", optimize=True)
    result_bytes = output.getvalue()
    combined_cutout_hash = _sha256(*[
        item["cutout_sha256"].encode("ascii") for item in source_metadata
    ])
    combined_rendered_hash = _sha256(*[
        value.encode("ascii") for value in rendered_hashes
    ])
    metadata = {
        "identity_mode": "pixel_preserved_composite",
        "composition_mode": "collage",
        "rgb_source": "original_bytes_decoded_no_retouch",
        "source_count": count,
        "sources": source_metadata,
        "cutout_sha256": combined_cutout_hash,
        "rendered_foreground_sha256": combined_rendered_hash,
        "mask_verified": all(
            item.get("mask_assurance") == "source_alpha" for item in source_metadata
        ),
        "allowed_foreground_transforms": ["alpha_extract", "lanczos_resize", "translate"],
        "placements": placements,
        "canvas_size": list(size),
        "output_sha256": _sha256(result_bytes),
    }
    return CompositeResult(result_bytes, metadata, _protection_mask(protection))


def apply_watermark(
    image_bytes: bytes,
    watermark_bytes: bytes,
    *,
    position: str = "top_right",
    scale_percent: int = 18,
    opacity_percent: int = 80,
    margin: int = 24,
) -> CompositeResult:
    """Apply a deterministic PNG watermark without involving an image model."""
    positions = {"top_left", "top_right", "bottom_left", "bottom_right", "center"}
    if position not in positions:
        raise ImageQualityError("Неизвестная позиция водяного знака")
    if isinstance(scale_percent, bool) or not isinstance(scale_percent, int):
        raise ImageQualityError("Размер водяного знака должен быть целым числом")
    if isinstance(opacity_percent, bool) or not isinstance(opacity_percent, int):
        raise ImageQualityError("Прозрачность водяного знака должна быть целым числом")
    if not 5 <= scale_percent <= 40:
        raise ImageQualityError("Размер водяного знака должен быть от 5% до 40%")
    if not 20 <= opacity_percent <= 100:
        raise ImageQualityError("Прозрачность водяного знака должна быть от 20% до 100%")

    canvas = _open_image(image_bytes, "Финальное изображение").convert("RGBA")
    watermark = _open_image(watermark_bytes, "Водяной знак").convert("RGBA")
    if watermark.width < 16 or watermark.height < 16:
        raise ImageQualityError("Водяной знак слишком маленький")
    alpha = watermark.getchannel("A")
    if alpha.getbbox() is None:
        raise ImageQualityError("Водяной знак полностью прозрачный")
    target_width = max(1, round(canvas.width * scale_percent / 100))
    scale = target_width / watermark.width
    target_height = max(1, round(watermark.height * scale))
    max_height = round(canvas.height * 0.35)
    if target_height > max_height:
        target_height = max_height
        target_width = max(1, round(watermark.width * target_height / watermark.height))
    rendered = watermark.resize((target_width, target_height), Image.Resampling.LANCZOS)
    rendered_alpha = rendered.getchannel("A").point(
        lambda value: round(value * opacity_percent / 100)
    )
    rendered.putalpha(rendered_alpha)

    coordinates = {
        "top_left": (margin, margin),
        "top_right": (canvas.width - margin - rendered.width, margin),
        "bottom_left": (margin, canvas.height - margin - rendered.height),
        "bottom_right": (
            canvas.width - margin - rendered.width,
            canvas.height - margin - rendered.height,
        ),
        "center": (
            (canvas.width - rendered.width) // 2,
            (canvas.height - rendered.height) // 2,
        ),
    }
    x, y = coordinates[position]
    if x < 0 or y < 0:
        raise ImageQualityError("Водяной знак не помещается на изображении")
    canvas.alpha_composite(rendered, (x, y))
    result_bytes = _encode_png(canvas.convert("RGB"))
    metadata = {
        "mode": "deterministic_png_overlay",
        "source_sha256": _sha256(watermark_bytes),
        "rendered_sha256": _sha256(
            f"{rendered.width}x{rendered.height}".encode("ascii"),
            rendered.tobytes(),
        ),
        "source_size": list(watermark.size),
        "rendered_size": list(rendered.size),
        "placement": {"x": x, "y": y},
        "position": position,
        "scale_percent": scale_percent,
        "opacity_percent": opacity_percent,
        "output_sha256": _sha256(result_bytes),
    }
    return CompositeResult(result_bytes, metadata)


def _font(size: int, *, bold: bool = False):
    from PIL import ImageFont

    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int, max_lines: int):
    words = text.split()
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
        if len(lines) == max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and words:
        consumed = len(" ".join(lines).split())
        if consumed < len(words):
            last = lines[-1]
            while last and draw.textbbox((0, 0), last + "…", font=font)[2] > max_width:
                last = last[:-1]
            lines[-1] = last.rstrip() + "…"
    return lines


def apply_text_overlay(
    image_bytes: bytes,
    *,
    title: str,
    subtitle: str = "",
) -> CompositeResult:
    """Render exact UTF-8 copy locally in the reserved top safe-zone."""
    clean_title = " ".join((title or "").split()).strip()
    clean_subtitle = " ".join((subtitle or "").split()).strip()
    if not clean_title:
        raise ImageQualityError("Заголовок инфографики пустой")
    if len(clean_title) > 120 or len(clean_subtitle) > 240:
        raise ImageQualityError("Текст инфографики превышает допустимую длину")

    canvas = _open_image(image_bytes, "Финальное изображение").convert("RGBA")
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    panel_height = 286 if clean_subtitle else 226
    draw.rounded_rectangle(
        (24, 22, canvas.width - 24, panel_height),
        radius=28,
        fill=(8, 12, 20, 190),
        outline=(255, 255, 255, 40),
        width=2,
    )
    title_font = _font(48, bold=True)
    subtitle_font = _font(27)
    title_lines = _wrap_text(draw, clean_title, title_font, canvas.width - 112, 2)
    y = 54
    for line in title_lines:
        draw.text((56, y), line, font=title_font, fill=(255, 255, 255, 255))
        y += 58
    rendered_texts = [clean_title]
    if clean_subtitle:
        subtitle_lines = _wrap_text(
            draw, clean_subtitle, subtitle_font, canvas.width - 112, 2
        )
        y = max(y + 10, 176)
        for line in subtitle_lines:
            draw.text((56, y), line, font=subtitle_font, fill=(232, 235, 240, 255))
            y += 37
        rendered_texts.append(clean_subtitle)
    result = Image.alpha_composite(canvas, overlay)
    result_bytes = _encode_png(result.convert("RGB"))
    return CompositeResult(result_bytes, {
        "mode": "deterministic_text_overlay",
        "rendered_texts": rendered_texts,
        "panel": {"x": 24, "y": 22, "width": canvas.width - 48, "height": panel_height - 22},
        "output_sha256": _sha256(result_bytes),
    })


def _as_text_list(values: Optional[Iterable[Any]]) -> Sequence[str]:
    if values is None:
        return []
    return [value for value in values if isinstance(value, str)]


def evaluate_final_image(
    image_bytes: bytes,
    *,
    identity_mode: str,
    text_mode: str = "none",
    expected_texts: Optional[Iterable[str]] = None,
    rendered_texts: Optional[Iterable[str]] = None,
    claims_pass: bool = True,
    background_text_check: Optional[Dict[str, Any]] = None,
    background_scene_check: Optional[Dict[str, Any]] = None,
    composite_metadata: Optional[Dict[str, Any]] = None,
    expected_size: Tuple[int, int] = WB_SIZE,
) -> Dict[str, Any]:
    """Evaluate hard publication gates without pretending to be a vision judge."""
    checks: Dict[str, Dict[str, Any]] = {}
    try:
        image = _open_image(image_bytes, "Финальное изображение").convert("RGB")
        dimensions_pass = image.size == expected_size
        entropy = round(image.entropy(), 3)
        mean_luma = round(ImageStat.Stat(image.convert("L")).mean[0], 2)
        visual_signal_pass = entropy >= 2.0 and 3.0 <= mean_luma <= 252.0
        checks["decode"] = {"pass": True}
        checks["dimensions"] = {
            "pass": dimensions_pass,
            "actual": list(image.size),
            "expected": list(expected_size),
        }
        checks["visual_signal"] = {
            "pass": visual_signal_pass,
            "entropy": entropy,
            "mean_luma": mean_luma,
        }
    except ImageQualityError as exc:
        checks["decode"] = {"pass": False, "reason": str(exc)}
        dimensions_pass = False
        visual_signal_pass = False

    metadata = composite_metadata or {}
    if identity_mode == "pixel_preserved_composite":
        identity_chain_pass = bool(
            metadata.get("cutout_sha256")
            and metadata.get("rendered_foreground_sha256")
            and metadata.get("identity_mode") == identity_mode
        )
        if not identity_chain_pass:
            identity_pass = False
            identity_reason = "foreground provenance metadata is incomplete"
        elif metadata.get("mask_verified") is True:
            identity_pass = True
            identity_reason = "foreground bypassed the model and mask is verified"
        else:
            identity_pass = None
            identity_reason = (
                "foreground RGB bypassed the model; automated mask requires review"
            )
    elif identity_mode == "generative_edit":
        identity_pass = None
        identity_reason = "generative edit requires human identity review"
    else:
        identity_pass = False
        identity_reason = "unknown identity contract"
    checks["identity"] = {"pass": identity_pass, "reason": identity_reason}

    expected = list(_as_text_list(expected_texts))
    rendered = list(_as_text_list(rendered_texts))
    if text_mode == "none":
        text_pass = not expected and not rendered
    elif text_mode == "deterministic_overlay":
        text_pass = expected == rendered
    else:
        text_pass = False
    checks["text"] = {
        "pass": text_pass,
        "mode": text_mode,
        "expected": expected,
        "rendered": rendered,
    }
    checks["claims"] = {"pass": bool(claims_pass)}
    background_check = background_text_check or {
        "checked": identity_mode != "pixel_preserved_composite",
        "pass": True if identity_mode != "pixel_preserved_composite" else None,
        "reason": (
            "not applicable to generative-edit review"
            if identity_mode != "pixel_preserved_composite"
            else "background text was not checked"
        ),
    }
    background_checked = bool(background_check.get("checked"))
    background_text_pass = background_check.get("pass") is True
    checks["background_text"] = dict(background_check)
    scene_check = background_scene_check or {
        "checked": identity_mode != "pixel_preserved_composite",
        "pass": True if identity_mode != "pixel_preserved_composite" else None,
        "reason": (
            "not applicable to generative-edit review"
            if identity_mode != "pixel_preserved_composite"
            else "background scene was not verified"
        ),
    }
    scene_checked = bool(scene_check.get("checked"))
    scene_pass = scene_check.get("pass") is True
    checks["background_scene"] = dict(scene_check)

    technical_pass = bool(dimensions_pass and visual_signal_pass)
    hard_failure = (
        not technical_pass
        or not text_pass
        or not claims_pass
        or identity_pass is False
        or (background_checked and not background_text_pass)
        or (scene_checked and not scene_pass)
    )
    if hard_failure:
        status = "rejected"
        publishable = False
    elif identity_pass is None or not background_checked or not scene_checked:
        status = "review_required"
        publishable = False
    else:
        status = "auto_pass"
        publishable = True

    return {
        "status": status,
        "publishable": publishable,
        "technical_pass": technical_pass,
        "identity_pass": identity_pass,
        "text_pass": text_pass,
        "claims_pass": bool(claims_pass),
        "checks": checks,
        "output_sha256": _sha256(image_bytes),
    }
