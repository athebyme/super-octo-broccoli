# -*- coding: utf-8 -*-
"""
Infographic Renderer — рендеринг инфографики из HTML-шаблонов через Playwright.

Берёт JSON rich_content (слайды с текстами) + фото товара →
рендерит красивые PNG 900x1200 (3:4) для WB.

Бесплатно, стабильно, полный контроль над дизайном.
"""

import base64
import io
import json
import logging
import os
import tempfile
from typing import Dict, List, Optional, Tuple

from PIL import Image

logger = logging.getLogger(__name__)

# Размеры для WB Rich-контента (соотношение 3:4, рекомендуемое WB)
WB_WIDTH = 900
WB_HEIGHT = 1200

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
    primary = palette[0] if len(palette) > 0 else '#6366f1'
    accent = palette[1] if len(palette) > 1 else '#8b5cf6'
    bg_color = palette[2] if len(palette) > 2 else '#f8fafc'

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
    title = slide.get('title', '')
    subtitle = slide.get('subtitle', '')
    bullets = slide.get('bullets') or []
    color_palette = design.get('color_palette', [])
    font_style = design.get('font_style', 'modern')

    is_dark = _is_dark_bg(slide_type)
    bg = _get_slide_bg_gradient(slide_type, color_palette)

    title_color = '#ffffff' if is_dark else '#1e293b'
    subtitle_color = '#e2e8f0' if is_dark else '#64748b'
    primary = color_palette[0] if color_palette else '#6366f1'

    font_family = {
        'modern': "'Inter', 'Segoe UI', system-ui, sans-serif",
        'classic': "'Georgia', 'Times New Roman', serif",
        'bold': "'Impact', 'Arial Black', sans-serif",
        'elegant': "'Playfair Display', 'Georgia', serif",
    }.get(font_style, "'Inter', 'Segoe UI', system-ui, sans-serif")

    accent = color_palette[1] if len(color_palette) > 1 else '#8b5cf6'

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
    badge_text = type_labels.get(slide_type, slide_type.upper())

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
    slide_index: int = 0
) -> str:
    """Строит HTML с AI-сгенерированным фоном и текстовым оверлеем."""
    slide_type = slide.get('type', 'hero')
    title = slide.get('title', '')
    subtitle = slide.get('subtitle', '')
    bullets = slide.get('bullets') or []
    color_palette = design.get('color_palette', [])
    font_style = design.get('font_style', 'modern')

    font_family = {
        'modern': "'Inter', 'Segoe UI', system-ui, sans-serif",
        'classic': "'Georgia', 'Times New Roman', serif",
        'bold': "'Impact', 'Arial Black', sans-serif",
        'elegant': "'Playfair Display', 'Georgia', serif",
    }.get(font_style, "'Inter', 'Segoe UI', system-ui, sans-serif")

    primary = color_palette[0] if color_palette else '#6366f1'
    accent = color_palette[1] if len(color_palette) > 1 else '#8b5cf6'

    # Фон: AI-картинка или градиент
    if bg_image_b64:
        bg_style = f'background:url(data:image/jpeg;base64,{bg_image_b64}) center/cover no-repeat;'
    else:
        bg_gradient = _get_slide_bg_gradient(slide_type, color_palette)
        bg_style = f'background:{bg_gradient};'

    # Затемнение поверх фона для читаемости текста
    overlay_style = 'background:linear-gradient(180deg, rgba(0,0,0,0.15) 0%, rgba(0,0,0,0.65) 50%, rgba(0,0,0,0.85) 100%);'

    # Фото товара — компактно, поверх фона
    photo_html = ''
    if product_photo_b64 and slide_type in ('hero', 'application', 'characteristics'):
        photo_html = f'''
        <div style="position:absolute;top:60px;right:40px;
                    width:280px;height:350px;border-radius:20px;overflow:hidden;
                    box-shadow:0 20px 40px rgba(0,0,0,0.4);border:3px solid rgba(255,255,255,0.2);">
            <img src="data:image/jpeg;base64,{product_photo_b64}"
                 style="width:100%;height:100%;object-fit:cover;" />
        </div>'''

    # Бейдж
    type_labels = {
        'hero': 'ГЛАВНОЕ', 'problem': 'ПРОБЛЕМА', 'advantages': 'ПРЕИМУЩЕСТВО',
        'characteristics': 'ХАРАКТЕРИСТИКИ', 'application': 'ПРИМЕНЕНИЕ',
        'bundling': 'КОМПЛЕКТАЦИЯ', 'trust': 'ГАРАНТИЯ', 'usage': 'ПРИМЕНЕНИЕ',
    }
    badge_text = type_labels.get(slide_type, slide_type.upper())

    # Буллеты
    items_html = ''
    if bullets:
        items = ''.join(
            f'<li style="margin-bottom:10px;padding-left:10px;position:relative;">'
            f'<span style="position:absolute;left:-18px;color:{accent};">&#10003;</span>'
            f'{b}</li>'
            for b in bullets[:5]
        )
        items_html = f'''
        <ul style="list-style:none;padding:0;margin:18px 0 0 22px;font-size:20px;
                   line-height:1.5;color:#ffffff;font-weight:500;">
            {items}
        </ul>'''

    # Текст всегда внизу, на затемнённом фоне — всегда хорошо читается
    text_max_width = '560px' if product_photo_b64 and slide_type in ('hero',) else '820px'

    return f'''<!DOCTYPE html>
<html>
<head><meta charset="utf-8">
<style>* {{ margin:0; padding:0; box-sizing:border-box; }}</style>
</head>
<body style="width:{WB_WIDTH}px;height:{WB_HEIGHT}px;overflow:hidden;margin:0;">
<div style="width:{WB_WIDTH}px;height:{WB_HEIGHT}px;{bg_style}
            position:relative;overflow:hidden;font-family:{font_family};">

    <!-- Gradient overlay for readability -->
    <div style="position:absolute;inset:0;{overlay_style}"></div>

    {photo_html}

    <!-- Badge -->
    <div style="position:absolute;top:28px;left:40px;z-index:10;
                background:rgba(255,255,255,0.15);backdrop-filter:blur(8px);
                border-radius:8px;padding:5px 14px;border:1px solid rgba(255,255,255,0.2);">
        <span style="font-size:11px;font-weight:700;letter-spacing:2px;color:#ffffff;">
            {badge_text}
        </span>
    </div>

    <!-- Content at bottom -->
    <div style="position:absolute;left:40px;bottom:60px;max-width:{text_max_width};z-index:10;">
        <h1 style="font-size:42px;font-weight:900;color:#ffffff;
                   line-height:1.1;letter-spacing:-0.5px;text-transform:uppercase;
                   margin-bottom:12px;text-shadow:0 2px 8px rgba(0,0,0,0.3);">
            {title}
        </h1>
        <div style="width:60px;height:4px;background:{accent};border-radius:3px;margin-bottom:14px;"></div>
        {f'<p style="font-size:20px;font-weight:500;color:rgba(255,255,255,0.85);line-height:1.4;text-shadow:0 1px 4px rgba(0,0,0,0.3);">{subtitle}</p>' if subtitle else ''}
        {items_html}
    </div>

    <!-- Bottom accent bar -->
    <div style="position:absolute;bottom:0;left:0;right:0;height:5px;
                background:linear-gradient(90deg, {primary}, {accent});"></div>
</div>
</body>
</html>'''


def render_hybrid_slides(
    rich_content: Dict,
    image_service,
    product_photos: Optional[List] = None,
    product_title: str = '',
    supplier_product_id: int = None,
    max_slides: int = 10
) -> List[Dict]:
    """
    Гибридный рендеринг: AI генерирует фон, Playwright накладывает текст + фото товара.

    Args:
        rich_content: JSON rich_content от AI
        image_service: ImageGenerationService instance
        product_photos: Фото товара (URL или dict)
        product_title: Название товара
        supplier_product_id: ID для загрузки фото из кэша
        max_slides: Максимум слайдов

    Returns:
        [{slide_number, slide_type, success, image_bytes, image_size, error, renderer}]
    """
    slides = rich_content.get('slides', [])[:max_slides]
    design = rich_content.get('design_recommendations', {})

    if not slides:
        return [{'slide_number': 0, 'success': False, 'error': 'Нет слайдов', 'renderer': 'hybrid'}]

    # Загружаем фото товара
    photo_b64 = None
    if supplier_product_id:
        for idx in range(3):
            photo_b64 = _fetch_photo_from_cache(supplier_product_id, idx)
            if photo_b64:
                break
    if not photo_b64 and product_photos:
        for entry in product_photos[:3]:
            photo_b64 = _fetch_photo_as_b64(entry)
            if photo_b64:
                break

    results = []

    # 1. Генерируем AI-фоны для каждого слайда
    logger.info(f"Generating {len(slides)} AI backgrounds...")
    bg_images = {}
    for i, slide in enumerate(slides):
        slide_num = slide.get('number', i + 1)
        try:
            success, img_bytes, error = image_service.generate_slide_image(
                slide_data=slide,
                product_photos=[],  # не передаём фото товара как референс для фона
                product_title=product_title
            )
            if success and img_bytes:
                bg_b64 = base64.b64encode(img_bytes).decode('utf-8')
                bg_images[i] = bg_b64
                logger.info(f"AI background {slide_num}: OK ({len(img_bytes)} bytes)")
            else:
                logger.warning(f"AI background {slide_num}: failed - {error}")
        except Exception as e:
            logger.error(f"AI background {slide_num}: error - {e}")

        # Пауза между запросами
        if i < len(slides) - 1:
            import time
            time.sleep(2)

    # 2. Рендерим через Playwright с AI-фонами
    logger.info(f"Rendering {len(slides)} slides with Playwright overlay...")
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
                    bg_b64 = bg_images.get(i)
                    html = _build_overlay_html(slide, design, bg_b64, photo_b64, i)

                    page = browser.new_page(
                        viewport={'width': WB_WIDTH, 'height': WB_HEIGHT},
                        device_scale_factor=1
                    )
                    page.set_content(html, wait_until='domcontentloaded')
                    page.wait_for_timeout(300)

                    png_bytes = page.screenshot(type='png', clip={
                        'x': 0, 'y': 0, 'width': WB_WIDTH, 'height': WB_HEIGHT
                    })
                    page.close()

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
                        'error': '',
                        'renderer': 'hybrid' if bg_b64 else 'template',
                        'has_ai_bg': bool(bg_b64)
                    })
                    logger.info(f"Hybrid slide {slide_num}: {len(jpeg_bytes)} bytes ({'AI bg' if bg_b64 else 'template bg'})")

                except Exception as e:
                    logger.error(f"Render error slide {slide_num}: {e}")
                    results.append({
                        'slide_number': slide_num, 'slide_type': slide_type,
                        'success': False, 'image_bytes': None, 'error': str(e),
                        'renderer': 'hybrid'
                    })

            browser.close()

    except Exception as e:
        logger.error(f"Playwright error: {e}")
        return [{'slide_number': 0, 'success': False, 'error': f'Playwright error: {e}', 'renderer': 'hybrid'}]

    return results
