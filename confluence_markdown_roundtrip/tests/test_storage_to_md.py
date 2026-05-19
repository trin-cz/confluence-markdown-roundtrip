"""Offline tests for the storage -> MD walker.

Pure-logic over hand-crafted storage XHTML samples. The aim is to pin down
every dispatch branch in `storage_to_md._Walker.block`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from confluence_markdown_roundtrip import sentinels as S
from confluence_markdown_roundtrip.storage_to_md import storage_to_md


FIXTURE = Path(__file__).parent / "fixtures" / "test-page.json"


# ---------------------------------------------------------------------------
# Per-construct tests on minimal XHTML
# ---------------------------------------------------------------------------


def _convert(xhtml: str, title: str = "T") -> tuple[str, dict]:
    md, sidecar = storage_to_md(xhtml, title)
    return md, sidecar.to_json()


class TestProse:
    def test_paragraph(self):
        md, _ = _convert("<p>hello world</p>")
        assert "hello world" in md

    def test_h2_to_h6_passthrough(self):
        md, _ = _convert("<h2>A</h2><h3>B</h3><h4>C</h4><h5>D</h5><h6>E</h6>")
        assert "## A" in md and "### B" in md and "#### C" in md
        assert "##### D" in md and "###### E" in md

    def test_h1_in_body_downgrades_to_h2(self):
        # Body H1 collides with synthetic title H1; we downgrade.
        md, _ = _convert("<h1>X</h1>")
        assert "## X" in md
        # the synthetic title H1 is still present at the top
        lines = [ln for ln in md.splitlines() if ln.startswith("# ")]
        assert lines[0] == "# T"

    def test_strong_em_code_link_inline(self):
        md, _ = _convert(
            '<p>a <strong>b</strong> <em>c</em> <code>d</code> '
            '<a href="https://e">e</a></p>'
        )
        assert "**b**" in md
        assert "*c*" in md
        assert "`d`" in md
        assert "[e](https://e)" in md

    def test_ul_and_ol(self):
        md, _ = _convert("<ul><li>a</li><li>b</li></ul>")
        assert "- a" in md and "- b" in md

        md, _ = _convert("<ol><li>a</li><li>b</li></ol>")
        assert "1. a" in md and "2. b" in md


class TestInlineCommentMarker:
    def test_cm_marker_wraps_text(self):
        uuid = "11111111-2222-3333-4444-555555555555"
        xml = f'<p>before <ac:inline-comment-marker ac:ref="{uuid}">phrase</ac:inline-comment-marker> after</p>'
        md, _ = _convert(xml)
        assert S.cm_open(uuid) in md
        assert S.cm_close(uuid) in md
        assert "phrase" in md


class TestOpaqueBlock:
    def test_unknown_macro_is_opaque(self):
        xml = '<ac:structured-macro ac:name="future-macro-X"><foo/></ac:structured-macro>'
        md, sj = _convert(xml)
        assert "[confluence: macro:future-macro-X]" in md
        assert len(sj["blocks"]) == 1

    def test_layout_is_opaque(self):
        md, sj = _convert("<ac:layout><ac:layout-section/></ac:layout>")
        assert "[confluence: ac:layout]" in md
        assert len(sj["blocks"]) == 1


class TestInlineOpaque:
    def test_user_mention_is_inline_span(self):
        xml = '<p>hello <ac:link><ri:user ri:account-id="abc"/></ac:link></p>'
        md, sj = _convert(xml)
        assert len(sj["inline_blocks"]) == 1
        h = next(iter(sj["inline_blocks"]))
        assert f'<span data-ci="{h}">' in md
        assert "[@mention]" in md


class TestCodeBlock:
    def test_language_on_fence_other_params_opaque(self):
        xml = (
            '<ac:structured-macro ac:name="code">'
            '<ac:parameter ac:name="language">python</ac:parameter>'
            '<ac:parameter ac:name="breakoutMode">wide</ac:parameter>'
            '<ac:parameter ac:name="breakoutWidth">760</ac:parameter>'
            "<ac:plain-text-body><![CDATA[print(1)\nprint(2)]]></ac:plain-text-body>"
            "</ac:structured-macro>"
        )
        md, sj = _convert(xml)
        assert "```python" in md
        assert "print(1)" in md and "print(2)" in md
        assert len(sj["code_blocks"]) == 1
        params = next(iter(sj["code_blocks"].values()))["params"]
        assert params == {"breakoutMode": "wide", "breakoutWidth": "760"}

    def test_no_language(self):
        xml = (
            '<ac:structured-macro ac:name="code">'
            "<ac:plain-text-body><![CDATA[bash here]]></ac:plain-text-body>"
            "</ac:structured-macro>"
        )
        md, _ = _convert(xml)
        # bare fence
        assert "```\nbash here\n```" in md


class TestTaskList:
    def test_task_list_uses_task_uuid(self):
        task_uuid = "6a0e3823-e9f0-4049-9d48-029dcf59d3de"
        xml = (
            '<ac:task-list ac:task-list-id="abc-list">'
            "<ac:task>"
            "<ac:task-id>1</ac:task-id>"
            f"<ac:task-uuid>{task_uuid}</ac:task-uuid>"
            "<ac:task-status>incomplete</ac:task-status>"
            "<ac:task-body>do thing</ac:task-body>"
            "</ac:task>"
            "</ac:task-list>"
        )
        md, sj = _convert(xml)
        assert f"- <!--ct:{task_uuid}--> do thing" in md
        assert sj["tasks"][task_uuid]["status"] == "incomplete"
        assert sj["tasks"][task_uuid]["task_id"] == "1"
        assert sj["tasks"][task_uuid]["task_list_id"] == "abc-list"


class TestImage:
    def test_attachment_image_with_attrs(self):
        xml = (
            '<ac:image ac:align="center" ac:width="300" ac:alt="diagram">'
            '<ri:attachment ri:filename="diagram.png" ri:version-at-save="2"/>'
            "</ac:image>"
        )
        md, sj = _convert(xml)
        # alt + path
        assert "[diagram]" in md
        assert "./_meta/attachments/diagram.png" in md
        # ci trailer with hash; sidecar has image attrs
        assert len(sj["images"]) == 1
        h = next(iter(sj["images"]))
        assert f"<!--ci:{h}-->" in md
        entry = sj["images"][h]
        assert entry["filename"] == "diagram.png"
        assert "version-at-save" in str(entry["ri_attrs"])

    def test_url_image_no_download(self):
        xml = '<ac:image><ri:url ri:value="https://example.com/x.png"/></ac:image>'
        md, sj = _convert(xml)
        assert "https://example.com/x.png" in md
        assert sj["images"] == {}


class TestEditablePanels:
    def _macro_panel(self, name: str, body: str = "<p>hi</p>") -> str:
        return (
            f'<ac:structured-macro ac:name="{name}" ac:schema-version="1">'
            f"<ac:rich-text-body>{body}</ac:rich-text-body>"
            "</ac:structured-macro>"
        )

    def _adf_panel(self, panel_type: str, body: str = "<p>hi</p>", fallback: str = "<div>fb</div>") -> str:
        return (
            "<ac:adf-extension>"
            '<ac:adf-node type="panel">'
            f'<ac:adf-attribute key="panel-type">{panel_type}</ac:adf-attribute>'
            '<ac:adf-attribute key="local-id">abc123</ac:adf-attribute>'
            f"<ac:adf-content>{body}</ac:adf-content>"
            "</ac:adf-node>"
            f"<ac:adf-fallback>{fallback}</ac:adf-fallback>"
            "</ac:adf-extension>"
        )

    # P1 — Phase 6: panel renders as a cp wrapper + GFM Alert blockquote.
    def test_macro_panel_pulls_editable(self):
        md, sj = _convert(self._macro_panel("info", "<p>body text</p>"))
        assert "<!--cp:" in md
        assert "> [!NOTE]" in md  # info → NOTE
        assert "> body text" in md
        assert len(sj["panels"]) == 1
        uuid, entry = next(iter(sj["panels"].items()))
        assert entry == {"shape": "macro", "name": "info", "params": {}}
        assert f"<!--/cp:{uuid}-->" in md

    # P2
    def test_all_four_macro_panel_types_pull(self):
        xml = "".join(self._macro_panel(n, f"<p>{n}-body</p>") for n in ("info", "note", "warning", "tip"))
        md, sj = _convert(xml)
        names = sorted(e["name"] for e in sj["panels"].values())
        assert names == ["info", "note", "tip", "warning"]
        # Each body is prefixed with `> `; alert kind comes from sidecar name.
        for n in ("info", "note", "warning", "tip"):
            assert f"> {n}-body" in md
        # GFM kinds the four macro panel types map to.
        assert "> [!NOTE]" in md       # info
        assert "> [!IMPORTANT]" in md  # note
        assert "> [!WARNING]" in md    # warning
        assert "> [!TIP]" in md        # tip

    # P3
    def test_adf_panel_pulls_editable(self):
        md, sj = _convert(self._adf_panel("note", "<p>note body</p>", "<div>fb</div>"))
        assert "<!--cp:" in md
        assert "> [!IMPORTANT]" in md  # note → IMPORTANT
        assert "> note body" in md
        assert len(sj["panels"]) == 1
        entry = next(iter(sj["panels"].values()))
        assert entry["shape"] == "adf"
        assert entry["name"] == "note"
        assert entry["adf_attrs"] == {}  # local-id is stripped
        assert "fb" in entry["adf_fallback"]

    # P4
    def test_adf_panel_success_and_error_supported(self):
        xml = self._adf_panel("success", "<p>S</p>") + self._adf_panel("error", "<p>E</p>")
        md, sj = _convert(xml)
        assert "> [!TIP]" in md       # success → TIP
        assert "> [!CAUTION]" in md   # error → CAUTION
        names = sorted(e["name"] for e in sj["panels"].values())
        assert names == ["error", "success"]
        for e in sj["panels"].values():
            assert e["shape"] == "adf"

    # P5
    def test_adf_custom_panel_stays_opaque(self):
        md, sj = _convert(self._adf_panel("custom", "<p>x</p>"))
        assert sj["panels"] == {}
        assert "[confluence: ac:adf-extension]" in md
        assert len(sj["blocks"]) == 1

    # P6
    def test_panel_body_with_rich_content(self):
        body = "<p>before <strong>bold</strong> after</p><ul><li><p>a</p></li><li><p>b</p></li></ul>"
        md, sj = _convert(self._macro_panel("warning", body))
        assert "**bold**" in md
        assert "- a" in md
        assert "- b" in md
        assert len(sj["panels"]) == 1

    def test_macro_panel_without_body_stays_opaque(self):
        # Edge case: well-formed but body-less panel macro. Should fall through
        # to opaque so we don't synthesize a panel with no content.
        xml = '<ac:structured-macro ac:name="info" ac:schema-version="1"/>'
        _, sj = _convert(xml)
        assert sj["panels"] == {}
        assert len(sj["blocks"]) == 1


class TestSimpleTable:
    def test_2x2_text_only_converts_to_gfm(self):
        xml = (
            "<table><tbody>"
            "<tr><th><p>A</p></th><th><p>B</p></th></tr>"
            "<tr><td><p>1</p></td><td><p>2</p></td></tr>"
            "</tbody></table>"
        )
        md, sj = _convert(xml)
        assert "| A | B |" in md
        assert "| --- | --- |" in md
        assert "| 1 | 2 |" in md
        assert sj["blocks"] == {}  # not opaqued

    def test_table_with_macro_in_cell_is_opaque(self):
        xml = (
            "<table><tbody>"
            "<tr><th><p>A</p></th><th><p>B</p></th></tr>"
            "<tr><td><ac:structured-macro ac:name=\"status\"/></td><td><p>2</p></td></tr>"
            "</tbody></table>"
        )
        md, sj = _convert(xml)
        assert "[confluence: table]" in md
        assert len(sj["blocks"]) == 1


# ---------------------------------------------------------------------------
# Live-fixture coverage
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_page() -> dict:
    if not FIXTURE.exists():
        pytest.skip(f"fixture {FIXTURE} not present")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class TestLiveFixture:
    """Sanity checks against the Phase 1.5 captured page."""

    def test_no_exceptions_on_full_body(self, fixture_page):
        body = fixture_page["body"]["storage"]["value"]
        title = fixture_page["title"]
        md, sidecar = storage_to_md(body, title)
        assert md.startswith(f"# {title}")

    def test_all_hashes_referenced_in_md_exist_in_sidecar(self, fixture_page):
        body = fixture_page["body"]["storage"]["value"]
        title = fixture_page["title"]
        md, sidecar = storage_to_md(body, title)
        sj = sidecar.to_json()
        for m in S.CB_RE.finditer(md):
            assert m.group(1) in sj["blocks"], f"cb hash {m.group(1)} missing from sidecar"
        for m in S.CI_SPAN_RE.finditer(md):
            assert m.group(1) in sj["inline_blocks"]
        for m in S.CI_TRAILER_RE.finditer(md):
            assert m.group(1) in sj["images"]
        for m in S.CT_RE.finditer(md):
            assert m.group(1) in sj["tasks"]
        for m in S.CC_RE.finditer(md):
            assert m.group(1) in sj["code_blocks"]

    def test_cm_markers_balanced(self, fixture_page):
        body = fixture_page["body"]["storage"]["value"]
        title = fixture_page["title"]
        md, _ = storage_to_md(body, title)
        opens = list(S.CM_OPEN_RE.finditer(md))
        closes = list(S.CM_CLOSE_RE.finditer(md))
        assert len(opens) == len(closes)
        for o, c in zip(opens, closes):
            assert o.group(1) == c.group(1)
