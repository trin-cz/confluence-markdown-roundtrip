"""Category A — round-trip identity (plan §"Category A").

For each of the 4 bootstrapped pages, verify that pulling and then
pushing-unchanged is a no-op:
- The fast dirty check short-circuits (no PUT issued).
- The CLI exits 0 and reports "clean".
- Confluence's version number is unchanged after the push.
"""

from __future__ import annotations

import pytest


@pytest.mark.online
@pytest.mark.parametrize("logical", ["root", "child_a", "child_b", "grandchild"])
def test_roundtrip_identity(
    logical, baselines, restore, make_workspace, push, live_client
):
    # Ensure the page is at its baseline (any prior test may have mutated it).
    restore(logical)
    baseline = baselines[logical]
    pre_version = live_client.get_page_version(baseline["page_id"])

    ws = make_workspace(baseline["page_id"])

    r = push(ws)
    assert r.returncode == 0, f"push failed: {r.stderr}"
    assert "clean" in r.stdout, f"expected clean short-circuit, got: {r.stdout}"

    # Server-side version must not advance.
    post_version = live_client.get_page_version(baseline["page_id"])
    assert post_version == pre_version, f"push bumped version: {pre_version} -> {post_version}"
