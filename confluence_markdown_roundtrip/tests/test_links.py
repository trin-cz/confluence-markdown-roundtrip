"""Phase 7 — local cross-page links in subtree pulls (read-only).

Offline L1-L8 + live L9 against the Alexandria subtree pulled this session.

The rewrite is one-way: pull sees a Confluence page link, emits `[text](rel)<!--cl:HASH-->`,
stores the original element XML in `sidecar.links[HASH]`. Push reads the trailer,
emits the stored XML verbatim. Visible text and path are display-only.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from lxml import etree

from confluence_markdown_roundtrip import sentinels as S
from confluence_markdown_roundtrip.md_to_storage import md_to_storage
from confluence_markdown_roundtrip.storage_to_md import storage_to_md


ACNS = "http://atlassian.com/content"
RINS = "http://atlassian.com/resource/identifier"


# Reusable in-tree index for the offline tests: A links to B.
PID_A = "1001"
PID_B = "1002"
PID_OUT = "9999"  # never in pid_to_relpath
INDEX = {
    "pid_to_relpath": {
        PID_A: "alexandria/a/index.md",
        PID_B: "alexandria/b/index.md",
    },
    "title_to_pid": {("", "A"): PID_A, ("", "B"): PID_B},
}


def _pull_one(xhtml: str, *, self_rel: str = "alexandria/a/index.md") -> tuple[str, dict]:
    md, sidecar = storage_to_md(
        xhtml,
        title="A",
        pid_to_relpath=INDEX["pid_to_relpath"],
        title_to_pid=INDEX["title_to_pid"],
        self_page_relpath=self_rel,
    )
    return md, sidecar.to_json()


def _canon(xhtml: str) -> bytes:
    wrapped = f'<root xmlns:ac="{ACNS}" xmlns:ri="{RINS}">{xhtml}</root>'
    return S.c14n2(S.strip_bookkeeping(etree.fromstring(wrapped.encode("utf-8"))))


# ---------------------------------------------------------------------------
# L1 — in-tree URL link rewrites to local path
# ---------------------------------------------------------------------------


def test_L1_in_tree_url_link_rewrites_to_local_path():
    xhtml = (
        '<p>see <a href="https://example.atlassian.net/wiki/spaces/EN/pages/1002/Backend">Backend</a> here</p>'
    )
    md, sc = _pull_one(xhtml)
    # Display path: from alexandria/a/ to alexandria/b/index.md → ../b/index.md
    m = re.search(r"\[Backend\]\(([^)]+)\)<!--cl:([0-9a-f]{12})-->", md)
    assert m, f"no cl-link in MD: {md!r}"
    assert m.group(1) == "../b/index.md"
    hash_ = m.group(2)
    assert hash_ in sc["links"]
    assert "<a" in sc["links"][hash_]["xml"]
    assert "/pages/1002/" in sc["links"][hash_]["xml"]


# ---------------------------------------------------------------------------
# L2 — ac:link / ri:content-title rewrite
# ---------------------------------------------------------------------------


def test_L2_ac_link_content_title_rewrites_to_local_path():
    xhtml = (
        '<p>see '
        '<ac:link>'
        '<ri:page ri:content-title="B" ri:space-key="EN"/>'
        '<ac:plain-text-link-body>Backend</ac:plain-text-link-body>'
        '</ac:link>'
        ' here</p>'
    )
    md, sc = _pull_one(xhtml)
    m = re.search(r"\[Backend\]\(([^)]+)\)<!--cl:([0-9a-f]{12})-->", md)
    assert m, f"no cl-link in MD: {md!r}"
    assert m.group(1) == "../b/index.md"
    assert m.group(2) in sc["links"]


# ---------------------------------------------------------------------------
# L3 — ac:link / ri:content-id rewrite
# ---------------------------------------------------------------------------


def test_L3_ac_link_content_id_rewrites_to_local_path():
    xhtml = (
        '<p>see '
        '<ac:link>'
        '<ri:page ri:content-id="1002"/>'
        '<ac:plain-text-link-body>Backend</ac:plain-text-link-body>'
        '</ac:link>'
        ' here</p>'
    )
    md, sc = _pull_one(xhtml)
    m = re.search(r"\[Backend\]\(([^)]+)\)<!--cl:([0-9a-f]{12})-->", md)
    assert m, f"no cl-link in MD: {md!r}"
    assert m.group(1) == "../b/index.md"


# ---------------------------------------------------------------------------
# L4 — out-of-tree links pass through (no cl trailer, no sidecar entry)
# ---------------------------------------------------------------------------


def test_L4_out_of_tree_link_passes_through():
    # URL form: page id 9999 is not in the index.
    xhtml_a = (
        '<p>see '
        '<a href="https://example.atlassian.net/wiki/spaces/EN/pages/9999/Other">Other</a>'
        ' here</p>'
    )
    md_a, sc_a = _pull_one(xhtml_a)
    assert "<!--cl:" not in md_a
    assert sc_a["links"] == {}
    assert "[Other](https://example.atlassian.net/wiki/spaces/EN/pages/9999/Other)" in md_a

    # ac:link form pointing at an unknown content-id → opaque inline.
    xhtml_b = (
        '<p>see '
        '<ac:link>'
        '<ri:page ri:content-id="9999"/>'
        '<ac:plain-text-link-body>Other</ac:plain-text-link-body>'
        '</ac:link>'
        '</p>'
    )
    md_b, sc_b = _pull_one(xhtml_b)
    assert "<!--cl:" not in md_b
    assert sc_b["links"] == {}
    # Falls through to inline-opaque (existing behavior).
    assert sc_b["inline_blocks"], "ac:link to out-of-tree should be inline opaque"


# ---------------------------------------------------------------------------
# L5 — round-trip canonical for every shape
# ---------------------------------------------------------------------------


SHAPES: dict[str, str] = {
    "url": '<p><a href="https://example.atlassian.net/wiki/spaces/EN/pages/1002/Backend">Backend</a></p>',
    "ac-link-content-id": (
        '<p><ac:link xmlns:ac="http://atlassian.com/content">'
        '<ri:page xmlns:ri="http://atlassian.com/resource/identifier" ri:content-id="1002"/>'
        '<ac:plain-text-link-body>Backend</ac:plain-text-link-body>'
        '</ac:link></p>'
    ),
    "ac-link-content-title": (
        '<p><ac:link xmlns:ac="http://atlassian.com/content">'
        '<ri:page xmlns:ri="http://atlassian.com/resource/identifier" ri:content-title="B" ri:space-key="EN"/>'
        '<ac:plain-text-link-body>Backend</ac:plain-text-link-body>'
        '</ac:link></p>'
    ),
    "url-anchored": (
        '<p><a href="https://example.atlassian.net/wiki/spaces/EN/pages/1002/Backend#deploy">Backend</a></p>'
    ),
}


@pytest.mark.parametrize("shape_name", sorted(SHAPES.keys()))
def test_L5_round_trip_canonical_for_all_link_forms(shape_name: str):
    xhtml = SHAPES[shape_name]
    md, side = _pull_one(xhtml)
    _, body = md_to_storage(md, side)
    assert _canon(body) == _canon(xhtml), (
        f"shape={shape_name}\norig={xhtml}\nrt={body}"
    )


# ---------------------------------------------------------------------------
# L6 — anchor preserved
# ---------------------------------------------------------------------------


def test_L6_anchor_link_round_trip_display_form():
    xhtml = (
        '<p><a href="https://example.atlassian.net/wiki/spaces/EN/pages/1002/Backend#deploy">Backend</a></p>'
    )
    md, sc = _pull_one(xhtml)
    m = re.search(r"\[Backend\]\(([^)]+)\)<!--cl:[0-9a-f]{12}-->", md)
    assert m, f"no cl-link in MD: {md!r}"
    assert m.group(1) == "../b/index.md#deploy"

    xhtml_ac = (
        '<p><ac:link xmlns:ac="http://atlassian.com/content" ac:anchor="install">'
        '<ri:page xmlns:ri="http://atlassian.com/resource/identifier" ri:content-id="1002"/>'
        '<ac:plain-text-link-body>Backend</ac:plain-text-link-body>'
        '</ac:link></p>'
    )
    md_ac, _ = _pull_one(xhtml_ac)
    m_ac = re.search(r"\[Backend\]\(([^)]+)\)<!--cl:[0-9a-f]{12}-->", md_ac)
    assert m_ac, f"no cl-link in MD: {md_ac!r}"
    assert m_ac.group(1) == "../b/index.md#install"


# ---------------------------------------------------------------------------
# L7 — user edits text and/or path → push ignores edits (sidecar wins)
# ---------------------------------------------------------------------------


def test_L7_user_text_or_path_edit_ignored():
    xhtml = (
        '<p><a href="https://example.atlassian.net/wiki/spaces/EN/pages/1002/Backend">Backend</a></p>'
    )
    md, side = _pull_one(xhtml)
    # User edits visible text and path
    edited = md.replace("[Backend](../b/index.md)", "[EDITED TEXT](some/other/path.md)")
    assert edited != md  # sanity: substitution happened
    _, body = md_to_storage(edited, side)
    # Original element restored verbatim — neither edit propagates.
    assert _canon(body) == _canon(xhtml)


# ---------------------------------------------------------------------------
# L8 — orphaned link aborts
# ---------------------------------------------------------------------------


def test_L8_orphaned_local_link_aborts():
    # Local-style path without a cl: trailer → bad-marker-syntax.
    sidecar = {
        "title": "T",
        "blocks": {}, "inline_blocks": {}, "tasks": {}, "code_blocks": {},
        "images": {}, "panels": {}, "links": {},
    }
    md = "# T\n\nsee [Backend](../b/index.md) here\n"
    with pytest.raises(S.PushAbort) as exc:
        md_to_storage(md, sidecar)
    assert exc.value.rule_id == "bad-marker-syntax"

    # cl: trailer with HASH not in sidecar → unknown-cl-hash.
    md2 = "# T\n\nsee [Backend](../b/index.md)<!--cl:000000000000--> here\n"
    with pytest.raises(S.PushAbort) as exc2:
        md_to_storage(md2, sidecar)
    assert exc2.value.rule_id == "unknown-cl-hash"


# ---------------------------------------------------------------------------
# L9 — live: Alexandria subtree has working cross-page links
# ---------------------------------------------------------------------------


@pytest.mark.online
def test_L9_alexandria_subtree_local_links_navigate(tmp_path: Path):
    """Pull the Alexandria subtree, count cl-links and verify each target
    file actually exists on disk."""
    r = subprocess.run(
        [
            sys.executable, "-m", "confluence_markdown_roundtrip.cli",
            "pull", "4942462978", "--subtree", "--into", str(tmp_path),
        ],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    root_dir = Path(r.stdout.strip().splitlines()[-1])

    cl_hits = 0
    broken: list[str] = []
    for md_path in root_dir.rglob("index.md"):
        text = md_path.read_text(encoding="utf-8")
        for m in re.finditer(r"\[[^\]]*\]\(([^)]+)\)<!--cl:[0-9a-f]{12}-->", text):
            cl_hits += 1
            rel = m.group(1).split("#", 1)[0]
            target = (md_path.parent / rel).resolve()
            if not target.exists():
                broken.append(f"{md_path}: {rel} → {target}")

    assert cl_hits > 0, "expected at least one cross-page link in Alexandria"
    assert not broken, f"broken cross-page links:\n" + "\n".join(broken[:20])
