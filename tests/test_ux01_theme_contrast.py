"""Guard semantic theme text and action color pairs against WCAG AA regressions."""

from __future__ import annotations

import re
from pathlib import Path


BASE_TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "base.html"
ROOT = BASE_TEMPLATE.parents[1]
CSS = re.sub(r"/\*.*?\*/", "", BASE_TEMPLATE.read_text(encoding="utf-8"), flags=re.S)
COMMON_CSS = re.sub(
    r"/\*.*?\*/",
    "",
    (ROOT / "static" / "common-product-content.css").read_text(encoding="utf-8"),
    flags=re.S,
)


def _properties(scope: str) -> dict[str, str]:
    match = re.search(scope + r"\s*\{([^{}]*)\}", CSS)
    assert match, f"missing CSS scope: {scope}"
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", match.group(1)))


def _rule(selector: str) -> str:
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", CSS):
        selectors = [part.strip() for part in match.group(1).split(",")]
        if selector in selectors:
            return match.group(2)
    raise AssertionError(f"missing CSS selector: {selector}")


def _common_rule(selector: str) -> str:
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", COMMON_CSS):
        selectors = [part.strip() for part in match.group(1).split(",")]
        if selector in selectors:
            return match.group(2)
    raise AssertionError(f"missing common-content CSS selector: {selector}")


def _hex(properties: dict[str, str], name: str) -> str:
    value = properties[name].strip()
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", value), f"{name} must remain a literal six-digit color"
    return value


def _luminance(color: str) -> float:
    components = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [component / 12.92 if component <= 0.04045 else ((component + 0.055) / 1.055) ** 2.4 for component in components]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(foreground: str, background: str) -> float:
    lighter, darker = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _assert_pair(properties: dict[str, str], foreground: str, background: str) -> None:
    ratio = _contrast(_hex(properties, foreground), _hex(properties, background))
    assert ratio >= 4.5, f"{foreground} on {background} has contrast {ratio:.2f}:1"


def test_muted_and_accent_text_have_aa_contrast_on_theme_surfaces() -> None:
    themes = (_properties(r":root"), _properties(r'\[data-theme="dark"\]'))
    surfaces = ("--bg-card", "--bg", "--bg-hover", "--accent-light")
    for theme in themes:
        for surface in ("--bg-card", "--bg", "--bg-hover"):
            _assert_pair(theme, "--text-muted", surface)
        for surface in surfaces:
            _assert_pair(theme, "--accent-text", surface)
            assert _contrast(_hex(theme, "--accent"), _hex(theme, surface)) >= 3.0

    for selector in (
        ".text-indigo-600",
        ".text-purple-600",
        ".sh-badge--purple",
        ".sh-quick-card-meta",
        ".sh-toast-action",
    ):
        assert "var(--accent-text)" in _rule(selector), f"{selector} must use the readable semantic accent text token"


def test_action_foregrounds_remain_readable_in_both_themes_and_hover_states() -> None:
    themes = (_properties(r":root"), _properties(r'\[data-theme="dark"\]'))
    for theme in themes:
        for background_token, foreground_token in (
            ("--action-background", "--action-foreground"),
            ("--action-background-hover", "--action-foreground"),
            ("--danger-action-background", "--danger-action-foreground"),
            ("--danger-action-background-hover", "--danger-action-foreground"),
            ("--ok-action-background", "--action-foreground"),
            ("--ok-action-background-hover", "--action-foreground"),
            ("--warn-action-background", "--action-foreground"),
            ("--warn-action-background-hover", "--action-foreground"),
        ):
            _assert_pair(theme, foreground_token, background_token)

    for selector, declarations in (
        (".sh-btn--accent", ("background: var(--action-background)", "color: var(--action-foreground)")),
        (".sh-btn--danger", ("background: var(--danger-action-background)", "color: var(--danger-action-foreground)")),
        (".sh-tab--active .sh-tab-count", ("background: var(--action-background)", "color: var(--action-foreground)")),
        (".sh-pagination-btn--active", ("background: var(--action-background)", "color: var(--action-foreground)")),
        (".sidebar-logo-mark", ("background: var(--action-background)", "color: var(--action-foreground)")),
    ):
        rule = _rule(selector)
        for declaration in declarations:
            assert declaration in rule, f"{selector} must use {declaration}"

    assert "var(--ok-action-background)" in _rule('[class~="bg-green-600"][class~="text-white"]')
    assert "var(--danger-action-background)" in _rule('[class~="bg-red-600"][class~="text-white"]')
    assert "var(--warn-action-background)" in _rule('[class~="bg-yellow-600"][class~="text-white"]')


def test_status_chip_foregrounds_have_aa_contrast_in_both_themes() -> None:
    status_pairs = (
        ("--ok", "--ok-bg"),
        ("--warn", "--warn-bg"),
        ("--danger", "--danger-bg"),
        ("--info", "--info-bg"),
    )
    themes = (_properties(r":root"), _properties(r'\[data-theme="dark"\]'))
    for theme in themes:
        for foreground, background in status_pairs:
            _assert_pair(theme, foreground, background)

    for selector, token in (
        ('.rounded-full[class*="bg-green-"]', "--ok"),
        ('.rounded-full[class*="bg-yellow-"]', "--warn"),
        ('.rounded-full[class*="bg-red-"]', "--danger"),
        ('.rounded-full[class*="bg-blue-"]', "--info"),
    ):
        assert f"color: var({token})" in _rule(selector)


def test_focus_indicators_use_opaque_high_contrast_tokens() -> None:
    themes = (_properties(r":root"), _properties(r'\[data-theme="dark"\]'))
    surfaces = ("--bg", "--bg-card", "--bg-hover", "--accent-light")
    for theme in themes:
        for surface in surfaces:
            ratio = _contrast(_hex(theme, "--focus-outline"), _hex(theme, surface))
            assert ratio >= 3.0, f"focus outline on {surface} has contrast {ratio:.2f}:1"

    for selector in (
        ".sh-dropdown-item:focus-visible",
        ".sh-modal-close:focus-visible",
        ".sh-stat-card--link:focus-visible",
        ".sh-card--interactive:focus-visible",
        ".sh-btn:focus-visible",
        ".sh-tab:focus-visible",
        ".sh-pagination-btn:focus-visible",
        ".sh-quick-card:focus-visible",
        "input:focus",
    ):
        assert "outline: 2px solid var(--focus-outline)" in _rule(selector)

    assert "--tw-ring-color: var(--focus-outline)" in _rule('[class*="ring-indigo"]')
    assert "outline: 2px solid var(--focus-outline)" in _common_rule(".cpc-value-input:focus-visible")
    assert "outline: 2px solid var(--focus-outline)" in _common_rule(".cpc-description-full:focus-visible")

    # The command-palette active marker keeps its separate accent because it already clears 3:1.
    for theme in themes:
        for surface in surfaces:
            assert _contrast(_hex(theme, "--accent"), _hex(theme, surface)) >= 3.0
    assert re.search(r"\.sh-cmdpal-item\.active\s*\{[^}]*outline: 2px solid var\(--accent\)", CSS)


def test_templates_and_static_do_not_use_legacy_text_or_focus_tokens() -> None:
    legacy_focus_outline = re.compile(
        r"(?<![\w-])outline(?:-color)?\s*:[^;{}<>]*?var\(\s*--focus-ring\s*\)",
        re.I | re.S,
    )
    legacy_accent_text = re.compile(
        r"(?<![\w-])color\s*:\s*var\(\s*--accent\s*\)(?![\w-])",
        re.I,
    )
    remaining = {"focus outlines": [], "accent text": []}
    for directory in (ROOT / "templates", ROOT / "static"):
        for path in directory.rglob("*"):
            if path.suffix not in {".html", ".css"}:
                continue
            source = path.read_text(encoding="utf-8")
            source = re.sub(r"/\*.*?\*/|<!--.*?-->", "", source, flags=re.S)
            if legacy_focus_outline.search(source):
                remaining["focus outlines"].append(str(path.relative_to(ROOT)))
            if legacy_accent_text.search(source):
                remaining["accent text"].append(str(path.relative_to(ROOT)))

    assert not remaining["focus outlines"], f"focus outlines still use alpha token: {remaining['focus outlines']}"
    assert not remaining["accent text"], f"text colors still use raw accent token: {remaining['accent text']}"
