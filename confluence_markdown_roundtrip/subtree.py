"""Subtree support: pull/push/status across a Confluence page tree.

The on-disk layout (plan §"On-disk layout") nests pages as subdirectories:

    <into-dir>/                  ← workspace root (--into); the workspace
      _meta/
        _subtree.json            ← workspace-level manifest (Phase 8)
      <topmost-ancestor-slug>/
        index.md
        _meta/
          index.conf.json
          index.md.orig
        <descendant-slug>/
          index.md
          _meta/...

`_subtree.json` is the manifest that links page IDs to relative directory
paths (relative to `<into-dir>`); it is the entry point for `push <dir>`
and `status <dir>`. Phase 8 onward, the manifest lives at workspace root,
above every page directory.
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
    path: str   # POSIX-style, relative to workspace root (--into), e.g. "engineering/architecture/index.md"
    parent_id: str | None
    title: str
    slug: str


@dataclass
class SubtreeManifest:
    """Phase 9: workspace holds a *forest* of trees, one per Confluence
    space pulled into the workspace. `root_page_ids` lists the topmost
    ancestor of each tree (or the requested page if no ancestors). The
    workspace is the union of these trees; pages are arranged on disk by
    Confluence parent-child within each tree.
    """

    root_page_ids: list[str]
    fetched_at: str
    pages: list[SubtreeEntry] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "root_page_ids": list(self.root_page_ids),
            "fetched_at": self.fetched_at,
            "pages": [e.__dict__ for e in self.pages],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SubtreeManifest":
        # Accept both Phase 8 (`root_page_id` singular) and Phase 9
        # (`root_page_ids` plural) manifests. Always emit plural on write.
        if "root_page_ids" in data:
            roots = [str(x) for x in data["root_page_ids"]]
        elif "root_page_id" in data:
            roots = [str(data["root_page_id"])]
        else:
            raise KeyError("manifest missing root_page_ids/root_page_id")
        return cls(
            root_page_ids=roots,
            fetched_at=data.get("fetched_at", ""),
            pages=[SubtreeEntry(**p) for p in data.get("pages", [])],
        )


def manifest_path_for(workspace_dir: Path) -> Path:
    """Workspace-level manifest path. `workspace_dir` is the `--into` directory."""
    return workspace_dir / "_meta" / SUBTREE_NAME


def is_subtree_root(path: Path) -> bool:
    return manifest_path_for(path).exists()


# ---------------------------------------------------------------------------
# Pull
# ---------------------------------------------------------------------------


def pull_subtree(
    client: ConfluenceClient,
    requested_page_id: int | str,
    into_dir: Path,
    *,
    with_ancestors: bool = True,
    with_descendants: bool = True,
) -> Path:
    """Pull a vertical slice of Confluence into `into_dir`.

    Phase 9: a workspace holds a *forest* of trees, one per Confluence
    space pulled into it. Each pull either re-pulls an existing tree
    (same topmost ancestor as some existing root_page_id) or **adds a
    new tree** to the forest.

    With `with_ancestors=False`, no upward walk: the requested page itself
    is the new tree's root. With `with_descendants=False`, no downward
    walk: only the ancestor chain + the requested page.

    Returns the path to the **requested page's directory**.

    After writing the new pages, every page in the workspace manifest is
    re-fetched and re-rendered so cross-page links (Phase 7) are recomputed
    against the now-complete `pid_to_relpath` index. Existing pages whose
    `index.md` is dirty get a `.remote.md` sibling, same as ordinary
    re-pull semantics.
    """
    requested_page = client.get_page(requested_page_id)

    # 1. Ancestor spine.
    ancestor_pages: list[Page] = []
    if with_ancestors:
        ancestor_ids = client.list_ancestors(requested_page.id)  # topmost-first
        # Walk from immediate parent upward; stop at the first failure
        # (perm-denied intermediate ancestor disconnects everything above).
        collected_bottom_up: list[Page] = []
        for aid in reversed(ancestor_ids):
            try:
                collected_bottom_up.append(client.get_page(aid))
            except APIError as e:
                print(
                    f"warning: stopping ancestor walk at {aid}: {e.status}",
                    file=sys.stderr,
                )
                break
        ancestor_pages = list(reversed(collected_bottom_up))

    # 2. Descendants of the requested page (BFS, page-typed).
    descendants = list(client.list_descendants(requested_page.id, depth=10)) if with_descendants else []

    # 3. This pull's tree root: topmost ancestor (or the requested page).
    new_tree_root = ancestor_pages[0] if ancestor_pages else requested_page

    # 4. Load existing workspace state (forest of zero or more trees).
    workspace_manifest_path = manifest_path_for(into_dir)
    existing_manifest: SubtreeManifest | None = None
    if workspace_manifest_path.exists():
        existing_manifest = load_manifest(into_dir)

    existing_by_pid: dict[str, SubtreeEntry] = (
        {e.page_id: e for e in existing_manifest.pages} if existing_manifest else {}
    )
    existing_roots: list[str] = list(existing_manifest.root_page_ids) if existing_manifest else []

    # 5. Determine the post-pull root set. If new_tree_root is already a
    #    root, this is an additive re-pull of that tree. Otherwise we're
    #    adding a new tree to the forest.
    all_roots: list[str] = list(existing_roots)
    if new_tree_root.id not in all_roots:
        all_roots.append(new_tree_root.id)

    # 6. Build the unified node map: ancestors (Page) + requested (Page)
    #    + descendants (stubs) + existing manifest entries (stubs).
    current_set: set[str] = {requested_page.id}
    current_set.update(p.id for p in ancestor_pages)
    current_set.update(d.id for d in descendants)

    by_id: dict[str, Page | _DescStub] = {}
    for p in ancestor_pages:
        by_id[p.id] = p
    by_id[requested_page.id] = requested_page
    for d in descendants:
        by_id[d.id] = _DescStub(d.id, d.title, d.parent_id)
    for pid, entry in existing_by_pid.items():
        if pid not in by_id:
            by_id[pid] = _DescStub(pid, entry.title, entry.parent_id)

    # 7. Build children_of and per-root reachability. Each root forms
    #    its own tree; BFS from each root independently. Pages from a
    #    given existing tree shouldn't be reparented under a different
    #    root just because their parent_id metadata happens to chain
    #    through one — only orphans (parent missing from the workspace)
    #    are treated as their own root (defensive fallback).
    children_of: dict[str, list[str]] = {}
    for pid, node in by_id.items():
        if pid in all_roots:
            continue  # roots don't have parents in this walk
        parent = node.parent_id
        if parent is None:
            continue  # truly parentless (space homepage); should be a root
        children_of.setdefault(parent, []).append(pid)

    bfs: list[str] = []
    seen: set[str] = set()
    for root_id in all_roots:
        if root_id not in by_id:
            # This root has no node in by_id — shouldn't happen for the
            # new tree, but defensively skip stale roots.
            continue
        queue: list[str] = [root_id]
        while queue:
            pid = queue.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            bfs.append(pid)
            for cid in children_of.get(pid, []):
                queue.append(cid)

    for pid in by_id:
        if pid not in seen:
            print(
                f"warning: page {pid} ({by_id[pid].title!r}) is not reachable "
                f"from any root in the forest — skipping",
                file=sys.stderr,
            )

    # 8. Slug assignment. Workspace-root slugs share a namespace (the "")
    #    parent_key bucket. Existing pages keep their slugs; new pages
    #    slugify per-parent with collision resolution.
    slug_of: dict[str, str] = {pid: e.slug for pid, e in existing_by_pid.items()}
    slugs_by_parent: dict[str, set[str]] = {}
    for entry in existing_by_pid.values():
        slugs_by_parent.setdefault(entry.parent_id or "", set()).add(entry.slug)

    for pid in bfs:
        if pid in slug_of:
            continue
        node = by_id[pid]
        # Workspace roots use parent_key "" (their slugs share one namespace
        # at the workspace top level). Non-root pages use their parent's id.
        if pid in all_roots:
            parent_key = ""
        else:
            parent_key = node.parent_id or ""
        taken = slugs_by_parent.setdefault(parent_key, set())
        slug = S.slugify_unique(node.title, taken)
        taken.add(slug)
        slug_of[pid] = slug

    # 9. Relative path per page (relative to into_dir).
    path_of: dict[str, str] = {}
    for pid, entry in existing_by_pid.items():
        if entry.path.endswith("/index.md"):
            path_of[pid] = entry.path[: -len("/index.md")]
        elif entry.path == "index.md":
            path_of[pid] = slug_of[pid]
        else:
            path_of[pid] = entry.path

    for pid in bfs:
        if pid in path_of:
            continue
        if pid in all_roots:
            path_of[pid] = slug_of[pid]
            continue
        node = by_id[pid]
        parent = node.parent_id
        if parent is None or parent not in path_of:
            continue  # orphan; already warned above
        path_of[pid] = f"{path_of[parent]}/{slug_of[pid]}"

    # 10. Fetch the current pull's pages (ancestors + requested + descendants).
    fetched_pages: dict[str, Page] = {}
    for pid in bfs:
        node = by_id[pid]
        if isinstance(node, Page):
            fetched_pages[pid] = node
            continue
        if pid not in current_set:
            continue  # prior-pull page; will re-fetch in the refresh pass
        try:
            fetched_pages[pid] = client.get_page(pid)
        except APIError as e:
            print(
                f"warning: skipping page {pid} ({node.title!r}): {e.status}",
                file=sys.stderr,
            )

    # 11. Build link-rewrite indices covering EVERY page in the forest.
    pid_to_relpath: dict[str, str] = {
        pid: f"{path_of[pid]}/index.md" for pid in path_of
    }
    title_to_pid: dict[tuple[str, str], str] = {
        ("", by_id[pid].title): pid for pid in path_of
    }

    # 12. Re-fetch every prior-pull page that's still in the manifest but
    #     not in the current pull set. Phase 9: link refresh — cross-page
    #     links in those pages may now resolve to newly-pulled targets, so
    #     we re-render them with the updated pid_to_relpath. Standard
    #     re_pull semantics: clean → overwrite, dirty → .remote.md sibling.
    for pid in bfs:
        if pid in fetched_pages or pid not in existing_by_pid:
            continue
        try:
            fetched_pages[pid] = client.get_page(pid)
        except APIError as e:
            print(
                f"warning: skipping refresh of prior page {pid}: {e.status}",
                file=sys.stderr,
            )

    # 13. Write each fetched page.
    for pid in bfs:
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

    # 14. Write the workspace-level manifest LAST.
    (into_dir / "_meta").mkdir(parents=True, exist_ok=True)
    manifest = SubtreeManifest(
        root_page_ids=all_roots,
        fetched_at=iso_now(),
    )
    for pid in bfs:
        if pid not in path_of:
            continue
        node = by_id[pid]
        manifest.pages.append(
            SubtreeEntry(
                page_id=pid,
                path=f"{path_of[pid]}/index.md",
                parent_id=node.parent_id if pid not in all_roots else None,
                title=node.title,
                slug=slug_of[pid],
            )
        )
    workspace_manifest_path.write_text(
        json.dumps(manifest.to_json(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # 15. Return path to the requested page's directory.
    return into_dir / path_of[requested_page.id]


@dataclass
class _DescStub:
    id: str
    title: str
    parent_id: str | None


# ---------------------------------------------------------------------------
# Subtree iteration helpers
# ---------------------------------------------------------------------------


def load_manifest(workspace_dir: Path) -> SubtreeManifest:
    path = manifest_path_for(workspace_dir)
    if not path.exists():
        raise S.meta_tampered(f"missing {path}", file=str(workspace_dir))
    try:
        return SubtreeManifest.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError) as e:
        raise S.meta_tampered(f"sidecar {path} not parseable: {e}", file=str(workspace_dir))


def page_dirs_leaf_first(workspace_dir: Path, manifest: SubtreeManifest) -> list[Path]:
    """Return page directories in leaf-first push order.

    Phase 9 (forest): for each tree in `manifest.root_page_ids`, post-order
    DFS produces children-before-parents within that tree. Trees are
    concatenated in declaration order (no inter-tree dependency — they're
    independent in Confluence, so the order between them doesn't matter
    for correctness).
    """
    by_id = {e.page_id: e for e in manifest.pages}
    children_of: dict[str | None, list[str]] = {}
    for e in manifest.pages:
        children_of.setdefault(e.parent_id, []).append(e.page_id)

    order: list[str] = []
    seen: set[str] = set()
    for root_id in manifest.root_page_ids:
        if root_id not in by_id or root_id in seen:
            continue
        stack: list[tuple[str, bool]] = [(root_id, False)]
        while stack:
            pid, processed = stack.pop()
            if processed:
                if pid not in seen:
                    seen.add(pid)
                    order.append(pid)
                continue
            if pid in seen:
                continue
            stack.append((pid, True))
            for child_id in children_of.get(pid, []):
                stack.append((child_id, False))

    out: list[Path] = []
    for pid in order:
        rel = by_id[pid].path
        out.append(workspace_dir / Path(rel).parent)
    return out
