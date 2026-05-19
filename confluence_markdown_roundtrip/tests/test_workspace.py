"""Tests for the on-disk workspace layout — write, read, integrity checks.

Most important coverage: the `orig-tampered` / `meta-tampered` rule-ids
fire correctly when the user (or their editor) touches files inside
`_meta/`. These are load-bearing safety checks, not cosmetic.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from confluence_markdown_roundtrip import sentinels as S
from confluence_markdown_roundtrip.workspace import PageWorkspace


def _make(tmp_path, md_text="# T\n\nhello\n"):
    ws = PageWorkspace(
        root=tmp_path / "page",
        page_id="123",
        title="T",
        space_id="SP",
        parent_id=None,
        base_version=1,
        base_storage_sha256="abc",
        base_md_sha256="",  # filled by write_initial
        fetched_at="2026-05-16T00:00:00Z",
    )
    ws.write_initial(md_text)
    return ws


class TestRoundTrip:
    def test_write_then_load(self, tmp_path):
        ws = _make(tmp_path)
        loaded = PageWorkspace.from_disk(ws.root)
        assert loaded.page_id == "123"
        assert loaded.title == "T"
        assert loaded.base_version == 1
        assert loaded.base_md_sha256 == ws.base_md_sha256

    def test_orig_matches_index_md_byte_for_byte(self, tmp_path):
        ws = _make(tmp_path, md_text="# Hi\n\nbody\n")
        assert ws.index_md_path.read_text() == ws.orig_path.read_text()

    def test_base_md_sha256_matches_orig_content(self, tmp_path):
        ws = _make(tmp_path)
        recomputed = hashlib.sha256(ws.orig_path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        assert recomputed == ws.base_md_sha256


class TestIntegrityChecks:
    def test_missing_sidecar_raises_meta_tampered(self, tmp_path):
        ws = _make(tmp_path)
        ws.sidecar_path.unlink()
        with pytest.raises(S.PushAbort) as exc:
            PageWorkspace.from_disk(ws.root)
        assert exc.value.rule_id == "meta-tampered"

    def test_corrupt_sidecar_raises_meta_tampered(self, tmp_path):
        ws = _make(tmp_path)
        ws.sidecar_path.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(S.PushAbort) as exc:
            PageWorkspace.from_disk(ws.root)
        assert exc.value.rule_id == "meta-tampered"

    def test_missing_orig_raises_orig_tampered(self, tmp_path):
        ws = _make(tmp_path)
        ws.orig_path.unlink()
        with pytest.raises(S.PushAbort) as exc:
            PageWorkspace.from_disk(ws.root)
        assert exc.value.rule_id == "orig-tampered"

    def test_modified_orig_raises_orig_tampered(self, tmp_path):
        ws = _make(tmp_path)
        ws.orig_path.write_text("tampered content\n", encoding="utf-8")
        with pytest.raises(S.PushAbort) as exc:
            PageWorkspace.from_disk(ws.root)
        assert exc.value.rule_id == "orig-tampered"

    def test_user_edited_index_md_is_fine(self, tmp_path):
        """index.md is user-owned — editing it must NOT trigger orig-tampered."""
        ws = _make(tmp_path)
        ws.index_md_path.write_text("# T\n\nnew body\n", encoding="utf-8")
        loaded = PageWorkspace.from_disk(ws.root)  # must not raise
        assert loaded.page_id == "123"


class TestSidecarShape:
    def test_sidecar_json_keys(self, tmp_path):
        ws = _make(tmp_path)
        data = json.loads(ws.sidecar_path.read_text())
        expected = {
            "page_id",
            "space_id",
            "title",
            "parent_id",
            "base_version",
            "base_storage_sha256",
            "base_md_sha256",
            "fetched_at",
            "blocks",
            "inline_blocks",
            "tasks",
            "code_blocks",
            "images",
            "metadata_preserve",
        }
        assert expected.issubset(data.keys())
