"""Subtree support: pull/push/status across a Confluence page tree.

The on-disk layout (plan §"On-disk layout") nests pages as subdirectories:

    <root-slug>/
      index.md
      _meta/
        _subtree.json     # only at the root
        index.conf.json
        index.md.orig
      child-a/
        index.md
        _meta/...
        grandchild/
          index.md
          _meta/...

`_subtree.json` is the manifest that links page IDs to relative directory
paths; it is the entry point for `push <dir>` and `status <dir>`.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import sentinels as S
from .api import APIError, ConfluenceClient, Page
from .storage_to_md import storage_to_md
from .workspace import PageWorkspace, download_referenced_attachments, iso_now


SUBTREE_NAME = "_subtree.json"


@dataclass
class SubtreeEntry:
    page_id: str
    path: str   # POSIX-style, relative to subtree root, e.g. "child-a/index.md"
    parent_id: str | None
    title: str
    slug: str


@dataclass
class SubtreeManifest:
    root_page_id: str
    space_id: str
    fetched_at: str
    pages: list[SubtreeEntry] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "root_page_id": self.root_page_id,
            "space_id": self.space_id,
            "fetched_at": self.fetched_at,
            "pages": [e.__dict__ for e in self.pages],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SubtreeManifest":
        return cls(
            root_page_id=str(data["root_page_id"]),
            space_id=str(data.get("space_id", "")),
            fetched_at=data.get("fetched_at", ""),
            pages=[SubtreeEntry(**p) for p in data.get("pages", [])],
        )


def manifest_path_for(root_dir: Path) -> Path:
    return root_dir / "_meta" / SUBTREE_NAME


def is_subtree_root(path: Path) -> bool:
    return manifest_path_for(path).exists()


# ---------------------------------------------------------------------------
# Pull
# ---------------------------------------------------------------------------


def pull_subtree(client: ConfluenceClient, root_page_id: int | str, into_dir: Path) -> Path:
    """Pull a page + all descendants. Returns the root workspace directory.

    BFS the descendant list (plan §"Subtree pull"), slugifying titles with
    collision resolution at each level. Then for every page in BFS order:
    fetch + storage_to_md + write its workspace. Write _subtree.json last.
    """
    root_page = client.get_page(root_page_id)

    # Collect descendants (BFS via API), filter to pages only.
    # `depth=10` is the v2 API's max; the default (omitted) only returns the
    # first level or two. Trees deeper than 10 levels need iterative walking
    # (Phase 7).
    descendants = list(client.list_descendants(root_page.id, depth=10))

    # Build tree: parent_id -> list of children, ordered as returned by API.
    children_of: dict[str, list[Page | _DescStub]] = {root_page.id: []}
    by_id: dict[str, Page | _DescStub] = {root_page.id: root_page}
    for d in descendants:
        children_of.setdefault(d.parent_id or "", []).append(_DescStub(d.id, d.title, d.parent_id))
        by_id[d.id] = _DescStub(d.id, d.title, d.parent_id)

    # BFS order from root.
    bfs_order: list[str] = [root_page.id]
    seen: set[str] = {root_page.id}
    queue = [root_page.id]
    while queue:
        nxt = queue.pop(0)
        for child in children_of.get(nxt, []):
            if child.id in seen:
                continue
            seen.add(child.id)
            bfs_order.append(child.id)
            queue.append(child.id)

    # Slugify each page; resolve collisions PER PARENT (siblings share
    # a directory, so the namespace is per-parent).
    slugs_by_parent: dict[str, set[str]] = {}
    slug_of: dict[str, str] = {}
    for pid in bfs_order:
        node = by_id[pid]
        parent = node.parent_id if pid != root_page.id else None
        taken = slugs_by_parent.setdefault(parent or "", set())
        slug = S.slugify_unique(node.title, taken)
        taken.add(slug)
        slug_of[pid] = slug

    # Compute relative path of each page (POSIX-style, joined with /).
    path_of: dict[str, str] = {root_page.id: slug_of[root_page.id]}
    for pid in bfs_order[1:]:
        parent = by_id[pid].parent_id or root_page.id
        path_of[pid] = f"{path_of[parent]}/{slug_of[pid]}"

    # Fetch each page; record which ones we got. Inaccessible descendants
    # (404 = perm-denied, archived, or stale entries) are skipped with a
    # warning so the rest of the subtree still pulls.
    fetched_pages: dict[str, Page] = {}
    for pid in bfs_order:
        if pid == root_page.id:
            fetched_pages[pid] = root_page
            continue
        try:
            fetched_pages[pid] = client.get_page(pid)
        except APIError as e:
            print(
                f"warning: skipping page {pid} ({by_id[pid].title!r}): {e.status}",
                file=sys.stderr,
            )

    # Build the in-tree index AFTER we know which pages actually fetched.
    # Links to skipped pages stay external on pull (since their pid is not
    # in pid_to_relpath); subsequent pulls that gain access will rewrite them.
    # path_of values are POSIX relative to into_dir; append /index.md to get
    # a file path suitable for `posixpath.relpath`.
    pid_to_relpath: dict[str, str] = {
        pid: f"{path_of[pid]}/index.md" for pid in fetched_pages
    }
    # title_to_pid is keyed by title alone: Confluence enforces unique titles
    # per space, and a subtree pull is always single-space (Future plans:
    # cross-space). The resolver in storage_to_md only consults this dict
    # when content-id is absent, so collision risk is bounded by the rare
    # ri:content-title-only link form.
    title_to_pid: dict[tuple[str, str], str] = {
        ("", page.title): pid for pid, page in fetched_pages.items()
    }

    # Now write each fetched page.
    root_workspace_dir = into_dir / slug_of[root_page.id]
    manifest = SubtreeManifest(
        root_page_id=root_page.id,
        space_id=root_page.space_id,
        fetched_at=iso_now(),
    )
    for pid in bfs_order:
        if pid not in fetched_pages:
            continue
        page = fetched_pages[pid]
        rel_dir = path_of[pid]
        page_dir = into_dir / rel_dir
        page_dir.mkdir(parents=True, exist_ok=True)

        md, sidecar_obj = storage_to_md(
            page.storage_body,
            page.title,
            pid_to_relpath=pid_to_relpath,
            title_to_pid=title_to_pid,
            self_page_relpath=pid_to_relpath[pid],
        )
        ws = PageWorkspace(
            root=page_dir,
            page_id=page.id,
            title=page.title,
            space_id=page.space_id,
            parent_id=page.parent_id,
            base_version=page.version,
            base_storage_sha256=hashlib.sha256(page.storage_body.encode("utf-8")).hexdigest(),
            base_md_sha256="",
            fetched_at=iso_now(),
            blocks=sidecar_obj.blocks,
            inline_blocks=sidecar_obj.inline_blocks,
            tasks=sidecar_obj.tasks,
            code_blocks=sidecar_obj.code_blocks,
            images=sidecar_obj.images,
            panels=sidecar_obj.panels,
            links=sidecar_obj.links,
        )
        ws.re_pull(md)

        if sidecar_obj.images:
            try:
                failed = download_referenced_attachments(client, page.id, ws)
            except APIError as e:
                print(f"warning: attachment fetch failed for {page.id}: {e}", file=sys.stderr)
            else:
                if failed:
                    print(
                        f"warning: could not download {len(failed)} attachment(s) for {page.id}: {', '.join(failed)}",
                        file=sys.stderr,
                    )

        # Paths are stored relative to the subtree root directory, not the
        # `--into` parent. Root's path is "index.md"; children sit at
        # "<rel-to-root>/index.md".
        if pid == root_page.id:
            rel_path = "index.md"
        else:
            rel_to_root = rel_dir[len(slug_of[root_page.id]) + 1 :]
            rel_path = f"{rel_to_root}/index.md"
        manifest.pages.append(
            SubtreeEntry(
                page_id=pid,
                path=rel_path,
                parent_id=by_id[pid].parent_id if pid != root_page.id else None,
                title=page.title,
                slug=slug_of[pid],
            )
        )

    # Write the manifest LAST so partial pulls don't look complete.
    (root_workspace_dir / "_meta").mkdir(parents=True, exist_ok=True)
    manifest_path_for(root_workspace_dir).write_text(
        json.dumps(manifest.to_json(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return root_workspace_dir


@dataclass
class _DescStub:
    id: str
    title: str
    parent_id: str | None


# ---------------------------------------------------------------------------
# Subtree iteration helpers
# ---------------------------------------------------------------------------


def load_manifest(root_dir: Path) -> SubtreeManifest:
    path = manifest_path_for(root_dir)
    if not path.exists():
        raise S.meta_tampered(f"missing {path}", file=str(root_dir))
    try:
        return SubtreeManifest.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError) as e:
        raise S.meta_tampered(f"sidecar {path} not parseable: {e}", file=str(root_dir))


def page_dirs_leaf_first(root_dir: Path, manifest: SubtreeManifest) -> list[Path]:
    """Return page directories in leaf-first push order.

    Order: child pages before their parents. Standard post-order DFS from
    the manifest root.
    """
    by_id = {e.page_id: e for e in manifest.pages}
    children_of: dict[str | None, list[str]] = {}
    for e in manifest.pages:
        children_of.setdefault(e.parent_id, []).append(e.page_id)

    order: list[str] = []
    stack: list[tuple[str, bool]] = [(manifest.root_page_id, False)]
    while stack:
        pid, processed = stack.pop()
        if processed:
            order.append(pid)
            continue
        stack.append((pid, True))
        for child_id in children_of.get(pid, []):
            stack.append((child_id, False))

    out: list[Path] = []
    for pid in order:
        rel = by_id[pid].path
        # rel is "index.md" for root or "<sub>/.../index.md" for descendants —
        # always relative to root_dir, so this resolves cleanly.
        out.append(root_dir / Path(rel).parent)
    return out
