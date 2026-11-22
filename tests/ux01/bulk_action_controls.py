"""Drive the seller workspace's direct and multi-destination actions."""


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


def workspace_action_launcher(page, group):
    launcher = page.locator(f'button[data-bulk-action-group="{group}"]')
    launcher.wait_for()
    return launcher


def invoke_workspace_action(page, key):
    """Follow a real selected-product action, checking its exact destination."""
    launcher = workspace_action_launcher(page, ACTION_GROUP[key])
    assert launcher.is_enabled(), key
    direct = launcher.get_attribute("data-bulk-direct-action")
    if direct:
        assert direct == key, (key, direct)
        launcher.click()
        return 1

    launcher.click()
    destination = page.locator(f'[data-bulk-action-key="{key}"]')
    destination.wait_for()
    assert destination.is_enabled(), key
    destination.click()
    return 2
