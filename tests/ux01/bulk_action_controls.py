"""Drive the seller workspace's direct and multi-destination actions."""

from urllib.parse import urlsplit


ACTION_GROUP = {
    "common_content": "cards",
    "wb_edit": "cards",
    "ozon_workspace_update": "cards",
    "wb_prices": "prices",
    "ozon_prices": "prices",
    "push_wb": "create",
    "ozon": "create",
    "refresh_supplier": "refresh",
}
PRICE_ACTIONS = {"wb_prices", "ozon_prices"}


def workspace_action_launcher(page, group):
    launcher = page.locator(f'button[data-bulk-action-group="{group}"]')
    launcher.wait_for()
    return launcher


def _native_price_dialog_available(page):
    return bool(page.evaluate("""() => {
        const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
        return !!(proxy && proxy.priceWorkspaceDialogAvailable);
    }"""))


def _assert_native_price_entry(page, key):
    if key == "wb_prices":
        dialog = page.locator('dialog[data-workspace-price-dialog][data-price-marketplace="wb"]')
        dialog.wait_for(state="visible")
        return

    page.wait_for_function("""() => {
        const chooser = document.querySelector(
            'dialog[aria-labelledby="mp-ozon-price-dialog-title"]'
        );
        const price = document.querySelector('dialog[data-workspace-price-dialog]');
        return !!((chooser && chooser.open) ||
            (price && price.open && price.dataset.priceMarketplace === 'ozon'));
    }""")


def _is_selection_read(response):
    parsed = urlsplit(response.url)
    return parsed.path == "/api/my-products/selection" and response.request.method == "POST"


def invoke_workspace_action(page, key, *, expected_selection_ids=None, selection_pending=None):
    """Follow a real selected-product action, checking its exact destination."""
    native_price_dialog = key in PRICE_ACTIONS and _native_price_dialog_available(page)
    launcher = workspace_action_launcher(page, ACTION_GROUP[key])
    assert launcher.is_enabled(), key
    action_already_launched = False
    if expected_selection_ids is not None:
        verified = page.evaluate("""() => {
            const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
            return !!(proxy && proxy.selectionRowsVerified);
        }""")
        assert not verified, "This case must exercise verification on the first group click"
        assert launcher.get_attribute("data-selection-intent-verify") is not None
        selected_ids = page.evaluate("""() => {
            const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
            return proxy ? proxy.selected.slice() : null;
        }""")
        assert selected_ids == expected_selection_ids and selected_ids
        with page.expect_response(_is_selection_read) as selection_read:
            launcher.press("Enter")
            if selection_pending is not None:
                selection_pending(launcher)
        response = selection_read.value
        assert response.status == 200
        assert response.request.post_data_json == {"ids": expected_selection_ids}
        payload = response.json()
        assert payload.get("success") is True
        assert [item.get("id") for item in payload.get("items", [])] == expected_selection_ids
        page.wait_for_function("""() => {
            const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
            return proxy && proxy.selectionRowsVerified && !proxy.selectionReviewLoading;
        }""")
        assert page.locator(".mp-selection-review").count() == 0
        page.wait_for_function("""({group, key}) => {
            const launcher = document.querySelector(`button[data-bulk-action-group="${group}"]`);
            const option = document.querySelector(`#mp-bulk-panel-${group} [data-bulk-action-key="${key}"]`);
            return !!(launcher && (launcher.dataset.bulkDirectAction === key || option));
        }""", arg={"group": ACTION_GROUP[key], "key": key})
        launcher = workspace_action_launcher(page, ACTION_GROUP[key])
        direct = launcher.get_attribute("data-bulk-direct-action")
        if direct:
            assert direct == key, (key, direct)
            action_already_launched = True
    else:
        direct = launcher.get_attribute("data-bulk-direct-action")
        if direct:
            assert direct == key, (key, direct)
            launcher.click()
            action_already_launched = True
        else:
            launcher.click()

    if action_already_launched:
        steps = 1
    else:
        destination = page.locator(f'[data-bulk-action-key="{key}"]')
        destination.wait_for()
        assert destination.is_enabled(), key
        destination.click()
        steps = 2

    # Keep this helper on the native drawer by default. Tests for the older,
    # full pricing tools opt into that route with the explicit legacy control.
    if native_price_dialog:
        _assert_native_price_entry(page, key)
    return steps


def invoke_legacy_price_action(page, key, account_id=None):
    """Open an existing full pricing route from the native drawer's own link."""
    if key not in PRICE_ACTIONS:
        raise AssertionError(f"Not a price action: {key!r}")
    native_price_dialog = _native_price_dialog_available(page)
    steps = invoke_workspace_action(page, key)
    if not native_price_dialog:
        # Asset-free acceptance runs retain the classic navigation fallback.
        return steps

    if key == "ozon_prices":
        chooser = page.locator('dialog[aria-labelledby="mp-ozon-price-dialog-title"]')
        if chooser.is_visible():
            assert account_id is not None, "A multi-store Ozon choice must name its exact account"
            account_option_values = chooser.locator("#mp-ozon-price-account option").evaluate_all(
                "options => options.map(option => option.value).filter(Boolean)"
            )
            assert str(account_id) in account_option_values, account_option_values
            chooser.locator("#mp-ozon-price-account").select_option(str(account_id))
            chooser.get_by_role("button", name="Открыть сравнение", exact=True).click()

    dialog = page.locator('dialog[data-workspace-price-dialog]')
    dialog.wait_for(state="visible")
    expected_marketplace = "wb" if key == "wb_prices" else "ozon"
    assert dialog.get_attribute("data-price-marketplace") == expected_marketplace
    legacy = dialog.locator("[data-price-legacy]")
    legacy.wait_for(state="visible")
    assert legacy.is_enabled()
    legacy.click()
    return steps
