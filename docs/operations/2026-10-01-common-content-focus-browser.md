# Common content editor browser acceptance continuation

This packet strengthens the synthetic browser fixture and its report protocol for the common product editor. It covers the empty editor and the two keyboard focus-loss cases identified in the selected-photo reorder and preview-cancel flows.

The fixture now records exactly 28 layout rows: `empty` and `selected` editor states at 320, 360, 390, 768, 1024, 1280, and 1440 CSS pixels in light and dark themes. Every row carries its state, kind, width, theme, and measured no-overflow result. Empty-route rows assert the internal catalog link, use Tab from the header's return link to verify it is keyboard reachable without activation, and accumulate API and mutation deltas; both must remain zero.

Keyboard observations identify the active same-photo move control after reaching the first and last photo positions, and the preview action restored after keyboard-cancelling a review. Each observation records whether the target is supported, enabled, visible, `:focus-visible`, and has a visible computed outline, plus its computed rectangle and viewport. The report runner rejects absent/duplicate matrix tuples, missing or failed named checks, missing/unsupported focus telemetry, an incorrect boundary direction, invisible outlines, and out-of-viewport focus geometry.

The existing synthetic write contract stays fixed at four preview POSTs, two apply POSTs, one expected conflict for each endpoint, one intentional empty-description override request, and zero provider attempts. The browser fixture continues to assert the persisted photo order `SECOND_PHOTO, PHOTO` and an unchanged channel draft.

Validation performed on the isolated clone based at `e44df456be0cf7dc4cdf160aea9a22ad0f2acd64`:

- `/usr/bin/python3 -m py_compile scripts/check_ux01.py tests/test_ux01_runner.py tests/ux01/common_content_browser.py` — passed.
- `/home/athebyme/super-octo-broccoli/venv/bin/python -m pytest -q tests/test_ux01_runner.py` — 15 passed, 19 subtests passed, no skips.
- `git diff --check` — passed.

The actual browser fixture was not run, and this report does not claim a browser pass or screenshots. The recorded environment evidence in [environment-blocks.json](../design/ozon-ux-completion-artifacts-2026-10-01/environment-blocks.json) shows loopback binding denied and Chromium blocked at startup by the restricted crashpad socket (`TargetClosedError`, `page_created: false`). No workaround was attempted. Actual DOM, responsive geometry, and screenshots remain unverified until the combined source can run in an environment with the approved synthetic browser fixture available.
