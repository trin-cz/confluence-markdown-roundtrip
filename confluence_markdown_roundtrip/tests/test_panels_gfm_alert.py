"""Phase 6 — panels render as GFM Alerts in MD preview.

V1: pulled MD contains the right `> [!KIND]` blocks for each panel type.
V2: pull → push (no edits) produces canonically-identical panel storage.
V3: a Phase-4-style opener (with `style=…` attr, no alert inside the body)
    still parses cleanly — backwards compat.
V4: user removes the `> [!KIND]` line manually → push still works, panel
    type unchanged (sidecar wins), body content preserved.

V1 uses the live Phase 1.5 fixture page (read-only); V2-V4 are offline.
The reference page ID is read from `~/.config/confluence-markdown-roundtrip/live-tests.toml`.
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
_UUID_A = "11111111-1111-4111-8111-111111111111"


# ---------------------------------------------------------------------------
# V1 — fixture pull emits the right GFM alert blocks
# ---------------------------------------------------------------------------


@pytest.mark.online
def test_V1_panel_renders_as_gfm_alert(tmp_path: Path, reference_page_id):
    r = subprocess.run(
        [sys.executable, "-m", "confluence_markdown_roundtrip.cli",
         "pull", reference_page_id, "--into", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    page_dir = Path(r.stdout.strip().splitlines()[-1])
    md = (page_dir / "index.md").read_text(encoding="utf-8")

    # The fixture page carries the four macro panel types: info, note (×2),
    # warning, tip. Their GFM-alert mapping:
    assert "> [!NOTE]" in md       # info → NOTE
    assert "> [!IMPORTANT]" in md  # note → IMPORTANT
    assert "> [!WARNING]" in md    # warning → WARNING
    assert "> [!TIP]" in md        # tip → TIP

    # No `style=` attribute on cp openers in Phase 6.
    assert " style=" not in re.search(r"<!--cp:[^>]+-->", md).group(0)


# ---------------------------------------------------------------------------
# V2 — unedited pull → push produces canonically-identical panel storage
# ---------------------------------------------------------------------------


def _panel_xml(name: str, body: str) -> str:
    return (
        f'<ac:structured-macro xmlns:ac="{ACNS}" ac:name="{name}" ac:schema-version="1">'
        f'<ac:rich-text-body>{body}</ac:rich-text-body>'
        f'</ac:structured-macro>'
    )


def _adf_panel_xml(panel_type: str, body: str) -> str:
    return (
        f'<ac:adf-extension xmlns:ac="{ACNS}">'
        f'<ac:adf-node type="panel">'
        f'<ac:adf-attribute key="panel-type">{panel_type}</ac:adf-attribute>'
        f'<ac:adf-content>{body}</ac:adf-content>'
        f'</ac:adf-node>'
        f'</ac:adf-extension>'
    )


def _canon(xhtml: str) -> bytes:
    wrapped = f'<root xmlns:ac="{ACNS}" xmlns:ri="{RINS}">{xhtml}</root>'
    return S.c14n2(S.strip_bookkeeping(etree.fromstring(wrapped.encode("utf-8"))))


@pytest.mark.parametrize("panel_name", ["info", "note", "warning", "tip"])
def test_V2_macro_panel_unedited_round_trip_is_canonical(panel_name: str):
    """Pull a macro panel → no edits → push → canonical XML matches input."""
    orig = _panel_xml(panel_name, "<p>some body text</p>")
    md, side = storage_to_md(orig, "T")
    _, body = md_to_storage(md, side.to_json())
    assert _canon(body) == _canon(orig)


@pytest.mark.parametrize("panel_type", ["info", "note", "warning", "success", "error"])
def test_V2_adf_panel_unedited_round_trip_is_canonical(panel_type: str):
    orig = _adf_panel_xml(panel_type, "<p>adf body</p>")
    md, side = storage_to_md(orig, "T")
    _, body = md_to_storage(md, side.to_json())
    assert _canon(body) == _canon(orig)


# ---------------------------------------------------------------------------
# V3 — Phase-4 style=… opener (with raw body, no alert) still parses
# ---------------------------------------------------------------------------


def test_V3_phase4_style_attr_opener_still_parses():
    """Workspaces pulled before Phase 6 have `<!--cp:UUID style=NAME-->` openers
    with raw paragraph bodies. Those must continue to push cleanly."""
    sidecar = {
        "title": "T",
        "blocks": {}, "inline_blocks": {}, "tasks": {}, "code_blocks": {}, "images": {},
        "panels": {_UUID_A: {"shape": "macro", "name": "info", "params": {}}},
    }
    md = (
        "# T\n\n"
        f"<!--cp:{_UUID_A} style=info-->\n\n"
        "phase-4 body text\n\n"
        f"<!--/cp:{_UUID_A}-->\n"
    )
    _, body = md_to_storage(md, sidecar)
    assert '<ac:structured-macro ac:name="info"' in body
    assert "<p>phase-4 body text</p>" in body


# ---------------------------------------------------------------------------
# V4 — user strips the [!KIND] tag manually; push still works
# ---------------------------------------------------------------------------


def test_V4_user_strips_alert_tag_pushes_cleanly():
    """A user might remove the visual `> [!KIND]` line and just keep `> body`.
    The strip is uniform on `>`-prefixed lines, so the body still extracts;
    panel type comes from the sidecar regardless."""
    sidecar = {
        "title": "T",
        "blocks": {}, "inline_blocks": {}, "tasks": {}, "code_blocks": {}, "images": {},
        "panels": {_UUID_A: {"shape": "macro", "name": "warning", "params": {}}},
    }
    md = (
        "# T\n\n"
        f"<!--cp:{_UUID_A}-->\n\n"
        "> body without alert tag\n\n"
        f"<!--/cp:{_UUID_A}-->\n"
    )
    _, body = md_to_storage(md, sidecar)
    # Panel type stays `warning` (sidecar), body content preserved.
    assert '<ac:structured-macro ac:name="warning"' in body
    assert "<p>body without alert tag</p>" in body
