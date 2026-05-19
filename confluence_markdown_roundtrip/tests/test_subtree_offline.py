"""Offline tests for subtree.py: manifest shape, leaf-first ordering,
non-page filter."""

from __future__ import annotations

from pathlib import Path

from confluence_markdown_roundtrip.subtree import (
    SubtreeEntry,
    SubtreeManifest,
    page_dirs_leaf_first,
)


class TestManifestSerialization:
    def test_round_trip(self):
        m = SubtreeManifest(
            root_page_id="1",
            space_id="S",
            fetched_at="2026-01-01T00:00:00Z",
            pages=[
                SubtreeEntry(page_id="1", path="index.md", parent_id=None, title="Root", slug="root"),
                SubtreeEntry(page_id="2", path="child-a/index.md", parent_id="1", title="Child A", slug="child-a"),
            ],
        )
        data = m.to_json()
        m2 = SubtreeManifest.from_json(data)
        assert m2.root_page_id == "1"
        assert len(m2.pages) == 2
        assert m2.pages[1].slug == "child-a"


class TestLeafFirstOrdering:
    def _manifest(self, edges: list[tuple[str, str | None]]) -> SubtreeManifest:
        """Build a manifest from (page_id, parent_id) pairs."""
        pages = [
            SubtreeEntry(
                page_id=pid,
                path="index.md" if parent is None else f"{pid}/index.md",
                parent_id=parent,
                title=f"P{pid}",
                slug=pid,
            )
            for pid, parent in edges
        ]
        return SubtreeManifest(
            root_page_id=edges[0][0],
            space_id="S",
            fetched_at="",
            pages=pages,
        )

    def test_root_only(self, tmp_path):
        m = self._manifest([("1", None)])
        order = page_dirs_leaf_first(tmp_path, m)
        assert order == [tmp_path]

    def test_single_child(self, tmp_path):
        m = self._manifest([("1", None), ("2", "1")])
        order = page_dirs_leaf_first(tmp_path, m)
        assert order == [tmp_path / "2", tmp_path]

    def test_grandchild_then_children_then_root(self, tmp_path):
        # tree:
        #   1 (root)
        #   ├── 2 (child A)
        #   │   └── 3 (grandchild)
        #   └── 4 (child B)
        # leaf-first: 3 before 2; 2,4 before 1.
        m = self._manifest([
            ("1", None),
            ("2", "1"),
            ("3", "2"),
            ("4", "1"),
        ])
        order = page_dirs_leaf_first(tmp_path, m)
        # 3 must come before 2; 2 and 4 must come before 1
        assert order.index(tmp_path / "3") < order.index(tmp_path / "2")
        assert order.index(tmp_path / "2") < order.index(tmp_path)
        assert order.index(tmp_path / "4") < order.index(tmp_path)


# ---------------------------------------------------------------------------
# D05 — descendants filter: pages only (offline mock)
# ---------------------------------------------------------------------------


class TestNonPageFilter:
    """The walker must filter `type != "page"` entries (whiteboards,
    databases, embeds). Tested via a synthetic httpx mock."""

    def test_filter_via_client(self, monkeypatch):
        from confluence_markdown_roundtrip.api import ConfluenceClient, Credentials

        # Synthetic httpx transport that returns a mixed descendants response.
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            # Match the descendants endpoint
            assert "/descendants" in str(request.url)
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"id": "10", "title": "real page", "type": "page", "parentId": "1"},
                        {"id": "11", "title": "wb", "type": "whiteboard", "parentId": "1"},
                        {"id": "12", "title": "db", "type": "database", "parentId": "1"},
                        {"id": "13", "title": "embed", "type": "embed", "parentId": "1"},
                        {"id": "14", "title": "folder", "type": "folder", "parentId": "1"},
                        {"id": "15", "title": "nested", "type": "page", "parentId": "10"},
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
        out = list(client.list_descendants("1"))
        ids = {d.id for d in out}
        assert ids == {"10", "15"}, f"expected pages only, got {ids}"
