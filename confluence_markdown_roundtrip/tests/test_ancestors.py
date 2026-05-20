"""Phase 8 — ancestor pull (vertical slice) tests.

Live tests pull from the Automated test area; pages above the test area
(the read-only reference fixture and the space homepage) are pulled
into workspaces but never PUT to — that constraint is enforced by
every AN test that mutates anything.

Offline tests use httpx.MockTransport for deterministic behavior.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "confluence_markdown_roundtrip.cli", *args],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# AN6 — endpoint contract (offline mock)
# ---------------------------------------------------------------------------


class TestAN6AncestorEndpointDoesNotPaginate:
    """Lock the no-pagination contract: list_ancestors() consumes one
    response and trusts the chain fits. No _links.next walk, no `limit`
    parameter sent."""

    def test_single_request_no_cursor_walk(self):
        from confluence_markdown_roundtrip.api import ConfluenceClient, Credentials

        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            assert "/ancestors" in str(request.url), request.url
            # Crucial: even if the doc claims a `next` URL exists, we must
            # not follow it. Emit one to prove the client ignores it.
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"id": "1001", "type": "page"},
                        {"id": "1002", "type": "page"},
                        {"id": "1003", "type": "page"},
                    ],
                    "_links": {
                        "next": "/wiki/api/v2/pages/9/ancestors?cursor=DEADBEEF",
                        "base": "https://x/wiki",
                    },
                },
            )

        creds = Credentials(base_url="https://x", email="u", api_token="t")
        client = ConfluenceClient(creds)
        client._client = httpx.Client(
            base_url="https://x",
            transport=httpx.MockTransport(handler),
            auth=(creds.email, creds.api_token),
        )

        ids = client.list_ancestors("9")
        assert ids == ["1001", "1002", "1003"]
        assert len(calls) == 1, f"expected single request, got {calls}"
        # Verify no `limit` parameter was sent.
        assert "limit=" not in calls[0], calls[0]

    def test_filters_non_page_types(self):
        from confluence_markdown_roundtrip.api import ConfluenceClient, Credentials

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"id": "1001", "type": "page"},
                        {"id": "9001", "type": "whiteboard"},
                        {"id": "1002", "type": "page"},
                    ],
                    "_links": {},
                },
            )

        creds = Credentials(base_url="https://x", email="u", api_token="t")
        client = ConfluenceClient(creds)
        client._client = httpx.Client(
            base_url="https://x",
            transport=httpx.MockTransport(handler),
            auth=(creds.email, creds.api_token),
        )

        ids = client.list_ancestors("9")
        assert ids == ["1001", "1002"]


# ---------------------------------------------------------------------------
# AN8 — permission-denied intermediate ancestor (offline mock)
# ---------------------------------------------------------------------------


class TestAN8PermissionDeniedAncestorSkipped:
    """Working assumption: the ancestors endpoint returns 200 with only
    the readable ancestors when intermediate ones are ACL-restricted (i.e.
    silent gap). Confirm pull_subtree degrades gracefully if a `get_page`
    for one of those ids comes back 404/403."""

    def test_get_page_failure_stops_walk_with_warning(self, capsys, tmp_path):
        """When a middle ancestor's get_page fails (e.g. ACL-restricted),
        the walk stops at the failure. Pages below the gap (closer to the
        requested page) survive in the workspace; pages above the gap are
        dropped because their parent metadata can't be reconstructed.
        Workspace's topmost is the shallowest readable ancestor below the
        gap."""
        from confluence_markdown_roundtrip.api import (
            APIError,
            ConfluenceClient,
            Credentials,
            Page,
        )
        from confluence_markdown_roundtrip.subtree import pull_subtree

        # Tree (topmost-first): 9001 → 9002 (denied) → 9003 → 9004 (requested)
        class FakeClient:
            def get_page(self, pid):
                if str(pid) == "9002":
                    raise APIError(403, "GET", "/wiki/api/v2/pages/9002", "forbidden")
                parents = {"9001": None, "9003": "9002", "9004": "9003"}
                return Page(
                    id=str(pid),
                    title=f"Page {pid}",
                    space_id="S",
                    parent_id=parents.get(str(pid)),
                    version=1,
                    storage_body=f"<p>body {pid}</p>",
                )

            def list_ancestors(self, pid):
                return ["9001", "9002", "9003"]

            def list_descendants(self, pid, *, depth=None):
                return iter([])

            def list_attachments(self, pid):
                return iter([])

        client = FakeClient()
        pull_subtree(client, "9004", tmp_path)  # type: ignore[arg-type]

        captured = capsys.readouterr()
        assert "9002" in captured.err, captured.err
        assert "stopping ancestor walk" in captured.err, captured.err

        # Manifest exists; topmost is 9003 (shallowest readable below the gap).
        manifest_path = tmp_path / "_meta" / "_subtree.json"
        assert manifest_path.exists()
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert data["root_page_ids"] == ["9003"], data
        pids = {p["page_id"] for p in data["pages"]}
        assert pids == {"9003", "9004"}, pids
        # Workspace dir layout: <tmp>/page-9003/page-9004/index.md (slugs derive
        # from titles; check the requested page exists somewhere on disk).
        requested = [p for p in data["pages"] if p["page_id"] == "9004"][0]
        assert (tmp_path / requested["path"]).exists()


# ---------------------------------------------------------------------------
# Live tests — gated by --integration
# ---------------------------------------------------------------------------


pytestmark_live = pytest.mark.online


@pytest.fixture
def grandchild_id(baselines):
    return baselines["grandchild"]["page_id"]


@pytest.fixture
def child_a_id(baselines):
    return baselines["child_a"]["page_id"]


@pytest.fixture
def test_area_id(baselines):
    return baselines["root"]["page_id"]


# ---------------------------------------------------------------------------
# AN1 — pull --subtree on non-root page includes ancestors
# ---------------------------------------------------------------------------


@pytest.mark.online
def test_AN1_subtree_pull_includes_ancestors(
    grandchild_id, child_a_id, test_area_id, tmp_path, restore
):
    """Pull --subtree on a deep page; manifest must contain the full chain
    upward to the space homepage, plus the requested page, plus any
    descendants. Verify parent_id chain reconstructs the hierarchy."""
    restore("grandchild")
    out = _run("pull", grandchild_id, "--subtree", "--into", str(tmp_path))
    assert out.returncode == 0, out.stderr

    manifest_path = tmp_path / "_meta" / "_subtree.json"
    assert manifest_path.exists()
    data = json.loads(manifest_path.read_text(encoding="utf-8"))

    # The chain to the space homepage is 4 ancestors deep + grandchild itself
    # = 5 pages (verified by live probe 2026-05-19). Tolerate >= 5 to keep
    # the test robust if extra ancestor pages are inserted above the test area.
    assert len(data["pages"]) >= 5, len(data["pages"])

    by_pid = {p["page_id"]: p for p in data["pages"]}
    # Grandchild is in the manifest.
    assert grandchild_id in by_pid
    # Its known ancestors are in the manifest too.
    assert child_a_id in by_pid
    assert test_area_id in by_pid

    # parent_id chain reconstructs: grandchild's parent is child_a; child_a's
    # parent is test_area.
    assert by_pid[grandchild_id]["parent_id"] == child_a_id
    assert by_pid[child_a_id]["parent_id"] == test_area_id

    # The topmost ancestor has parent_id = None.
    roots = [p for p in data["pages"] if p["parent_id"] is None]
    assert len(roots) == 1, [p["page_id"] for p in roots]
    assert data["root_page_ids"] == [roots[0]["page_id"]]

    # Every entry's path exists on disk.
    for entry in data["pages"]:
        assert (tmp_path / entry["path"]).exists(), f"missing {entry['path']}"


# ---------------------------------------------------------------------------
# AN3 — workspace manifest at <into>/_meta/_subtree.json (not per-page)
# ---------------------------------------------------------------------------


@pytest.mark.online
def test_AN3_workspace_manifest_at_into_root(grandchild_id, tmp_path, restore):
    restore("grandchild")
    out = _run("pull", grandchild_id, "--subtree", "--into", str(tmp_path))
    assert out.returncode == 0, out.stderr

    # Manifest at workspace root, NOT inside any page's _meta/.
    assert (tmp_path / "_meta" / "_subtree.json").exists()

    # Per-page _meta dirs exist for each page in the manifest, none of
    # which contain a _subtree.json.
    data = json.loads((tmp_path / "_meta" / "_subtree.json").read_text())
    for entry in data["pages"]:
        page_dir = tmp_path / Path(entry["path"]).parent
        assert (page_dir / "_meta" / "index.conf.json").exists(), page_dir
        # The page's own _meta/ must NOT contain _subtree.json.
        assert not (page_dir / "_meta" / "_subtree.json").exists(), page_dir

    # CLI's stdout points at the requested page's directory.
    target = Path(out.stdout.strip().splitlines()[-1])
    by_pid = {p["page_id"]: p for p in data["pages"]}
    expected = tmp_path / Path(by_pid[grandchild_id]["path"]).parent
    assert target == expected, (target, expected)


# ---------------------------------------------------------------------------
# AN4 — leaf-first push includes ancestors but never PUTs outside test scope
# ---------------------------------------------------------------------------


@pytest.mark.online
def test_AN4_leaf_first_push_includes_ancestors(
    child_a_id, grandchild_id, test_area_id, tmp_path, restore, live_client
):
    """Pull --subtree Child Alpha. The slice includes ancestors above the
    test area (the reference fixture and the space homepage) — those are
    pulled but must never be PUT to. Edit Grandchild Charlie (descendant),
    Child Alpha (requested), and Automated test area (ancestor inside test
    scope). Verify push order is grandchild → child_a → test_area, and
    the two above-scope ancestors are not in the push lines."""
    for logical in ("root", "child_a", "grandchild"):
        restore(logical)

    out = _run("pull", child_a_id, "--subtree", "--into", str(tmp_path))
    assert out.returncode == 0, out.stderr

    manifest = json.loads((tmp_path / "_meta" / "_subtree.json").read_text())
    by_pid = {p["page_id"]: p for p in manifest["pages"]}

    # Mutate only pages inside the Automated test area.
    safe_to_mutate = {test_area_id, child_a_id, grandchild_id}
    for pid in safe_to_mutate:
        entry = by_pid[pid]
        index_md = tmp_path / entry["path"]
        index_md.write_text(
            index_md.read_text(encoding="utf-8") + f"\n\nAN4 edit on {pid}.\n",
            encoding="utf-8",
        )

    push = _run("push", str(tmp_path))
    assert push.returncode == 0, push.stderr

    push_lines = [ln for ln in push.stdout.splitlines() if "pushed ->" in ln]
    pushed_paths = [ln.split(":", 1)[0] for ln in push_lines]
    pushed_pids: set[str] = set()
    for pid, entry in by_pid.items():
        page_dir_str = str(tmp_path / Path(entry["path"]).parent)
        if page_dir_str in pushed_paths:
            pushed_pids.add(pid)

    # Exactly the safe-to-mutate set was pushed.
    assert pushed_pids == safe_to_mutate, (pushed_pids, safe_to_mutate)

    # Push order: deepest first → grandchild before child_a before test_area.
    def order(pid: str) -> int:
        target = str(tmp_path / Path(by_pid[pid]["path"]).parent)
        return pushed_paths.index(target)

    assert order(grandchild_id) < order(child_a_id) < order(test_area_id)


# ---------------------------------------------------------------------------
# AN7 — Phase 9: cross-tree pulls append to root_page_ids (no abort)
# ---------------------------------------------------------------------------


@pytest.mark.online
def test_AN7_cross_tree_pull_appends_to_root_set(grandchild_id, tmp_path, restore):
    """Phase 9 inverts Phase 8 AN7: a pull whose topmost ancestor differs
    from any existing workspace root **appends a new tree** to the forest
    rather than aborting. We simulate the cross-tree case by tampering
    the manifest's root_page_ids to a bogus id, then re-pulling — the
    pull must succeed and end with both ids in root_page_ids."""
    restore("grandchild")
    out = _run("pull", grandchild_id, "--subtree", "--into", str(tmp_path))
    assert out.returncode == 0, out.stderr

    manifest_path = tmp_path / "_meta" / "_subtree.json"
    data = json.loads(manifest_path.read_text())
    actual_root = data["root_page_ids"][0]
    # Replace with a bogus id so the next pull's topmost won't match.
    # The next pull's real topmost should then be APPENDED.
    data["root_page_ids"] = ["99999999999999"]
    # Add a synthetic page entry so the bogus id has on-disk presence;
    # otherwise the forest walker silently drops the orphan root.
    data["pages"].append({
        "page_id": "99999999999999",
        "path": "phantom/index.md",
        "parent_id": None,
        "title": "Phantom",
        "slug": "phantom",
    })
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    out2 = _run("pull", grandchild_id, "--subtree", "--into", str(tmp_path))
    assert out2.returncode == 0, (out2.returncode, out2.stderr)

    new_data = json.loads(manifest_path.read_text())
    # Both roots are present.
    assert "99999999999999" in new_data["root_page_ids"]
    assert actual_root in new_data["root_page_ids"]


# ---------------------------------------------------------------------------
# AN_additional — same-tree re-pull is additive (no abort, no new root)
# ---------------------------------------------------------------------------


@pytest.mark.online
def test_AN_additive_repull_same_tree(grandchild_id, child_a_id, tmp_path, restore):
    """Pulling a different page within the same vertical slice into the
    same --into must succeed AND not add a duplicate root."""
    for logical in ("root", "child_a", "grandchild"):
        restore(logical)

    out1 = _run("pull", grandchild_id, "--subtree", "--into", str(tmp_path))
    assert out1.returncode == 0, out1.stderr
    manifest1 = json.loads((tmp_path / "_meta" / "_subtree.json").read_text())
    roots1 = manifest1["root_page_ids"]
    assert len(roots1) == 1, roots1

    out2 = _run("pull", child_a_id, "--subtree", "--into", str(tmp_path))
    assert out2.returncode == 0, out2.stderr
    manifest2 = json.loads((tmp_path / "_meta" / "_subtree.json").read_text())
    # Still one root — same tree as before.
    assert manifest2["root_page_ids"] == roots1
    pids = {p["page_id"] for p in manifest2["pages"]}
    assert grandchild_id in pids
    assert child_a_id in pids
