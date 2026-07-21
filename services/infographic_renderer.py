# -*- coding: utf-8 -*-
"""
Infographic Renderer — рендеринг инфографики из HTML-шаблонов через Playwright.

Берёт JSON rich_content (слайды с текстами) + фото товара →
рендерит красивые PNG 900x1200 (3:4) для WB.

Бесплатно, стабильно, полный контроль над дизайном.
"""

import base64
import html
import io
import json
import logging
import os
import tempfile
import re
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

# Размеры для WB Rich-контента (соотношение 3:4, рекомендуемое WB)
WB_WIDTH = 900
WB_HEIGHT = 1200

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _safe_color(value, fallback):
    return value if isinstance(value, str) and _HEX_COLOR_RE.fullmatch(value) else fallback

# Автоматический поиск Chromium
_CHROMIUM_PATH = None


def _find_chromium() -> Optional[str]:
    """Находит установленный Chromium для Playwright"""
    global _CHROMIUM_PATH
    if _CHROMIUM_PATH:
        return _CHROMIUM_PATH

    import glob
    # Стандартные пути Playwright
    search_paths = [
        os.path.expanduser('~/.cache/ms-playwright/chromium-*/chrome-linux/chrome'),
        os.path.expanduser('~/.cache/ms-playwright/chromium_headless_shell-*/chrome-linux/headless_shell'),
        '/usr/bin/chromium-browser',
        '/usr/bin/chromium',
        '/usr/bin/google-chrome',
        '/usr/bin/google-chrome-stable',
    ]
    for pattern in search_paths:
        matches = sorted(glob.glob(pattern), reverse=True)
        for match in matches:
            if os.path.isfile(match) and os.access(match, os.X_OK):
                _CHROMIUM_PATH = match
                logger.info(f"Found Chromium at: {match}")
                return match
    return None


def _get_slide_bg_gradient(slide_type: str, color_palette: List[str] = None) -> str:
    """Возвращает CSS-градиент фона в зависимости от типа слайда"""
    palette = color_palette or []
    primary = _safe_color(palette[0], '#6366f1') if len(palette) > 0 else '#6366f1'
    accent = _safe_color(palette[1], '#8b5cf6') if len(palette) > 1 else '#8b5cf6'
    bg_color = _safe_color(palette[2], '#f8fafc') if len(palette) > 2 else '#f8fafc'

    gradients = {
        'hero': f'linear-gradient(135deg, {primary} 0%, {accent} 100%)',
        'problem': f'linear-gradient(135deg, #1e293b 0%, #334155 100%)',
        'advantages': f'linear-gradient(135deg, {bg_color} 0%, #ffffff 100%)',
        'characteristics': f'linear-gradient(135deg, #f1f5f9 0%, #e2e8f0 100%)',
        'application': f'linear-gradient(135deg, {primary}15 0%, {accent}15 100%)',
        'bundling': f'linear-gradient(135deg, #fefce8 0%, #fef3c7 100%)',
        'trust': f'linear-gradient(135deg, #ecfdf5 0%, #d1fae5 100%)',
    }
    return gradients.get(slide_type, f'linear-gradient(135deg, {bg_color} 0%, #ffffff 100%)')


def _is_dark_bg(slide_type: str) -> bool:
    """Определяет, нужен ли светлый текст (тёмный фон)"""
    return slide_type in ('hero', 'problem')


def _build_bullets_html(bullets: List[str], is_dark: bool) -> str:
    """Генерирует HTML для буллетов"""
    if not bullets:
        return ''
    text_color = '#ffffff' if is_dark else '#1e293b'
    items = ''.join(
        f'<li style="margin-bottom:10px;padding-left:10px;position:relative;">'
        f'<span style="position:absolute;left:-18px;color:{("#a78bfa" if is_dark else "#6366f1")};">&#10003;</span>'
        f'{b}</li>'
        for b in bullets[:5]
    )
    return f'''
    <ul style="list-style:none;padding:0;margin:20px 0 0 22px;font-size:20px;
               line-height:1.5;color:{text_color};font-weight:500;">
        {items}
    </ul>'''


def _build_slide_html(
    slide: Dict,
    design: Dict,
    product_photo_b64: Optional[str] = None,
    slide_index: int = 0
) -> str:
    """Строит HTML для одного слайда инфографики"""
    slide_type = slide.get('type', 'hero')
    title = html.escape(str(slide.get('title', '')))
    subtitle = html.escape(str(slide.get('subtitle', '')))
    bullets = [html.escape(str(value)) for value in (slide.get('bullets') or [])]
    color_palette = design.get('color_palette', [])
    font_style = design.get('font_style', 'modern')

    is_dark = _is_dark_bg(slide_type)
    bg = _get_slide_bg_gradient(slide_type, color_palette)

    title_color = '#ffffff' if is_dark else '#1e293b'
    subtitle_color = '#e2e8f0' if is_dark else '#64748b'
    primary = _safe_color(color_palette[0], '#6366f1') if color_palette else '#6366f1'

    font_family = {
        'modern': "'Inter', 'Segoe UI', system-ui, sans-serif",
        'classic': "'Georgia', 'Times New Roman', serif",
        'bold': "'Impact', 'Arial Black', sans-serif",
        'elegant': "'Playfair Display', 'Georgia', serif",
    }.get(font_style, "'Inter', 'Segoe UI', system-ui, sans-serif")

    accent = _safe_color(color_palette[1], '#8b5cf6') if len(color_palette) > 1 else '#8b5cf6'

    # Фото товара — верхняя половина на hero, или вставка на других слайдах
    photo_html = ''
    has_photo = bool(product_photo_b64)
    if has_photo and slide_type in ('hero', 'application', 'bundling', 'characteristics'):
        if slide_type == 'hero':
            photo_html = f'''
            <div style="position:absolute;top:0;left:0;right:0;height:580px;overflow:hidden;">
                <img src="data:image/jpeg;base64,{product_photo_b64}"
                     style="width:100%;height:100%;object-fit:cover;" />
                <div style="position:absolute;bottom:0;left:0;right:0;height:120px;
                            background:linear-gradient(transparent, {bg.split(',')[0].replace('linear-gradient(135deg', '').strip() if 'linear-gradient' in bg else '#1e293b'});"></div>
            </div>'''
        else:
            photo_html = f'''
            <div style="position:absolute;top:40px;right:40px;
                        width:260px;height:260px;border-radius:20px;overflow:hidden;
                        box-shadow:0 12px 24px rgba(0,0,0,0.15);">
                <img src="data:image/jpeg;base64,{product_photo_b64}"
                     style="width:100%;height:100%;object-fit:cover;" />
            </div>'''

    # Декоративный элемент
    decoration = ''
    if slide_type == 'hero' and not has_photo:
        decoration = f'''
        <div style="position:absolute;right:-80px;top:-80px;width:350px;height:350px;
                    background:radial-gradient(circle, {accent}30, transparent 70%);
                    border-radius:50%;"></div>'''
    elif slide_type == 'trust':
        decoration = f'''
        <div style="position:absolute;top:36px;right:40px;">
            <svg width="60" height="60" viewBox="0 0 24 24" fill="none" stroke="{primary}" stroke-width="1.5">
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
                <path d="M9 12l2 2 4-4"/>
            </svg>
        </div>'''

    # Бейдж типа слайда
    badge_bg = 'rgba(255,255,255,0.2)' if is_dark else 'rgba(0,0,0,0.08)'
    badge_color = '#ffffff' if is_dark else '#64748b'
    type_labels = {
        'hero': 'ГЛАВНОЕ',
        'problem': 'ПРОБЛЕМА',
        'advantages': 'ПРЕИМУЩЕСТВО',
        'characteristics': 'ХАРАКТЕРИСТИКИ',
        'application': 'ПРИМЕНЕНИЕ',
        'bundling': 'КОМПЛЕКТАЦИЯ',
        'trust': 'ГАРАНТИЯ',
        'usage': 'ПРИМЕНЕНИЕ',
    }
    badge_text = html.escape(type_labels.get(slide_type, str(slide_type).upper()))

    # Контент-зона: вертикальный макет 900x1200
    # Hero с фото: текст снизу, фото сверху
    # Остальные: текст на всю ширину
    if has_photo and slide_type == 'hero':
        content_top = '600px'
        text_max_width = '820px'
    else:
        content_top = '90px'
        text_max_width = '820px'

    bullets_html = _build_bullets_html(bullets, is_dark)
    underline_color = accent if is_dark else primary

    # Адаптируем размеры шрифтов под вертикальный формат
    title_size = '40px' if has_photo and slide_type == 'hero' else '44px'
    subtitle_size = '22px'

    return f'''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
</style>
</head>
<body style="width:{WB_WIDTH}px;height:{WB_HEIGHT}px;overflow:hidden;margin:0;">
<div style="width:{WB_WIDTH}px;height:{WB_HEIGHT}px;background:{bg};
            position:relative;overflow:hidden;font-family:{font_family};">

    {decoration}
    {photo_html}

    <!-- Badge -->
    <div style="position:absolute;top:{'620px' if has_photo and slide_type == 'hero' else '28px'};left:40px;
                background:{badge_bg};border-radius:8px;padding:5px 14px;z-index:10;">
        <span style="font-size:12px;font-weight:700;letter-spacing:2px;color:{badge_color};">
            {badge_text}
        </span>
    </div>

    <!-- Content -->
    <div style="position:absolute;left:40px;top:{content_top};max-width:{text_max_width};padding-right:36px;">
        <h1 style="font-size:{title_size};font-weight:900;color:{title_color};
                   line-height:1.1;letter-spacing:-0.5px;text-transform:uppercase;
                   margin-bottom:12px;margin-top:40px;">
            {title}
        </h1>
        <div style="width:60px;height:4px;background:{underline_color};border-radius:3px;margin-bottom:16px;"></div>
        {f'<p style="font-size:{subtitle_size};font-weight:500;color:{subtitle_color};line-height:1.4;max-width:700px;">{subtitle}</p>' if subtitle else ''}
        {bullets_html}
    </div>

    <!-- Bottom bar -->
    <div style="position:absolute;bottom:0;left:0;right:0;height:5px;
                background:linear-gradient(90deg, {primary}, {accent});"></div>
</div>
</body>
</html>'''


def _resolve_photo_url(photo_entry) -> Optional[str]:
    """Извлекает URL из записи о фото (строка или dict с ключами original/blur/sexoptovik)"""
    if isinstance(photo_entry, str):
        return photo_entry
    if isinstance(photo_entry, dict):
        # Приоритет: original → blur → sexoptovik → первое значение
        for key in ('original', 'blur', 'sexoptovik'):
            if photo_entry.get(key):
                return photo_entry[key]
        # Берём первый непустой URL
        for v in photo_entry.values():
            if isinstance(v, str) and v.startswith('http'):
                return v
    return None


def _fetch_photo_as_b64(photo_entry) -> Optional[str]:
    """Скачивает фото и конвертит в base64. Принимает строку URL или dict."""
    url = _resolve_photo_url(photo_entry)
    if not url:
        return None
    try:
        import requests as req
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'image/*,*/*;q=0.8',
        }
        if 'sexoptovik.ru' in url:
            headers['Referer'] = 'https://sexoptovik.ru/admin/'

        # Пробуем все URL из dict если доступны
        urls_to_try = [url]
        if isinstance(photo_entry, dict):
            for key in ('original', 'blur', 'sexoptovik'):
                u = photo_entry.get(key)
                if u and u != url and u not in urls_to_try:
                    urls_to_try.append(u)

        for try_url in urls_to_try:
            try:
                resp = req.get(try_url, headers=headers, timeout=15, allow_redirects=True)
                content_type = resp.headers.get('Content-Type', '')
                if resp.status_code == 200 and len(resp.content) > 1000 and (
                    content_type.startswith('image/') or len(resp.content) > 5000
                ):
                    img = Image.open(io.BytesIO(resp.content))
                    img = img.convert('RGB')
                    img.thumbnail((900, 1200), Image.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format='JPEG', quality=85)
                    return base64.b64encode(buf.getvalue()).decode('utf-8')
            except Exception as e:
                logger.debug(f"Photo fetch failed {try_url}: {e}")
                continue

        logger.warning(f"All photo URLs failed for entry: {list(urls_to_try)}")
    except Exception as e:
        logger.warning(f"Failed to fetch photo: {e}")
    return None


def _fetch_photo_from_cache(product_id: int, photo_idx: int = 0) -> Optional[str]:
    """Загружает фото из локального кэша через photo_cache сервис."""
    try:
        from models import SupplierProduct
        from services.photo_cache import get_photo_cache
        import json as _json

        product = SupplierProduct.query.get(product_id)
        if not product or not product.photo_urls_json:
            return None

        photos = _json.loads(product.photo_urls_json)
        if photo_idx >= len(photos):
            return None

        ph = photos[photo_idx]
        if isinstance(ph, dict):
            url = ph.get('sexoptovik') or ph.get('original') or ph.get('blur')
        elif isinstance(ph, str):
            url = ph
        else:
            return None

        supplier_type = product.supplier.code if product.supplier else 'unknown'
        external_id = product.external_id or ''
        cache = get_photo_cache()

        if cache.is_cached(supplier_type, external_id, url):
            cache_path = cache.get_cache_path(supplier_type, external_id, url)
            with open(cache_path, 'rb') as f:
                img_data = f.read()
            img = Image.open(io.BytesIO(img_data))
            img = img.convert('RGB')
            img.thumbnail((900, 1200), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=85)
            return base64.b64encode(buf.getvalue()).decode('utf-8')
    except Exception as e:
        logger.debug(f"Cache photo load failed for product {product_id}: {e}")
    return None


def render_slide_to_png(
    slide: Dict,
    design: Dict,
    product_photo_b64: Optional[str] = None,
    slide_index: int = 0
) -> Tuple[bool, Optional[bytes], str]:
    """
    Рендерит один слайд в PNG через Playwright.

    Args:
        slide: Данные слайда из AI rich_content
        design: design_recommendations из rich_content
        product_photo_b64: Base64 фото товара (опционально)
        slide_index: Номер слайда

    Returns:
        (success, png_bytes, error_message)
    """
    try:
        from playwright.sync_api import sync_playwright

        html = _build_slide_html(slide, design, product_photo_b64, slide_index)

        chromium_path = _find_chromium()
        launch_opts = {
            'headless': True,
            'args': ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
        }
        if chromium_path:
            launch_opts['executable_path'] = chromium_path

        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_opts)
            page = browser.new_page(
                viewport={'width': WB_WIDTH, 'height': WB_HEIGHT},
                device_scale_factor=1
            )
            page.set_content(html, wait_until='domcontentloaded')
            page.wait_for_timeout(300)

            png_bytes = page.screenshot(type='png', clip={
                'x': 0, 'y': 0,
                'width': WB_WIDTH,
                'height': WB_HEIGHT
            })
            browser.close()

        # Оптимизируем через Pillow (PNG → JPEG для WB, меньше размер)
        img = Image.open(io.BytesIO(png_bytes))
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=92)
        jpeg_bytes = buf.getvalue()

        logger.info(f"Slide {slide_index + 1} rendered: {len(jpeg_bytes)} bytes")
        return True, jpeg_bytes, ''

    except Exception as e:
        logger.error(f"Render error slide {slide_index}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False, None, str(e)


def render_all_slides(
    rich_content: Dict,
    product_photos: Optional[List] = None,
    max_slides: int = 10,
    supplier_product_id: int = None
) -> List[Dict]:
    """
    Рендерит все слайды из rich_content.

    Args:
        rich_content: Полный JSON rich_content от AI
        product_photos: Список URL/dict фотографий товара
        max_slides: Максимум слайдов
        supplier_product_id: ID SupplierProduct для загрузки фото из кэша

    Returns:
        [{slide_number, slide_type, success, image_bytes, error}]
    """
    slides = rich_content.get('slides', [])[:max_slides]
    design = rich_content.get('design_recommendations', {})

    if not slides:
        return [{'slide_number': 0, 'success': False, 'error': 'Нет слайдов в rich_content'}]

    # Сначала пробуем из локального кэша, потом по URL
    photo_b64 = None
    if supplier_product_id:
        for idx in range(3):
            photo_b64 = _fetch_photo_from_cache(supplier_product_id, idx)
            if photo_b64:
                logger.info(f"Photo loaded from cache for product {supplier_product_id}")
                break

    if not photo_b64 and product_photos:
        for entry in product_photos[:3]:
            photo_b64 = _fetch_photo_as_b64(entry)
            if photo_b64:
                break

    results = []
    try:
        from playwright.sync_api import sync_playwright

        chromium_path = _find_chromium()
        launch_opts = {
            'headless': True,
            'args': ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
        }
        if chromium_path:
            launch_opts['executable_path'] = chromium_path

        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_opts)

            for i, slide in enumerate(slides):
                slide_num = slide.get('number', i + 1)
                slide_type = slide.get('type', 'unknown')

                try:
                    html = _build_slide_html(slide, design, photo_b64, i)

                    page = browser.new_page(
                        viewport={'width': WB_WIDTH, 'height': WB_HEIGHT},
                        device_scale_factor=1
                    )
                    page.set_content(html, wait_until='domcontentloaded')
                    page.wait_for_timeout(200)

                    png_bytes = page.screenshot(type='png', clip={
                        'x': 0, 'y': 0,
                        'width': WB_WIDTH,
                        'height': WB_HEIGHT
                    })
                    page.close()

                    # PNG → JPEG
                    img = Image.open(io.BytesIO(png_bytes))
                    buf = io.BytesIO()
                    img.save(buf, format='JPEG', quality=92)
                    jpeg_bytes = buf.getvalue()

                    results.append({
                        'slide_number': slide_num,
                        'slide_type': slide_type,
                        'success': True,
                        'image_bytes': jpeg_bytes,
                        'image_size': len(jpeg_bytes),
                        'error': ''
                    })
                    logger.info(f"Slide {slide_num}/{len(slides)} ({slide_type}) rendered: {len(jpeg_bytes)} bytes")

                except Exception as e:
                    logger.error(f"Error rendering slide {slide_num}: {e}")
                    results.append({
                        'slide_number': slide_num,
                        'slide_type': slide_type,
                        'success': False,
                        'image_bytes': None,
                        'error': str(e)
                    })

            browser.close()

    except Exception as e:
        logger.error(f"Playwright error: {e}")
        return [{'slide_number': 0, 'success': False, 'error': f'Playwright error: {e}'}]

    return results


def render_slide_preview_b64(
    slide: Dict,
    design: Dict,
    product_photo_b64: Optional[str] = None,
    slide_index: int = 0,
    preview_width: int = 720
) -> Tuple[bool, Optional[str], str]:
    """
    Рендерит превью слайда (уменьшенное) и возвращает base64.
    Для быстрого предпросмотра в UI.
    """
    success, img_bytes, error = render_slide_to_png(slide, design, product_photo_b64, slide_index)
    if not success:
        return False, None, error

    # Уменьшаем для превью
    img = Image.open(io.BytesIO(img_bytes))
    ratio = preview_width / img.width
    preview_height = int(img.height * ratio)
    img = img.resize((preview_width, preview_height), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=80)
    b64 = base64.b64encode(buf.getvalue()).decode('utf-8')

    return True, b64, ''


# ============================================================================
# ГИБРИДНЫЙ РЕЖИМ: AI-фон + Playwright текстовый оверлей
# ============================================================================

def _build_overlay_html(
    slide: Dict,
    design: Dict,
    bg_image_b64: Optional[str] = None,
    product_photo_b64: Optional[str] = None,
    slide_index: int = 0,
) -> str:
    """Build a catalog-style overlay with hero hierarchy or grouped fact cards."""
    del product_photo_b64  # Foreground is already restored pixel-for-pixel locally.
    slide_type = str(slide.get('type') or 'hero')
    eyebrow = html.escape(str(slide.get('eyebrow') or ''))
    title = html.escape(str(slide.get('title') or ''))
    subtitle = html.escape(str(slide.get('subtitle') or ''))
    color_palette = design.get('color_palette') or []
    font_style = design.get('font_style', 'modern')
    font_family = {
        'modern': "'Inter', 'Segoe UI', system-ui, sans-serif",
        'classic': "'Georgia', 'Times New Roman', serif",
        'bold': "'Arial Black', 'Segoe UI', sans-serif",
        'elegant': "'Georgia', 'Times New Roman', serif",
    }.get(font_style, "'Inter', 'Segoe UI', system-ui, sans-serif")
    primary = _safe_color(color_palette[0], '#173f2a') if color_palette else '#173f2a'
    accent = _safe_color(color_palette[1], '#e0a52b') if len(color_palette) > 1 else '#e0a52b'
    surface = _safe_color(color_palette[3], '#ffffff') if len(color_palette) > 3 else '#ffffff'
    if bg_image_b64:
        bg_style = f'background:url(data:image/png;base64,{bg_image_b64}) center/cover no-repeat;'
    else:
        bg_style = f'background:{_get_slide_bg_gradient(slide_type, color_palette)};'

    raw_cards = slide.get('facts') if isinstance(slide.get('facts'), list) else []
    # Old durable v1 rows become one properly styled card instead of the old
    # giant label/value strip.
    if not raw_cards and slide_type != 'hero' and (title or subtitle):
        raw_cards = [{'label': slide.get('title') or '', 'value': slide.get('subtitle') or ''}]
        title = ''
        subtitle = ''
    cards = []
    for raw in raw_cards[:4]:
        if not isinstance(raw, dict):
            continue
        label = html.escape(str(raw.get('label') or ''))
        value = html.escape(str(raw.get('value') or ''))
        if label and value:
            cards.append((label, value))

    if slide_type == 'hero':
        title_length = len(str(slide.get('title') or ''))
        title_size = 42 if title_length <= 52 else 35 if title_length <= 90 else 30 if title_length <= 125 else 26
        eyebrow_html = (
            f'<span class="eyebrow">{eyebrow}</span>' if eyebrow else ''
        )
        copy_html = f'''
        <section id="safe-copy" data-fit class="hero-panel">
            <div class="hero-topline">
                <span class="catalog-label">КАТАЛОГ</span>
                {eyebrow_html}
            </div>
            <h1 id="slide-title" style="font-size:{title_size}px">{title}</h1>
            <div class="accent-line"></div>
            {f'<p class="hero-subtitle">{subtitle}</p>' if subtitle else ''}
        </section>'''
    else:
        count = len(cards)
        card_html = []
        for label, value in cards:
            raw_length = len(html.unescape(value))
            value_size = 28 if raw_length <= 28 else 23 if raw_length <= 58 else 19 if raw_length <= 105 else 16
            card_html.append(f'''
                <article class="fact-card" data-fit>
                    <div class="fact-label">{label}</div>
                    <div class="fact-value" style="font-size:{value_size}px">{value}</div>
                </article>''')
        grid_class = 'single' if count == 1 else 'pair' if count == 2 else 'quad'
        copy_html = f'''
        <section id="safe-copy" class="facts-panel">
            <header class="facts-heading">
                <div>
                    <div class="catalog-label dark">ТОЧНЫЕ ДАННЫЕ</div>
                    <h1>Характеристики</h1>
                </div>
                <span class="fact-count">Фактов: {count}</span>
            </header>
            <div class="fact-grid {grid_class}">{''.join(card_html)}</div>
        </section>'''

    slide_number = max(1, int(slide.get('number') or slide_index + 1))
    return f'''<!DOCTYPE html>
<html>
<head><meta charset="utf-8">
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ width:{WB_WIDTH}px; height:{WB_HEIGHT}px; overflow:hidden; }}
.hero-panel {{ position:absolute; z-index:10; left:30px; right:30px; top:28px;
    height:326px; overflow:hidden; padding:28px 32px 25px; border-radius:30px;
    background:linear-gradient(135deg, {primary} 0%, {primary}ee 72%, #111111e8 100%);
    border:1px solid rgba(255,255,255,.22); box-shadow:0 22px 50px rgba(0,0,0,.18); }}
.hero-topline {{ display:flex; align-items:center; gap:10px; min-height:27px; margin-bottom:17px; }}
.catalog-label {{ display:inline-flex; align-items:center; border-radius:999px; padding:6px 11px;
    background:{accent}; color:#171717; font-size:11px; line-height:1; font-weight:900; letter-spacing:1.6px; }}
.catalog-label.dark {{ background:{primary}; color:#fff; }}
.eyebrow {{ min-width:0; max-width:620px; overflow:hidden; white-space:nowrap; text-overflow:ellipsis;
    border:1px solid rgba(255,255,255,.28); border-radius:999px; padding:5px 11px;
    color:rgba(255,255,255,.9); font-size:12px; font-weight:700; letter-spacing:.25px; }}
.hero-panel h1 {{ max-width:780px; color:#fff; font-weight:900; line-height:1.03;
    letter-spacing:-.8px; overflow-wrap:anywhere; }}
.accent-line {{ width:76px; height:5px; margin:14px 0 12px; border-radius:5px; background:{accent}; }}
.hero-subtitle {{ color:rgba(255,255,255,.9); font-size:21px; line-height:1.25; font-weight:650; overflow-wrap:anywhere; }}
.facts-panel {{ position:absolute; z-index:10; left:30px; right:30px; top:27px; height:482px; overflow:hidden; }}
.facts-heading {{ height:79px; display:flex; align-items:center; justify-content:space-between; gap:24px;
    padding:0 5px 12px; }}
.facts-heading h1 {{ margin-top:8px; color:{primary}; font-size:34px; line-height:1; font-weight:900; letter-spacing:-.6px; }}
.fact-count {{ flex:none; border:1px solid {primary}33; border-radius:999px; padding:8px 13px;
    background:{surface}d9; color:{primary}; font-size:13px; font-weight:800; }}
.fact-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; }}
.fact-grid.single {{ grid-template-columns:1fr; }}
.fact-card {{ position:relative; min-width:0; height:181px; overflow:hidden; padding:21px 23px 18px;
    border-radius:23px; background:{surface}f2; border:1px solid {primary}24;
    box-shadow:0 13px 30px rgba(17,24,39,.11); }}
.fact-card::before {{ content:''; position:absolute; left:0; top:0; bottom:0; width:7px; background:{accent}; }}
.fact-grid.quad .fact-card {{ height:187px; }}
.fact-grid.single .fact-card {{ height:225px; padding:28px 30px; }}
.fact-label {{ color:{primary}; font-size:12px; line-height:1.2; font-weight:900; letter-spacing:1.15px;
    text-transform:uppercase; overflow-wrap:anywhere; margin-bottom:13px; }}
.fact-value {{ color:#181b1f; line-height:1.12; font-weight:850; letter-spacing:-.25px; overflow-wrap:anywhere; }}
.slide-index {{ position:absolute; z-index:12; right:25px; bottom:24px; width:46px; height:46px;
    border-radius:50%; display:flex; align-items:center; justify-content:center; background:{primary}; color:#fff;
    font-size:14px; font-weight:900; box-shadow:0 8px 22px rgba(0,0,0,.16); }}
</style>
</head>
<body>
<main style="width:{WB_WIDTH}px;height:{WB_HEIGHT}px;{bg_style}position:relative;overflow:hidden;font-family:{font_family};">
    {copy_html}
    <div class="slide-index">{slide_number}</div>
    <div style="position:absolute;left:0;right:0;bottom:0;height:7px;background:linear-gradient(90deg,{primary},{accent});"></div>
</main>
</body>
</html>'''


def _template_background_bytes(
    design: Dict,
    slide_type: str = 'hero',
    slide_index: int = 0,
) -> bytes:
    """Text-free deterministic catalog set with depth and product pedestal."""
    palette = design.get("color_palette") or []
    primary_hex = _safe_color(palette[0], "#173f2a") if palette else "#173f2a"
    accent_hex = _safe_color(palette[1], "#e0a52b") if len(palette) > 1 else "#e0a52b"
    base_hex = _safe_color(palette[2], "#edf4e8") if len(palette) > 2 else "#edf4e8"
    primary = tuple(int(primary_hex[index:index + 2], 16) for index in (1, 3, 5))
    accent = tuple(int(accent_hex[index:index + 2], 16) for index in (1, 3, 5))
    base = tuple(int(base_hex[index:index + 2], 16) for index in (1, 3, 5))
    white = (255, 255, 255)
    strip = Image.new("RGB", (1, WB_HEIGHT))
    pixels = strip.load()
    for y in range(WB_HEIGHT):
        ratio = min(1.0, y / max(WB_HEIGHT * .82, 1))
        color = tuple(round(a + (b - a) * ratio * .72) for a, b in zip(base, white))
        pixels[0, y] = color
    image = strip.resize((WB_WIDTH, WB_HEIGHT), Image.Resampling.NEAREST).convert('RGBA')
    layer = Image.new('RGBA', image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    shift = (slide_index % 3) * 34
    if slide_type == 'hero':
        draw.ellipse((80 - shift, 475, 820 - shift, 1215), fill=(*accent, 30))
        draw.ellipse((205 + shift, 560, 735 + shift, 1090), outline=(*primary, 45), width=4)
        draw.rounded_rectangle((122, 1015, 778, 1145), radius=65, fill=(*primary, 20))
    else:
        draw.ellipse((-140 + shift, 545, 520 + shift, 1205), fill=(*primary, 22))
        draw.ellipse((430 - shift, 610, 1040 - shift, 1220), fill=(*accent, 27))
        draw.rounded_rectangle((175, 1040, 725, 1148), radius=54, fill=(*primary, 24))
    for x in range(70, WB_WIDTH, 152):
        draw.ellipse((x, 1138, x + 6, 1144), fill=(*primary, 60))
    image = Image.alpha_composite(image, layer).convert('RGB')
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _fetch_product_photo_bytes(
    supplier_product_id: Optional[int],
    product_photos: Optional[List],
) -> Optional[bytes]:
    """Fetch original bytes without thumbnailing or JPEG recompression."""
    entries = list(product_photos or [])
    if supplier_product_id:
        try:
            from models import SupplierProduct
            from services.photo_cache import get_photo_cache

            product = SupplierProduct.query.get(supplier_product_id)
            stored = json.loads(product.photo_urls_json or "[]") if product else []
            if isinstance(stored, list):
                entries = stored + entries
            if product:
                cache = get_photo_cache()
                supplier_type = product.supplier.code if product.supplier else "unknown"
                for entry in stored if isinstance(stored, list) else []:
                    url = _resolve_photo_url(entry)
                    if url and cache.is_cached(supplier_type, product.external_id or "", url):
                        path = cache.get_cache_path(supplier_type, product.external_id or "", url)
                        with open(path, "rb") as source:
                            data = source.read()
                        Image.open(io.BytesIO(data)).verify()
                        return data
        except Exception as exc:
            logger.debug("Original cache photo unavailable: %s", exc)

    try:
        from services.image_lab_service import download_public_image

        for entry in entries[:6]:
            url = _resolve_photo_url(entry)
            if not url:
                continue
            data = download_public_image(url)
            Image.open(io.BytesIO(data)).verify()
            return data
    except Exception as exc:
        logger.debug("Original photo fetch failed: %s", exc)
    return None


def render_hybrid_slides(
    rich_content: Dict,
    image_service,
    product_photos: Optional[List] = None,
    product_title: str = '',
    supplier_product_id: int = None,
    source_photo_bytes: Optional[bytes] = None,
    max_slides: int = 10
) -> List[Dict]:
    """Production render: verified copy + empty AI background + unchanged RGB."""
    from services.infographic_content import (
        validate_fact_safe_rich_content,
        visible_texts,
    )
    from services.infographic_quality import (
        compose_identity_preserving,
        evaluate_background_text,
        evaluate_final_image,
    )

    valid, validation_errors = validate_fact_safe_rich_content(rich_content)
    if not valid:
        return [{
            'slide_number': 0,
            'success': False,
            'error': 'Fact-safe validation: ' + '; '.join(validation_errors),
            'renderer': 'hybrid',
            'quality': {'status': 'rejected', 'publishable': False},
        }]
    slides = rich_content.get('slides', [])[:max(1, min(max_slides, 10))]
    design = rich_content.get('design_recommendations', {})
    source_bytes = source_photo_bytes or _fetch_product_photo_bytes(
        supplier_product_id, product_photos,
    )
    if source_bytes:
        try:
            Image.open(io.BytesIO(source_bytes)).verify()
        except Exception:
            source_bytes = None
    if not source_bytes:
        return [{
            'slide_number': 0,
            'success': False,
            'error': 'Исходное фото недоступно; генеративная замена запрещена',
            'renderer': 'hybrid',
            'quality': {'status': 'rejected', 'publishable': False},
        }]

    prepared = []
    for i, slide in enumerate(slides):
        slide_num = slide.get('number', i + 1)
        scene_key = (slide.get('image_concept') or {}).get('scene_key', 'luxury')
        background = None
        background_source = 'template'
        background_note = ''
        try:
            success, candidate, error = image_service.generate_background(scene_key)
            if success and candidate:
                candidate_check = evaluate_background_text(candidate)
                if candidate_check.get('checked') and candidate_check.get('pass'):
                    background = candidate
                    background_source = 'ai_background'
                else:
                    background_note = (
                        candidate_check.get('reason')
                        or 'AI-фон не прошёл no-text gate'
                    )
            else:
                background_note = error or 'AI-фон недоступен'
        except Exception as exc:
            background_note = str(exc)
        if background is None:
            background = _template_background_bytes(design, str(
                slide.get('type') or 'hero'
            ), i)
        try:
            is_fact_slide = slide.get('type') in {'fact_grid', 'characteristics'}
            composite = compose_identity_preserving(
                background,
                source_bytes,
                top_reserved_ratio=.45 if is_fact_slide else .31,
                max_width_ratio=.68 if is_fact_slide else .82,
            )
            prepared.append({
                'slide': slide,
                'slide_number': slide_num,
                'background_source': background_source,
                'background_note': background_note,
                'composite': composite,
            })
        except Exception as exc:
            prepared.append({
                'slide': slide,
                'slide_number': slide_num,
                'error': str(exc),
            })

    results = []
    try:
        from playwright.sync_api import sync_playwright

        chromium_path = _find_chromium()
        launch_opts = {
            'headless': True,
            'args': ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
        }
        if chromium_path:
            launch_opts['executable_path'] = chromium_path

        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_opts)

            for i, item in enumerate(prepared):
                slide = item['slide']
                slide_num = item['slide_number']
                slide_type = slide.get('type', 'unknown')
                if item.get('error'):
                    results.append({
                        'slide_number': slide_num,
                        'slide_type': slide_type,
                        'success': False,
                        'image_bytes': None,
                        'error': item['error'],
                        'renderer': 'hybrid',
                        'quality': {'status': 'rejected', 'publishable': False},
                    })
                    continue
                try:
                    composite = item['composite']
                    scene_b64 = base64.b64encode(composite.image_bytes).decode('ascii')
                    markup = _build_overlay_html(slide, design, scene_b64, None, i)

                    page = browser.new_page(
                        viewport={'width': WB_WIDTH, 'height': WB_HEIGHT},
                        device_scale_factor=1
                    )
                    page.set_content(markup, wait_until='domcontentloaded')
                    page.wait_for_timeout(300)
                    clipped = page.evaluate("""() => {
                        const root = document.getElementById('safe-copy');
                        if (!root || root.scrollHeight > root.clientHeight) return true;
                        return [...document.querySelectorAll('[data-fit]')].some(
                            el => el.scrollHeight > el.clientHeight || el.scrollWidth > el.clientWidth
                        );
                    }""")
                    if clipped:
                        page.close()
                        raise ValueError('Текст не помещается в foreground-free safe-zone')

                    png_bytes = page.screenshot(type='png', clip={
                        'x': 0, 'y': 0, 'width': WB_WIDTH, 'height': WB_HEIGHT
                    })
                    page.close()

                    texts = visible_texts({'slides': [slide]})
                    quality = evaluate_final_image(
                        png_bytes,
                        identity_mode='pixel_preserved_composite',
                        text_mode='deterministic_overlay',
                        expected_texts=texts,
                        rendered_texts=texts,
                        claims_pass=True,
                        composite_metadata=composite.metadata,
                        background_text_check={
                            'checked': True,
                            'pass': True,
                            'reason': item['background_source'],
                        },
                        background_scene_check={
                            'checked': item['background_source'] == 'template',
                            'pass': (
                                True if item['background_source'] == 'template'
                                else None
                            ),
                            'reason': (
                                'deterministic empty template'
                                if item['background_source'] == 'template'
                                else 'AI scene requires person/object/empty-zone review'
                            ),
                        },
                    )
                    accepted = quality['status'] != 'rejected'

                    results.append({
                        'slide_number': slide_num,
                        'slide_type': slide_type,
                        'success': accepted,
                        'publishable': quality['publishable'],
                        'image_bytes': png_bytes if accepted else None,
                        'image_size': len(png_bytes) if accepted else 0,
                        'error': '' if accepted else 'Финальный quality gate отклонил слайд',
                        'renderer': item['background_source'],
                        'has_ai_bg': item['background_source'] == 'ai_background',
                        'background_note': item['background_note'],
                        'quality': quality,
                        'composite_metadata': composite.metadata,
                    })
                    logger.info(
                        "Hybrid slide %s: %s (%s)",
                        slide_num,
                        quality['status'],
                        item['background_source'],
                    )

                except Exception as e:
                    logger.error(f"Render error slide {slide_num}: {e}")
                    results.append({
                        'slide_number': slide_num, 'slide_type': slide_type,
                        'success': False, 'image_bytes': None, 'error': str(e),
                        'renderer': 'hybrid',
                        'quality': {'status': 'rejected', 'publishable': False},
                    })

            browser.close()

    except Exception as e:
        logger.error(f"Playwright error: {e}")
        return [{
            'slide_number': 0,
            'success': False,
            'error': f'Playwright error: {e}',
            'renderer': 'hybrid',
            'quality': {'status': 'rejected', 'publishable': False},
        }]

    return results
