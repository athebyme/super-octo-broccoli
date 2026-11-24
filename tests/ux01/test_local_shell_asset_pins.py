"""Source-level checks for the shell's locally pinned browser runtimes."""

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ASSETS = ROOT / "tests/ozon_release/assets"
VENDOR = ROOT / "static/vendor"

PINNED_ASSETS = {
    "tailwindcss-3.4.17-play-cdn.js": "https://cdn.tailwindcss.com",
    "alpine-collapse-3.17.4.min.js": (
        "https://cdn.jsdelivr.net/npm/@alpinejs/collapse@3.x.x/dist/cdn.min.js"
    ),
    "alpine-focus-3.17.4.min.js": (
        "https://cdn.jsdelivr.net/npm/@alpinejs/focus@3.x.x/dist/cdn.min.js"
    ),
    "alpine-3.17.4.global.min.js": (
        "https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"
    ),
}


def test_shell_runtime_bytes_and_licenses_match_verified_fixture_assets():
    manifest = json.loads((FIXTURE_ASSETS / "manifest.json").read_text(encoding="utf-8"))

    for destination_name, source_url in PINNED_ASSETS.items():
        source_record = manifest[source_url]
        source = (FIXTURE_ASSETS / source_record["file"]).read_bytes()
        destination = (VENDOR / destination_name).read_bytes()
        assert destination == source
        assert hashlib.sha256(destination).hexdigest() == source_record["sha256"]

    assert (VENDOR / "TAILWIND-LICENSE.txt").read_bytes() == (
        FIXTURE_ASSETS / "TAILWIND-LICENSE.txt"
    ).read_bytes()
    assert (VENDOR / "ALPINE-LICENSE.txt").read_bytes() == (
        FIXTURE_ASSETS / "ALPINE-LICENSE.txt"
    ).read_bytes()


def test_base_and_login_use_local_assets_in_plugin_initialization_order():
    base = (ROOT / "templates/base.html").read_text(encoding="utf-8")
    login = (ROOT / "templates/login.html").read_text(encoding="utf-8")

    assert "https://cdn.tailwindcss.com" not in base
    assert "https://cdn.jsdelivr.net/npm/@alpinejs/" not in base
    assert "https://cdn.jsdelivr.net/npm/alpinejs@" not in base + login
    assert "vendor/alpine-3.17.4.global.min.js" in login
    assert "https://fonts.googleapis.com/" in base
    assert "https://fonts.googleapis.com/" in login

    asset_positions = [
        base.index("vendor/tailwindcss-3.4.17-play-cdn.js"),
        base.index("vendor/alpine-collapse-3.17.4.min.js"),
        base.index("vendor/alpine-focus-3.17.4.min.js"),
        base.index("sh-ui.js"),
        base.index("vendor/alpine-3.17.4.global.min.js"),
    ]
    assert asset_positions == sorted(asset_positions)
    assert "if (window.tailwind)" in base
    assert "window.tailwind.config = {" in base
