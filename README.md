# confluence-markdown-roundtrip

Edit Confluence Cloud pages locally as Markdown.

`pull` a page (or subtree), edit `index.md` in your editor, `push` it back. Everything Markdown can't represent — inline comments, opaque macros, code-block parameters, task state, image attributes, panels — survives in a per-page sidecar and is reattached on push.

## Status

v1. Confluence Cloud only. Storage format (XHTML) only.

Tested against `example.atlassian.net` (modern editor). No structural ops in v1 — page create / move / delete / rename-with-link-rewrite go through the Confluence UI.

## Install

Requires Python ≥ 3.11.

```
uv sync
uv run confluence-markdown-roundtrip --help
```

Or with pip:

```
pip install -e .
confluence-markdown-roundtrip --help
```

## Credentials

```
mkdir -p ~/.config/confluence-markdown-roundtrip
cp credentials.example.toml ~/.config/confluence-markdown-roundtrip/credentials.toml
chmod 0600 ~/.config/confluence-markdown-roundtrip/credentials.toml
$EDITOR ~/.config/confluence-markdown-roundtrip/credentials.toml
```

Generate the API token at <https://id.atlassian.com/manage-profile/security/api-tokens>. The CLI refuses to start if mode is broader than `0600`. Token never enters argv, env vars, stdout, stderr, or error messages.

## Usage

```
confluence-markdown-roundtrip pull <page-url-or-id> [--into DIR] [--subtree]
confluence-markdown-roundtrip push <path>
confluence-markdown-roundtrip status <path>
```

`pull` writes `<into>/<slug>/index.md` plus a `_meta/` sidecar. `push` walks `<path>` — a single page or a subtree root — and uploads dirty pages leaf-first. `status` prints a TSV row per page: `page_id title local_version remote_version dirty conflict`.

Workflow:

```
confluence-markdown-roundtrip pull <page-id-or-url> --into ./pages
$EDITOR ./pages/<slug>/index.md
confluence-markdown-roundtrip status ./pages/<slug>
diff ./pages/<slug>/index.md ./pages/<slug>/_meta/index.md.orig
confluence-markdown-roundtrip push ./pages/<slug>
```

On version conflict, push aborts and writes `<file>.md.remote` with the server's current state. Merge by hand, delete the `.remote` file, retry.

## Workspace layout

```
<root-slug>/
  index.md             user-editable
  _meta/               tool-owned, do not touch
    index.md.orig      verbatim copy of index.md at pull time
    index.conf.json    sidecar metadata
    attachments/       downloaded image binaries
    _subtree.json      only at subtree roots
  child-a/             only in subtrees
    index.md
    _meta/...
```

Editing anything inside `_meta/` aborts push with `orig-tampered` or `meta-tampered`. Restore via re-pull.

## What you can edit in `index.md`

- Prose, headings H2–H6, lists, blockquotes, inline code, links.
- The H1 — this is the page title. Editing renames the page on push.
- Text inside `<!--cm:UUID-->...<!--/cm:UUID-->` — the inline-comment range tracks surviving text.
- Text after `- <!--ct:UUID--> ` on a task line. State (checkbox, assignee, due date) is sidecar-owned.
- Code-block contents and the fence language. Every other code-macro parameter is sidecar-owned.
- GFM table cells (inline content only).
- Body text inside a panel — lines prefixed with `> ` between `<!--cp:UUID-->` and `<!--/cp:UUID-->`.
- Reordering paragraphs, list items, table rows, tasks, opaque blocks (move label + marker as a pair).

## What aborts push

- Modifying any `HASH` or `UUID` inside a marker.
- Removing one half of a paired marker.
- Pasting marker syntax with a HASH not in the sidecar.
- Editing the `[text]` or `(path)` of a `<!--cl:HASH-->`-marked cross-page link (display-only; sidecar restores it verbatim).
- Adding `![](./_meta/attachments/X.png)` referencing a file not already in the workspace.
- Creating, renaming, or deleting `index.md` files or page directories.
- Touching anything inside `_meta/`.

The skill at [confluence_markdown_roundtrip/skill/SKILL.md](confluence_markdown_roundtrip/skill/SKILL.md) documents marker semantics in full for an agent driving the CLI.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success / no changes |
| 1 | `status` found dirty or conflicting pages |
| 2 | Validation error (`PushAbort` — see rule-id in stderr) |
| 3 | Version conflict; `.remote.md` written |
| 4 | API error |

## Repo layout

```
pyproject.toml                 package metadata + dependencies
confluence_markdown_roundtrip/ Python package
  api.py                       v2 REST client
  cli.py                       pull / push / status entry points
  storage_to_md.py             XHTML -> Markdown + sidecar
  md_to_storage.py             Markdown + sidecar -> XHTML
  subtree.py                   subtree walk + manifest
  workspace.py                 on-disk page directory contract
  sentinels.py                 PushAbort rule IDs, slugify, etc.
  skill/SKILL.md               agent-facing operational guide
  tests/                       pytest suite (offline + live)
PLAN.md                        full design spec (locked decisions, schemas, edge cases)
CLAUDE.md                      session-to-session operational lessons
```

## Known limitations

- Page structure (create / move / delete / rename-with-link-rewrite) is not supported in v1.
- ADF-native pages are read via storage-format compatibility; new modern-editor features may degrade to opaque blocks until added to the whitelist in `storage_to_md.py`.
- `<ri:url>` (image-from-the-web) round-trips for legacy pages but is never produced by the modern editor.
- Custom panels (`panel-type=custom` with user-set colors/icons) stay opaque.
