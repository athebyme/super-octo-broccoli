# -*- coding: utf-8 -*-
"""Промпт-пресеты «Фотостудии»: атмосферы 18+-безопасных сцен и санитайзер.

Спека: docs/superpowers/specs/2026-07-14-infographics-design.md (§3).
Принципы: эмоции через свет/фактуры, нейтральная лексика, промпты на английском,
запрет текста/людей в кадре (текст кладёт Playwright-рендерер).
"""

import re

# Сцены атмосфер. mode A (edit) — сцена вокруг товара; mode B — пустой фон.
ATMOSPHERE_PRESETS = {
    "boudoir": {
        "label": "Будуар",
        "scene": (
            "an elegant boudoir scene: dark silk fabric, warm candlelight, "
            "soft shadows, intimate cozy mood"
        ),
    },
    "neon": {
        "label": "Неон",
        "scene": (
            "a moody scene lit by magenta and violet neon glow, dark "
            "background, subtle reflections on a glossy surface"
        ),
    },
    "luxury": {
        "label": "Люкс",
        "scene": (
            "a luxury still-life scene: black marble surface, gold accents, "
            "dramatic single spotlight, premium editorial look"
        ),
    },
    "spa": {
        "label": "Спа",
        "scene": (
            "a serene spa scene: light stone, eucalyptus leaves, soft "
            "daylight, gentle steam, clean minimal composition"
        ),
    },
}

_EDIT_TEMPLATE = (
    "Research-only generative edit. Preserve the complete foreground from the "
    "source photo exactly as supplied, including any person already present, "
    "product geometry, colors, materials, packaging and printed labels. Do not "
    "redraw, replace, retouch or crop the foreground. Replace only the background "
    "with {scene}. Professional commercial photography, vertical 3:4 composition. "
    "Do not add people or objects; the generated background must contain no people "
    "or extra objects. Do not generate "
    "text, watermarks or new logos."
)

_BACKGROUND_TEMPLATE = (
    "Empty product photography backdrop: {scene}. A clear empty area in the "
    "center of the surface for product placement, nothing in the middle. "
    "Professional commercial photography, cinematic soft lighting, vertical "
    "3:4 composition. No objects in focus, no people, no text, no watermarks."
)

_ANGLE_TEMPLATE = (
    "Research-only novel-view product synthesis using every supplied image as "
    "reference evidence for one and the same catalog SKU. Reconstruct exactly one "
    "product in the requested camera view and place it in {scene}. Preserve the "
    "visible product identity: silhouette, proportions, colors, materials, controls, "
    "seams and distinctive parts. Do not copy packaging, close-up crops or alternate "
    "views into the scene as extra objects. Do not add accessories, people, readable "
    "text, watermarks or new logos. Professional commercial photography, vertical "
    "3:4 composition. Geometry not visible in the references is uncertain and the "
    "result requires human review."
)

_NATIVE_SCENE_TEMPLATE = (
    "Research-only native image-to-image scene generation. Use the first supplied "
    "catalog photo as the primary product reference and integrate exactly one "
    "instance of that same item into {scene}. Preserve the visible silhouette, "
    "proportions, colors, materials, controls, packaging and printed labels as "
    "closely as possible. Re-render the whole result as one coherent photograph: "
    "the product and environment must share physically plausible ambient light, "
    "contact shadows, reflections and color spill. Do not make the result look like "
    "a pasted cutout on a separately generated background. Keep the source camera "
    "view unless the prompt explicitly requests a supported novel view. Replace the "
    "original environment; do not "
    "retain or create a second copy of the product, package, close-up or accessory. "
    "Do not add people, new readable text, watermarks or new logos. Professional "
    "commercial photography, vertical 3:4 composition. The model may redraw every "
    "pixel, so the result requires human identity review."
)

# Рискованная лексика -> нейтральная (только для текста, попадающего в промпт;
# fail-open: незнакомые слова не трогаем, товар в промпте не описываем без нужды).
_SANITIZE_REPLACEMENTS = (
    (re.compile(r"вибратор\w*", re.IGNORECASE), "аксессуар"),
    (re.compile(r"эрот\w+", re.IGNORECASE), "элегантный"),
    (re.compile(r"секс[\w-]*", re.IGNORECASE), "для двоих"),
    (re.compile(r"интим\w*", re.IGNORECASE), "личный"),
    (re.compile(r"фаллоимитатор\w*", re.IGNORECASE), "аксессуар"),
    (re.compile(r"страпон\w*", re.IGNORECASE), "аксессуар"),
    (re.compile(r"бдсм", re.IGNORECASE), "смелый стиль"),
    (re.compile(r"анальн\w+", re.IGNORECASE), "компактный"),
    (re.compile(r"мастурбатор\w*", re.IGNORECASE), "аксессуар"),
)

# Только диагностический benchmark моделей. Эти фразы запрещено переносить в
# карточку товара без совпадающего verified fact и нельзя использовать в prod flow.
TEXT_SAMPLE_PHRASES = [
    "Гипоаллергенный медицинский силикон",
    "Бесшумный мотор до 40 дБ",
    "10 режимов вибрации",
    "Зарядка через USB Type-C",
    "Водонепроницаемый корпус IPX7",
    "Мягкое покрытие софт-тач",
    "До 2 часов работы без подзарядки",
    "Анатомическая форма",
    "Премиальная подарочная упаковка",
    "Гарантия 12 месяцев",
    "Сделано из безопасных материалов",
    "Полностью водостойкий",
    "Идея подарка для пары",
    "Компактный дорожный формат",
    "Лёгкий уход и очистка",
]

# Только диагностический benchmark текста модели, не production copy.
SHORT_TEXT_SAMPLES = ["−30%", "ХИТ", "NEW", "ТОП", "18+"]


def build_edit_prompt_for_scene(scene: str) -> str:
    """Промпт режима A из произвольного описания сцены (категорийные промпты)."""
    return _EDIT_TEMPLATE.format(scene=scene)


def build_background_prompt_for_scene(scene: str) -> str:
    """Промпт режима B из произвольного описания сцены."""
    return _BACKGROUND_TEMPLATE.format(scene=scene)


def build_angle_prompt_for_scene(scene: str) -> str:
    """Research-only prompt for synthesizing a view absent from source pixels."""
    return _ANGLE_TEMPLATE.format(scene=scene)


def build_native_scene_prompt_for_scene(scene: str) -> str:
    """Prompt for a raw-photo i2i scene whose provider output is the final image."""
    return _NATIVE_SCENE_TEMPLATE.format(scene=scene)


def build_edit_prompt(preset_key: str) -> str:
    """Промпт режима A: сцена вокруг товара с исходного фото."""
    return build_edit_prompt_for_scene(ATMOSPHERE_PRESETS[preset_key]["scene"])


def build_background_prompt(preset_key: str) -> str:
    """Промпт режима B: пустой атмосферный фон без товара."""
    return build_background_prompt_for_scene(ATMOSPHERE_PRESETS[preset_key]["scene"])


def build_angle_prompt(preset_key: str) -> str:
    """Research-only novel-view prompt using a safe atmosphere preset."""
    return build_angle_prompt_for_scene(ATMOSPHERE_PRESETS[preset_key]["scene"])


def build_native_scene_prompt(preset_key: str) -> str:
    """Research-only native scene prompt using a safe atmosphere preset."""
    return build_native_scene_prompt_for_scene(ATMOSPHERE_PRESETS[preset_key]["scene"])


def sanitize_prompt(text: str) -> str:
    """Заменяет рискованную лексику на нейтральную перед вставкой в промпт."""
    out = text or ""
    for pattern, replacement in _SANITIZE_REPLACEMENTS:
        out = pattern.sub(replacement, out)
    return out
