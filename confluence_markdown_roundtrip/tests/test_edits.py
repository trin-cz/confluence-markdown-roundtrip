"""Category B — edit survives round-trip (plan §"Category B").

Each test: pull → mutate index.md programmatically → push → re-pull to a
fresh dir → diff. The diff must show the intended mutation and nothing
else. The `restore("root")` fixture runs before each test to put the
fixture page back to its baseline body.

This file covers a representative subset of the 23 Category B tests:
text edits, headings, list/task/code/cm marker survival, opaque blocks.
The remaining variants (reorder, image alt, GFM tables) can be added
later once these prove stable.
"""

from __future__ import annotations

import re

import pytest

from confluence_markdown_roundtrip import sentinels as S


pytestmark = pytest.mark.online


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mutate(path, replacements: list[tuple[str, str]]) -> None:
    """Read index.md, apply (old, new) replacements, write back. Each
    replacement must change exactly one occurrence — asserts that to keep
    tests deterministic."""
    md = (path / "index.md").read_text(encoding="utf-8")
    for old, new in replacements:
        count = md.count(old)
        assert count == 1, f"expected exactly 1 occurrence of {old!r}, got {count}"
        md = md.replace(old, new)
    (path / "index.md").write_text(md, encoding="utf-8")


def _read(path) -> str:
    return (path / "index.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# B01 — paragraph text edit
# ---------------------------------------------------------------------------


def test_B01_paragraph_text_edit(restore, make_workspace, push, re_pull, baselines):
    restore("root")
    bl = baselines["root"]
    ws = make_workspace(bl["page_id"])

    _mutate(ws, [("Third paragraph: distinct text used by reorder tests.",
                   "Third paragraph: EDITED TEXT for B01.")])
    r = push(ws)
    assert r.returncode == 0, r.stderr

    fresh = re_pull(bl["page_id"])
    md = _read(fresh)
    assert "Third paragraph: EDITED TEXT for B01." in md
    assert "Third paragraph: distinct text used by reorder tests." not in md


# ---------------------------------------------------------------------------
# B04 — H2 edit
# ---------------------------------------------------------------------------


def test_B04_h2_edit(restore, make_workspace, push, re_pull, baselines):
    restore("root")
    bl = baselines["root"]
    ws = make_workspace(bl["page_id"])

    _mutate(ws, [("## A heading exercised by B04",
                   "## A heading RENAMED for B04")])
    r = push(ws)
    assert r.returncode == 0, r.stderr

    fresh = re_pull(bl["page_id"])
    md = _read(fresh)
    assert "## A heading RENAMED for B04" in md
    assert "## A heading exercised by B04" not in md


# ---------------------------------------------------------------------------
# B05 — H1 (title) rename
# ---------------------------------------------------------------------------


def test_B05_h1_renames_page(restore, make_workspace, push, re_pull, baselines, live_client):
    restore("root")
    bl = baselines["root"]
    ws = make_workspace(bl["page_id"])

    new_title = "Automated test area (B05 RENAMED)"
    _mutate(ws, [(f"# {bl['title']}", f"# {new_title}")])
    r = push(ws)
    assert r.returncode == 0, r.stderr

    try:
        page = live_client.get_page(bl["page_id"])
        assert page.title == new_title

        fresh = re_pull(bl["page_id"])
        md = _read(fresh)
        assert md.startswith(f"# {new_title}\n")
    finally:
        # Manually restore title since `restore()` only PUTs body; title
        # comes from the H1 of `index.md`. Future tests will be re-bootstrapped.
        live_client.update_page(
            bl["page_id"],
            title=bl["title"],
            storage_body=bl["storage_body"],
            base_version=live_client.get_page_version(bl["page_id"]),
        )


# ---------------------------------------------------------------------------
# B07 — inline-comment text edit (UUID preserved on re-pull)
# ---------------------------------------------------------------------------


def test_B07_inline_comment_text_edit(restore, make_workspace, push, re_pull, baselines):
    restore("root")
    bl = baselines["root"]
    ws = make_workspace(bl["page_id"])

    md = _read(ws)
    # Grab the UUID of the first cm marker
    m = S.CM_OPEN_RE.search(md)
    assert m is not None, "no cm:UUID open marker in pulled MD"
    uuid1 = m.group(1)

    _mutate(ws, [("alpha anchor phrase one",
                   "alpha anchor phrase one EDITED INSIDE MARKER")])
    r = push(ws)
    assert r.returncode == 0, r.stderr

    fresh = re_pull(bl["page_id"])
    md2 = _read(fresh)
    # UUID survives
    assert f"<!--cm:{uuid1}-->" in md2
    assert f"<!--/cm:{uuid1}-->" in md2
    # Marker contents now include the new text
    pair = re.search(
        rf"<!--cm:{uuid1}-->(.+?)<!--/cm:{uuid1}-->", md2, flags=re.DOTALL
    )
    assert pair is not None
    assert "EDITED INSIDE MARKER" in pair.group(1)


# ---------------------------------------------------------------------------
# B11 — task text edit (state preserved)
# ---------------------------------------------------------------------------


def test_B11_task_text_edit(restore, make_workspace, push, re_pull, baselines):
    restore("root")
    bl = baselines["root"]
    ws = make_workspace(bl["page_id"])

    md = _read(ws)
    # Find first task UUID
    m = re.search(rf"- <!--ct:({S._UUID})--> (first task body)", md)
    assert m is not None, f"first task line not found in:\n{md}"
    task_uuid = m.group(1)

    _mutate(ws, [(f"- <!--ct:{task_uuid}--> first task body",
                   f"- <!--ct:{task_uuid}--> first task EDITED")])
    r = push(ws)
    assert r.returncode == 0, r.stderr

    fresh = re_pull(bl["page_id"])
    md2 = _read(fresh)
    assert f"- <!--ct:{task_uuid}--> first task EDITED" in md2

    # Task status survived: it was "incomplete" — confirm sidecar still says so
    import json
    sidecar = json.loads((fresh / "_meta" / "index.conf.json").read_text())
    assert sidecar["tasks"][task_uuid]["status"] == "incomplete"


# ---------------------------------------------------------------------------
# B12 — code body edit (language + opaque params survive)
# ---------------------------------------------------------------------------


def test_B12_code_body_edit(restore, make_workspace, push, re_pull, baselines):
    restore("root")
    bl = baselines["root"]
    ws = make_workspace(bl["page_id"])

    md = _read(ws)
    assert 'return "world"' in md, "code block body not present"

    _mutate(ws, [('return "world"', 'return "EDITED"')])
    r = push(ws)
    assert r.returncode == 0, r.stderr

    fresh = re_pull(bl["page_id"])
    md2 = _read(fresh)
    assert 'return "EDITED"' in md2

    # Opaque params (breakoutMode, breakoutWidth) preserved in sidecar.
    import json
    sj = json.loads((fresh / "_meta" / "index.conf.json").read_text())
    code_params = next(iter(sj["code_blocks"].values()))["params"]
    assert code_params.get("breakoutMode") == "wide"
    assert code_params.get("breakoutWidth") == "760"


# ---------------------------------------------------------------------------
# B14 — code params untouched survive a no-edit push
# ---------------------------------------------------------------------------


def test_B14_code_opaque_params_preserved(restore, make_workspace, push, re_pull, baselines):
    restore("root")
    bl = baselines["root"]
    ws = make_workspace(bl["page_id"])

    # Touch *something else* (so the dirty check fires) but don't touch code.
    _mutate(ws, [("Fourth paragraph: more distinct text for delete/insert tests.",
                   "Fourth paragraph: B14 touched (not the code block).")])
    r = push(ws)
    assert r.returncode == 0, r.stderr

    fresh = re_pull(bl["page_id"])
    import json
    sj = json.loads((fresh / "_meta" / "index.conf.json").read_text())
    code_params = next(iter(sj["code_blocks"].values()))["params"]
    assert code_params.get("breakoutMode") == "wide"
    assert code_params.get("breakoutWidth") == "760"


# ---------------------------------------------------------------------------
# B21 — block-opaque label rewrite (no body change)
# ---------------------------------------------------------------------------


def _panel_uuid_with_body(md: str, body_substr: str) -> str:
    """Locate the cp:UUID open marker whose enclosed body contains `body_substr`.
    Asserts exactly one match for determinism."""
    pat = re.compile(
        rf"<!--cp:({S._UUID})(?:\s+style=([a-z]+))?-->(.*?)<!--/cp:\1-->",
        flags=re.DOTALL,
    )
    hits = [m for m in pat.finditer(md) if body_substr in m.group(3)]
    assert len(hits) == 1, f"expected exactly one cp pair containing {body_substr!r}, got {len(hits)}"
    return hits[0].group(1)


# ---------------------------------------------------------------------------
# P11 — macro panel body edit survives round-trip
# ---------------------------------------------------------------------------


def test_P11_macro_panel_body_edit(restore, make_workspace, push, re_pull, baselines):
    restore("root")
    bl = baselines["root"]
    ws = make_workspace(bl["page_id"])

    _mutate(ws, [("information panel body", "information panel body EDITED P11")])
    r = push(ws)
    assert r.returncode == 0, r.stderr

    # cp UUIDs are minted per pull, so re-locate the panel by its edited body.
    fresh = re_pull(bl["page_id"])
    md2 = _read(fresh)
    new_uuid = _panel_uuid_with_body(md2, "information panel body EDITED P11")
    pair = re.search(
        rf"<!--cp:{new_uuid}-->(.*?)<!--/cp:{new_uuid}-->",
        md2,
        flags=re.DOTALL,
    )
    assert pair is not None
    # GFM-alert kind for `info` is NOTE; verify it survived the round trip.
    assert "> [!NOTE]" in pair.group(1)

    import json
    sidecar = json.loads((fresh / "_meta" / "index.conf.json").read_text())
    entry = sidecar["panels"][new_uuid]
    assert entry["shape"] == "macro"
    assert entry["name"] == "info"


# ---------------------------------------------------------------------------
# P12 — ADF panel body edit survives round-trip
# ---------------------------------------------------------------------------


def test_P12_adf_panel_body_edit(restore, make_workspace, push, re_pull, baselines):
    restore("root")
    bl = baselines["root"]
    ws = make_workspace(bl["page_id"])

    _mutate(ws, [("adf success panel body", "adf success panel body EDITED P12")])
    r = push(ws)
    assert r.returncode == 0, r.stderr

    fresh = re_pull(bl["page_id"])
    md2 = _read(fresh)
    new_uuid = _panel_uuid_with_body(md2, "adf success panel body EDITED P12")
    pair = re.search(
        rf"<!--cp:{new_uuid}-->(.*?)<!--/cp:{new_uuid}-->",
        md2,
        flags=re.DOTALL,
    )
    assert pair is not None
    # success maps to TIP in the GFM-alert mapping.
    assert "> [!TIP]" in pair.group(1)

    import json
    sidecar = json.loads((fresh / "_meta" / "index.conf.json").read_text())
    entry = sidecar["panels"][new_uuid]
    assert entry["shape"] == "adf"
    assert entry["name"] == "success"


def test_B21_block_opaque_label_rewrite_is_cosmetic(restore, make_workspace, push, re_pull, baselines):
    """Rewriting the `> [confluence: expand]` label line should not change
    what's on Confluence — the `<!--cb:HASH-->` placeholder is source of truth."""
    restore("root")
    bl = baselines["root"]
    ws = make_workspace(bl["page_id"])

    # Hit the expand macro's label
    md = _read(ws)
    label = "> [confluence: macro:expand]"
    assert md.count(label) == 1, f"expected one expand label, got {md.count(label)}"

    _mutate(ws, [(label, "> [confluence: NOT THE REAL KIND]")])
    r = push(ws)
    assert r.returncode == 0, r.stderr

    fresh = re_pull(bl["page_id"])
    md2 = _read(fresh)
    # Confluence-side content unchanged — re-pulled label is back to canonical
    assert "> [confluence: macro:expand]" in md2
