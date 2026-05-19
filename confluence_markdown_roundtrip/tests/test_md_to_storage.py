"""Offline tests for the MD -> storage emitter.

Exercises every branch of the emitter against MD fragments that mirror
what `storage_to_md` produces, plus the abort paths from plan
§"Push abort format".
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from confluence_markdown_roundtrip import sentinels as S
from confluence_markdown_roundtrip.md_to_storage import md_to_storage
from confluence_markdown_roundtrip.storage_to_md import storage_to_md


FIXTURE = Path(__file__).parent / "fixtures" / "test-page.json"


UUID_A = "11111111-2222-3333-4444-555555555555"
UUID_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
HASH_A = "deadbeefcafe"
HASH_B = "0123456789ab"


def _empty_sidecar(**overrides) -> dict:
    base = {
        "title": "T",
        "blocks": {},
        "inline_blocks": {},
        "tasks": {},
        "code_blocks": {},
        "images": {},
        "panels": {},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Title extraction (plan §"Title handling")
# ---------------------------------------------------------------------------


class TestTitle:
    def test_h1_becomes_title(self):
        title, body = md_to_storage("# My Title\n\nparagraph\n", _empty_sidecar())
        assert title == "My Title"
        assert "<p>paragraph</p>" in body

    def test_missing_h1_aborts(self):
        with pytest.raises(S.PushAbort) as exc:
            md_to_storage("## H2 not H1\n", _empty_sidecar())
        assert exc.value.rule_id == "missing-h1"

    def test_blank_lines_before_h1_ok(self):
        title, _ = md_to_storage("\n\n# Title\n", _empty_sidecar())
        assert title == "Title"

    def test_only_blank_aborts(self):
        with pytest.raises(S.PushAbort) as exc:
            md_to_storage("\n\n", _empty_sidecar())
        assert exc.value.rule_id == "missing-h1"


# ---------------------------------------------------------------------------
# Prose
# ---------------------------------------------------------------------------


class TestProse:
    def test_paragraph(self):
        _, body = md_to_storage("# T\n\nhello\n", _empty_sidecar())
        assert "<p>hello</p>" in body

    def test_h2_h6(self):
        _, body = md_to_storage("# T\n\n## A\n\n### B\n", _empty_sidecar())
        assert "<h2>A</h2>" in body
        assert "<h3>B</h3>" in body

    def test_inline_formatting(self):
        _, body = md_to_storage(
            "# T\n\na **b** *c* `d` [e](https://x)\n", _empty_sidecar()
        )
        assert "<strong>b</strong>" in body
        assert "<em>c</em>" in body
        assert "<code>d</code>" in body
        assert '<a href="https://x">e</a>' in body

    def test_ul_and_ol(self):
        _, body = md_to_storage("# T\n\n- a\n- b\n", _empty_sidecar())
        assert "<ul><li><p>a</p></li><li><p>b</p></li></ul>" == _strip_ws(body).split("\n", 1)[0][:200] or "<ul>" in body
        # Loose check — order matters, exact whitespace doesn't:
        assert "<ul>" in body and "</ul>" in body
        assert body.count("<li>") == 2

        _, body = md_to_storage("# T\n\n1. a\n2. b\n", _empty_sidecar())
        assert "<ol>" in body and "</ol>" in body


# ---------------------------------------------------------------------------
# CM markers
# ---------------------------------------------------------------------------


class TestCM:
    def test_cm_pair_becomes_inline_comment_marker(self):
        md = f"# T\n\npre {S.cm_open(UUID_A)}wrapped{S.cm_close(UUID_A)} post\n"
        _, body = md_to_storage(md, _empty_sidecar())
        assert f'<ac:inline-comment-marker ac:ref="{UUID_A}">wrapped</ac:inline-comment-marker>' in body

    def test_cm_unmatched_open_aborts(self):
        md = f"# T\n\npre {S.cm_open(UUID_A)}wrapped end\n"
        with pytest.raises(S.PushAbort) as exc:
            md_to_storage(md, _empty_sidecar())
        assert exc.value.rule_id == "unmatched-cm"

    def test_cm_unmatched_close_aborts(self):
        md = f"# T\n\npre wrapped{S.cm_close(UUID_A)} end\n"
        with pytest.raises(S.PushAbort) as exc:
            md_to_storage(md, _empty_sidecar())
        assert exc.value.rule_id == "unmatched-cm"

    def test_cm_mismatched_uuid_aborts(self):
        md = f"# T\n\n{S.cm_open(UUID_A)}x{S.cm_close(UUID_B)}\n"
        with pytest.raises(S.PushAbort) as exc:
            md_to_storage(md, _empty_sidecar())
        assert exc.value.rule_id == "unmatched-cm"

    def test_cm_bad_uuid_aborts(self):
        md = "# T\n\n<!--cm:not-a-uuid-->x<!--/cm:not-a-uuid-->\n"
        with pytest.raises(S.PushAbort) as exc:
            md_to_storage(md, _empty_sidecar())
        assert exc.value.rule_id == "bad-marker-syntax"


# ---------------------------------------------------------------------------
# CB markers
# ---------------------------------------------------------------------------


class TestCB:
    def test_cb_known_hash_reinjects_xml(self):
        sidecar = _empty_sidecar(
            blocks={HASH_A: {"xml": '<ac:structured-macro ac:name="info"><x/></ac:structured-macro>', "kind": "macro:info"}}
        )
        md = f"# T\n\n> [confluence: macro:info]\n{S.cb(HASH_A)}\n"
        _, body = md_to_storage(md, sidecar)
        assert '<ac:structured-macro ac:name="info">' in body
        # label blockquote does NOT appear in body
        assert "[confluence:" not in body

    def test_cb_unknown_hash_aborts(self):
        md = f"# T\n\n{S.cb(HASH_A)}\n"
        with pytest.raises(S.PushAbort) as exc:
            md_to_storage(md, _empty_sidecar())
        assert exc.value.rule_id == "unknown-cb-hash"


# ---------------------------------------------------------------------------
# Inline opaque span
# ---------------------------------------------------------------------------


class TestInlineOpaque:
    def test_ci_span_reinjects_xml(self):
        sidecar = _empty_sidecar(
            inline_blocks={HASH_A: {"xml": '<ac:link><ri:user ri:account-id="abc"/></ac:link>', "kind": "link"}}
        )
        md = f"# T\n\nhi {S.ci_span(HASH_A, '[@mention]')} bye\n"
        _, body = md_to_storage(md, sidecar)
        assert '<ac:link><ri:user ri:account-id="abc"/></ac:link>' in body
        # cosmetic placeholder text is dropped
        assert "[@mention]" not in body

    def test_ci_span_unknown_hash_aborts(self):
        md = f"# T\n\n{S.ci_span(HASH_A, '[x]')}\n"
        with pytest.raises(S.PushAbort) as exc:
            md_to_storage(md, _empty_sidecar())
        assert exc.value.rule_id == "unknown-ci-hash"


# ---------------------------------------------------------------------------
# Code blocks
# ---------------------------------------------------------------------------


class TestCode:
    def test_fence_language_and_body(self):
        md = "# T\n\n```python\nprint(1)\n```\n"
        _, body = md_to_storage(md, _empty_sidecar())
        assert '<ac:structured-macro ac:name="code">' in body
        assert '<ac:parameter ac:name="language">python</ac:parameter>' in body
        assert "<![CDATA[print(1)]]>" in body

    def test_fence_with_cc_trailer_reattaches_params(self):
        sidecar = _empty_sidecar(
            code_blocks={UUID_A: {"params": {"breakoutMode": "wide", "breakoutWidth": "760"}}}
        )
        md = f"# T\n\n```python\nbody\n```\n{S.cc(UUID_A)}\n"
        _, body = md_to_storage(md, sidecar)
        assert '<ac:parameter ac:name="breakoutMode">wide</ac:parameter>' in body
        assert '<ac:parameter ac:name="breakoutWidth">760</ac:parameter>' in body
        assert '<ac:parameter ac:name="language">python</ac:parameter>' in body

    def test_fence_no_language(self):
        md = "# T\n\n```\nplain\n```\n"
        _, body = md_to_storage(md, _empty_sidecar())
        assert "<![CDATA[plain]]>" in body
        assert 'ac:parameter ac:name="language"' not in body


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


class TestTaskList:
    def test_task_list_emits_ac_task_list(self):
        sidecar = _empty_sidecar(
            tasks={
                UUID_A: {"status": "incomplete", "task_id": "1", "task_list_id": "L"},
                UUID_B: {"status": "complete", "task_id": "2", "task_list_id": "L"},
            }
        )
        md = (
            f"# T\n\n"
            f"- {S.ct(UUID_A)} first\n"
            f"- {S.ct(UUID_B)} second\n"
        )
        _, body = md_to_storage(md, sidecar)
        assert '<ac:task-list ac:task-list-id="L">' in body
        assert f"<ac:task-uuid>{UUID_A}</ac:task-uuid>" in body
        assert "<ac:task-status>incomplete</ac:task-status>" in body
        assert "<ac:task-status>complete</ac:task-status>" in body
        assert "<ac:task-body>first</ac:task-body>" in body
        assert "<ac:task-body>second</ac:task-body>" in body


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


class TestImage:
    def test_image_with_ci_trailer_reattaches_attrs(self):
        sidecar = _empty_sidecar(
            images={
                HASH_A: {
                    "filename": "diagram.png",
                    "ac_attrs": {
                        "{http://atlassian.com/content}align": "center",
                        "{http://atlassian.com/content}width": "300",
                    },
                    "ri_attrs": {
                        "{http://atlassian.com/resource/identifier}filename": "diagram.png",
                        "{http://atlassian.com/resource/identifier}version-at-save": "2",
                    },
                }
            }
        )
        md = f"# T\n\n![alt-text](./_meta/attachments/diagram.png){S.ci_trailer(HASH_A)}\n"
        _, body = md_to_storage(md, sidecar)
        assert "<ac:image" in body
        assert 'ac:align="center"' in body
        assert 'ac:width="300"' in body
        assert 'ac:alt="alt-text"' in body
        assert '<ri:attachment ri:filename="diagram.png" ri:version-at-save="2"/>' in body

    def test_new_attachment_without_sidecar_aborts(self):
        md = "# T\n\n![](./_meta/attachments/new.png)\n"
        with pytest.raises(S.PushAbort) as exc:
            md_to_storage(md, _empty_sidecar())
        assert exc.value.rule_id == "new-attachment"


# ---------------------------------------------------------------------------
# Editable panels
# ---------------------------------------------------------------------------


class TestEditablePanels:
    # P7
    def test_macro_panel_push_uses_sidecar_name_not_hint(self):
        # marker says style=warning, sidecar says name=info — sidecar wins.
        sidecar = _empty_sidecar(
            panels={UUID_A: {"shape": "macro", "name": "info", "params": {}}}
        )
        md = (
            f"# T\n\n"
            f"<!--cp:{UUID_A} style=warning-->\n\n"
            f"body text\n\n"
            f"<!--/cp:{UUID_A}-->\n"
        )
        _, body = md_to_storage(md, sidecar)
        assert '<ac:structured-macro ac:name="info"' in body
        assert "ac:name=\"warning\"" not in body
        assert "<p>body text</p>" in body
        assert "<ac:rich-text-body>" in body

    def test_macro_panel_with_params_reattaches_them(self):
        sidecar = _empty_sidecar(
            panels={UUID_A: {"shape": "macro", "name": "info", "params": {"title": "Hello"}}}
        )
        md = f"# T\n\n<!--cp:{UUID_A} style=info-->\n\nbody\n\n<!--/cp:{UUID_A}-->\n"
        _, body = md_to_storage(md, sidecar)
        assert '<ac:parameter ac:name="title">Hello</ac:parameter>' in body

    # P8
    def test_adf_panel_push_preserves_fallback(self):
        fallback = '<div class="panel"><div class="panelContent">old body</div></div>'
        sidecar = _empty_sidecar(
            panels={
                UUID_A: {
                    "shape": "adf",
                    "name": "success",
                    "adf_attrs": {},
                    "adf_fallback": fallback,
                }
            }
        )
        md = f"# T\n\n<!--cp:{UUID_A} style=success-->\n\nnew body\n\n<!--/cp:{UUID_A}-->\n"
        _, body = md_to_storage(md, sidecar)
        assert '<ac:adf-extension><ac:adf-node type="panel">' in body
        assert '<ac:adf-attribute key="panel-type">success</ac:adf-attribute>' in body
        assert "<ac:adf-content><p>new body</p></ac:adf-content>" in body
        assert f"<ac:adf-fallback>{fallback}</ac:adf-fallback>" in body

    def test_adf_panel_extra_attrs_reemitted(self):
        sidecar = _empty_sidecar(
            panels={
                UUID_A: {
                    "shape": "adf",
                    "name": "info",
                    "adf_attrs": {"someKey": "someVal"},
                    "adf_fallback": "",
                }
            }
        )
        md = f"# T\n\n<!--cp:{UUID_A} style=info-->\n\nx\n\n<!--/cp:{UUID_A}-->\n"
        _, body = md_to_storage(md, sidecar)
        assert '<ac:adf-attribute key="someKey">someVal</ac:adf-attribute>' in body

    # P9
    def test_panel_push_unknown_uuid_aborts(self):
        md = f"# T\n\n<!--cp:{UUID_A} style=info-->\n\nbody\n\n<!--/cp:{UUID_A}-->\n"
        with pytest.raises(S.PushAbort) as exc:
            md_to_storage(md, _empty_sidecar())
        assert exc.value.rule_id == "unknown-cp-uuid"

    # P10
    def test_panel_push_unmatched_open_aborts(self):
        sidecar = _empty_sidecar(
            panels={UUID_A: {"shape": "macro", "name": "info", "params": {}}}
        )
        md = f"# T\n\n<!--cp:{UUID_A} style=info-->\n\nbody\n"
        with pytest.raises(S.PushAbort) as exc:
            md_to_storage(md, sidecar)
        assert exc.value.rule_id == "unmatched-cp"

    def test_panel_push_unmatched_close_aborts(self):
        md = f"# T\n\n<!--/cp:{UUID_A}-->\n"
        with pytest.raises(S.PushAbort) as exc:
            md_to_storage(md, _empty_sidecar())
        assert exc.value.rule_id == "unmatched-cp"

    def test_panel_push_mismatched_uuid_aborts(self):
        sidecar = _empty_sidecar(
            panels={
                UUID_A: {"shape": "macro", "name": "info", "params": {}},
                UUID_B: {"shape": "macro", "name": "info", "params": {}},
            }
        )
        md = f"# T\n\n<!--cp:{UUID_A} style=info-->\n\nx\n\n<!--/cp:{UUID_B}-->\n"
        with pytest.raises(S.PushAbort) as exc:
            md_to_storage(md, sidecar)
        assert exc.value.rule_id == "unmatched-cp"


# ---------------------------------------------------------------------------
# End-to-end round-trip on the live fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_page() -> dict:
    if not FIXTURE.exists():
        pytest.skip(f"fixture {FIXTURE} not present")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class TestRoundTrip:
    def test_fixture_pull_then_push_returns_title_unchanged(self, fixture_page):
        body = fixture_page["body"]["storage"]["value"]
        title = fixture_page["title"]
        md, sidecar = storage_to_md(body, title)
        title_out, _ = md_to_storage(md, sidecar.to_json())
        assert title_out == title

    def test_fixture_round_trip_preserves_opaque_payloads(self, fixture_page):
        body = fixture_page["body"]["storage"]["value"]
        title = fixture_page["title"]
        md, sidecar = storage_to_md(body, title)
        _, body_out = md_to_storage(md, sidecar.to_json())
        # Every opaque block's payload XML appears verbatim in the output.
        # We check for a distinctive substring per block.
        for h, entry in sidecar.to_json()["blocks"].items():
            xml = entry["xml"]
            # extract a stable inner substring (ignore xmlns + outer attrs)
            inner = re.sub(r"^<[^>]+>", "", xml)
            inner = re.sub(r"<[^/][^>]*>$", "", inner)
            inner = inner.strip()[:50]
            if inner and "<" not in inner:
                assert inner in body_out

    def test_fixture_round_trip_preserves_panel_bodies(self, fixture_page):
        # Every panel body's plain text from input survives to output.
        body = fixture_page["body"]["storage"]["value"]
        title = fixture_page["title"]
        md, sidecar = storage_to_md(body, title)
        _, body_out = md_to_storage(md, sidecar.to_json())
        assert sidecar.panels, "fixture must contain editable panels"
        # extract panel body chunks from input (between rich-text-body / adf-content tags)
        for chunk in re.findall(r"<ac:rich-text-body>(.*?)</ac:rich-text-body>", body, re.DOTALL):
            text = re.sub(r"<[^>]+>", "", chunk).strip()
            if text:
                assert text in body_out
        for chunk in re.findall(r"<ac:adf-content>(.*?)</ac:adf-content>", body, re.DOTALL):
            text = re.sub(r"<[^>]+>", "", chunk).strip()
            if text:
                assert text in body_out

    def test_fixture_round_trip_preserves_cm_uuids(self, fixture_page):
        body = fixture_page["body"]["storage"]["value"]
        title = fixture_page["title"]
        md, sidecar = storage_to_md(body, title)
        _, body_out = md_to_storage(md, sidecar.to_json())
        # All cm UUIDs from input must appear in output as ac:ref="UUID"
        for m in re.finditer(r'ac:ref="([0-9a-f-]+)"', body):
            assert f'ac:ref="{m.group(1)}"' in body_out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s)
