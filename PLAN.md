# Plan: Confluence Round-Trip Edit Skill

## Goal

A skill that:

1. Pulls a Confluence page (or subtree) → emits editable Markdown + sidecar metadata.
2. User edits the Markdown freely.
3. Pushes edits back to the same page(s).

Must survive across the round-trip:

- **Inline comments** anchored to text ranges.
- **Embedded content** (macros, panels, attachments, layouts, etc.) the user does not edit.
- **Page version** integrity — detect concurrent edits, refuse to clobber.

Subtree mode operates on the page itself + all descendants.

## Locked decisions

| # | Branch | Decision |
|---|---|---|
| 1 | Deployment | Confluence Cloud only |
| 2 | Body format | Storage format (XHTML) only |
| 3 | Opaque scope | Medium — convert prose, code blocks, task lists, simple tables; everything else opaque |
| 4 | Inline comment marker | HTML comments: `<!--cm:UUID-->text<!--/cm:UUID-->` |
| 5 | Block opaque marker | Visible label line + comment placeholder on next line |
| 6 | Inline opaque marker | `<span data-ci="HASH">visible</span>` |
| 7 | Conflict on push | Abort, write `.remote.md`, manual merge |
| 8 | Structural ops | Body + title (H1) only — no create/delete/move in v1 |
| 9 | Auth | API token in a local credentials file (not env vars, not CLI args) so values never enter the agent's context |
| 10 | Page metadata | Sidecar-preserves-only; not editable in v1 |
| 11 | Re-pull | Overwrite clean, write `.remote.md` for dirty |
| 12 | Footer/page comments | Ignored (survive on Confluence automatically) |
| 13 | Dirty check | Two-stage: MD-hash fast path → storage-hash verifier |
| 14 | Subtree scope | Root + all descendants; cross-tree links stay opaque |
| 15 | Deliverable | SKILL.md + Python CLI |
| 16 | Language | Python (lxml + markdown-it-py + httpx) |
| 17 | Task lists | Text editable, state opaque: `- <!--ct:UUID--> task text` |
| 18 | Tables | All-cells-inline-only converts to GFM; any complexity → opaque |
| 19 | Code blocks | Fenced; **only `language` and body are editable** (language goes on the MD fence, body inside the fence). Every other `<ac:parameter>` on the macro — `title`, `theme`, `linenumbers`, `firstline`, `collapse`, `breakoutMode`, `breakoutWidth`, and any future param the editor adds — is preserved in the sidecar as an opaque key/value map via the `<!--cc:UUID-->` trailer. Closed enumeration explicitly rejected. |
| 20 | Slug | Slug stamped at pull time, never auto-renamed; collisions get numeric suffix |
| 21 | Phase 1 | Narrow, question-driven; produces `notes.md`, not a doc archive |
| 22 | Phase 1.5 | Create test page on existing tenant before phase 2 spike |
| 23 | Images | Read-only refs; attachment images downloaded to `_meta/attachments/`, linked from MD with hash trailer for non-default params |

## Marker reference

| Construct | MD form |
|---|---|
| Inline comment range | `<!--cm:UUID-->text<!--/cm:UUID-->` |
| Block opaque | `> \[confluence: <kind>\]`<br>`<!--cb:HASH-->` |
| Inline opaque | `<span data-ci="HASH">visible</span>` |
| Task item (state opaque) | `- <!--ct:UUID--> text` |
| Code block extra params | trailer `<!--cc:UUID-->` after fence |
| Image with non-default attrs | `![alt](./_meta/attachments/file.png)<!--ci:HASH-->` |
| Image (plain) | `![alt](./_meta/attachments/file.png)` |
| External URL image | `![alt](https://...)` (no download) |
| Editable panel (info/note/warning/tip/success/error) | `<!--cp:UUID-->`<br>`> [!KIND]`<br>`> <body markdown>`<br>`<!--/cp:UUID-->` (Phase 6: GFM Alert blockquote inside the wrapper; `KIND` is derived from the sidecar panel name) |

`HASH` = sha256[0:12] of opaque XML content (stable). `UUID` = identifier (existing Confluence ID for comments/tasks; UUID4 generated otherwise).

## Storage → MD mapping

Convert to MD:

- `<p>`, `<h1..h6>` (h1 only via synthetic title — see below), `<ul>`, `<ol>`, `<li>`, `<strong>`, `<em>`, `<code>`, `<a>` (plain hrefs only), `<br>`, `<blockquote>`, plain `<table>` if every cell is inline-only.
- `<ac:structured-macro ac:name="code">` → fenced code block. The `<ac:parameter ac:name="language">` value (if any) becomes the MD fence language; the body of `<ac:plain-text-body>` becomes the fence content. Every other `<ac:parameter>` child is captured opaquely in `sidecar.code_blocks[UUID].params` and the fence is suffixed with a `<!--cc:UUID-->` trailer. No closed enumeration — we don't know what params the editor may add.
- `<ac:task-list>` → `- <!--ct:UUID-->` per task; state in sidecar.
- `<ac:inline-comment-marker ac:ref="UUID">X</...>` → `<!--cm:UUID-->X<!--/cm:UUID-->`.
- `<ac:image>` with attachment ref → download attachment, link as `![alt](./_meta/attachments/file)`. Non-default attrs (size/align/layout/thumbnail) → `<!--ci:HASH-->` trailer + sidecar.
- `<ac:image>` with URL ref → `![alt](url)`, no download.
- Editable panels — two storage shapes, same MD form. Body is rendered recursively as MD, wrapped in a GFM Alert blockquote (`> [!KIND]\n> <body>`), the whole thing surrounded by `<!--cp:UUID-->` ... `<!--/cp:UUID-->`. `KIND` is derived deterministically from `sidecar.panels[UUID].name` (info→NOTE, note→IMPORTANT, tip/success→TIP, warning→WARNING, error→CAUTION) and is cosmetic — push side strips the alert prefix and reads the panel type from the sidecar.
  - Legacy: `<ac:structured-macro ac:name="info|note|warning|tip">` with `<ac:rich-text-body>`. 4 type values.
  - Modern (ADF): `<ac:adf-extension>` containing `<ac:adf-node type="panel">` with `<ac:adf-attribute key="panel-type">VALUE</ac:adf-attribute>` and `<ac:adf-content>`. 5 type values: info, note, warning, success, error. (`custom` panel-type stays opaque — it carries arbitrary user-set colors/icons that don't survive the round-trip cleanly.)

Opaque (block):

- Layouts (`<ac:layout*>`).
- Tables with any non-inline cell content.
- All other `<ac:structured-macro>` (jira, toc, expand, attachments-list, etc.).
- Any unknown `ac:*` element.

Opaque (inline):

- User mentions (`<ac:link><ri:user/></ac:link>`).
- Cross-page links (`<ac:link><ri:page/></ac:link>`).
- Status macros, inline emoji macros, inline Jira links.
- Any inline `ac:*` element not covered above.

## Title handling

Page title lives in Confluence metadata, not in body. On pull, the title is synthesized as the leading `# Title` H1 of `index.md` and stored in `sidecar.title`. On push: read the H1 from `index.md`, set as Confluence page title, do **not** serialize it into the storage body. If the H1 differs from `sidecar.title`, rename the page on push. If the first non-blank line of `index.md` is not an H1, push aborts with a clear error.

## Sidecar schemas

### Per-page `index.conf.json`

```jsonc
{
  "page_id": "12345",
  "space_key": "DOCS",
  "title": "Page Title",
  "parent_id": "12344",
  "base_version": 17,
  "base_storage_sha256": "...",
  "base_md_sha256": "...",
  "fetched_at": "2026-05-15T...",
  "blocks": {
    "<HASH>": { "xml": "<ac:...>...</ac:...>", "kind": "macro:info" }
  },
  "inline_blocks": {
    "<HASH>": { "xml": "<ac:link>...</ac:link>", "kind": "user-mention" }
  },
  "tasks": {
    "<UUID>": { "status": "incomplete", "assignee": "...", "due_date": "...", "xml_attrs": {...} }
  },
  "code_blocks": {
    "<UUID>": {
      "params": {
        // every <ac:parameter ac:name="X">VALUE</ac:parameter> child except language,
        // captured verbatim. Examples observed in the wild:
        //   "title": "...", "theme": "...",
        //   "linenumbers": "true", "firstline": "1", "collapse": "false",
        //   "breakoutMode": "wide", "breakoutWidth": "760"
        // Values are strings (storage is XHTML; no typed coercion).
      }
    }
  },
  "images": {
    "<HASH>": { "filename": "diagram.png", "attrs": { "width": "300", "align": "center" } }
  },
  "panels": {
    "<UUID>": {
      "shape": "macro",      // "macro" (legacy) or "adf" (modern adf-extension); chooses push-side emitter branch
      "name": "info",        // for macro: ac:name; for adf: panel-type. Source of truth on push.
      "params": {},          // macro only: any <ac:parameter> children, captured verbatim
      "adf_attrs": {},       // adf only: any other <ac:adf-attribute> children besides panel-type (e.g. local-id stripped as bookkeeping)
      "adf_fallback": "..."  // adf only: <ac:adf-fallback> innerXML preserved verbatim and re-emitted on push (stale-but-tolerated when body changes; modern renderer reads adf-content)
    }
  },
  "comments": {
    "<UUID>": { "resolved": false, "anchor_text_snapshot": "..." }
  },
  "metadata_preserve": {
    "labels": [...],
    "restrictions": {...},
    "properties": {...}
  }
}
```

### Subtree `_subtree.json`

```jsonc
{
  "root_page_id": "12345",
  "space_key": "DOCS",
  "fetched_at": "2026-05-15T...",
  "pages": [
    { "page_id": "12345", "path": "index.md",                    "parent_id": null,    "title": "Root", "slug": "root" },
    { "page_id": "12346", "path": "child-a/index.md",            "parent_id": "12345", "title": "Child A", "slug": "child-a" },
    { "page_id": "12347", "path": "child-a/grandchild/index.md", "parent_id": "12346", "title": "Grandchild", "slug": "grandchild" }
  ]
}
```

## On-disk layout

```
<root-slug>/
  index.md
  _meta/
    _subtree.json
    index.md.orig
    index.conf.json
    attachments/
      diagram.png
  child-a/
    index.md
    _meta/
      index.md.orig
      index.conf.json
      attachments/
        ...
    grandchild/
      index.md
      _meta/
        index.md.orig
        index.conf.json
```

Each page directory has exactly two visible entries: `index.md` (user-editable) and `_meta/` (read-only sidecar bundle). Everything inside `_meta/` is owned by the tool — the user and the agent must never touch it. Specifically:

- `_meta/index.md.orig` — verbatim copy of `index.md` as written by `pull`. Enables local `diff index.md _meta/index.md.orig`, gives `status` a real change view, supports 3-way merge in future conflict-resolution work. Re-pull rewrites it in lockstep with `index.md`. Its sha256 must always equal `sidecar.base_md_sha256`; mismatch aborts push.
- `_meta/index.conf.json` — per-page sidecar metadata (schema above).
- `_meta/attachments/` — binary copies of referenced attachments. Image refs in `index.md` use the relative path `./_meta/attachments/<filename>`.
- `_meta/_subtree.json` — present only in the root page's `_meta/` directory; describes the full tree.

## CLI surface

```
confluence-markdown-roundtrip pull <page-url-or-id> [--subtree] [--depth N] [--into DIR]
confluence-markdown-roundtrip push <path>
    file → push one page
    dir  → walk _meta/_subtree.json, push every dirty page leaf-first
confluence-markdown-roundtrip status <path>
    file → base vs remote version, dirty bit
    dir  → table per page: dirty, remote-advanced, conflict
```

Credentials are read from a local file, never from env vars or CLI args. Default path: `$XDG_CONFIG_HOME/confluence-markdown-roundtrip/credentials.toml` (falls back to `~/.config/confluence-markdown-roundtrip/credentials.toml`). Override with `--credentials PATH`. File format (TOML):

```toml
base_url  = "https://example.atlassian.net"
email     = "user@example.com"
api_token = "ATATT..."
```

File permissions must be `0600` (owner-read/write only). The CLI refuses to start if mode is broader, mirroring ssh-key handling. The token never appears on stdout, stderr, in log output, or in error messages. The agent invoking the CLI does not see and must not handle credential values.

## Push pipeline (per page)

1. Read `index.md`, `_meta/index.md.orig`, and `_meta/index.conf.json`.
2. Integrity check: `sha256(_meta/index.md.orig)` must equal `sidecar.base_md_sha256`. Missing or mismatched → abort with `orig-tampered`. Restore via re-pull.
3. Fast dirty check: byte-compare `index.md` to `_meta/index.md.orig`. If equal → skip.
4. Parse MD → AST. Walk:
   - Standard MD → storage XHTML.
   - `<!--cm:UUID-->X<!--/cm:UUID-->` → `<ac:inline-comment-marker ac:ref="UUID">X</...>`. Unmatched halves → abort.
   - `<!--cb:HASH-->` → inline sidecar `blocks[HASH].xml`. Missing hash → abort.
   - `<span data-ci="HASH">X</span>` → inline sidecar `inline_blocks[HASH].xml`. Visible text in span ignored on push (sidecar is source of truth).
   - `- <!--ct:UUID--> text` → reconstruct `<ac:task>` from sidecar `tasks[UUID]` with new text. Missing UUID → new task with default state.
   - Fenced code → `<ac:structured-macro ac:name="code">`. The fence's language → `<ac:parameter ac:name="language">`. The fence content → `<ac:plain-text-body><![CDATA[...]]></ac:plain-text-body>`. If a `<!--cc:UUID-->` trailer is present, every key in `code_blocks[UUID].params` becomes another `<ac:parameter>` child verbatim. If no trailer, no params beyond language.
   - `![alt](path)<!--ci:HASH-->` → `<ac:image>` with attrs from `images[HASH]`; path → `<ri:attachment ri:filename="...">`.
   - `![alt](./_meta/attachments/...)` without trailer + filename matches existing attachment → preserve. New filename → abort (v1 doesn't add attachments).
5. Storage-hash dirty check: hash the new storage. If equal to `base_storage_sha256` → skip.
6. `GET` current version. If `current > base_version` → abort, write `.remote.md` alongside.
7. Read H1 from MD. If differs from `sidecar.title` → include title change in `PUT`.
8. `PUT /pages/{id}` with new body + `version.number = base_version + 1`.
9. On success: update sidecar with new version, new hashes, new fetched_at. Overwrite `_meta/index.md.orig` with the freshly-pushed `index.md` so the new baseline matches the new sidecar.

## Subtree push order

Walk `_meta/_subtree.json` `.pages` leaf-first (children before parents). On first failure, stop. No transactional rollback (Confluence has no multi-page transaction). Already-pushed pages stay applied; user resumes with `push` to retry remaining.

## Subtree pull

1. Resolve root page id (URL or id).
2. Walk descendants via v2 API (`GET /pages/{id}/descendants`, paginated). BFS order.
3. For each page: pull single-page artifacts. Slugify title; resolve collisions with `-2`, `-3` suffix recorded in `_meta/_subtree.json`. Write `index.md` then copy it byte-for-byte to `_meta/index.md.orig`. Write `_meta/index.conf.json`.
4. Download referenced attachment images to `<page-dir>/_meta/attachments/`.
5. Write the root page's `_meta/_subtree.json` last.

Re-pull on existing directory: per-file, overwrite clean (`index.md` equals `_meta/index.md.orig`) or write `<file>.remote.md` sibling for dirty. On clean overwrite, `_meta/index.md.orig` is rewritten in lockstep with `index.md`.

## Edit-time edge cases

| User action | Behavior |
|---|---|
| Deletes a `<!--cb:HASH-->` line + its label | Block removed. |
| Duplicates a block placeholder | Block appears twice. Acceptable. |
| Edits inside `<!--cm:UUID-->X<!--/cm:UUID-->` | Comment anchor shrinks/expands with surviving text. Confluence keeps the comment. |
| Deletes everything between `cm` markers | Markers also removed → comment orphans on Confluence. Acceptable; user intent. |
| Breaks marker syntax (one half deleted, malformed UUID) | Push aborts with line number. Never silently drops. |
| Pastes literal `<!--cb:HASH-->` with unknown hash | Push aborts. |
| Changes H1 | Title rename on push. |
| Creates new `index.md` in a new directory | Warning on push, no creation in v1. |
| Deletes `index.md` for a page in `_meta/_subtree.json` | Warning on push, no deletion in v1. |
| Renames a directory | Warning on push, no move in v1. Sidecar still points to original `page_id`. |
| Adds `![](./new.png)` without trailer | Push aborts (no new image upload in v1). |
| Edits or deletes any file inside `_meta/` | Push aborts (`orig-tampered` for `index.md.orig`; `meta-tampered` for others). Restore via re-pull. |
| Confluence-side advance between pull and push | Push aborts, write `<file>.remote.md`. |

## SKILL.md — editing rules for the agent

The SKILL.md ships not just operational instructions (how to invoke the CLI) but also a **rules-of-edit** section the agent reads before touching any pulled MD file. Rules below are the canonical content of that section.

### What the agent can edit freely

- Prose: paragraphs, headings H2–H6, bold/italic, ordered/unordered lists, blockquotes, inline code, links to URLs.
- The H1 of `index.md` — but be aware: this is the page title. Changing it renames the page on push.
- Text inside `<!--cm:UUID-->...<!--/cm:UUID-->`. The comment range tracks the surviving text. Splitting or extending text inside the markers is fine.
- Text after `- <!--ct:UUID--> ` on a task line. Task state (checkbox, assignee, due date) lives in the sidecar; the editable part is only the text.
- Code block contents and the language fence (e.g. ` ```python ` → ` ```rust `). Nothing else about a code block is editable — title, theme, line-numbers toggle, first-line number, collapse default, `breakoutMode`, `breakoutWidth`, and any other code-macro parameter live in the sidecar and are reattached on push. Keep the `<!--cc:UUID-->` trailer attached to the fence so the sidecar lookup works.
- The body inside the GFM Alert blockquote wrapped in `<!--cp:UUID-->` and `<!--/cp:UUID-->` (info/note/warning/tip/success/error panels). The lines start with `> ` (one-level blockquote); edit the text after the prefix. The `> [!KIND]` tag is a display aid only; the panel type is fixed by the sidecar. To change the panel type, edit the page in Confluence.
- GFM table cells (text + inline formatting only).
- Reordering: paragraphs, sections, list items, table rows, tasks (entire `- <!--ct:UUID--> ...` line), opaque blocks (label + `<!--cb:HASH-->` placeholder together).

### What the agent may edit with care

- **Block opaques** (`> [confluence: <kind>]` line + `<!--cb:HASH-->` line). Move them together as a pair. The `> [...]` line is a human-readable label; rewriting it does not change what is uploaded — the `cb:HASH` placeholder is the source of truth. Treat as a two-line unit.
- **Inline opaques** (`<span data-ci="HASH">visible</span>`). The visible text is for human readability; **it is ignored on upload**. The `data-ci` attribute drives the lookup. Do not modify `data-ci`. Modify visible text only to improve local readability.
- **Image refs**. Path (`./_meta/attachments/...`) must point to an existing file in `_meta/attachments/`. Alt text is editable. If a `<!--ci:HASH-->` trailer is present, keep it attached.
- **Trailers** (`<!--cc:UUID-->`, `<!--ct:UUID-->`, `<!--ci:HASH-->`). Stay attached to their owner line/block. Do not detach, reorder relative to their owner, or change the id.

### What the agent must not do

These will cause `push` to abort:

- Modify any `HASH` or `UUID` inside a marker.
- Remove one half of a paired marker without removing the other half: `<!--cm:UUID-->...<!--/cm:UUID-->`, `<span data-ci>...</span>`.
- Paste literal marker syntax that doesn't correspond to a real sidecar entry.
- Add a new `![](./somefile.png)` ref pointing to a file that doesn't exist in `_meta/attachments/` and isn't already in `sidecar.images`.
- Create a new `.md` file or directory under the subtree (no page creation in v1).
- Delete an `index.md` referenced in `_meta/_subtree.json` (no page deletion in v1).
- Rename a directory (no page move in v1).
- Touch **anything** inside `_meta/` — that folder is owned by the tool. Files there (`index.md.orig`, `index.conf.json`, `_subtree.json`, `attachments/`) are read-only from the user's and agent's perspective. Use `diff index.md _meta/index.md.orig` to see local changes, never modify the `.orig` side.

### Intentional but lossy edits

These are allowed and the consequences are user intent:

- Deleting a block opaque (label + `cb:` placeholder) removes the block from the page.
- Deleting an inline opaque span removes that element from the page.
- Deleting all text between `cm:` markers, then deleting the markers, orphans the inline comment on Confluence.
- Deleting an image ref removes the image from the page (the attachment file on Confluence persists; the page no longer references it).
- Duplicating a block opaque duplicates the block on the page.

### Source of truth, quick reference

| Marker | What lives in the MD | What lives in the sidecar |
|---|---|---|
| `<!--cm:UUID-->X<!--/cm:UUID-->` | text X (editable) | comment metadata; UUID anchors the Confluence comment |
| `<!--cb:HASH-->` | placeholder + visible label (cosmetic) | full XML of the opaque block |
| `<span data-ci="HASH">X</span>` | placeholder + visible label (cosmetic) | full XML of the inline opaque |
| `- <!--ct:UUID--> text` | task text (editable) | checkbox state, assignee, due date, IDs |
| code fence ` ```lang ` + `<!--cc:UUID-->` | language + content (editable) | every other `<ac:parameter>` on the macro — title, theme, linenumbers, firstline, collapse, breakoutMode, breakoutWidth, anything else the editor produces |
| `![alt](path)<!--ci:HASH-->` | alt text, path (editable) | width, height, align, layout, thumbnail |
| `<!--cp:UUID-->` + GFM Alert `> [!KIND]` + `> body` + `<!--/cp:UUID-->` | panel body markdown inside the `> ` blockquote (editable) | panel kind (`info`/`note`/`warning`/`tip`/`success`/`error`), macro vs adf shape, any `<ac:parameter>` children, ADF fallback |

### Operational workflow the agent should follow

1. Before editing: read the file plus its `_meta/index.conf.json` to know which UUIDs and HASHes are valid.
2. After editing: run `confluence-markdown-roundtrip status <path>` to confirm the file is recognized as dirty and not broken.
3. To upload: `confluence-markdown-roundtrip push <path>`. If push aborts, read the error — it will name the line and the rule violated.
4. On conflict (`.remote.md` written): inspect both files, manually merge, delete `.remote.md`, retry push.
5. Never touch anything inside any `_meta/` directory. If the sidecar feels wrong, run `pull` to refresh.

## Module layout

```
confluence_markdown_roundtrip/
  api.py             # get_page, list_descendants, update_page, update_title, get_attachment, version check, pagination
                     # plus inline-comments client: list_comments, create_inline_comment (used by test bootstrap only,
                     # not by pull/push). No delete_comment — the suite is additive on comments.
  storage_to_md.py   # lxml walker → MD + sidecar; element dispatch table
  md_to_storage.py   # markdown-it AST walker → XHTML; sentinel + placeholder reinjection
  sentinels.py       # cm/cb/ci/ct/cc encode/decode + strict validation
  attachments.py     # download to _meta/attachments/, hash, sidecar attrs
  subtree.py         # tree walk, slugify+collision, dirty detection, push ordering
  cli.py             # pull / push / status
  skill/SKILL.md     # the Claude Code skill wrapper
  tests/
    conftest.py                # session bootstrap + per-test baseline restore + helpers
    fixtures/
      page-spec.json           # logical-name → expected_title + template + comments[{slot, anchor, text}]
      template-root.xml        # rich storage XHTML covering every element type
                               # (inline-comment markers use named slots like __CM_SLOT_1__ as ac:ref; bootstrap substitutes real UUIDs)
      template-child-a.xml
      template-child-b.xml
      template-grandchild.xml
      sample-storage-modern.xml # offline-only fixture (no tenant required)
      sample-storage-classic.xml
      sample-md-with-each-marker.md
    test_units.py              # offline: sentinels, slugify, hash, c14n2 compare
    test_storage_to_md.py      # offline: walker over sample XML
    test_md_to_storage.py      # offline: emitter
    test_roundtrip_identity.py # online: Phase 2 category A
    test_edits.py              # online: Phase 2 category B (23 cases)
    test_aborts.py             # mixed: Phase 2 category C (9 cases)
    test_subtree.py            # online: Phase 3 category D (5 cases)
```

## Conventions and constants

- **Repo layout**: code lives under `confluence/code/` (sibling to `plan.md` and `notes.md`). Pulled subtrees live under `confluence/workspaces/<root-slug>/` to keep tenant content out of the code tree.
- **Hash**: `sha256(xml_bytes).hexdigest()[:12]` everywhere. 12 hex chars; collision risk negligible at page scale.
- **Slugify**: NFKD-normalize → strip diacritics → lowercase → replace non-`[a-z0-9]+` runs with `-` → strip leading/trailing `-` → truncate at 60 chars (cut on last `-` before 60). Empty → `page`. Collisions → `-2`, `-3`, …
- **Canonical XML compare**: `lxml.etree.tostring(tree, method="c14n2")` for round-trip byte equality.
- **Page-id from URL**: regex `/pages/(\d+)` first; else accept bare digits as id; else error.
- **Credentials**: TOML file at `$XDG_CONFIG_HOME/confluence-markdown-roundtrip/credentials.toml` (or `~/.config/...`); keys `base_url`, `email`, `api_token`; file mode must be `0600` or the CLI refuses to start. Loaded by `api.py` at process startup. No env-var fallback. No CLI flag carrying the token. Token never written to stdout/stderr/logs. On the rare error path that includes a request preview, redact `Authorization:` headers to `Basic ***`.
- **Attachment downloads**: same API token via `Authorization: Basic` (email + token), loaded from the credentials file as above.
- **Marker disambiguation**: comments matching `^(cm|/cm|cb|cc|ct|ci):` are skill markers; all other HTML comments pass through as opaque text. Inline span markers identified by `data-ci` attr only.
- **Push abort format**: exit code `2` for validation errors, `3` for conflict, `4` for API errors. Stderr line shape: `error: <file>:<line>: <rule-id>: <message>`. Rule IDs: `unmatched-cm`, `unmatched-cp`, `unknown-cb-hash`, `unknown-ci-hash`, `unknown-cl-hash`, `unknown-cp-uuid`, `new-attachment`, `missing-h1`, `bad-marker-syntax`, `version-conflict`, `orig-tampered` (`sha256(_meta/index.md.orig) ≠ sidecar.base_md_sha256` or `.orig` missing), `meta-tampered` (other `_meta/` files missing or unparseable).
- **MD parser**: `markdown-it-py` with `mdit-py-plugins` extras enabled: `tables`, `tasklists` (disabled — we handle tasks via marker, not GFM), `strikethrough`. `html` option **on** so HTML comments and spans pass through.
- **MD serializer**: custom AST→XHTML emitter. No existing library produces Confluence storage format; this is the project's core work.
- **Skill install path**: `~/.claude/skills/confluence-markdown-roundtrip/SKILL.md` for global use. The skill body invokes the CLI by absolute path or by `confluence-markdown-roundtrip` on `PATH`.

### Python project bootstrap (Phase 0)

```toml
# confluence/code/pyproject.toml
[project]
name = "confluence-markdown-roundtrip"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "httpx>=0.27",
  "lxml>=5.0",
  "markdown-it-py>=3.0",
  "mdit-py-plugins>=0.4",
  "click>=8.1",
]
[project.scripts]
confluence-markdown-roundtrip = "confluence_markdown_roundtrip.cli:main"
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

Install: `cd confluence/code && uv tool install --editable .` → `confluence-markdown-roundtrip` on `PATH`.

## Phased roadmap

### Phase 0 — Bootstrap

1. Create `confluence/code/` with `pyproject.toml` above and a skeletal `confluence_markdown_roundtrip/` package: `__init__.py`, empty `cli.py` with a `main()` that prints usage.
2. `uv tool install --editable .`.
3. Verify `confluence-markdown-roundtrip --help` works.

### Phase 1 — Research (narrow, question-driven)

Produce `confluence/notes.md` with answers to:

1. Storage tag inventory (which `ac:*` and `ri:*` appear in real pages).
2. Exact API call to fetch body with `<ac:inline-comment-marker>` present (v2 `body-format=storage`).
3. Update endpoint, concurrency model, required fields.
4. ADF differences (compatibility risk, deprecation timeline).
5. Whether Atlassian's MCP server exposes raw storage (almost certainly no → skip MCP).
6. How `mark` (github.com/kovetskiy/mark) maps MD → storage; what it loses.

Source list: developer.atlassian.com v2 + v1 docs, confluence.atlassian.com storage-format docs, support.atlassian.com MCP docs, `mark` source. No raw archive — extract answers into `notes.md`.

`notes.md` must also include the concrete API contracts `api.py` will implement: for each endpoint used (get page body, list descendants, update page, get attachment, get current version), the path, required headers, query params, and the response fields read. Without this, phase 3 implementation is blocked.

### Phase 1.5 — Test setup

Create a Confluence Cloud test page with:

- Title with non-ASCII characters (slugify stress test).
- At least 2 paragraphs of prose.
- One info panel.
- One code block (with language + linenumbers).
- One simple table (2x2, text only).
- One complex table (cell with a macro).
- One task list (2 tasks, one completed, one with assignee).
- One image attachment + one external URL image.
- One inline comment on a phrase.
- One footer comment.
- One user mention.

Generate API token. Create `~/.config/confluence-markdown-roundtrip/credentials.toml` with `base_url`, `email`, `api_token` (see §"CLI surface"); `chmod 0600` the file. Verify the CLI loads it with a no-op call (e.g. `confluence-markdown-roundtrip status` against a path that has just been pulled).

### Phase 2 — Single-page round-trip + automated test suite

**Goal**: the storage→MD→storage converter is correct and reliable for single pages, and **every editable surface defined in SKILL.md survives a full round-trip** (mutate MD → push → re-pull → diff). Verified by a `pytest` suite that runs **fully unattended** against a live tenant. Subtree functionality is deferred to Phase 3, but its tests reuse the same infrastructure built here.

#### Test fixtures (live tenant)

Permanent tree dedicated to automated tests:

- Root: [Automated test area](https://example.atlassian.net/wiki/spaces/TEST/pages/1000000001/Automated+test+area) — id `1000000001`, space `TEST`.
- 2 children directly under root, 1 grandchild under one of them. Child IDs are **auto-discovered** at session start via `GET /pages/1000000001/descendants` (the test suite does not hard-code child IDs — that would couple the suite to the current state of the tree).
- Each page is pre-populated with a rich set of elements covering every converter code path: prose, H2-H6 headings, list, simple GFM-convertible table, complex table (cell containing a macro), info panel, code block with `breakoutMode`+`breakoutWidth`+`language`, task list, image attachment, inline-comment marker, user mention, opaque block, inline opaque (user mention link).
- The tree is **owned by the test suite**. Tests will mutate it freely; the baseline-restore mechanism returns it to a known state between tests.

#### Hard constraint — page-level structure is immutable

The Automated test area tree (root `1000000001` + 2 children + 1 grandchild) is created **once, manually**, by a human. From that point forward the test suite is bound by three "nevers":

- **Suite never creates pages.** No `POST /pages`.
- **Suite never deletes pages.** No `DELETE /pages/{id}`.
- **Suite never moves pages.** No parent reassignment.

Everything else inside those four pages is fair game for automated provisioning: body content (macros, panels, tables, code blocks, task lists, images, opaque blocks), titles, and **inline comments** (additive only — bootstrap creates missing comments, never deletes existing ones). Orphaned comments from prior runs are fine and get reused.

This bounds the blast radius of the test suite. A broken suite can corrupt page content but cannot multiply, delete, or re-parent anything at the page level.

#### One-time human setup

Before the first run, a human creates the 4 pages with the right parent-child structure and gives them their expected titles per `tests/fixtures/page-spec.json`. **That is the entire manual prerequisite.** No body content, no comments — bootstrap provisions everything else.

#### Fixture lifecycle: session bootstrap + per-test restore

**Committed canonical state** — `tests/fixtures/`:

```
tests/fixtures/
  page-spec.json           # logical-name → expected_title + template_filename + comments
                           # {
                           #   "root": {
                           #     "expected_title": "Automated test area",
                           #     "template": "template-root.xml",
                           #     "comments": [
                           #       { "slot": "__CM_SLOT_1__", "anchor": "the phrase to wrap", "text": "comment body" },
                           #       { "slot": "__CM_SLOT_2__", "anchor": "another phrase",     "text": "second comment" }
                           #     ]
                           #   },
                           #   "child_a": { ..., "comments": [] },
                           #   ...
                           # }
  template-root.xml        # rich storage XHTML covering every element type
  template-child-a.xml     # subset
  template-child-b.xml     # subset
  template-grandchild.xml  # subset
```

Templates are pure storage XHTML. Inline-comment markers in templates use named slots (e.g. `__CM_SLOT_1__`) as the `ac:ref` placeholder. Bootstrap matches each `slot` in `page-spec.json` to a real comment UUID and substitutes it into the template.

**Session bootstrap** (runs once per `pytest` invocation, session-scoped fixture in `conftest.py`):

1. **Discover and verify structure.** `GET /pages/1000000001/descendants`. Verify the tree shape: 1 root + 2 children of root + 1 grandchild of one child. **Wrong shape → abort the session** with instructions to fix in the UI (the suite cannot create or move pages).
2. **Assign logical names.** For each page, match its title against `expected_title` in `page-spec.json`. Title mismatch → reset via `PUT /pages/{id}/title` to the expected value (titles are allowed to be re-asserted; this isn't structural).
3. **Reconcile inline comments per page** (additive — never deletes):
   - GET the current page comments via the inline-comments API.
   - For each slot in `page-spec.json[page].comments`, check whether a matching comment already exists (by `anchor` text or by a per-suite marker string in the comment body). If yes, reuse its UUID. If no, `POST` a new inline comment anchored to `anchor` with body `text`. Capture the UUID.
   - Build a `{slot_name: real_uuid}` map for this page.
4. **Assemble + PUT.** Read the template, substitute slot placeholders with the captured UUIDs, PUT body + expected title. Version increments.
5. **Capture session baseline.** GET the resulting page, canonicalize storage (strip `local-id`, `ac:local-id`, `ac:macro-id`), store as the in-memory session baseline. Not written to disk; regenerated every session.

Bootstrap is **idempotent**: it never creates a comment that already exists (matched by anchor text or marker string), never deletes anything, and PUTs deterministic content. Running it N times in a row converges to the same state as one run, modulo version-number increments.

**Per-test restore** (function-scoped fixture, runs before every mutating test):

1. PUT the session baseline (body + expected title) to the page. Version increments.
2. No tearDown — the next test's setUp resets again. After the final test the four pages are in their baseline body+title state.

**Concurrency**: tests run **serially** (`pytest -p no:xdist`). The four fixture pages are shared mutable state; parallel sessions race.

**Inline-comment lifecycle across tests**: tests that delete a `cm` marker (B10) leave the Confluence comment orphaned. The next test's restore PUTs the marker XML back referencing the same UUID; Confluence relinks the marker to the still-existing comment record. **Suite never deletes comments**; orphaned comments from prior runs are reused on the next bootstrap rather than re-created. Over many sessions the comment count is stable.

**Accepted operational cost**: a human accidentally deleting an inline comment in the UI means the next session's bootstrap creates a replacement (with a new UUID) and re-PUTs the body. Test output is unaffected; the only visible change is a new comment record on Confluence. Not a recovery action — just normal idempotence.

#### Online vs offline split

| Tier | Scope | Requires credentials | Default behavior |
|---|---|---|---|
| **Offline** | Sentinel encode/decode, slugify, hashing, canonical XML compare, storage→MD walker on committed XML samples, MD→storage emitter on committed AST samples, opaque-map preservation. Pure logic, no network. | No | Run by default; CI-friendly |
| **Online** | Round-trip identity per page, edit-survives-roundtrip per capability, push-abort paths that need a real version response. | Yes (`credentials.toml`) | Gated by `pytest --integration`; skipped otherwise |

The offline tier alone must cover 100% of `storage_to_md.py`, `md_to_storage.py`, `sentinels.py` line coverage. The online tier proves the offline tier's assumptions against the real server.

#### Test layout

```
tests/
  conftest.py
    # session: load credentials, discover child page IDs, build baseline map
    # function: restore_baseline(page_id), make_workspace(page_id), pull(page_id), push(workspace_path)
  fixtures/
    baseline-<id>.xml × 4                  # committed; live-tenant-owned baselines
    sample-storage-modern.xml              # one captured fixture per editor flavor
    sample-storage-classic.xml             # if obtainable; otherwise xfail
    sample-md-with-each-marker.md          # offline MD→storage roundtrip
  test_units.py                            # offline: sentinels, slugify, hash, canonical compare
  test_storage_to_md.py                    # offline: walker over sample-storage-*.xml
  test_md_to_storage.py                    # offline: emitter
  test_roundtrip_identity.py               # online category A
  test_edits.py                            # online category B
  test_aborts.py                           # mixed; some pure-local, some require server
```

#### Per-template required elements

Each template's storage XHTML must contain (at least) the elements below. The test suite assumes these structures exist; missing elements break specific Category B tests. **Anchor strings inside `__CM_SLOT_N__` markers must match the `text_selection` field for that slot in `page-spec.json` exactly** — bootstrap uses substring search to find where to wrap the comment marker.

**`template-root.xml`** (kitchen-sink, drives most of Category B):

| Element | Quantity | Notes |
|---|---|---|
| `<p>` of prose | ≥ 4 | Distinct text; B01-B03 mutate, B16 reorders. The first one wraps the `__CM_SLOT_1__` marker around the substring `alpha anchor phrase one`. The second wraps `__CM_SLOT_2__` around `bravo anchor phrase two`. |
| `<h1>` (synthetic, from title) | 1 | Sourced from `expected_title`; not in body XHTML. B05 mutates the H1 in MD. |
| `<h2>` heading | 1 | B04 mutates. |
| `<ul>` with ≥ 3 `<li>` items | 1 | B17 reorders. |
| Info panel (`<ac:structured-macro ac:name="info">`) | 1 | Plain prose body. |
| Code block (`<ac:structured-macro ac:name="code">`) | 1 | Parameters: `language=python`, `breakoutMode=wide`, `breakoutWidth=760`. Body: a 3-5-line Python snippet. B12 edits body, B13 changes language, B14 verifies all params survive untouched push. |
| Simple table | 1 | 2×2, every cell text-only. B15 edits a cell, B18 reorders rows. |
| Complex table | 1 | 2×2, one cell contains a status macro (`<ac:structured-macro ac:name="status">`). Stays opaque on round-trip per plan §"Tables". |
| Task list (`<ac:task-list>`) | 1 | 3 tasks: one incomplete, one complete, one with a user mention assignee. B11 edits text, B19 reorders. |
| Image attachment | 1 | `<ac:image>` with non-default attrs (`ac:align`, `ac:width`, `ac:layout`) wrapping `<ri:attachment ri:filename="…" ri:version-at-save="1">`. The attachment file must exist on the page. B23 edits alt. |
| Opaque block (block-level) | 1 | Any block macro that isn't info/code (e.g. `<ac:structured-macro ac:name="expand">`). Round-trips opaquely via `<!--cb:HASH-->`. B20 reorders, B21 rewrites the visible label. |
| Inline opaque | 1 | A user-mention link (`<ac:link><ri:user ri:account-id="…"/></ac:link>`) inside one of the paragraphs. Round-trips opaquely via `<span data-ci="HASH">`. B22 edits the visible span text and verifies the push ignores it. |
| Inline comment markers | 2 | `__CM_SLOT_1__` and `__CM_SLOT_2__`. Bootstrap replaces with real UUIDs. B07-B10 operate on slot 1. |

**`template-child-a.xml`** — minimal; exists for subtree tests:

| Element | Quantity | Notes |
|---|---|---|
| `<p>` of prose | 2 | One identifies the page ("Child Alpha — distinct content"). |
| `<h2>` | 1 | Used by D03 to verify per-page mutation surfaced on push. |

**`template-child-b.xml`** — minimal:

| Element | Quantity | Notes |
|---|---|---|
| `<p>` of prose | 1 | Identifies the page. |
| Task list | 1 | 1 incomplete task — exercises tasks in a non-root page. |

**`template-grandchild.xml`** — minimal:

| Element | Quantity | Notes |
|---|---|---|
| `<p>` of prose | 1 | Identifies the page. Depth-2 sanity check for D01. |

#### Category B test → template element mapping

Every Category B test acts on a known element in a known template. The bootstrap guarantees that element exists; the test mutates it programmatically.

| Test | Template | Acts on |
|---|---|---|
| B01 paragraph text edit | root | first `<p>` (the one not wrapping `__CM_SLOT_1__`) |
| B02 add paragraph | root | insert after first `<p>` |
| B03 remove paragraph | root | last `<p>` |
| B04 H2 edit | root | the `<h2>` |
| B05 H1 rename | root | the synthetic H1 (page title) |
| B06 inline formatting | root | wrap `**…**` around words in the first `<p>` |
| B07 cm text edit | root | text inside `<!--cm:UUID-->…<!--/cm:UUID-->` for slot 1 |
| B08 cm split | root | insert characters mid-text inside slot 1 marker |
| B09 cm extend | root | extend text inside slot 1 marker |
| B10 cm orphan | root | delete slot 1 markers + wrapped text |
| B11 task text edit | root | the first `<ac:task>` in the task list |
| B12 code body edit | root | the code macro's `<ac:plain-text-body>` |
| B13 code language change | root | the code macro's `language` parameter (via MD fence) |
| B14 code opaque params preserved | root | code macro `breakoutMode`+`breakoutWidth` (verify untouched) |
| B15 GFM table cell edit | root | first cell of the simple table |
| B16 reorder paragraphs | root | swap first and last `<p>` |
| B17 reorder list items | root | swap two `<li>` |
| B18 reorder table rows | root | swap rows of the simple table |
| B19 reorder tasks | root | swap two tasks in the task list |
| B20 reorder opaque block | root | move the expand macro relative to surrounding `<p>`s |
| B21 block-opaque label rewrite | root | rewrite the `> [confluence: expand]` label of the expand macro |
| B22 inline-opaque visible text | root | rewrite text inside the user-mention `<span data-ci="HASH">…</span>` |
| B23 image alt edit | root | the `<ac:image>`'s alt attribute (via MD `![alt](…)`) |

#### MD → storage emitter — algorithm sketch

The emitter is the project's hardest piece. `md_to_storage.py` consumes a markdown-it token stream and a sidecar; emits Confluence storage XHTML.

```
walk(tokens, sidecar):
    out = StringBuilder()
    for token in tokens:
        match token.type:
            "heading_open" with level == 1:
                # H1 is synthesized as page title; skip in body output
                skip until matching heading_close
            "heading_open" with level >= 2:
                emit f"<h{level}>"
            "paragraph_open":   emit "<p>"
            "bullet_list_open": emit "<ul>"
            "list_item_open":   emit "<li>"
            "fence" (code block):
                uuid = trailer_uuid_or_new(token)
                params = sidecar.code_blocks.get(uuid, {}).get("params", {})
                emit f'<ac:structured-macro ac:name="code">'
                if token.info: emit f'<ac:parameter ac:name="language">{token.info}</ac:parameter>'
                for k, v in params.items(): emit f'<ac:parameter ac:name="{k}">{v}</ac:parameter>'
                emit f'<ac:plain-text-body><![CDATA[{token.content}]]></ac:plain-text-body>'
                emit '</ac:structured-macro>'
            "html_inline" or "html_block":
                # Markers — strict order: cm pairs first (validated), then cb, then ci spans, then ct
                if matches_cm_marker(token): handle_cm(token, out)
                elif matches_cb_marker(token): inject_block_opaque(sidecar.blocks[hash], out)
                elif matches_ci_span(token):    inject_inline_opaque(sidecar.inline_blocks[hash], out)
                elif matches_ct_marker(token): # consumed inside list_item handler
                    pass
                else: emit token.content (passthrough)
            "image":
                if path.startswith("./_meta/attachments/"):
                    hash = ci_trailer_for(token) or new_attachment_hash(path)
                    attrs = sidecar.images[hash].attrs if hash in sidecar.images else {}
                    emit f'<ac:image {render_attrs(attrs["image"])}>'
                    emit f'<ri:attachment {render_attrs(attrs["ri:attachment"])} />'
                    emit '</ac:image>'
                else:
                    # external URL image (legacy editor only — see notes.md)
                    emit f'<ac:image><ri:url ri:value="{token.src}" /></ac:image>'
            "table_open":      emit "<table><tbody>"
            "tr_open":         emit "<tr>"
            "td_open" / "th_open": emit "<td>" / "<th>"
            "inline":
                # recursive walk over inline tokens — emphasis, code, link, text, html_inline
                walk_inline(token.children, out, sidecar)
            ...
    return out.toString()

walk_inline(children, out, sidecar):
    # Inline pass — emit <strong>, <em>, <code>, <a>, plus inline marker re-injection
    # cm markers re-pair text spans; ci spans pull from sidecar.inline_blocks
    ...
```

Key invariants the emitter enforces (each maps to an abort in plan §"Push abort format"):

- Every `cm:UUID` opener has a matching `cm:UUID` closer with the same UUID. Otherwise → `unmatched-cm`.
- Every `cb:HASH` placeholder references a hash present in `sidecar.blocks`. Otherwise → `unknown-cb-hash`.
- Every `<span data-ci>` references a hash present in `sidecar.inline_blocks`. Otherwise → `unknown-ci-hash`.
- Every image path starting with `./_meta/attachments/` references a file that exists OR has a known `<!--ci:HASH-->` trailer matching `sidecar.images`. Otherwise → `new-attachment`.
- First non-blank line of the MD is `# Title`. Otherwise → `missing-h1`.
- All marker UUIDs / HASHes are well-formed (UUID4 or 12-hex). Otherwise → `bad-marker-syntax`.

Validation runs **as a pre-pass** before emission. If validation fails, no output is produced; the CLI exits non-zero with the rule-id.

#### `status` command output format

Stdout is **TSV** with a header line. One row per page. Fields:

```
page_id<TAB>title<TAB>local_version<TAB>remote_version<TAB>dirty<TAB>conflict
```

- `page_id` — integer.
- `title` — page title (tabs in titles are forbidden by Confluence; no escaping needed).
- `local_version` — the `base_version` from sidecar.
- `remote_version` — fetched via `GET /pages/{id}?include-version=true` at status time.
- `dirty` — `1` if `index.md` differs from `_meta/index.md.orig` (byte-compare), else `0`.
- `conflict` — `1` if `remote_version > local_version` AND `dirty == 1`, else `0`.

Single-page invocation (`status <file>`) prints exactly one data row plus the header. Subtree invocation (`status <dir>`) prints one row per page in `_meta/_subtree.json`. Exit code is `0` if any dirty/conflict rows exist (so shell users can pipe to `awk` without needing to invert), `1` if everything clean. Errors (missing sidecar, network failure) print to stderr and exit `4`.

Example:
```
$ confluence-markdown-roundtrip status ./auto-test-area
page_id	title	local_version	remote_version	dirty	conflict
1000000001	Automated test area	5	5	0	0
1000000002	Child Alpha	2	2	0	0
1000000003	Child Bravo	1	1	0	0
1000000004	Grandchild Charlie	1	2	1	1
```

#### Category A — round-trip identity, no edits (4 tests)

For each of the 4 fixture pages: `pull(page_id) → push(workspace, no changes)`. Push must short-circuit on the dirty check (step 3 of push pipeline → byte-compare `index.md` vs `_meta/index.md.orig` is equal → no PUT). Verify: no version bump on Confluence, no warnings.

Then: `pull(page_id) → in-memory storage→MD→storage → c14n2 byte-compare` to original storage (with bookkeeping attrs stripped). Verify byte equality.

- `test_roundtrip_identity[root]`
- `test_roundtrip_identity[child_a]`
- `test_roundtrip_identity[child_b]`
- `test_roundtrip_identity[grandchild]`

#### Category B — edit survives round-trip (one per editable surface)

Each test follows the same pattern: `pull → mutate index.md programmatically → push → re-pull to a fresh dir → diff`. The diff must show the intended mutation and nothing else. **The baseline-restore fixture runs before each test.**

| # | Test | Capability under test |
|---|---|---|
| B01 | `test_edit_paragraph_text` | Change text inside an existing `<p>`. |
| B02 | `test_add_paragraph` | Insert a new `<p>` between existing ones. |
| B03 | `test_remove_paragraph` | Delete a `<p>`. |
| B04 | `test_edit_heading_h2` | Change H2 text. |
| B05 | `test_edit_h1_renames_page` | Change the `# Title` line; verify Confluence page title was renamed AND new title appears in re-pull. |
| B06 | `test_inline_formatting` | Add/remove `**bold**`, `*italic*`, `` `code` ``. |
| B07 | `test_inline_comment_text_edit` | Edit text inside `<!--cm:UUID-->...<!--/cm:UUID-->`; verify UUID preserved on re-pull. |
| B08 | `test_inline_comment_split_text` | Split text inside `cm` markers (insert characters in the middle); verify range tracks. |
| B09 | `test_inline_comment_extend_text` | Extend text inside `cm` markers; verify range grows. |
| B10 | `test_inline_comment_orphan` | Delete all text + the two `cm` half-markers together; verify marker absent on re-pull and the comment is orphaned on Confluence (acceptable per plan §"Edit-time edge cases"). |
| B11 | `test_task_text_edit` | Edit text after `- <!--ct:UUID--> `; verify task state (checkbox, assignee, due date) unchanged on re-pull. |
| B12 | `test_code_body_edit` | Edit fenced code body; verify language fence preserved AND all opaque params from sidecar (`breakoutMode`, `breakoutWidth`, …) re-emitted on push. |
| B13 | `test_code_language_change` | Change ` ```python ` → ` ```rust `; verify on re-pull. |
| B14 | `test_code_opaque_params_preserved` | Push without touching any code-block param; verify every `<ac:parameter>` survives byte-equal in the next pull. |
| B15 | `test_gfm_table_cell_text_edit` | Edit a cell in a simple GFM table; verify only that cell changes. |
| B16 | `test_reorder_paragraphs` | Swap two `<p>`s; verify order on re-pull. |
| B17 | `test_reorder_list_items` | Swap two items in a `<ul>`. |
| B18 | `test_reorder_table_rows` | Swap two rows in a GFM table. |
| B19 | `test_reorder_task_items` | Swap two task lines (`- <!--ct:UUID--> ...`). |
| B20 | `test_reorder_opaque_block` | Move a (label + `<!--cb:HASH-->`) pair to a new position; verify content unchanged, position changed. |
| B21 | `test_block_opaque_label_rewrite` | Rewrite the `> [confluence: info]` label text only (no `cb:HASH` change); verify Confluence content unchanged (sidecar is source of truth). |
| B22 | `test_inline_opaque_visible_text_ignored` | Change visible text inside `<span data-ci="HASH">visible</span>`; verify Confluence content unchanged (visible text is cosmetic). |
| B23 | `test_image_alt_text_edit` | Change alt in `![alt](./_meta/attachments/file)<!--ci:HASH-->`; verify alt updated AND HASH trailer + sidecar attrs intact. |

#### Category C — push aborts (one per rule-id)

Each test constructs a deliberately broken MD state, attempts push, verifies:
- Process exit code matches plan §"Push abort format".
- Stderr line shape matches `error: <file>:<line>: <rule-id>: <message>`.
- No PUT was made (Confluence version unchanged).
- No partial sidecar write.

| # | Test | Rule ID |
|---|---|---|
| C01 | `test_abort_unmatched_cm` | `unmatched-cm` — open `<!--cm:UUID-->` with no closing half |
| C02 | `test_abort_unknown_cb_hash` | `unknown-cb-hash` — `<!--cb:DEADBEEF-->` not in sidecar |
| C03 | `test_abort_unknown_ci_hash` | `unknown-ci-hash` — `<span data-ci="DEADBEEF">x</span>` not in sidecar |
| C04 | `test_abort_new_attachment` | `new-attachment` — `![](./_meta/attachments/notexist.png)` referencing non-existent file |
| C05 | `test_abort_missing_h1` | `missing-h1` — first non-blank line is `## H2`, not `# H1` |
| C06 | `test_abort_bad_marker_syntax` | `bad-marker-syntax` — `<!--cm:not-a-uuid-->X<!--/cm:not-a-uuid-->` |
| C07 | `test_abort_orig_tampered` | `orig-tampered` — modify `_meta/index.md.orig` between pull and push |
| C08 | `test_abort_meta_tampered` | `meta-tampered` — corrupt `_meta/index.conf.json` |
| C09 | `test_abort_version_conflict` | `version-conflict` — bump remote version via direct API PUT, then push; verify abort AND `<file>.remote.md` is written |

#### Test infrastructure helpers (in `conftest.py`)

```python
# --- session-scoped, runs once per pytest invocation ---

def discover_pages() -> dict[str, int]:
    """Return {"root": 1000000001, "child_a": <id>, "child_b": <id>, "grandchild": <id>}."""

def bootstrap_fixtures(pages: dict[str, int]) -> dict[int, bytes]:
    """
    PUT each template, ensure inline comments exist, capture session baselines.
    Idempotent — running twice produces no net change.
    Returns {page_id: canonical_storage_bytes} — the in-memory session baselines.
    """

# --- function-scoped, runs before each mutating test ---

def restore_baseline(page_id: int, session_baselines: dict[int, bytes]) -> None:
    """PUT session baseline to the page. Increment version. Verify integrity."""

def make_workspace(page_id: int) -> Path:
    """Fresh tmp dir; run `confluence-markdown-roundtrip pull <id> --into <tmp>`. Return path."""

def push(workspace_path: Path) -> CompletedProcess:
    """Run `confluence-markdown-roundtrip push <path>`. Return exit code + stderr."""

def diff_md(a: Path, b: Path) -> list[Hunk]:
    """Structured diff between two index.md files. Used by category B."""
```

#### CLI surface for tests

The CLI itself stays the same (`pull`, `push`, `status`). The test runner just invokes those subcommands. No `dev`-only commands — bootstrap is part of the test session, not a separate maintainer step.

#### Definition of done for Phase 2

- All offline tests pass (`pytest tests/`).
- All online tests pass against the live tenant (`pytest tests/ --integration`).
- Coverage of `storage_to_md.py`, `md_to_storage.py`, `sentinels.py` ≥ 95% (offline tier).
- The four fixture pages end in baseline state after a full suite run.
- A second consecutive suite run (no manual cleanup between) passes identically — proves true automation, no hidden state, no flakes.

### Phase 3 — Subtree + skill packaging

- Implement `_meta/_subtree.json`, descendants walk, leaf-first push.
- Slugify + collision resolution.
- Per-page `_meta/attachments/`.
- Write `SKILL.md` instructing the agent on pull/push/status invocation.
- Extend the Phase 2 automated suite with subtree-mode tests against the same fixture tree (root `1000000001` + 2 children + 1 grandchild), each baseline-restored per test:

| # | Test | Capability |
|---|---|---|
| D01 | `test_subtree_pull` | `pull --subtree <root>` produces `_meta/_subtree.json` with 4 entries, correct parent links, correct slugs, correct on-disk layout (`<root-slug>/child-a/grandchild/index.md`). |
| D02 | `test_subtree_slugify_collisions` | Rename two children to the same title via API before pulling; verify the slug collision resolver appends `-2`. |
| D03 | `test_subtree_leaf_first_push` | Mutate one paragraph in all 4 pages, `push <root-dir>`, verify PUTs were issued grandchild → children → root (capture order via per-page request timestamps). |
| D04 | `test_subtree_repull_partial_dirty` | Mutate root + child_a only; re-pull; verify `.remote.md` written only for those two; clean pages overwritten. |
| D05 | `test_subtree_skips_non_pages` | **Offline-only** (cannot add non-page descendants to the live tree). Synthetic `GET /pages/{id}/descendants` response with `type: "whiteboard"` and `type: "database"` mixed in; verify walker filters to pages only. Lives in `test_units.py` rather than `test_subtree.py`. |

### Phase 4 — Editable panels

Promote Confluence's panel constructs from opaque-block to editable. Only the panel **body** becomes editable; the **style** (which type it is) stays under sidecar control.

**Two storage shapes, one MD form.** Confluence Cloud emits panels in two forms depending on how they were authored. Both round-trip via the same `<!--cp:UUID-->` MD construct; the sidecar's `shape` discriminator routes the push back to the correct XHTML.

- **Legacy macro** (`shape: "macro"`): `<ac:structured-macro ac:name="info|note|warning|tip">` with `<ac:rich-text-body>`. 4 type values.
- **Modern ADF panel** (`shape: "adf"`): `<ac:adf-extension>` → `<ac:adf-node type="panel">` → `<ac:adf-attribute key="panel-type">VALUE</ac:adf-attribute>` + `<ac:adf-content>` body. 5 type values: info, note, warning, success, error. `panel-type=custom` stays opaque (carries arbitrary colors/icons).

**Out of scope.** Other ADF-extension types (anything where `ac:adf-node` is not `panel`, or `panel-type` is `custom`) stay opaque per Phase 2/3.

**MD form.**

```
<!--cp:UUID style=info-->

body markdown (any blocks: paragraphs, lists, code, nested panels, ...)

<!--/cp:UUID-->
```

- `UUID` is a UUID4 minted at pull time; persists across edits.
- `style=NAME` is a non-editable hint for the human reader. The push pipeline reads `NAME` from `sidecar.panels[UUID].name`, not from the marker. Editing the hint in MD has no effect; a missing hint is accepted.
- Blank line before/after each marker is required for CommonMark to recognize them as `html_block` tokens. The pull side emits with the blank lines; the push validator does not enforce them (CommonMark will).
- Nesting is permitted: a `cp:` body can contain another `cp:` pair with a different UUID. Open/close pairing is UUID-matched.

**Sidecar.** New top-level key (see §"Sidecar schemas" for full shape). Discriminated by `shape`:

```jsonc
"panels": {
  "<UUID-of-macro-panel>": {
    "shape": "macro", "name": "info", "params": {}
  },
  "<UUID-of-adf-panel>": {
    "shape": "adf", "name": "note", "adf_attrs": {}, "adf_fallback": "<div>...</div>"
  }
}
```

**ADF fallback policy.** The `<ac:adf-fallback>` element contains a self-contained HTML rendering of the panel (with inline styles for background-color, border, icon). When the user edits the body, this fallback becomes stale for legacy renderers. v1 chooses *preserve verbatim*: pull captures `adf_fallback` once; push reattaches it untouched. The modern Confluence web renderer reads `<ac:adf-content>` (which we update) and ignores the fallback, so the visible page on the web is always current. Only out-of-date export pipelines see stale fallback. Server-side regeneration of the fallback is not attempted in v1.

**Pull algorithm sketch** (in `storage_to_md._Walker`).

```
block(el):
  ...existing branches...
  if el is <ac:adf-extension> wrapping an editable adf-node:
    return _panel_adf(el)
  ...

_macro(el):
  if ac:name in {info, note, warning, tip} and has ac:rich-text-body:
    return _panel_macro(el)
  ...existing branches...

_panel_macro(el):
  uuid = uuid4()
  name = el@ac:name
  params = {p@ac:name: p.text for p in el/ac:parameter}
  body_lines = _render_body(el/ac:rich-text-body)
  sidecar.panels[uuid] = {"shape": "macro", "name": name, "params": params}
  return [cp_open(uuid, style=name), "", *body_lines, cp_close(uuid)]

_panel_adf(el):
  node = el/ac:adf-node[type=panel]
  panel_type = node//ac:adf-attribute[key=panel-type].text
  if panel_type == "custom": return _opaque_block(el)  # fall back to opaque
  uuid = uuid4()
  adf_attrs = {a@key: a.text for a in node//ac:adf-attribute if key != panel-type}
  fb = el/ac:adf-fallback
  adf_fallback = c14n(fb.children) if fb else ""    # innerXML, verbatim
  body_lines = _render_body(node/ac:adf-content)
  sidecar.panels[uuid] = {"shape": "adf", "name": panel_type,
                          "adf_attrs": adf_attrs, "adf_fallback": adf_fallback}
  return [cp_open(uuid, style=panel_type), "", *body_lines, cp_close(uuid)]
```

**Push algorithm sketch** (in `md_to_storage._Emitter`).

```
_emit_one():
  if html_block token matches CP_OPEN_RE:
    return _emit_panel(uuid)
  ...existing branches...

_emit_panel(uuid):
  collect tokens until matching CP_CLOSE_RE with same uuid (UUID-matched, nesting OK)
  body_xhtml = _Emitter(collected, self.sidecar).run()
  entry = sidecar.panels[uuid]                # required; abort unknown-cp-uuid if absent
  if entry.shape == "macro":
    emit:
      <ac:structured-macro ac:name="{entry.name}" ac:schema-version="1">
        {<ac:parameter ac:name="K">V</ac:parameter> for K,V in entry.params}
        <ac:rich-text-body>{body_xhtml}</ac:rich-text-body>
      </ac:structured-macro>
  elif entry.shape == "adf":
    emit:
      <ac:adf-extension>
        <ac:adf-node type="panel">
          <ac:adf-attribute key="panel-type">{entry.name}</ac:adf-attribute>
          {<ac:adf-attribute key="K">V</ac:adf-attribute> for K,V in entry.adf_attrs}
          <ac:adf-content>{body_xhtml}</ac:adf-content>
        </ac:adf-node>
        <ac:adf-fallback>{entry.adf_fallback}</ac:adf-fallback>
      </ac:adf-extension>
```

**Validation pre-pass.** Walk extended with a `cp_stack` like `cm_stack`:

- `unmatched-cp` — open without close, mismatched UUID, or close without open.
- `unknown-cp-uuid` — UUID not in `sidecar.panels`. New panels created in MD are not supported in this phase (mirrors the `new-attachment` policy).

**Style hint policy.** The `style=NAME` payload is informational only: the push validator does **not** enforce that the hint matches the sidecar name. This keeps the hint a pure read-only display aid; a stale hint after sidecar edit is a documentation issue, not an abort.

**Tests** (additions to `test_storage_to_md.py`, `test_md_to_storage.py`, plus B-test on the live tenant).

| # | Test | Capability |
|---|---|---|
| P1 | `test_macro_panel_pulls_editable` | Single `<ac:structured-macro ac:name="info">` body emits cp markers. Sidecar `panels[UUID] == {shape:"macro", name:"info", params:{}}`. |
| P2 | `test_all_four_macro_panel_types_pull` | info, note, warning, tip each render with correct `style=` hint and `name`. |
| P3 | `test_adf_panel_pulls_editable` | `<ac:adf-extension>` panel emits cp markers. Sidecar `panels[UUID] == {shape:"adf", name:"note", adf_attrs:{...}, adf_fallback:"..."}`. |
| P4 | `test_adf_panel_success_and_error_supported` | panel-type values `success` and `error` round-trip identically to info/note/warning. |
| P5 | `test_adf_custom_panel_stays_opaque` | `panel-type=custom` falls through to opaque-block. |
| P6 | `test_panel_body_with_rich_content` | Body with bold + link + list round-trips through pull. |
| P7 | `test_macro_panel_push_uses_sidecar_name_not_hint` | Push with `style=warning` in marker but sidecar `name=info` emits `<ac:structured-macro ac:name="info">`. |
| P8 | `test_adf_panel_push_preserves_fallback` | Push reattaches `<ac:adf-fallback>` content verbatim from sidecar. |
| P9 | `test_panel_push_unknown_uuid_aborts` | Push aborts with `unknown-cp-uuid`. |
| P10 | `test_panel_push_unmatched_aborts` | Open without close → `unmatched-cp`. |
| P11 | `test_macro_panel_body_edit_survives_round_trip` | Edit one word in body MD, push, re-pull, verify storage retains macro name + new body. (Live B-test on legacy panel.) |
| P12 | `test_adf_panel_body_edit_survives_round_trip` | Same as P11 for the ADF panel at the top of the fixture page. |
| P13 | `test_panel_round_trip_byte_stable` | Pull-then-push without edits produces byte-equal storage (modulo bookkeeping) for the panel fragments. |

**Definition of done.** Plan §"Storage → MD mapping" updated (done above), both panel shapes editable, P1–P13 green, fixture page round-trips with all 6 fixture panels in the editable surface.

### Phase 5 — Attachment binary download (read-only)

Download the binary for every `<ac:image>`-referenced attachment so the MD renders with real images in any local previewer. Read-only: no upload, no new attachments. Image references already round-trip via the storage→MD mapping; only the local binary copy was missing.

**Auth path correction.** The earlier conclusion (CLAUDE.md "Phase 2/3 online findings" #5) said `/wiki/download/attachments/...` requires OAuth under Basic auth. That is true for the **v2** `_links.download` URL, but the **v1** child-attachment endpoint accepts Basic and returns the binary:

```
GET /wiki/rest/api/content/{page_id}/child/attachment/{attachment_id}/download
Authorization: Basic <email:api_token>
```

Confirmed against the live tenant: returns `200 image/png` with full bytes; works with or without the `att` prefix on the attachment id. No OAuth, no scope dance.

**Algorithm.** The pull pipeline already calls `_download_referenced_attachments` (cli.py); the v2 path it uses fails with the OAuth wall and only logs a warning. Replace with a v1 fetch:

```
for att in client.list_attachments(page_id):
  if att.title not in referenced_filenames: continue
  data = client.download_attachment_v1(page_id, att.id)
  write to _meta/attachments/{att.title}
```

`att.id` keeps the `att` prefix as returned by v2 list (the v1 download endpoint accepts both forms).

**Sidecar.** No schema change. `images[HASH]` already records `filename` + dimensions; the binary lives at `_meta/attachments/{filename}`. The path in the MD (`./_meta/attachments/{filename}`) was already what we emit on pull.

**MD preview.** Once the binaries are on disk, any markdown previewer (VS Code, Obsidian, GitHub web view of a checked-in workspace) renders the images inline via the relative path.

**Out of scope (still in Phase 6).**
- **Uploading** new attachments referenced from MD (still aborts with `new-attachment` per existing policy).
- Re-downloading on every pull regardless of whether the binary already exists (current behavior is "download if absent"; if the user wants forced refresh on attachment version bumps, that's a follow-on).

**Tests.**

| # | Test | Capability |
|---|---|---|
| A1 | `test_A1_attachment_binaries_downloaded_on_pull` | After pull, every `<ac:image ri:attachment ri:filename="X">` reference has a file at `_meta/attachments/X` whose bytes match what the v1 endpoint returns. (Live B-test against the Phase 1.5 fixture page 1234567890 — it has a PNG and an SVG.) **Done.** |
| A2 | `test_subtree_pull_downloads_attachments` | Subtree-pull regression: every page in the tree that references an attachment must have its binaries fetched, not just the single-page path. **Done.** |

(The originally-planned A2 — `test_pull_warns_but_succeeds_when_attachment_inaccessible` — was dropped: warn-and-continue is exercised by code review of the helper and by manual stderr inspection; no live restricted-attachment fixture is worth maintaining for it.)

**Definition of done.** v1 download path implemented for both single-page and subtree pull, A1+subtree test green, CLAUDE.md finding #5 corrected, the user can preview pulled MD with images rendering inline. **Phase 5 complete.**

### Phase 6 — Panels render as GFM Alerts in MD preview

Confluence panels currently round-trip via `<!--cp:UUID style=X-->...<!--/cp:UUID-->` HTML-comment markers, which are *invisible* in any MD previewer. The local file looks like plain prose surrounded by HTML comments, so editing the round-tripped MD in VS Code / Obsidian / GitHub web view gives no visual cue of the panel kind. Make the body of each panel render with the matching color, icon, and border by emitting a [GFM Alert](https://github.com/orgs/community/discussions/16925) inside the existing `cp:` wrapper. The push side must produce byte-identical storage XHTML for an unedited pull — the visual layer is one-way decoration the push pipeline strips back out.

**Type mapping (Confluence → GFM Alert).** Determined deterministically from `sidecar.panels[uuid].name`, not from anything the user types in MD:

| Confluence panel | GFM alert | Notes |
|---|---|---|
| `info` | `NOTE` | blue info |
| `note` | `IMPORTANT` | purple — closest to Confluence note |
| `tip` | `TIP` | green |
| `success` | `TIP` | green (no GFM equivalent for "success") |
| `warning` | `WARNING` | yellow |
| `error` | `CAUTION` | red |

Mapping helper lives in `sentinels.py` next to `cp_open/cp_close`: `panel_kind_to_gfm(name) -> str`.

**On-disk marker shape.**

Phase 4 (current):
```
<!--cp:UUID style=note-->

body line 1
body line 2

<!--/cp:UUID-->
```

Phase 6 (new):
```
<!--cp:UUID-->
> [!IMPORTANT]
> body line 1
> body line 2
<!--/cp:UUID-->
```

The `style=...` attribute is dropped from the opener (it duplicated `sidecar.panels[uuid].name`). The opener carries the UUID only.

**storage_to_md changes** (sketch):
- `S.cp_open(uuid)` — drop the `style=` parameter (or keep optional + always pass nothing).
- After opener, emit one `> [!<KIND>]` line where `KIND = panel_kind_to_gfm(panel_name)`.
- Prefix every body line (including blank lines and nested block elements) with `> `, à la a one-level blockquote.

**md_to_storage changes** (sketch):
- When extracting cp body between `<!--cp:UUID-->` and `<!--/cp:UUID-->`:
  - If the first non-blank line matches `^> \[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*$`, drop that line.
  - Strip exactly one leading `> ` (or `>` on otherwise-empty lines) from each remaining body line.
- Feed the un-prefixed body to the existing panel-body XHTML converter — no change downstream.
- The alert kind is ignored on push; `sidecar.panels[uuid].name` is the source of truth for panel type.

**Backwards compat with Phase 4 workspaces.**
- Parser accepts both `<!--cp:UUID-->` and `<!--cp:UUID style=...-->` openers; the attribute is read and discarded.
- Body extractor's blockquote strip is a no-op for bodies without the alert prefix — Phase 4 workspaces continue to push cleanly without re-pull.

**Edge cases.**
- User removes the `> [!KIND]` tag manually → body still extracts (no alert pattern matches first line, but `> ` strip still works on remaining lines).
- User breaks out of the blockquote (un-prefixed paragraph inside the cp wrapper) → that paragraph is consumed as-is into the panel body. Push still succeeds; visual rendering on next pull restores the prefix.
- Nested panels → Confluence does not support; not relevant.
- Multi-block bodies (paragraph + list + nested block) → each block-line prefixed with `> ` on emit, stripped uniformly on parse. GFM alerts permit arbitrary block content inside the blockquote per the spec, so renderers handle this correctly.

**Marker syntax doc updates.** The "Markers" table in plan §"Marker syntax in the Markdown" and in `SKILL.md` must reflect the dropped `style=` attribute and the new visual form.

**Tests.**

| # | Test | Capability |
|---|---|---|
| V1 | `test_panel_renders_as_gfm_alert` | After pull of fixture page 1234567890 (info/note/warning/success/error panels), MD contains five `> [!KIND]` blocks with the expected mapping. |
| V2 | `test_unedited_pull_pushes_canonically_identical_storage` | Pull → no edits → `md_to_storage` produces storage XHTML whose canonical form equals the canonical pulled storage. Canonical = lxml `c14n2` with `local-id` / `ac:local-id` / `ac:macro-id` stripped (same canonicalization used for the block-HASH; see CLAUDE.md "Persistent decisions"). Guards round-trip identity across the panel rewrite. |
| V3 | `test_phase4_style_attr_opener_still_parses` | Hand-craft `index.md` with `<!--cp:UUID style=note-->` (Phase 4 form, no alert inside) → push succeeds, body converts normally. Backwards compat. |
| V4 | `test_user_strips_alert_tag_pushes_cleanly` | Remove the `> [!KIND]` line from a pulled panel body → push succeeds, panel type unchanged (from sidecar), body content preserved. |

**Definition of done.** V1–V4 green, fixture page renders with visible borders/icons/colors in VS Code preview (manual confirmation), `confluence-markdown-roundtrip pull <id> && confluence-markdown-roundtrip push <dir>` on an unedited workspace produces a no-op (no version bump server-side). **Phase 6 complete.** Implementation: [test_panels_gfm_alert.py](code/confluence_markdown_roundtrip/tests/test_panels_gfm_alert.py); helpers in [sentinels.py](code/confluence_markdown_roundtrip/sentinels.py) (`panel_kind_to_gfm`, `GFM_ALERT_OPENER_RE`); emit in [storage_to_md.py](code/confluence_markdown_roundtrip/storage_to_md.py) (`_wrap_panel_with_alert`); strip in [md_to_storage.py](code/confluence_markdown_roundtrip/md_to_storage.py) (`_strip_panel_alert_wrappers`).

### Phase 7 — Local cross-page links in subtree pulls (read-only)

Pages in a Confluence subtree link to each other constantly. After a subtree pull, those links still point to `https://tenant/wiki/spaces/KEY/pages/PID/...` URLs — clicking one in a local previewer leaves the workspace. Rewrite in-tree page links to relative paths (`[text](../other-page/index.md)`) so navigating the local copy feels like browsing the live space. Out-of-tree links pass through unchanged.

**Read-only.** The local link is a *display form* only. The visible link text and path in MD are not editable: push restores the original Confluence link element from the sidecar verbatim, ignoring whatever the user typed. Editing link target or text must be done on the Confluence side. This keeps the implementation small: no resolution-on-push, no edit semantics, just opaque verbatim restoration with a navigable display layer.

**Scope.**
- Subtree pulls only. Single-page pulls have no peers in the workspace; rewriting is a no-op.
- In-bound: links *into* the tree. Out-bound links (to external sites, to pages in other Confluence spaces, to anchor-only references inside the same page) pass through.

**Storage forms to recognize.** Confluence Cloud emits at least four shapes that need handling:

| Shape | Example | How to resolve target page id |
|---|---|---|
| `<a>` with URL href | `<a href="https://tenant/wiki/spaces/EN/pages/4943052820/Backend">Backend</a>` (often with `data-card-appearance="inline"`) | parse `/pages/PID/` from href |
| `<ac:link>` + `<ri:page content-id>` | `<ac:link><ri:page ri:content-id="4943052820"/><ac:plain-text-link-body>text</ac:plain-text-link-body></ac:link>` | read attribute |
| `<ac:link>` + `<ri:page content-title>` | `<ac:link><ri:page ri:content-title="Backend" ri:space-key="EN"/>...</ac:link>` | (space, title) → manifest lookup |
| Any of the above with `ac:anchor="section-name"` | adds `<ac:link ac:anchor="...">` wrapper or `#anchor` URL suffix | preserve anchor through rewrite |

**Pre-pull index** (subtree.py). After the BFS but before iterating pages, build two dicts from the manifest:

```
pid_to_relpath: dict[str, str]      # "4943052820" -> "alexandria/backend/index.md"
title_to_pid:   dict[(str, str), str]  # (space_key, title) -> pid
```

Both are passed into `storage_to_md(...)`. Single-page pulls get empty dicts.

**Sidecar schema addition.** A new top-level key `links` parallel to `blocks`/`inline_blocks`/`panels`:

```
links: {
  HASH: { xml: "<original <a> or <ac:link> element, verbatim>" }
}
```

That's the whole entry. The full original element is the source of truth for push; pull parses it once to compute the navigable display form (relative path + anchor). No need to break out target_page_id, attrs, body form — they're all inside `xml`.

`HASH = sha256(c14n2(stripped))[:12]` of the original `<a>` or `<ac:link>` element, same convention as `cb:` / `ci:` markers.

**MD marker shape.**

```
[text](relative/path/to/other/index.md)<!--cl:HASH-->
[text](relative/path#anchor-name)<!--cl:HASH-->
```

The `<!--cl:HASH-->` (cross-link) trailer is the source of truth on push. The visible `text` and `(path)` are display-only — push regenerates the link from the sidecar XML, ignoring both.

**storage_to_md changes.**
- `_Walker` gains constructor params `pid_to_relpath`, `title_to_pid`, `self_page_relpath`.
- New inline handler for `<a>`: if href parses as a tenant pages URL and PID is in `pid_to_relpath`, emit `[text](rel)<!--cl:HASH-->` + `sidecar.links[HASH] = {xml: <original verbatim>}`. Else emit as a normal `[text](href)`.
- `<ac:link>` containing `<ri:page>`: resolve target id by content-id or (space_key, title). If in-tree → same `cl:` form. Else → opaque-inline (existing `data-ci="HASH"` path).
- Anchor: append `#anchor-name` to the displayed relative path. The anchor is already inside the stored XML, so push doesn't need to reconstruct it separately.

**md_to_storage changes.**
- Markdown link tokens: peek at the following `html_inline` token for a `<!--cl:HASH-->` trailer.
  - If present → look up `sidecar.links[HASH].xml`, emit it verbatim, consume the trailer. Visible text and path are discarded.
  - If absent and the path looks like a workspace-relative file (no scheme, doesn't start with `http`/`mailto:`/`#`) → abort `bad-marker-syntax` (orphaned local link). The MD no longer round-trips, and silently emitting `<a href="../foo/index.md">` would corrupt the Confluence page.
  - If absent and path is a normal URL → emit `<a href="URL">text</a>` as before.
- Validation pre-pass adds rule `unknown-cl-hash`.

**Path computation.** At MD-emit time:
```
rel = posixpath.relpath(target_relpath, start=posixpath.dirname(self_page_relpath))
```
Both inputs are POSIX-style paths relative to `into_dir` (the subtree root parent). Output goes straight into the `[text](rel)` form.

**Edge cases & decisions.**

- **Page renamed between pull and push.** Sidecar.xml is unchanged; Confluence's URL-slug redirects handle the rename server-side.
- **Page deleted from the tree between pull and push.** Push regenerates the link by stored XML. Confluence renders a "page no longer exists" placeholder. No abort — matches pre-Phase-7 behavior for any link.
- **Subtree scope changed on re-pull.** A link previously in-tree may now be out-of-tree. Subsequent pull writes it as a plain external URL (no `cl:` trailer). User loses local navigation for that link; no data loss.
- **User edits visible text or path.** Silently ignored on push (sidecar wins). Local-side edits to links are not a supported feature.
- **User deletes the `cl:` trailer.** Push aborts (`bad-marker-syntax`). The MD no longer round-trips.
- **Self-link** (page → itself, usually with an anchor). Treat as in-tree with target == self. Display path becomes `index.md#anchor`.
- **Same link target multiple times in one page.** Same HASH, same sidecar entry, multiple `cl:` trailers in MD.

**Tests.**

| # | Test | Capability |
|---|---|---|
| L1 | `test_in_tree_url_link_rewrites_to_local_path` | Pull subtree where page A links to page B via tenant URL. Verify A's index.md contains `[text](../b/index.md)<!--cl:HASH-->` and `sidecar.links[HASH].xml` contains the original `<a>` element. |
| L2 | `test_ac_link_content_title_rewrites_to_local_path` | Same as L1 but link source is `<ac:link><ri:page ri:content-title="..."/></ac:link>`. Resolution uses the (space, title) → pid index. |
| L3 | `test_ac_link_content_id_rewrites_to_local_path` | Variant: `<ri:page ri:content-id="PID"/>`. Direct lookup. |
| L4 | `test_out_of_tree_link_passes_through` | Link to a page **not** in the subtree → MD `[text](https://...)` with no `cl:` trailer; `sidecar.links` has no entry. |
| L5 | `test_round_trip_canonical_for_all_link_forms` | Pull → no edit → push → canonical XML matches input. Parameterized over (URL-form, ac-link/content-id, ac-link/content-title, anchored). |
| L6 | `test_anchor_link_round_trip` | URL with `#section-anchor` and `<ac:link ac:anchor="...">` both round-trip; MD displays `#section-anchor` on the relative path. |
| L7 | `test_user_text_or_path_edit_ignored` | User edits the `[text]` and/or `(path)` of a cl-marked link → push canonical XML still matches original (edits silently overridden by sidecar). |
| L8 | `test_orphaned_local_link_aborts` | User removes `<!--cl:HASH-->` trailer (or HASH not in sidecar) → push aborts with `bad-marker-syntax` (orphaned trailer) or `unknown-cl-hash` (HASH missing). |
| L9 | `test_alexandria_subtree_local_links_navigate` | Live test on the Alexandria subtree. Pull, verify at least N>0 cross-page links rewrote to relative paths and each target file exists on disk. |

**Marker syntax doc update.** Add the `cl:` marker to the table in plan §"Marker syntax in the Markdown" and in `SKILL.md`. Add the rule id `unknown-cl-hash` to plan §"Push abort format".

**Out of scope (see "Future plans").**
- Editable cross-page links (changing target by typing a new path, editing anchor, etc.).
- Rewriting links to **attachments** of other in-tree pages (Phase 5 handles only embedded images, not link-style refs to attachments).
- Rewriting links inside opaque content (an `<ac:structured-macro>` body containing a page link stays opaque).
- Cross-space tree pulls.

**Definition of done.** L1–L9 green, the Alexandria subtree pulled today renders with working cross-page links in VS Code preview (Cmd-click → opens the right local file), full round-trip canonical identity for every internal link without any user edits. **Phase 7 complete.** Implementation: [test_links.py](code/confluence_markdown_roundtrip/tests/test_links.py); helpers in [sentinels.py](code/confluence_markdown_roundtrip/sentinels.py) (`CL_RE`, `cl()`, `unknown_cl_hash`); emit in [storage_to_md.py](code/confluence_markdown_roundtrip/storage_to_md.py) (`_maybe_rewrite_a_as_cl`, `_maybe_rewrite_ac_link_as_cl`, `_emit_cl`, `_parse_tenant_page_url`); push handling in [md_to_storage.py](code/confluence_markdown_roundtrip/md_to_storage.py) (`_inline_link`, `_looks_like_local_workspace_path`); subtree index in [subtree.py](code/confluence_markdown_roundtrip/subtree.py) (two-pass: fetch then convert with `pid_to_relpath` / `title_to_pid` / `self_page_relpath`).

## Future plans (no scheduled phase)

Items below are out of scope for the current roadmap. They are recorded so the design implications are visible, but no work is committed; treat any of them as a fresh planning effort if a use case forces it.

- ADF support.
- OAuth 3LO.
- Attachment upload (new images via MD).
- Structural ops: page create / delete / move / rename-with-link-rewriting.
- Page metadata via frontmatter.
- Bulk operations across a whole space.
- Footer comment read/reply.
- Generic `panel` macro (single macro with `panelType`/`bgColor` params) — separate from the legacy four.
- Changing panel kind by editing the alert tag (today the alert is cosmetic; the kind comes from sidecar).
- Iterative descendant-walk for trees deeper than 10 levels (current fix passes `depth=10`; v2 API caps individual calls at 10).
- Cross-space subtree pulls + link rewriting across spaces.
- Rewriting links to attachments of other in-tree pages.

## Open questions

1. Permission-denied descendants — do they appear in the descendant list with stubs or vanish silently? Need to test in phase 1.5.
2. Pagination behavior of `GET /pages/{id}/descendants` — page size, cursor format.
3. Confluence whitespace renormalization on `PUT` — does an identical body trigger a version bump? Verify in phase 2.
4. Subtree size cap default — leave unlimited with progress output, or guard at 100/500/1000? Decide during phase 3 based on real targets.
5. Newer-editor pages: storage representation of ADF-native pages may differ from classic-editor pages. Compare in phase 2 against a page created in the modern editor.
