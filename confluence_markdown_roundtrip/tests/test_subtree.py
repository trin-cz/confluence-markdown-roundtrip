"""Category D — subtree pull/push/status (plan §"Phase 3 — Subtree + skill packaging").

The 4-page fixture tree (root + 2 children + 1 grandchild) is the live
target. Each test starts by re-running bootstrap on every page (via the
session fixture) and then exercises subtree operations.

D-tests pass `--no-ancestors` to scope the workspace to the test area
itself; Phase 8 ancestor behavior is covered by the AN-tests in
`test_ancestors.py`. Without `--no-ancestors`, every D-test would pull
the ancestors above the test-area root (including the space homepage
and the read-only reference fixture) into the workspace, and a few
mutate every page in the manifest — which would violate the CLAUDE.md
"do not mutate the reference page" rule.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest


pytestmark = pytest.mark.online


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "confluence_markdown_roundtrip.cli", *args],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# D01 — pull --subtree produces manifest + on-disk layout
# ---------------------------------------------------------------------------


def test_D01_subtree_pull(baselines, tmp_path, restore):
    # Restore every page so we have a known shape.
    for logical in ("root", "child_a", "child_b", "grandchild"):
        restore(logical)
    root_id = baselines["root"]["page_id"]
    out = _run("pull", root_id, "--subtree", "--no-ancestors", "--into", str(tmp_path))
    assert out.returncode == 0, out.stderr

    # Manifest now lives at workspace root (--into level), not inside a page dir.
    manifest_path = tmp_path / "_meta" / "_subtree.json"
    assert manifest_path.exists(), f"missing workspace manifest at {manifest_path}"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert data["root_page_ids"] == [root_id]
    assert len(data["pages"]) == 4

    by_pid = {p["page_id"]: p for p in data["pages"]}
    # Root entry — path is now relative to <into>, so includes the topmost slug.
    root_entry = by_pid[root_id]
    assert root_entry["path"].endswith("/index.md")
    assert root_entry["path"].count("/") == 1, root_entry["path"]
    assert root_entry["parent_id"] is None
    topmost_slug = root_entry["path"][: -len("/index.md")]

    # Grandchild's path is nested two levels under the topmost slug
    grandchild_id = baselines["grandchild"]["page_id"]
    grandchild_entry = by_pid[grandchild_id]
    assert grandchild_entry["path"].startswith(f"{topmost_slug}/")
    assert grandchild_entry["path"].count("/") == 3, grandchild_entry["path"]
    assert grandchild_entry["path"].endswith("/index.md")
    assert grandchild_entry["parent_id"] == baselines["child_a"]["page_id"]

    # On-disk layout: each page entry's path points at a real index.md
    for entry in data["pages"]:
        assert (tmp_path / entry["path"]).exists(), f"missing {entry['path']}"

    # CLI's stdout should print the requested page's directory.
    target_dir = Path(out.stdout.strip().splitlines()[-1])
    assert target_dir == tmp_path / topmost_slug


# ---------------------------------------------------------------------------
# D03 — leaf-first push order
# ---------------------------------------------------------------------------


def test_D03_subtree_leaf_first_push(baselines, tmp_path, restore, live_client):
    for logical in ("root", "child_a", "child_b", "grandchild"):
        restore(logical)
    root_id = baselines["root"]["page_id"]
    out = _run("pull", root_id, "--subtree", "--no-ancestors", "--into", str(tmp_path))
    assert out.returncode == 0, out.stderr

    # Workspace root is --into. Push operates on the workspace.
    manifest_path = tmp_path / "_meta" / "_subtree.json"
    manifest = json.loads(manifest_path.read_text())

    # Mutate every page so each is dirty.
    for entry in manifest["pages"]:
        index_md = tmp_path / entry["path"]
        index_md.write_text(
            index_md.read_text(encoding="utf-8") + "\n\nD03 mutated.\n",
            encoding="utf-8",
        )

    push = _run("push", str(tmp_path))
    assert push.returncode == 0, push.stderr

    # The CLI prints one "pushed -> version N" line per page in push order.
    push_lines = [ln for ln in push.stdout.splitlines() if "pushed ->" in ln]
    assert len(push_lines) == 4, push.stdout

    pushed_paths_in_order = [ln.split(":", 1)[0] for ln in push_lines]
    # Find the index of each logical page in the push order.
    by_pid_path: dict[str, str] = {}
    for entry in manifest["pages"]:
        by_pid_path[entry["page_id"]] = str(tmp_path / Path(entry["path"]).parent)

    def order_index(pid: str) -> int:
        target = by_pid_path[pid]
        for i, p in enumerate(pushed_paths_in_order):
            if p == target:
                return i
        return -1

    grandchild_id = baselines["grandchild"]["page_id"]
    child_a_id = baselines["child_a"]["page_id"]
    child_b_id = baselines["child_b"]["page_id"]
    root_id_str = baselines["root"]["page_id"]

    # Hard ordering invariants: child before its parent.
    assert order_index(grandchild_id) < order_index(child_a_id)
    assert order_index(child_a_id) < order_index(root_id_str)
    assert order_index(child_b_id) < order_index(root_id_str)


# ---------------------------------------------------------------------------
# D04 — re-pull partial dirty writes .remote.md for dirty pages only
# ---------------------------------------------------------------------------


def test_D04_subtree_repull_partial_dirty(baselines, tmp_path, restore, live_client):
    for logical in ("root", "child_a", "child_b", "grandchild"):
        restore(logical)
    root_id = baselines["root"]["page_id"]
    out = _run("pull", root_id, "--subtree", "--no-ancestors", "--into", str(tmp_path))
    assert out.returncode == 0, out.stderr

    # Manifest at workspace root (--into level).
    manifest = json.loads((tmp_path / "_meta" / "_subtree.json").read_text())
    by_pid = {p["page_id"]: p for p in manifest["pages"]}
    root_index = tmp_path / by_pid[baselines["root"]["page_id"]]["path"]
    child_a_index = tmp_path / by_pid[baselines["child_a"]["page_id"]]["path"]
    child_b_index = tmp_path / by_pid[baselines["child_b"]["page_id"]]["path"]
    grandchild_index = tmp_path / by_pid[baselines["grandchild"]["page_id"]]["path"]

    root_index.write_text(root_index.read_text(encoding="utf-8") + "\n\nD04 root edit.\n", encoding="utf-8")
    child_a_index.write_text(child_a_index.read_text(encoding="utf-8") + "\n\nD04 child_a edit.\n", encoding="utf-8")

    # Re-pull into the same directory.
    out2 = _run("pull", root_id, "--subtree", "--no-ancestors", "--into", str(tmp_path))
    assert out2.returncode == 0, out2.stderr

    # Dirty pages get .remote.md siblings; clean pages overwrite in place.
    assert root_index.with_suffix(".md.remote").exists()
    assert child_a_index.with_suffix(".md.remote").exists()
    # Clean pages do NOT get .remote files.
    assert not child_b_index.with_suffix(".md.remote").exists()
    assert not grandchild_index.with_suffix(".md.remote").exists()

    # The dirty pages' index.md still contains the local edits.
    assert "D04 root edit." in root_index.read_text(encoding="utf-8")
    assert "D04 child_a edit." in child_a_index.read_text(encoding="utf-8")
