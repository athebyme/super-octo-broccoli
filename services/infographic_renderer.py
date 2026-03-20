# -*- coding: utf-8 -*-
"""
Infographic Renderer — рендеринг инфографики из HTML-шаблонов через Playwright.

Берёт JSON rich_content (слайды с текстами) + фото товара →
рендерит красивые PNG 1440x810 для WB.

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

# Размеры для WB Rich-контента
WB_WIDTH = 1440
WB_HEIGHT = 810

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
        f'<li style="margin-bottom:12px;padding-left:12px;position:relative;">'
        f'<span style="position:absolute;left:-20px;color:{("#a78bfa" if is_dark else "#6366f1")};">&#10003;</span>'
        f'{b}</li>'
        for b in bullets[:4]
    )
    return f'''
    <ul style="list-style:none;padding:0;margin:32px 0 0 24px;font-size:28px;
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

    # Фото товара — показываем справа на hero, или как фон
    photo_html = ''
    if product_photo_b64 and slide_type in ('hero', 'application', 'bundling', 'characteristics'):
        if slide_type == 'hero':
            photo_html = f'''
            <div style="position:absolute;right:40px;top:50%;transform:translateY(-50%);
                        width:500px;height:650px;border-radius:24px;overflow:hidden;
                        box-shadow:0 25px 50px rgba(0,0,0,0.3);">
                <img src="data:image/jpeg;base64,{product_photo_b64}"
                     style="width:100%;height:100%;object-fit:cover;" />
            </div>'''
        else:
            photo_html = f'''
            <div style="position:absolute;right:60px;bottom:60px;
                        width:350px;height:350px;border-radius:20px;overflow:hidden;
                        box-shadow:0 15px 30px rgba(0,0,0,0.15);">
                <img src="data:image/jpeg;base64,{product_photo_b64}"
                     style="width:100%;height:100%;object-fit:cover;" />
            </div>'''

    # Декоративный элемент
    accent = color_palette[1] if len(color_palette) > 1 else '#8b5cf6'
    decoration = ''
    if slide_type == 'hero':
        decoration = f'''
        <div style="position:absolute;left:-100px;top:-100px;width:400px;height:400px;
                    background:radial-gradient(circle, {accent}30, transparent 70%);
                    border-radius:50%;"></div>
        <div style="position:absolute;right:500px;bottom:-50px;width:300px;height:300px;
                    background:radial-gradient(circle, {primary}20, transparent 70%);
                    border-radius:50%;"></div>'''
    elif slide_type == 'trust':
        decoration = f'''
        <div style="position:absolute;top:40px;right:60px;">
            <svg width="80" height="80" viewBox="0 0 24 24" fill="none" stroke="{primary}" stroke-width="1.5">
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
                <path d="M9 12l2 2 4-4"/>
            </svg>
        </div>'''

    # Номер слайда — маленький бейдж
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
    }
    badge_text = type_labels.get(slide_type, slide_type.upper())

    # Контент-зона: текст слева, фото справа (если есть)
    text_max_width = '780px' if product_photo_b64 and slide_type in ('hero',) else '1200px'

    bullets_html = _build_bullets_html(bullets, is_dark)

    # Подчёркивание заголовка
    underline_color = accent if is_dark else primary

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
    <div style="position:absolute;top:32px;left:48px;
                background:{badge_bg};border-radius:8px;padding:6px 16px;">
        <span style="font-size:14px;font-weight:700;letter-spacing:2px;color:{badge_color};">
            {badge_text}
        </span>
    </div>

    <!-- Content -->
    <div style="position:absolute;left:48px;top:100px;max-width:{text_max_width};padding-right:40px;">
        <h1 style="font-size:56px;font-weight:900;color:{title_color};
                   line-height:1.1;letter-spacing:-1px;text-transform:uppercase;
                   margin-bottom:16px;">
            {title}
        </h1>
        <div style="width:80px;height:5px;background:{underline_color};border-radius:3px;margin-bottom:20px;"></div>
        {f'<p style="font-size:30px;font-weight:500;color:{subtitle_color};line-height:1.4;max-width:700px;">{subtitle}</p>' if subtitle else ''}
        {bullets_html}
    </div>

    <!-- Bottom bar -->
    <div style="position:absolute;bottom:0;left:0;right:0;height:6px;
                background:linear-gradient(90deg, {primary}, {accent});"></div>
</div>
</body>
</html>'''


def _fetch_photo_as_b64(photo_url: str) -> Optional[str]:
    """Скачивает фото и конвертит в base64"""
    try:
        import requests as req
        resp = req.get(photo_url, timeout=15)
        if resp.status_code == 200:
            img = Image.open(io.BytesIO(resp.content))
            img = img.convert('RGB')
            # Ресайз до разумного размера
            img.thumbnail((800, 1000), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=85)
            return base64.b64encode(buf.getvalue()).decode('utf-8')
    except Exception as e:
        logger.warning(f"Failed to fetch photo {photo_url}: {e}")
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
    product_photos: Optional[List[str]] = None,
    max_slides: int = 10
) -> List[Dict]:
    """
    Рендерит все слайды из rich_content.

    Args:
        rich_content: Полный JSON rich_content от AI
        product_photos: Список URL фотографий товара
        max_slides: Максимум слайдов

    Returns:
        [{slide_number, slide_type, success, image_bytes, error}]
    """
    slides = rich_content.get('slides', [])[:max_slides]
    design = rich_content.get('design_recommendations', {})

    if not slides:
        return [{'slide_number': 0, 'success': False, 'error': 'Нет слайдов в rich_content'}]

    # Скачиваем первое фото для использования в слайдах
    photo_b64 = None
    if product_photos:
        for url in product_photos[:3]:
            photo_b64 = _fetch_photo_as_b64(url)
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
