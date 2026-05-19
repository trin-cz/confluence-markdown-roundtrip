"""Phase 5 — attachment binary download (read-only).

The Phase 1.5 reference fixture page carries a PNG and an SVG attachment
referenced by `<ac:image>` tags. After `pull`, both binaries must land in
`_meta/attachments/<filename>` so any markdown previewer renders them inline.

Live-only: gated by `--integration` like the rest of the online suite. The
page is the read-only reference fixture; the test never writes to Confluence.
The page ID comes from `~/.config/confluence-markdown-roundtrip/live-tests.toml`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.online


def test_A1_attachment_binaries_downloaded_on_pull(make_workspace, reference_page_id):
    ws = make_workspace(reference_page_id)
    att_dir = ws / "_meta" / "attachments"
    assert att_dir.is_dir()

    # Filenames are stable; the fixture page has a PNG and an SVG.
    files = {p.name: p for p in att_dir.iterdir() if p.is_file()}
    assert "image-20260516-131005.png" in files, f"got: {sorted(files)}"
    assert "logo.svg" in files, f"got: {sorted(files)}"

    png = files["image-20260516-131005.png"].read_bytes()
    assert png.startswith(b"\x89PNG\r\n\x1a\n"), "not a PNG"
    assert len(png) > 1000

    svg = files["logo.svg"].read_bytes()
    assert svg.lstrip().startswith(b"<"), "not XML/SVG"
    assert b"<svg" in svg


def test_subtree_pull_downloads_attachments(tmp_path: Path, reference_page_id):
    """Subtree-pull regression: every page in the tree that references an
    attachment must have its binaries fetched, not just the single-page path."""
    r = subprocess.run(
        [
            sys.executable, "-m", "confluence_markdown_roundtrip.cli",
            "pull", reference_page_id, "--subtree", "--into", str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    root_dir = Path(r.stdout.strip().splitlines()[-1])

    att_dir = root_dir / "_meta" / "attachments"
    files = {p.name for p in att_dir.iterdir() if p.is_file()}
    assert "image-20260516-131005.png" in files, f"got: {sorted(files)}"
    assert "logo.svg" in files, f"got: {sorted(files)}"
