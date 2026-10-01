"""Guard semantic theme text and action color pairs against WCAG AA regressions."""

from __future__ import annotations

import re
from pathlib import Path


BASE_TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "base.html"
CSS = re.sub(r"/\*.*?\*/", "", BASE_TEMPLATE.read_text(encoding="utf-8"), flags=re.S)


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
    for theme in themes:
        _assert_pair(theme, "--text-muted", "--bg-card")
        _assert_pair(theme, "--text-muted", "--bg")
        _assert_pair(theme, "--accent-text", "--accent-light")
        _assert_pair(theme, "--accent-text", "--bg-card")

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
