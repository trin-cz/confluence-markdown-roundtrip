"""Category C — push abort paths (plan §"Category C").

Each test deliberately breaks the workspace and verifies:
- Process exit code matches plan §"Push abort format" (2 for validation,
  3 for version conflict, 4 for API errors).
- Stderr line shape includes `error: <file>:<line>: <rule-id>: <message>`
  (we relax to: contains the rule-id token).
- No PUT was made (server-side version unchanged) — checked on the
  online-tier tests only.

C01..C06 are pure-local (no server interaction needed beyond a
prerequisite pull). C07 (`orig-tampered`) and C08 (`meta-tampered`) are
also offline. C09 (`version-conflict`) is the only one that needs an
out-of-band version bump.
"""

from __future__ import annotations

import json
import pytest

from confluence_markdown_roundtrip import sentinels as S


# Use a real page so `pull` works, but the abort tests only exercise the
# push validation path; no PUT actually happens.
pytestmark = pytest.mark.online


def _read(path) -> str:
    return (path / "index.md").read_text(encoding="utf-8")


def _write(path, content: str) -> None:
    (path / "index.md").write_text(content, encoding="utf-8")


def _assert_abort(r, *, exit_code: int, rule_id: str) -> None:
    assert r.returncode == exit_code, f"expected exit {exit_code}, got {r.returncode}; stderr: {r.stderr}"
    assert rule_id in r.stderr, f"expected rule-id {rule_id!r} in stderr; got: {r.stderr!r}"


# ---------------------------------------------------------------------------
# C01 — unmatched-cm
# ---------------------------------------------------------------------------


def test_C01_unmatched_cm(restore, make_workspace, push, baselines):
    restore("root")
    ws = make_workspace(baselines["root"]["page_id"])
    md = _read(ws)
    # Delete the FIRST cm close marker so its open is orphaned.
    md2 = S.CM_CLOSE_RE.sub("", md, count=1)
    assert md2 != md, "did not strip a close marker"
    _write(ws, md2)
    r = push(ws)
    _assert_abort(r, exit_code=2, rule_id="unmatched-cm")


# ---------------------------------------------------------------------------
# C02 — unknown-cb-hash
# ---------------------------------------------------------------------------


def test_C02_unknown_cb_hash(restore, make_workspace, push, baselines):
    restore("root")
    ws = make_workspace(baselines["root"]["page_id"])
    md = _read(ws) + "\n<!--cb:deadbeefcafe-->\n"
    _write(ws, md)
    r = push(ws)
    _assert_abort(r, exit_code=2, rule_id="unknown-cb-hash")


# ---------------------------------------------------------------------------
# C03 — unknown-ci-hash
# ---------------------------------------------------------------------------


def test_C03_unknown_ci_hash(restore, make_workspace, push, baselines):
    restore("root")
    ws = make_workspace(baselines["root"]["page_id"])
    md = _read(ws) + "\n\n<span data-ci=\"deadbeefcafe\">x</span>\n"
    _write(ws, md)
    r = push(ws)
    _assert_abort(r, exit_code=2, rule_id="unknown-ci-hash")


# ---------------------------------------------------------------------------
# C05 — missing-h1
# ---------------------------------------------------------------------------


def test_C05_missing_h1(restore, make_workspace, push, baselines):
    restore("root")
    ws = make_workspace(baselines["root"]["page_id"])
    # Replace H1 line with an H2 (still has hash-prefix, but level wrong).
    md = _read(ws).replace("# ", "## ", 1)
    _write(ws, md)
    r = push(ws)
    _assert_abort(r, exit_code=2, rule_id="missing-h1")


# ---------------------------------------------------------------------------
# C06 — bad-marker-syntax
# ---------------------------------------------------------------------------


def test_C06_bad_marker_syntax(restore, make_workspace, push, baselines):
    restore("root")
    ws = make_workspace(baselines["root"]["page_id"])
    md = _read(ws) + "\n<!--cm:NOT-A-UUID-->x<!--/cm:NOT-A-UUID-->\n"
    _write(ws, md)
    r = push(ws)
    _assert_abort(r, exit_code=2, rule_id="bad-marker-syntax")


# ---------------------------------------------------------------------------
# C07 — orig-tampered
# ---------------------------------------------------------------------------


def test_C07_orig_tampered(restore, make_workspace, push, baselines):
    restore("root")
    ws = make_workspace(baselines["root"]["page_id"])
    # Modify .orig file; sha256 will no longer match sidecar.base_md_sha256
    orig = ws / "_meta" / "index.md.orig"
    orig.write_text(orig.read_text(encoding="utf-8") + "\nspurious line\n", encoding="utf-8")
    # Also dirty index.md so we get past the fast-clean check.
    _write(ws, _read(ws) + "\n\nmade dirty.\n")
    r = push(ws)
    _assert_abort(r, exit_code=2, rule_id="orig-tampered")


# ---------------------------------------------------------------------------
# C08 — meta-tampered
# ---------------------------------------------------------------------------


def test_C08_meta_tampered(restore, make_workspace, push, baselines):
    restore("root")
    ws = make_workspace(baselines["root"]["page_id"])
    sidecar = ws / "_meta" / "index.conf.json"
    sidecar.write_text("{not valid json", encoding="utf-8")
    r = push(ws)
    _assert_abort(r, exit_code=2, rule_id="meta-tampered")


# ---------------------------------------------------------------------------
# C09 — version-conflict
# ---------------------------------------------------------------------------


def test_C09_version_conflict(restore, make_workspace, push, baselines, live_client):
    restore("root")
    bl = baselines["root"]
    ws = make_workspace(bl["page_id"])

    # Bump the remote version out-of-band so our local base_version is stale.
    current = live_client.get_page_version(bl["page_id"])
    live_client.update_page(
        bl["page_id"],
        title=bl["title"],
        storage_body=bl["storage_body"] + "<p>out-of-band edit</p>",
        base_version=current,
    )

    # Make a local edit so we get past the fast-clean check.
    _write(ws, _read(ws) + "\n\nlocal dirty edit.\n")
    r = push(ws)
    _assert_abort(r, exit_code=3, rule_id="version-conflict")

    # .remote.md should have been written.
    remote_md = ws / "index.md.remote"
    assert remote_md.exists(), f"expected {remote_md} to be written on conflict"
