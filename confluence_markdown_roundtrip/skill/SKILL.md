---
name: confluence-markdown-roundtrip
description: Edit Confluence Cloud pages locally as Markdown. Pull, edit, push, with full preservation of inline comments, opaque macros, code-block parameters, task state, image attributes, and subtrees.
---

# confluence-markdown-roundtrip

A skill for editing Confluence Cloud pages via a Markdown round-trip. The
CLI (`confluence-markdown-roundtrip`) does the conversion in both directions and
preserves everything the editor can't sensibly represent as Markdown by
keeping it in a per-page sidecar (`_meta/index.conf.json`).

## When to use this skill

- The user wants to make a substantive edit to a Confluence page.
- The user wants to bulk-edit a tree of related pages.
- The user is reviewing what's on a Confluence page and prefers Markdown
  diffs to Confluence's editor view.

If the user just wants to **read** a page, `confluence-markdown-roundtrip pull`
followed by a `cat index.md` is fine, but pulling writes a `_meta/`
sidecar — only do this when local editing is expected.

## CLI surface

```
confluence-markdown-roundtrip pull <page-url-or-id> [--into DIR] [--subtree]
confluence-markdown-roundtrip push <path>
confluence-markdown-roundtrip status <path>
```

- `pull` writes `<into>/<slug>/index.md` and a `_meta/` directory.
- `push` reads the workspace and uploads. If `<path>` is a subtree root
  (contains `_meta/_subtree.json`), every dirty page is pushed leaf-first.
- `status` prints a TSV row per page: `page_id title local_version remote_version dirty conflict`.

Credentials live in `~/.config/confluence-markdown-roundtrip/credentials.toml`
(mode `0600`). Token never enters argv, env vars, or stdout.

## Workspace layout

```
<root-slug>/
  index.md             ← user edits this
  _meta/               ← tool-owned, do not touch
    index.md.orig
    index.conf.json
    attachments/
    _subtree.json      ← only at subtree roots
  child-a/             ← only in subtrees
    index.md
    _meta/...
```

`index.md` is the only file the user (or you, the agent) should edit.
`_meta/` is tool-owned: editing anything in it will cause push to abort
with `orig-tampered` or `meta-tampered`.

## Marker syntax in the Markdown

The pulled Markdown contains skill-specific markers that the round-trip
relies on. Treat them as load-bearing.

| Marker | What lives in the MD | What lives in the sidecar |
|---|---|---|
| `<!--cm:UUID-->X<!--/cm:UUID-->` | text `X` (editable) | comment metadata; UUID anchors the Confluence comment |
| `<!--cb:HASH-->` (with a `> [confluence: …]` label) | placeholder + cosmetic label | full XML of the opaque block |
| `<span data-ci="HASH">X</span>` | cosmetic visible text | full XML of the inline opaque |
| `- <!--ct:UUID--> text` | task text (editable) | checkbox state, assignee, due date, IDs |
| `` ```lang `` fence + `<!--cc:UUID-->` | language + body (editable) | every other `<ac:parameter>` on the macro |
| `![alt](path)<!--ci:HASH-->` | alt + path (editable) | width, height, align, layout, thumbnail |
| `<!--cp:UUID-->`, then a `> [!KIND]` GFM Alert blockquote, then `<!--/cp:UUID-->` | body text inside the blockquote (editable) | panel kind (info/note/warning/tip/success/error), macro/adf shape, params, ADF fallback |
| `[text](path/to/other/index.md)<!--cl:HASH-->` | navigable display form only — text and path are NOT editable | full XML of the original Confluence page link; push restores it verbatim |

## What you can edit freely

- Prose, headings H2–H6, lists, blockquotes, inline code, links.
- The H1 of `index.md` — this is the page title. Editing renames the page.
- Text inside `<!--cm:UUID-->...<!--/cm:UUID-->` — the comment range
  tracks surviving text.
- Text after `- <!--ct:UUID--> ` on a task line. State (checkbox,
  assignee) lives in the sidecar.
- Code-block contents and the language on the fence. All other code-block
  parameters are sidecar-owned and reattached on push.
- GFM table cells (text + inline formatting only).
- Body text inside a panel — the lines prefixed with `> ` between the
  `<!--cp:UUID-->` and `<!--/cp:UUID-->` markers. Strip the `> ` mentally
  when reading; keep one `> ` on every body line when writing.
- Reordering paragraphs, sections, list items, table rows, tasks, opaque
  blocks (move label + marker as a pair).

## What you must not do

These cause `push` to abort:

- Modify any `HASH` or `UUID` inside a marker.
- Remove one half of a paired marker (`cm`/`/cm`, `cp`/`/cp`, `<span data-ci>`/`</span>`).
- Paste literal marker syntax with a HASH not in the sidecar.
- Edit the `[text]` or `(path)` of a `<!--cl:HASH-->`-marked cross-page link
  expecting the change to propagate. Both are display-only; push restores
  the original Confluence link verbatim from the sidecar. Deleting the
  `<!--cl:HASH-->` trailer (and leaving the local-style path behind)
  causes push to abort with `bad-marker-syntax`.
- Edit the `> [!KIND]` alert tag inside a panel to change its visual kind —
  it is cosmetic only; the actual panel type comes from the sidecar and is
  ignored on push. Visual decoration only.
- Replace the `> [!KIND]` line or the surrounding `> ` blockquote prefix
  with an unrelated GFM Alert just because the previewer would render it.
  Touch only the body text inside the prefix.
- Add `![](./_meta/attachments/X.png)` referencing a file not already in
  the workspace.
- Create, rename, or delete `index.md` files or page directories.
- Touch anything inside `_meta/`.

## Workflow

1. **Pull** the page or subtree you want to edit:
   ```
   confluence-markdown-roundtrip pull <page-id-or-url> [--subtree] [--into DIR]
   ```
2. **Edit** the resulting `index.md` (or several, in subtree mode).
3. **Check** what changed:
   ```
   confluence-markdown-roundtrip status <path>
   diff <path>/index.md <path>/_meta/index.md.orig
   ```
4. **Push**:
   ```
   confluence-markdown-roundtrip push <path>
   ```
   On conflict the push aborts and writes `<file>.md.remote` with the
   server's current state. Inspect both, merge by hand, delete the
   `.remote` file, and retry.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success / no changes |
| 1 | `status` found dirty or conflicting pages |
| 2 | Validation error (`PushAbort` — see rule-id in stderr) |
| 3 | Version conflict; `.remote.md` written |
| 4 | API error |

## Known limitations

- Page **structure** (create/move/delete/rename-with-link-rewrite) is not
  supported in v1. Use the Confluence UI for those.
- ADF-native pages are read via storage-format compatibility; new
  modern-editor features may degrade to opaque blocks until added to the
  whitelist in `storage_to_md.py`.
