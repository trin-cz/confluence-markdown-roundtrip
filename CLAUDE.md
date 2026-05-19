# Confluence round-trip project — session-to-session notes

Project-state and operational lessons that don't belong in `plan.md` or `notes.md`. Read this first.

## Tenant + fixture page IDs

Tenant-specific page IDs live in `~/.config/confluence-markdown-roundtrip/live-tests.toml` (gitignored). The file holds two values:

- `reference_page_id` — Phase 1.5 read-only fixture. A real page with 10/11 element types (panels, code, tasks, table, image, inline comments). Used by `test_attachments`, `test_panels_gfm_alert`. Don't mutate.
- `root_page_id` — Phase 2 automated test tree root. The suite walks descendants to discover children. The tree shape is fixed:
  - Root: title "Automated test area"
  - Child Alpha (under root): title "Child Alpha", with a grandchild "Grandchild Charlie"
  - Child Bravo (under root): title "Child Bravo"

Credentials (tenant URL, email, API token) live in `~/.config/confluence-markdown-roundtrip/credentials.toml` (mode `0600`). Inside the container that's `/home/claude/.config/...`.

## Token-handling protocol (load-bearing)

The user leaked the same Atlassian API token three times in one session by pasting `-v` / `-i` / `-u` output. Lessons:

- **Never** put a token in `argv`. `-u email:TOKEN` exposes it in shell history and process listings. `-H 'Authorization: Bearer TOKEN'` same.
- **Never** print `-v` or `-i` output containing an `Authorization:` header — Basic auth base64 reverses to plaintext `email:token`. Masking the middle of a long suffix doesn't help; the visible portion is enough.
- When designing a test command for the user to run, default to reading credentials from the file with a small Python `python3 -c "..."` block that uses `tomllib` + `urllib.request`. Output should only print body + status code; never the auth header.
- If a token leaks anyway: tell the user to revoke it **immediately** at `https://id.atlassian.com/manage-profile/security/api-tokens`, regenerate, update credentials file. Do not move on until they confirm.
- Two token flavors exist on Atlassian:
  - **Legacy** (no scope picker, account-wide) — only works with Basic auth (`email:token`).
  - **Scoped** (per-app, per-permission) — works as Bearer; can 404 on pages where the scope doesn't grant `read:page:confluence`. 404 from a Bearer scoped token usually means missing scope, not missing page.

## Confluence v2 API path namespace

Each Atlassian product mounts under its own URL prefix on `<tenant>.atlassian.net`. Confluence is `/wiki/...`. Jira is `/rest/api/3/...`. Don't confuse them.

Common path mistakes that cost session time:
- `/wiki/spaces/<KEY>/pages/<id>/<slug>` — this is the **browser UI URL**, served by SSR with session cookies. Basic-auth Authorization is ignored. 302 → `/login`.
- `/wiki/api/v2/pages/<id>` — this is the **API URL**. Accepts Basic auth.

`body-format=storage` query param is required to get storage XHTML in `body.storage.value`.

## Modern editor surface artifacts

Real Confluence Cloud pages today are authored in the modern editor, which produces storage XHTML containing:

- `local-id` / `ac:local-id` attribute on virtually every element (per-element UUIDs for collaborative editing anchors). **Strip on pull**, let server regenerate on push.
- `ac:macro-id` on every `<ac:structured-macro>` — same treatment.
- `ri:version-at-save="N"` on every `<ri:attachment>` ref. Preserve in sidecar.
- Empty `<p local-id="…"/>` between block elements (visual spacing).
- Code blocks default to `breakoutMode=wide`, `breakoutWidth=760` — neither is in the storage-format reference. Treat code-block params as opaque-on-attributes, not a closed enumeration.
- Tables have HTML5-style `data-table-width`, `data-layout` attributes alongside `ac:local-id`s.
- User mentions: `<ac:link><ri:user ri:account-id="..." ri:local-id="..."/></ac:link>`. Display name resolves at render time from `ri:account-id` — it is **not** in the storage.
- `<ri:url>` for "image from the web" **never produced** by the modern editor. The editor auto-downloads URL images to attachments. The plan's `<ri:url>` mapping path is dead code on modern-editor pages; keep for legacy compatibility.

When auditing element coverage, look for `<ac:structured-macro>` literally — `<ac:image>` is structurally similar but isn't a structured-macro and doesn't satisfy "macro in cell" tests.

## Plan-audit habit (per user's global CLAUDE.md)

Before declaring the plan executable, simulate a cold session reading it. In this session the audit caught two real gaps:

1. Plan called for `create_inline_comment` in `api.py` but the endpoint wasn't researched in `notes.md`. (Fixed: `POST /wiki/api/v2/inline-comments` with `inlineCommentProperties.{textSelection, textSelectionMatchCount, textSelectionMatchIndex}` is officially supported in v2.)
2. Templates referenced by tests didn't exist on disk, and the plan didn't enumerate what each template must contain. (Fixed: per-template required-elements table + B-test → element mapping committed to plan.)

Soft gaps (algorithm sketch for the hardest module, output format of `status`) were also worth filling before declaring done.

## Constraint negotiation pattern

The user iterated several times on test-suite blast radius:
1. First pass: suite freely creates/modifies/deletes.
2. "Don't modify tree structure" → suite became read-only on page CRUD; comments became pre-existing.
3. "Comments and macros are fine, just not page CRUD" → suite regained additive control over body content and comments, kept hard line at page level.

The settled rule is **three nevers**: never create, delete, or move pages. Everything else (body content, titles, inline comments — additive only) is fair game for the suite.

When the user adds a constraint, implement it minimally and surgically. Don't bundle other changes. Each refinement is its own commit-sized edit.

## Persistent decisions worth remembering

- Credentials file (TOML) at `~/.config/confluence-markdown-roundtrip/credentials.toml`, `chmod 0600`. No env vars, no CLI args carrying the token. Loaded by `api.py` at process startup.
- Per-page directory has exactly two visible entries: `index.md` (user-editable) and `_meta/` (read-only sidecar bundle). Everything else nests inside `_meta/`. Image paths in MD use `./_meta/attachments/<file>`.
- `_meta/index.md.orig` is a verbatim copy of `index.md` at pull time. Enables `diff index.md _meta/index.md.orig`. Push aborts (`orig-tampered`) if its sha256 doesn't match `sidecar.base_md_sha256`.
- HASH = `sha256(canonical_xml).hexdigest()[:12]`. Canonical XML = lxml `c14n2` with `local-id`/`ac:local-id`/`ac:macro-id` stripped.
- All test communication never includes the token. The user's own test commands should follow the same rule.
- Helpers invoked from pull need explicit coverage on **both** the single-page path (`cli.py`) and the subtree path (`subtree.py`). The 2026-05-17 attachment-download regression hid here: `_download_referenced_attachments` was only wired into the single-page path; the subtree path silently skipped it for months while A1 (single-page) stayed green. When adding any per-page pull-side helper, write at least one test that exercises it via `--subtree`.

## Phase 2/3 online findings (surfaced only by going live — not in docs)

These were learned by running the test suite against the live tenant. Each cost real debugging time; future sessions should not have to rediscover them.

1. **Inline-comment-marker `ac:ref` is `properties.inlineMarkerRef`, NOT the comment id.** The v2 endpoint `GET /pages/{id}/inline-comments` returns `{id, properties: {inlineMarkerRef, inlineOriginalSelection}, ...}`. The `id` is the Confluence content id (long integer). The `inlineMarkerRef` is the UUID that goes in `<ac:inline-comment-marker ac:ref="UUID">` in storage XHTML. They are different. Same applies to `POST /inline-comments` — read the marker_ref from response properties, not the id.

2. **`text_selection` clears when the marker is orphaned.** If a PUT removes a comment's marker from the body, the inline-comments listing returns that comment with `properties.textSelection = ''`. The original anchor text survives in `properties.inlineOriginalSelection` (read-only). Match orphaned comments by `inlineOriginalSelection` or by comment body content — not by `textSelection`.

3. **`<li><p>text</p></li>` is the modern-editor list-item shape.** Walker must unwrap a single `<p>` child of `<li>` before inline rendering, same pattern as table cells. Otherwise `<p>` falls through to the inline opaque path.

4. **Confluence emits HTML named entities (`&mdash;`, `&hellip;`, `&nbsp;`, ...) in storage XHTML.** lxml's strict XML parser rejects them. Fix: prepend a DOCTYPE with the entity table to the wrapped fragment before parsing. Numeric entities (`&#8212;`) work without help.

5. **Attachment binary download has two endpoints with different auth.** The v2 `_links.download` URL (`/wiki/download/attachments/...`) returns 401 with `WWW-Authenticate: OAuth ...` under Basic auth — this was the original finding and remains true. But the **v1 child-attachment download** endpoint accepts Basic auth and returns the bytes: `GET /wiki/rest/api/content/{page_id}/child/attachment/{attachment_id}/download`. Works with or without the `att` prefix on `attachment_id`. Use the v1 path for read-only fetch of image binaries; OAuth is only needed for the v2 path or for uploads.

6. **Confluence has a small lag between PUT and version surfacing on GET.** notes.md §3 mentioned this; in practice it manifests as the test suite's `restore()` PUT racing against a prior test's PUT. Mitigation: retry the restore PUT once on 409 with a short sleep. Production push pipeline does NOT need retry — its conflict is genuine.

7. **A bootstrap matching comments by anchor text breaks across runs.** Match by **comment body content** instead; the body is per-spec unique and persists across PUTs even when markers come and go.

8. **Pytest collection order is not stable across files.** Tests that share mutable state (the live test pages) must call `restore("logical_name")` before any mutation. Don't rely on test ordering; assume the previous test left junk.
