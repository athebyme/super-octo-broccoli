"""Offline Chromium geometry reproduction for the real WB analytics page.

The Jinja shell/page are loaded from a selected git revision or worktree. Local
CSS is inlined from that same source, while versioned CDN assets are fulfilled
from the checked-in hash-pinned browser fixture. The only application JSON is
synthetic analytics data served by this local test app.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import threading
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

os.environ.setdefault("SKIP_SCHEDULER", "1")

from flask import Flask, Response, abort, jsonify, render_template
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server


REPOSITORY = Path(__file__).resolve().parents[2]
ASSETS = REPOSITORY / "tests/ozon_release/assets"
ASSET_MANIFEST = ASSETS / "manifest.json"
WIDTHS = (320, 390, 768, 1024, 1280, 1440)
LONG_LABEL = "Заказы за весь доступный период по выбранному кабинету Wildberries"
LARGE_VALUE = 987654321098765


def source_bytes(path: str, source: str) -> bytes:
    relative = PurePosixPath(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Unsafe source path: {path}")
    if source == "worktree":
        return (REPOSITORY / str(relative)).read_bytes()
    return subprocess.check_output(
        ["git", "show", f"{source}:{relative.as_posix()}"], cwd=REPOSITORY,
    )


def source_text(path: str, source: str) -> str:
    return source_bytes(path, source).decode("utf-8")


def _inline_source_styles(rendered: str, source: str, hashes: dict[str, str]) -> str:
    def replace_link(match: re.Match[str]) -> str:
        tag = match.group(0)
        rel = re.search(r"\brel=['\"]([^'\"]+)['\"]", tag, re.IGNORECASE)
        href = re.search(r"\bhref=['\"]([^'\"]+)['\"]", tag, re.IGNORECASE)
        if not rel or "stylesheet" not in rel.group(1).split() or not href:
            return tag
        parsed = urlsplit(href.group(1))
        if parsed.netloc or not parsed.path.startswith("/static/") or not parsed.path.endswith(".css"):
            return tag
        relative = unquote(parsed.path.removeprefix("/"))
        payload = source_bytes(relative, source)
        hashes[relative] = hashlib.sha256(payload).hexdigest()
        css = payload.decode("utf-8")
        label = relative.replace('"', "")
        return f'<style data-ux01-source-css="{label}">\n{css}\n</style>'

    return re.sub(r"<link\b[^>]*>", replace_link, rendered, flags=re.IGNORECASE)


def synthetic_summary() -> dict:
    return {
        "data": {
            "kpi": {
                "revenue": LARGE_VALUE,
                "orders": LARGE_VALUE,
                "avgCheck": LARGE_VALUE,
                "buyouts": LARGE_VALUE,
                "cancels": LARGE_VALUE,
                "openCardCount": LARGE_VALUE,
                "addToCartCount": LARGE_VALUE,
            },
            "dynamics": {"revenue": None, "orders": None, "buyouts": None},
            "conversions": {
                "addToCartPercent": 87.6,
                "cartToOrderPercent": 76.5,
                "buyoutPercent": 65.4,
            },
            "topProducts": [],
            "dailyData": [],
            "created_at": "2026-09-30T12:00:00Z",
        }
    }


def build_app(source: str, report: dict) -> Flask:
    app = Flask(__name__, static_folder=None, template_folder=str(REPOSITORY / "templates"))
    app.config.update(TESTING=True, SECRET_KEY="ux01-synthetic-browser",
                      MARKETPLACE_OZON_ENABLED=False)
    app.jinja_loader = ChoiceLoader([
        DictLoader({
            "base.html": source_text("templates/base.html", source),
            "analytics.html": source_text("templates/analytics.html", source),
        }),
        FileSystemLoader(str(REPOSITORY / "templates")),
    ])

    template_hashes = {
        "templates/base.html": hashlib.sha256(
            source_bytes("templates/base.html", source)).hexdigest(),
        "templates/analytics.html": hashlib.sha256(
            source_bytes("templates/analytics.html", source)).hexdigest(),
    }
    stylesheet_hashes: dict[str, str] = {}
    report["source_hashes"] = {"templates": template_hashes, "stylesheets": stylesheet_hashes}

    def fixture_url_for(endpoint: str, *args, **kwargs) -> str:
        if endpoint == "static":
            return "/static/" + str(kwargs.get("filename", ""))
        suffix = "/" + "/".join(str(arg) for arg in args) if args else ""
        return "/synthetic/" + endpoint + suffix

    synthetic_user = SimpleNamespace(
        is_authenticated=True,
        is_admin=False,
        username="synthetic-seller",
        seller=SimpleNamespace(id=101, company_name="Synthetic seller"),
    )

    @app.get("/analytics")
    def analytics_page():
        rendered = render_template(
            "analytics.html",
            current_user=synthetic_user,
            url_for=fixture_url_for,
            csrf_token=lambda: "synthetic-csrf-token",
            get_flashed_messages=lambda **_kwargs: [],
        )
        return _inline_source_styles(rendered, source, stylesheet_hashes)

    @app.route("/static/<path:filename>", methods=["GET", "HEAD"])
    def static_fixture(filename: str):
        try:
            payload = source_bytes("static/" + filename, source)
        except (FileNotFoundError, subprocess.CalledProcessError, ValueError):
            abort(404)
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return Response(payload, mimetype=content_type)

    @app.route("/api/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def api_fixture(path: str):
        call = {"method": request_method(), "path": "/api/" + path}
        report["api_calls"].append(call)
        if call["method"] != "GET":
            report["blocked_writes"].append(call)
            return jsonify({"error": "Synthetic browser fixture blocks writes"}), 405
        if path == "analytics/summary":
            return jsonify(synthetic_summary())
        if path == "analytics/products":
            return jsonify({"data": {"items": []}})
        if path == "notifications/unread-count":
            return jsonify({"unread_count": 0})
        if path == "tasks/tray":
            return jsonify({"items": [], "count": 0})
        report["unexpected_api_calls"].append(call)
        return jsonify({"error": "Unexpected API request in synthetic fixture"}), 404

    @app.get("/favicon.ico")
    def no_favicon():
        return Response(status=204)

    return app


def request_method() -> str:
    from flask import request
    return request.method


def load_pinned_assets() -> tuple[dict, dict[str, dict]]:
    manifest = json.loads(ASSET_MANIFEST.read_text(encoding="utf-8"))
    by_url = {}
    digests = {}
    for url, item in manifest.items():
        path = ASSETS / item["file"]
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != item["sha256"]:
            raise AssertionError(f"Pinned browser asset hash mismatch: {url}")
        by_url[url] = {**item, "payload": payload}
        digests[url] = digest
    return by_url, digests


def _measure(page, source: str, width: int, theme: str, sidebar: str, text_scale: int) -> dict:
    return page.evaluate(
        """({source, width, theme, sidebar, textScale}) => {
            const rect = node => {
                if (!node) return null;
                const r = node.getBoundingClientRect();
                return {left: Math.round(r.left), right: Math.round(r.right),
                        top: Math.round(r.top), width: Math.round(r.width),
                        height: Math.round(r.height)};
            };
            const textBounds = node => {
                if (!node) return null;
                const range = document.createRange();
                range.selectNodeContents(node);
                const boxes = Array.from(range.getClientRects());
                if (!boxes.length) return null;
                return {left: Math.round(Math.min(...boxes.map(r => r.left))),
                    right: Math.round(Math.max(...boxes.map(r => r.right))),
                    lineBoxes: boxes.length};
            };
            const sidebarNode = document.querySelector('.sidebar');
            const main = document.querySelector('.main-content');
            const workspace = document.querySelector('.analytics-workspace')
                || document.querySelector('#main-content > div');
            const grid = document.querySelector('.sh-stat-grid--4');
            const cards = Array.from(grid ? grid.querySelectorAll('.sh-stat-card') : [])
                .map(card => {
                    const value = card.querySelector('.sh-stat-value');
                    const label = card.querySelector('.sh-stat-label');
                    return {card: rect(card), label: label && label.textContent,
                        labelBounds: textBounds(label),
                        labelClientWidth: label?.clientWidth || 0,
                        labelScrollWidth: label?.scrollWidth || 0,
                        value: value && value.textContent,
                        valueBounds: textBounds(value),
                        valueClientWidth: value?.clientWidth || 0,
                        valueScrollWidth: value?.scrollWidth || 0,
                        valueRect: rect(value)};
                });
            const tableWrap = document.querySelector('.analytics-table-scroll')
                || document.querySelector('.sh-table-wrap');
            const pageScrollWidth = document.documentElement.scrollWidth;
            const offenders = Array.from(document.querySelectorAll('body *'))
                .map(node => ({node, r: node.getBoundingClientRect()}))
                .filter(({node, r}) => r.width > 0 && r.right > innerWidth + 1
                    && !node.closest('.sidebar'))
                .slice(0, 8)
                .map(({node, r}) => ({tag: node.tagName,
                    className: typeof node.className === 'string' ? node.className.slice(0, 80) : '',
                    right: Math.round(r.right), width: Math.round(r.width),
                    text: (node.innerText || '').slice(0, 70)}));
            return {source, viewport: {width, innerWidth, pageScrollWidth,
                    overflowsViewport: pageScrollWidth > innerWidth + 1},
                requestedTheme: theme, actualTheme: document.documentElement.dataset.theme,
                sidebarState: sidebar,
                sidebarRect: rect(sidebarNode), sidebarComputedWidth: getComputedStyle(sidebarNode).width,
                mainContentRect: rect(main), workspaceRect: rect(workspace), kpiGridRect: rect(grid),
                cards, tableScroll: tableWrap ? {clientWidth: tableWrap.clientWidth,
                    scrollWidth: tableWrap.scrollWidth,
                    overflowX: getComputedStyle(tableWrap).overflowX,
                    role: tableWrap.getAttribute('role'),
                    ariaLabel: tableWrap.getAttribute('aria-label'),
                    tabIndex: tableWrap.getAttribute('tabindex')} : null,
                pageOverflowX: getComputedStyle(document.documentElement).overflowX,
                bodyOverflowX: getComputedStyle(document.body).overflowX,
                textScale, offenders};
        }""",
        {"source": source, "width": width, "theme": theme,
         "sidebar": sidebar, "textScale": text_scale},
    )


def _failure_diagnostics(page) -> dict:
    return page.evaluate("""() => {
        const box = node => {
            if (!node) return null;
            const r = node.getBoundingClientRect();
            const css = getComputedStyle(node);
            return {
                left: r.left, right: r.right, width: r.width,
                clientWidth: node.clientWidth, scrollWidth: node.scrollWidth,
                marginLeft: css.marginLeft, transition: css.transition,
                transform: css.transform, boxSizing: css.boxSizing,
                display: css.display
            };
        };
        const main = document.querySelector('.main-content');
        const sidebar = document.querySelector('.sidebar');
        const root = document.documentElement;
        return {
            viewport: {innerWidth, rootClientWidth: root.clientWidth,
                rootScrollWidth: root.scrollWidth,
                bodyClientWidth: document.body.clientWidth,
                bodyScrollWidth: document.body.scrollWidth},
            main: box(main), sidebar: box(sidebar),
            sidebarClasses: sidebar?.className,
            bodyMargin: getComputedStyle(document.body).margin,
            bodyFont: getComputedStyle(document.body).fontFamily,
            documentFont: getComputedStyle(root).fontFamily,
            alpineReady: !!window.Alpine,
            chartReady: typeof window.Chart === 'function',
            chartVersion: window.Chart?.version || null,
            canvas: Array.from(document.querySelectorAll('canvas')).map(box),
            inlinedStyles: Array.from(document.querySelectorAll('[data-ux01-source-css]'))
                .map(node => node.getAttribute('data-ux01-source-css')),
            stylesheetCount: document.styleSheets.length,
            theme: root.dataset.theme,
            reducedMotion: matchMedia('(prefers-reduced-motion: reduce)').matches
        };
    }""")


def _after_failures(row: dict) -> list[str]:
    failures = []
    if row.get("requestedTheme") != row.get("actualTheme"):
        failures.append("requested_theme_not_applied")
    if row["viewport"]["overflowsViewport"]:
        failures.append("root_horizontal_overflow")
    if row["pageOverflowX"] == "hidden" or row["bodyOverflowX"] == "hidden":
        failures.append("page_wide_overflow_hidden")
    grid = row["kpiGridRect"]
    if not grid or len(row["cards"]) != 4:
        failures.append("primary_kpi_grid_missing")
    else:
        for index, card in enumerate(row["cards"]):
            if card["card"]["left"] < grid["left"] - 1 or card["card"]["right"] > grid["right"] + 1:
                failures.append(f"kpi_card_{index}_outside_grid")
            if card["valueScrollWidth"] > card["valueClientWidth"] + 1:
                failures.append(f"kpi_value_{index}_clipped")
            if card["labelScrollWidth"] > card["labelClientWidth"] + 1:
                failures.append(f"kpi_label_{index}_clipped")
    local = row["tableScroll"]
    if not local or local["overflowX"] not in ("auto", "scroll"):
        failures.append("table_missing_local_horizontal_scroll")
    elif not local["role"] == "region" or not local["ariaLabel"] or local["tabIndex"] != "0":
        failures.append("table_scroll_region_not_accessibly_labelled")
    return failures


def run(source: str, report_path: Path, artifacts_dir: Path, chromium: str | None) -> dict:
    report = {
        "status": "running",
        "source": source,
        "scope": "synthetic_real_jinja_shell_and_analytics_local_http",
        "widths": list(WIDTHS),
        "themes": ["light", "dark"],
        "sidebar_states": ["open", "collapsed"],
        "text_scales_percent": [100, 200],
        "api_calls": [],
        "unexpected_api_calls": [],
        "blocked_writes": [],
        "provider_attempts": 0,
        "unexpected_external_requests": [],
        "javascript_errors": [],
        "layouts": [],
        "after_failures": [],
    }
    manifest, pinned_hashes = load_pinned_assets()
    report["pinned_asset_hashes"] = pinned_hashes
    app = build_app(source, report)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    browser = None
    page = None
    current_case = {"stage": "launch"}

    try:
        with sync_playwright() as playwright:
            launch_options = {
                "headless": True,
                "args": ["--no-sandbox", "--disable-dev-shm-usage"],
            }
            if chromium:
                launch_options["executable_path"] = chromium
            browser = playwright.chromium.launch(**launch_options)
            context = browser.new_context(
                viewport={"width": 1440, "height": 1100},
                reduced_motion="reduce",
                service_workers="block",
            )

            def bridge(route):
                request = route.request
                parsed = urlsplit(request.url)
                if parsed.hostname == "127.0.0.1" and parsed.port == server.server_port:
                    if parsed.path == "/analytics" or parsed.path.startswith("/static/") or parsed.path.startswith("/api/") or parsed.path == "/favicon.ico":
                        if parsed.path.startswith("/api/") and request.method != "GET":
                            report["blocked_writes"].append({"method": request.method, "path": parsed.path})
                            route.abort()
                            return
                        route.continue_()
                        return
                    report["unexpected_api_calls"].append({"method": request.method, "path": parsed.path})
                    route.abort()
                    return

                asset = manifest.get(request.url)
                if not asset:
                    parsed_asset_url = urlsplit(request.url)
                    if parsed_asset_url.path in ("", "/"):
                        without_query = f"{parsed_asset_url.scheme}://{parsed_asset_url.netloc}"
                    else:
                        without_query = request.url.split("?", 1)[0]
                    asset = manifest.get(without_query)
                if asset:
                    route.fulfill(
                        status=200,
                        body=asset["payload"],
                        content_type=asset["content_type"],
                        headers={"access-control-allow-origin": "*"},
                    )
                    return
                report["unexpected_external_requests"].append({
                    "url": request.url,
                    "method": request.method,
                })
                route.abort()

            context.route("**/*", bridge)
            context.add_init_script("""
                (() => {
                    const query = new URL(location.href).searchParams;
                    const theme = query.get('__ux_theme');
                    const sidebar = query.get('__ux_sidebar');
                    if (theme === 'light' || theme === 'dark') localStorage.setItem('sh-theme', theme);
                    if (sidebar === 'open' || sidebar === 'collapsed') {
                        localStorage.setItem('sh-sidebar', sidebar === 'open' ? 'open' : 'closed');
                    }
                })();
            """)
            page = context.new_page()
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: report["javascript_errors"].append(str(error)))
            report["measurement_note"] = (
                "Each viewport/theme/sidebar state loads in a fresh page to avoid "
                "responsive canvas state accumulating across cases. The synthetic "
                "fixture disables only the main-content margin transition to measure "
                "settled geometry; reduced motion is enabled and two RAFs are awaited. "
                "Text 200% doubles each element's computed font size for that state."
            )

            for width in WIDTHS:
                current_case["stage"] = "viewport"
                current_case["width"] = width
                page.set_viewport_size({"width": width, "height": 1100})
                for theme in ("light", "dark"):
                    for sidebar in ("open", "collapsed"):
                        current_case.update({"stage": "navigate", "theme": theme,
                                             "sidebar": sidebar})
                        response = page.goto(
                            base + f"/analytics?__ux_theme={theme}&__ux_sidebar={sidebar}",
                            wait_until="networkidle",
                        )
                        if not response or response.status != 200:
                            raise AssertionError(("synthetic analytics route failed",
                                                  response.status if response else None))
                        # The real shell animates `margin-left` for 200 ms. The
                        # fixture measures settled geometry, so suppress that
                        # transition locally and wait two frames below.
                        page.add_style_tag(content=".main-content { transition: none !important; }")
                        page.locator(".sh-stat-grid--4").wait_for()
                        page.wait_for_function("""() => {
                            const grid = document.querySelector('.sh-stat-grid--4');
                            const canvas = document.querySelector('canvas');
                            return grid && canvas && canvas.getBoundingClientRect().width > 0
                                && document.querySelectorAll('.sh-stat-grid--4 .sh-stat-value').length === 4;
                        }""")
                        page.evaluate("document.fonts.ready")
                        page.evaluate("""() => {
                            const grid = document.querySelector('.sh-stat-grid--4');
                            grid.querySelector('.sh-stat-label').textContent = %s;
                            const value = new Intl.NumberFormat('ru-RU', {
                                style: 'currency', currency: 'RUB', maximumFractionDigits: 0
                            }).format(%d);
                            grid.querySelectorAll('.sh-stat-value').forEach(node => node.textContent = value);
                            const table = document.querySelector('.sh-table');
                            if (table) table.style.minWidth = '760px';
                            window.__ux01OriginalFontStyles = new Map(
                                Array.from(document.querySelectorAll('body *')).map(node => [
                                    node, {
                                        inline: node.style.getPropertyValue('font-size'),
                                        priority: node.style.getPropertyPriority('font-size')
                                    }
                                ])
                            );
                        }""" % (json.dumps(LONG_LABEL, ensure_ascii=False), LARGE_VALUE))
                        page.evaluate("""() => {
                            const nodes = Array.from(document.querySelectorAll('body *'));
                            for (const node of nodes) {
                                let original = window.__ux01OriginalFontStyles.get(node);
                                if (!original) {
                                    original = {
                                        inline: node.style.getPropertyValue('font-size'),
                                        priority: node.style.getPropertyPriority('font-size')
                                    };
                                    window.__ux01OriginalFontStyles.set(node, original);
                                }
                                if (original.inline) {
                                    node.style.setProperty('font-size', original.inline, original.priority);
                                } else {
                                    node.style.removeProperty('font-size');
                                }
                            }
                            window.__ux01BaseFontSizes = new Map(nodes.map(node => [
                                node, {
                                    computed: parseFloat(getComputedStyle(node).fontSize),
                                    ...window.__ux01OriginalFontStyles.get(node)
                                }
                            ]));
                        }""")
                        for text_scale in (100, 200):
                            current_case.update({"stage": "wait_for_settled_layout",
                                                 "text_scale": text_scale})
                            page.evaluate("""textScale => {
                                for (const [node, base] of window.__ux01BaseFontSizes) {
                                    if (base.inline) node.style.setProperty('font-size', base.inline, base.priority);
                                    else node.style.removeProperty('font-size');
                                    if (textScale === 200 && Number.isFinite(base.computed) && base.computed > 0) {
                                        node.style.setProperty('font-size', `${base.computed * 2}px`, 'important');
                                    }
                                }
                            }""", text_scale)
                            page.evaluate("""() => new Promise(resolve => requestAnimationFrame(
                                () => requestAnimationFrame(resolve)))""")
                            expected_left = 0 if width <= 1023 else (260 if sidebar == "open" else 72)
                            current_case["expected_main_left"] = expected_left
                            try:
                                page.wait_for_function(
                                    "expected => Math.abs(document.querySelector('.main-content').getBoundingClientRect().left - expected) < 1",
                                    arg=expected_left,
                                )
                            except Exception:
                                report["failure_diagnostics"] = _failure_diagnostics(page)
                                raise
                            current_case["stage"] = "measure"
                            row = _measure(page, source, width, theme, sidebar, text_scale)
                            report["layouts"].append(row)
                            if source == "worktree":
                                failures = _after_failures(row)
                                if width <= 768 and row["tableScroll"] and row["tableScroll"]["scrollWidth"] <= row["tableScroll"]["clientWidth"]:
                                    failures.append("wide_table_not_locally_scrollable")
                                if failures:
                                    report["after_failures"].append({
                                        "width": width, "theme": theme,
                                        "sidebar": sidebar, "text_scale": text_scale,
                                        "failures": failures,
                                    })
                            if width in (390, 1024) and sidebar == "open" and text_scale == 100:
                                screenshot = artifacts_dir / f"analytics-{source}-{theme}-{width}.png"
                                page.screenshot(path=str(screenshot), full_page=True)
                                row["screenshot"] = str(screenshot)

    except Exception as exc:
        report["harness_error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "case": dict(current_case),
        }
        if page and not report.get("failure_diagnostics"):
            try:
                report["failure_diagnostics"] = _failure_diagnostics(page)
            except Exception as diagnostic_error:
                report["failure_diagnostics_error"] = str(diagnostic_error)
    finally:
        # `sync_playwright()` owns and closes the browser before leaving its
        # context; calling `browser.close()` here would use a closed event loop.
        server.shutdown()
        thread.join(timeout=3)

    report["status"] = "complete" if not report["javascript_errors"] and not report.get("harness_error") else "failed"
    if report["unexpected_api_calls"] or report["blocked_writes"] or report["unexpected_external_requests"]:
        report["status"] = "failed"
    if source == "worktree" and report["after_failures"]:
        report["status"] = "failed"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True,
                        help="git revision for frozen templates, or 'worktree'")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--chromium", default=os.environ.get("UX01_BROWSER_CHROMIUM")
                        or os.environ.get("OZON_BROWSER_CHROMIUM"))
    args = parser.parse_args()
    result = run(args.source, args.report, args.artifacts, args.chromium)
    summary = {
        "source": result["source"],
        "status": result["status"],
        "layout_rows": len(result["layouts"]),
        "page_overflow_rows": sum(row["viewport"]["overflowsViewport"] for row in result["layouts"]),
        "after_failure_rows": len(result["after_failures"]),
        "api_calls": len(result["api_calls"]),
        "unexpected_api_calls": len(result["unexpected_api_calls"]),
        "blocked_writes": len(result["blocked_writes"]),
        "unexpected_external_requests": len(result["unexpected_external_requests"]),
        "javascript_errors": len(result["javascript_errors"]),
        "report": str(args.report),
        "artifacts": str(args.artifacts),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
