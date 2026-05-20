"""Command-line entry point.

Surface (plan §"CLI surface"):

  confluence-markdown-roundtrip pull <page-url-or-id> [--into DIR] [--credentials PATH]
  confluence-markdown-roundtrip push <path> [--credentials PATH]
  confluence-markdown-roundtrip status <path> [--credentials PATH]

Phase 2 is single-page only. Subtree flags live but error until Phase 3.

Exit codes (plan §"Push abort format"):
  0 — success / clean
  1 — `status` finds dirty/conflict rows (so shell users can pipe to awk)
  2 — validation error (`PushAbort` with non-conflict rule)
  3 — version conflict
  4 — API error
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import click
import httpx

from . import sentinels as S
from .api import APIError, ConfluenceClient, CredentialsError, VersionConflict, load_credentials
from .md_to_storage import md_to_storage
from .storage_to_md import storage_to_md
from .subtree import is_subtree_root, load_manifest, page_dirs_leaf_first, pull_subtree
from .workspace import PageWorkspace, download_referenced_attachments, iso_now


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------


@click.group(help="Round-trip Confluence pages through Markdown.")
@click.option(
    "--credentials",
    "credentials_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Override credentials file (default: ~/.config/confluence-markdown-roundtrip/credentials.toml).",
)
@click.pass_context
def cli(ctx: click.Context, credentials_path: Path | None) -> None:
    ctx.ensure_object(dict)
    ctx.obj["credentials_path"] = credentials_path


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("page_arg")
@click.option(
    "--into",
    "into_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    help="Workspace directory. Single-page pulls land at <into>/<slug>/; "
         "subtree/ancestor pulls treat <into> as the workspace root.",
)
@click.option("--subtree", is_flag=True, help="Pull the page + all descendants.")
@click.option(
    "--ancestors/--no-ancestors",
    "with_ancestors",
    default=None,
    help="Include the ancestor chain to the space homepage. "
         "Defaults to on with --subtree; off otherwise. "
         "Use --no-ancestors with --subtree to skip the upward walk.",
)
@click.pass_context
def pull(
    ctx: click.Context,
    page_arg: str,
    into_dir: Path,
    subtree: bool,
    with_ancestors: bool | None,
) -> None:
    """Fetch a Confluence page and write `index.md` + `_meta/` to disk."""
    try:
        page_id = S.page_id_from_arg(page_arg)
    except ValueError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(2)

    # Resolve the tristate `with_ancestors` flag.
    if with_ancestors is None:
        # No explicit flag: ancestors default on with --subtree, off without.
        resolved_with_ancestors = bool(subtree)
    else:
        resolved_with_ancestors = with_ancestors

    creds = _load_creds(ctx)
    with ConfluenceClient(creds) as client:
        # Subtree pull, or single-page pull with ancestors: both produce a
        # workspace with a manifest at <into>/_meta/_subtree.json. The
        # difference is whether descendants are also pulled.
        if subtree or resolved_with_ancestors:
            try:
                target_dir = pull_subtree(
                    client,
                    page_id,
                    into_dir,
                    with_ancestors=resolved_with_ancestors,
                    with_descendants=subtree,
                )
            except S.PushAbort as e:
                _emit_abort(e)
                sys.exit(2)
            except APIError as e:
                _emit_api_error(e)
                sys.exit(4)
            click.echo(str(target_dir))
            return

        # Plain single-page pull: legacy layout, no workspace-level manifest.
        try:
            page = client.get_page(page_id)
        except APIError as e:
            _emit_api_error(e)
            sys.exit(4)

        md, sidecar_obj = storage_to_md(page.storage_body, page.title)
        slug = S.slugify(page.title)
        root = into_dir / slug
        root.mkdir(parents=True, exist_ok=True)

        ws = PageWorkspace(
            root=root,
            page_id=page.id,
            title=page.title,
            space_id=page.space_id,
            parent_id=page.parent_id,
            base_version=page.version,
            base_storage_sha256=hashlib.sha256(page.storage_body.encode("utf-8")).hexdigest(),
            base_md_sha256="",  # filled by write_initial
            fetched_at=iso_now(),
            blocks=sidecar_obj.blocks,
            inline_blocks=sidecar_obj.inline_blocks,
            tasks=sidecar_obj.tasks,
            code_blocks=sidecar_obj.code_blocks,
            images=sidecar_obj.images,
            panels=sidecar_obj.panels,
            links=sidecar_obj.links,
        )
        ws.write_initial(md)

        # Download referenced attachments (best-effort — Phase 2).
        if sidecar_obj.images:
            try:
                failed = download_referenced_attachments(client, page.id, ws)
            except APIError as e:
                click.echo(f"warning: attachment fetch failed: {e}", err=True)
            else:
                if failed:
                    click.echo(
                        f"warning: could not download {len(failed)} attachment(s): {', '.join(failed)}",
                        err=True,
                    )

    click.echo(str(root))


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def push(ctx: click.Context, path: Path) -> None:
    """Push edits in <path>/index.md back to Confluence.

    If `path` is a subtree root (contains `_meta/_subtree.json`), every
    dirty page is pushed leaf-first. On first failure the walk stops; pages
    already pushed stay applied (plan §"Subtree push order").
    """
    if path.is_file():
        path = path.parent
    if not path.is_dir():
        click.echo(f"error: {path} is not a page directory", err=True)
        sys.exit(2)

    if is_subtree_root(path):
        _push_subtree(ctx, path)
        return

    try:
        ws = PageWorkspace.from_disk(path)
    except S.PushAbort as e:
        _emit_abort(e)
        sys.exit(2)

    try:
        _push_one(ctx, ws)
    except S.PushAbort as e:
        _emit_abort(e)
        sys.exit(3 if e.rule_id == "version-conflict" else 2)
    except APIError as e:
        _emit_api_error(e)
        sys.exit(4)


def _push_subtree(ctx: click.Context, root_dir: Path) -> None:
    try:
        manifest = load_manifest(root_dir)
    except S.PushAbort as e:
        _emit_abort(e)
        sys.exit(2)
    page_dirs = page_dirs_leaf_first(root_dir, manifest)
    any_failed = False
    for d in page_dirs:
        try:
            ws = PageWorkspace.from_disk(d)
        except S.PushAbort as e:
            _emit_abort(e)
            any_failed = True
            break
        try:
            _push_one(ctx, ws)
        except S.PushAbort as e:
            _emit_abort(e)
            sys.exit(3 if e.rule_id == "version-conflict" else 2)
        except APIError as e:
            _emit_api_error(e)
            sys.exit(4)
    if any_failed:
        sys.exit(2)


def _push_one(ctx: click.Context, ws: PageWorkspace) -> None:
    md_text = ws.read_index_md()
    orig_text = ws.read_orig()

    # Fast dirty check: byte-compare to .orig.
    if md_text == orig_text:
        click.echo(f"{ws.root}: clean — no push")
        return

    title, new_storage = md_to_storage(md_text, ws.sidecar_dict(), file_path=str(ws.index_md_path))

    # Storage dirty check: short-circuit if storage hash + title unchanged.
    new_storage_hash = hashlib.sha256(new_storage.encode("utf-8")).hexdigest()
    if new_storage_hash == ws.base_storage_sha256 and title == ws.title:
        click.echo(f"{ws.root}: storage hash unchanged — no push")
        ws.orig_path.write_text(md_text, encoding="utf-8")
        ws.base_md_sha256 = hashlib.sha256(md_text.encode("utf-8")).hexdigest()
        ws.write_sidecar()
        return

    # Version check before PUT.
    creds = _load_creds(ctx)
    with ConfluenceClient(creds) as client:
        remote_version = client.get_page_version(ws.page_id)
        if remote_version > ws.base_version:
            _write_remote_md(ws, client)
            raise S.version_conflict(ws.base_version, remote_version, file=str(ws.index_md_path))

        try:
            updated = client.update_page(
                ws.page_id,
                title=title,
                storage_body=new_storage,
                base_version=ws.base_version,
            )
        except VersionConflict:
            _write_remote_md(ws, client)
            raise S.version_conflict(ws.base_version, ws.base_version + 1, file=str(ws.index_md_path))

    ws.title = updated.title
    ws.base_version = updated.version
    ws.base_storage_sha256 = hashlib.sha256(updated.storage_body.encode("utf-8")).hexdigest()
    ws.fetched_at = iso_now()
    ws.index_md_path.write_text(md_text, encoding="utf-8")
    ws.orig_path.write_text(md_text, encoding="utf-8")
    ws.base_md_sha256 = hashlib.sha256(md_text.encode("utf-8")).hexdigest()
    ws.write_sidecar()
    click.echo(f"{ws.root}: pushed -> version {updated.version}")


def _write_remote_md(ws: PageWorkspace, client: ConfluenceClient) -> None:
    page = client.get_page(ws.page_id)
    md, _ = storage_to_md(page.storage_body, page.title)
    target = ws.index_md_path.with_suffix(".md.remote")
    target.write_text(md, encoding="utf-8")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def status(ctx: click.Context, path: Path) -> None:
    """Print TSV status row for one page or every page in a subtree."""
    if path.is_file():
        path = path.parent
    if not path.is_dir():
        click.echo(f"error: {path} is not a page directory", err=True)
        sys.exit(2)

    if is_subtree_root(path):
        sys.exit(_status_subtree(ctx, path))
    sys.exit(_status_one(ctx, path))


def _status_one(ctx: click.Context, path: Path) -> int:
    try:
        ws = PageWorkspace.from_disk(path)
    except S.PushAbort as e:
        _emit_abort(e)
        return 2

    md_text = ws.read_index_md()
    orig_text = ws.read_orig()
    dirty = 0 if md_text == orig_text else 1

    creds = _load_creds(ctx)
    with ConfluenceClient(creds) as client:
        try:
            remote_version = client.get_page_version(ws.page_id)
        except APIError as e:
            _emit_api_error(e)
            return 4

    conflict = 1 if (remote_version > ws.base_version and dirty) else 0
    click.echo("page_id\ttitle\tlocal_version\tremote_version\tdirty\tconflict")
    click.echo(f"{ws.page_id}\t{ws.title}\t{ws.base_version}\t{remote_version}\t{dirty}\t{conflict}")
    return 0 if (dirty == 0 and conflict == 0) else 1


def _status_subtree(ctx: click.Context, root_dir: Path) -> int:
    try:
        manifest = load_manifest(root_dir)
    except S.PushAbort as e:
        _emit_abort(e)
        return 2

    creds = _load_creds(ctx)
    rows: list[tuple[str, str, int, int, int, int]] = []
    with ConfluenceClient(creds) as client:
        for entry in manifest.pages:
            page_dir = root_dir / Path(entry.path).parent
            try:
                ws = PageWorkspace.from_disk(page_dir)
            except S.PushAbort as e:
                _emit_abort(e)
                return 2
            dirty = 0 if ws.read_index_md() == ws.read_orig() else 1
            try:
                remote_version = client.get_page_version(ws.page_id)
            except APIError as e:
                _emit_api_error(e)
                return 4
            conflict = 1 if (remote_version > ws.base_version and dirty) else 0
            rows.append((ws.page_id, ws.title, ws.base_version, remote_version, dirty, conflict))

    click.echo("page_id\ttitle\tlocal_version\tremote_version\tdirty\tconflict")
    for r in rows:
        click.echo(f"{r[0]}\t{r[1]}\t{r[2]}\t{r[3]}\t{r[4]}\t{r[5]}")
    any_dirty_or_conflict = any(r[4] or r[5] for r in rows)
    return 1 if any_dirty_or_conflict else 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_creds(ctx: click.Context):
    try:
        return load_credentials(ctx.obj.get("credentials_path"))
    except CredentialsError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(2)


def _emit_abort(e: S.PushAbort) -> None:
    click.echo(f"error: {e}", err=True)


def _emit_api_error(e: APIError | httpx.HTTPError) -> None:
    msg = str(e)
    msg = msg.replace("Basic ", "Basic ***")
    click.echo(f"error: {msg}", err=True)


def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()
