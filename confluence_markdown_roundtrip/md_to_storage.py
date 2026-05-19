"""Markdown -> Confluence storage XHTML.

Inverse of `storage_to_md`. Reads:
- `index.md` text
- The page's sidecar (so opaque blocks/inlines can be reinjected by hash/UUID)

Emits:
- (title, storage_xhtml) tuple

The serializer is a markdown-it-py token walker. Validation runs as a
pre-pass; any failure raises a `PushAbort` with the rule-id specified
in plan §"Push abort format" — no partial output is produced.
"""

from __future__ import annotations

import re
from typing import Any
from xml.sax.saxutils import escape as xml_escape, quoteattr as xml_quoteattr

from markdown_it import MarkdownIt
from markdown_it.token import Token

from . import sentinels as S


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def md_to_storage(md_text: str, sidecar: dict[str, Any], *, file_path: str | None = None) -> tuple[str, str]:
    """Convert MD + sidecar to (title, storage_xhtml).

    Raises `sentinels.PushAbort` on any validation failure — never emits a
    partial body.
    """
    title, body_md = _extract_title(md_text, file_path)
    body_md = _strip_panel_alert_wrappers(body_md)
    md = _build_parser()
    tokens = md.parse(body_md)
    _validate(tokens, sidecar, file_path)

    emitter = _Emitter(tokens, sidecar)
    emitter.run()
    return title, "".join(emitter.out)


def _build_parser() -> MarkdownIt:
    # `html=True` keeps HTML comments + spans flowing through as tokens.
    # `tables` is needed for GFM tables (plan §"Tables").
    # We deliberately leave `tasklists` disabled — task markers in this skill
    # use `<!--ct:UUID-->` not GFM `[ ]`.
    return MarkdownIt("commonmark", {"html": True}).enable("table")


# ---------------------------------------------------------------------------
# Title extraction (plan §"Title handling")
# ---------------------------------------------------------------------------


_H1_RE = re.compile(r"^#\s+(.+?)\s*$")


# Matches the full text of one cp-wrapped panel, regardless of whether the
# opener carries the legacy `style=` attribute. Groups: (opener, uuid, body, closer).
_CP_RANGE_RE = re.compile(
    r"(<!--cp:([0-9a-fA-F\-]{36})(?:\s+style=[a-z]+)?-->)(.*?)(<!--/cp:\2-->)",
    re.DOTALL,
)


def _strip_panel_alert_wrappers(body_md: str) -> str:
    """Pre-pass over `body_md`: for every cp-wrapped panel, if its body is
    a uniformly `>`-prefixed blockquote (the Phase 6 GFM-alert form),
    peel one layer of `>` from every line and drop a leading `[!KIND]`
    line. Phase 4 bodies (no `>` prefix) pass through untouched."""
    def repl(m: re.Match) -> str:
        opener, _uuid, content, closer = m.group(1), m.group(2), m.group(3), m.group(4)
        return opener + _peel_gfm_alert(content) + closer
    return _CP_RANGE_RE.sub(repl, body_md)


def _peel_gfm_alert(content: str) -> str:
    """Strip one layer of `>` blockquote prefix from `content` iff every
    non-blank line is `>`-prefixed. Drop a leading `[!KIND]` line if present
    after the strip. Otherwise return `content` unchanged."""
    lines = content.split("\n")
    non_blank = [ln for ln in lines if ln.strip()]
    if not non_blank:
        return content
    if not all(ln.lstrip().startswith(">") for ln in non_blank):
        return content
    peeled: list[str] = []
    for ln in lines:
        if not ln.strip():
            peeled.append(ln)
            continue
        stripped = ln.lstrip()
        if stripped.startswith("> "):
            peeled.append(stripped[2:])
        elif stripped == ">":
            peeled.append("")
        else:  # starts with ">" but no space — rare; strip just the marker
            peeled.append(stripped[1:])
    first_idx = next((i for i, ln in enumerate(peeled) if ln.strip()), None)
    if first_idx is not None and S.GFM_ALERT_OPENER_RE.match(peeled[first_idx]):
        peeled.pop(first_idx)
    return "\n".join(peeled)


def _extract_title(md_text: str, file_path: str | None) -> tuple[str, str]:
    """Strip the leading `# Title` H1 from `md_text`. Returns (title, body_md).
    Raises `missing-h1` if the first non-blank line isn't an H1."""
    lines = md_text.splitlines()
    idx = 0
    while idx < len(lines) and lines[idx].strip() == "":
        idx += 1
    if idx >= len(lines):
        raise S.missing_h1(file=file_path)
    m = _H1_RE.match(lines[idx])
    if not m:
        raise S.missing_h1(file=file_path)
    title = m.group(1)
    body = "\n".join(lines[idx + 1 :]).lstrip("\n")
    return title, body


# ---------------------------------------------------------------------------
# Validation pre-pass
# ---------------------------------------------------------------------------


def _validate(tokens: list[Token], sidecar: dict[str, Any], file_path: str | None) -> None:
    """Walk the token stream once, raising on the first marker violation.

    Checks:
    - Every `<!--cm:UUID-->` has a matching close with the same UUID.
    - Every `<!--cb:HASH-->` references `sidecar.blocks[HASH]`.
    - Every `<span data-ci="HASH">` references `sidecar.inline_blocks[HASH]`.
    - All marker UUIDs/HASHes parse cleanly.
    - First non-blank line is `# H1` (already enforced by `_extract_title`).
    """
    blocks = sidecar.get("blocks", {})
    inline_blocks = sidecar.get("inline_blocks", {})
    panels = sidecar.get("panels", {})
    links = sidecar.get("links", {})

    cm_stack: list[tuple[str, int]] = []  # (uuid, line)
    cp_stack: list[tuple[str, int]] = []  # (uuid, line)

    def line_of(tok: Token) -> int:
        return (tok.map[0] + 1) if tok.map else 0

    def walk(toks: list[Token]) -> None:
        for tok in toks:
            if tok.type in ("html_inline", "html_block"):
                _check_marker(
                    tok, cm_stack, cp_stack, blocks, inline_blocks, panels, links,
                    file_path, line_of(tok),
                )
            if tok.children:
                walk(tok.children)

    walk(tokens)

    if cm_stack:
        uuid, line = cm_stack[0]
        raise S.unmatched_cm(f"cm:{uuid} open with no matching close", file=file_path, line=line)
    if cp_stack:
        uuid, line = cp_stack[0]
        raise S.unmatched_cp(f"cp:{uuid} open with no matching close", file=file_path, line=line)


def _check_marker(
    tok: Token,
    cm_stack: list[tuple[str, int]],
    cp_stack: list[tuple[str, int]],
    blocks: dict[str, Any],
    inline_blocks: dict[str, Any],
    panels: dict[str, Any],
    links: dict[str, Any],
    file_path: str | None,
    line: int,
) -> None:
    content = tok.content
    # First: catch malformed marker syntax. If anything looks like a marker
    # but doesn't parse, fail early with bad-marker-syntax.
    if "<!--" in content and any(k in content for k in ("cm:", "cb:", "cc:", "ct:", "ci:", "cp:", "cl:")):
        any_match = S.ANY_MARKER_PREFIX_RE.search(content)
        if any_match:
            # If any of the strict regexes match for this content, we're good.
            if not (
                S.CM_OPEN_RE.search(content)
                or S.CM_CLOSE_RE.search(content)
                or S.CB_RE.search(content)
                or S.CC_RE.search(content)
                or S.CT_RE.search(content)
                or S.CI_TRAILER_RE.search(content)
                or S.CP_OPEN_RE.search(content)
                or S.CP_CLOSE_RE.search(content)
                or S.CL_RE.search(content)
            ):
                raise S.bad_marker_syntax(
                    f"malformed marker: {any_match.group(0)}", file=file_path, line=line
                )

    # CM open
    for m in S.CM_OPEN_RE.finditer(content):
        cm_stack.append((m.group(1), line))
    # CM close
    for m in S.CM_CLOSE_RE.finditer(content):
        uuid = m.group(1)
        if not cm_stack or cm_stack[-1][0] != uuid:
            raise S.unmatched_cm(
                f"cm:{uuid} close without matching open (or mismatched UUID)",
                file=file_path,
                line=line,
            )
        cm_stack.pop()
    # CP open
    for m in S.CP_OPEN_RE.finditer(content):
        uuid = m.group(1)
        if uuid not in panels:
            raise S.unknown_cp_uuid(uuid, file=file_path, line=line)
        cp_stack.append((uuid, line))
    # CP close
    for m in S.CP_CLOSE_RE.finditer(content):
        uuid = m.group(1)
        if not cp_stack or cp_stack[-1][0] != uuid:
            raise S.unmatched_cp(
                f"cp:{uuid} close without matching open (or mismatched UUID)",
                file=file_path,
                line=line,
            )
        cp_stack.pop()
    # CB block
    for m in S.CB_RE.finditer(content):
        h = m.group(1)
        if h not in blocks:
            raise S.unknown_cb_hash(h, file=file_path, line=line)
    # Span data-ci
    for m in S.CI_SPAN_RE.finditer(content):
        h = m.group(1)
        if h not in inline_blocks:
            raise S.unknown_ci_hash(h, file=file_path, line=line)
    # cl: trailer
    for m in S.CL_RE.finditer(content):
        h = m.group(1)
        if h not in links:
            raise S.unknown_cl_hash(h, file=file_path, line=line)


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------

_LABEL_RE = re.compile(r"^\s*\[confluence:\s*[^\]]+\]\s*$")


class _Emitter:
    def __init__(self, tokens: list[Token], sidecar: dict[str, Any]):
        self.tokens = tokens
        self.sidecar = sidecar
        self.i = 0
        self.out: list[str] = []

    # --- public driver ----------------------------------------------------

    def run(self) -> None:
        while self.i < len(self.tokens):
            self._emit_one()

    # --- block-level dispatch --------------------------------------------

    def _emit_one(self) -> None:
        t = self.tokens[self.i]
        ty = t.type
        if ty == "heading_open":
            self._emit_heading()
        elif ty == "paragraph_open":
            self._emit_paragraph()
        elif ty == "bullet_list_open":
            self._emit_list(ordered=False)
        elif ty == "ordered_list_open":
            self._emit_list(ordered=True)
        elif ty == "blockquote_open":
            self._emit_blockquote()
        elif ty == "fence":
            self._emit_fence(t)
            self.i += 1
        elif ty == "html_block":
            content = (t.content or "").rstrip("\n")
            m = S.CP_OPEN_RE.search(content)
            if m:
                self._emit_panel(m.group(1))
            else:
                self._emit_html_block(t)
                self.i += 1
        elif ty == "table_open":
            self._emit_table()
        elif ty == "hr":
            self.out.append("<hr/>")
            self.i += 1
        else:
            # safety net — skip unrecognized token
            self.i += 1

    # --- heading ---------------------------------------------------------

    def _emit_heading(self) -> None:
        # All body H1s have already been pulled out as title. Treat H1 here
        # as H2 to match the pull-side downgrade in storage_to_md.
        t = self.tokens[self.i]
        tag = t.tag
        if tag == "h1":
            tag = "h2"
        self.i += 1  # past _open
        inline = self.tokens[self.i]
        self.i += 1
        # past _close
        self.i += 1
        self.out.append(f"<{tag}>{self._inline(inline.children or [])}</{tag}>")

    # --- paragraph -------------------------------------------------------

    def _emit_paragraph(self) -> None:
        self.i += 1  # past _open
        inline = self.tokens[self.i]
        self.i += 1
        self.i += 1  # past _close
        rendered = self._inline(inline.children or [])
        if not rendered.strip():
            return  # collapse empty paragraphs
        self.out.append(f"<p>{rendered}</p>")

    # --- list ------------------------------------------------------------

    def _emit_list(self, *, ordered: bool) -> None:
        # Special case: a task list. We detect it by inspecting list items
        # for ct: markers. If every list_item contains a ct marker, treat as
        # a task-list; otherwise normal <ul>/<ol>.
        # NOTE: markdown-it uses `bullet_list_*` / `ordered_list_*` token
        # types, not `ul_*` / `ol_*` — keep this distinct from the HTML tag.
        list_prefix = "ordered_list" if ordered else "bullet_list"
        tag = "ol" if ordered else "ul"
        close_type = f"{list_prefix}_close"
        self.i += 1  # past list_open
        items: list[list[Token]] = []
        while self.tokens[self.i].type != close_type:
            if self.tokens[self.i].type == "list_item_open":
                self.i += 1
                inner: list[Token] = []
                while self.tokens[self.i].type != "list_item_close":
                    inner.append(self.tokens[self.i])
                    self.i += 1
                self.i += 1  # past list_item_close
                items.append(inner)
            else:
                self.i += 1
        self.i += 1  # past list_close

        if not ordered and all(_item_is_task(it) for it in items):
            self._emit_task_list(items)
            return

        self.out.append(f"<{tag}>")
        for inner in items:
            self.out.append("<li>")
            sub = _Emitter(inner, self.sidecar)
            sub.run()
            self.out.append("".join(sub.out))
            self.out.append("</li>")
        self.out.append(f"</{tag}>")

    # --- task list -------------------------------------------------------

    def _emit_task_list(self, items: list[list[Token]]) -> None:
        # Use the first task's task_list_id if available.
        first_uuid = _task_uuid_of(items[0])
        list_id = ""
        if first_uuid:
            list_id = self.sidecar.get("tasks", {}).get(first_uuid, {}).get("task_list_id", "")

        if list_id:
            self.out.append(f'<ac:task-list ac:task-list-id="{xml_escape(list_id)}">')
        else:
            self.out.append("<ac:task-list>")
        for inner in items:
            self._emit_task(inner)
        self.out.append("</ac:task-list>")

    def _emit_task(self, item_tokens: list[Token]) -> None:
        uuid = _task_uuid_of(item_tokens)
        body_text = _task_body_text(item_tokens)
        meta = self.sidecar.get("tasks", {}).get(uuid or "", {})
        status = meta.get("status", "incomplete")
        task_id = meta.get("task_id", "")
        self.out.append("<ac:task>")
        if task_id:
            self.out.append(f"<ac:task-id>{xml_escape(task_id)}</ac:task-id>")
        if uuid:
            self.out.append(f"<ac:task-uuid>{xml_escape(uuid)}</ac:task-uuid>")
        self.out.append(f"<ac:task-status>{xml_escape(status)}</ac:task-status>")
        self.out.append(f"<ac:task-body>{body_text}</ac:task-body>")
        self.out.append("</ac:task>")

    # --- blockquote ------------------------------------------------------

    def _emit_blockquote(self) -> None:
        # Detect the "label" form: a blockquote containing exactly one
        # paragraph whose content matches `[confluence: <kind>]`. That
        # blockquote is cosmetic — the next cb: marker is the source of
        # truth. Skip emission entirely.
        if self._is_label_blockquote():
            self._skip_blockquote()
            return
        self.i += 1  # past blockquote_open
        self.out.append("<blockquote>")
        while self.tokens[self.i].type != "blockquote_close":
            self._emit_one()
        self.i += 1  # past blockquote_close
        self.out.append("</blockquote>")

    def _is_label_blockquote(self) -> bool:
        # Tokens following `blockquote_open`: paragraph_open, inline, paragraph_close, blockquote_close
        j = self.i + 1
        if j + 3 >= len(self.tokens):
            return False
        if self.tokens[j].type != "paragraph_open":
            return False
        inline = self.tokens[j + 1]
        if inline.type != "inline":
            return False
        if self.tokens[j + 2].type != "paragraph_close":
            return False
        if self.tokens[j + 3].type != "blockquote_close":
            return False
        return bool(_LABEL_RE.match(inline.content or ""))

    def _skip_blockquote(self) -> None:
        # Advance past blockquote_open ... blockquote_close
        depth = 0
        while self.i < len(self.tokens):
            t = self.tokens[self.i]
            if t.type == "blockquote_open":
                depth += 1
            elif t.type == "blockquote_close":
                depth -= 1
                self.i += 1
                if depth == 0:
                    return
                continue
            self.i += 1

    # --- fenced code -----------------------------------------------------

    def _emit_fence(self, t: Token) -> None:
        # Check the next token for a cc: trailer (it lives on its own line
        # after the closing fence and parses as `html_block`).
        uuid: str | None = None
        if self.i + 1 < len(self.tokens):
            nxt = self.tokens[self.i + 1]
            if nxt.type == "html_block":
                m = S.CC_RE.search(nxt.content or "")
                if m:
                    uuid = m.group(1)
                    # consume the trailer
                    self.tokens.pop(self.i + 1)
        language = (t.info or "").strip()
        params: dict[str, str] = {}
        if uuid:
            entry = self.sidecar.get("code_blocks", {}).get(uuid)
            if entry:
                params = entry.get("params", {})
        self.out.append('<ac:structured-macro ac:name="code">')
        if language:
            self.out.append(f'<ac:parameter ac:name="language">{xml_escape(language)}</ac:parameter>')
        for k, v in params.items():
            self.out.append(f'<ac:parameter ac:name="{xml_escape(k)}">{xml_escape(v)}</ac:parameter>')
        body = t.content or ""
        # Strip the single trailing newline that markdown-it appends to fence bodies.
        if body.endswith("\n"):
            body = body[:-1]
        self.out.append(f"<ac:plain-text-body><![CDATA[{body}]]></ac:plain-text-body>")
        self.out.append("</ac:structured-macro>")

    # --- editable panel --------------------------------------------------

    def _emit_panel(self, uuid: str) -> None:
        """Collect tokens between matching cp markers, sub-emit them as the
        panel body XHTML, then wrap in the storage shape recorded in
        `sidecar.panels[uuid]` (legacy macro or modern adf-extension)."""
        self.i += 1  # past cp open html_block
        body_tokens: list[Token] = []
        while self.i < len(self.tokens):
            t = self.tokens[self.i]
            if t.type == "html_block":
                content = (t.content or "").rstrip("\n")
                m = S.CP_CLOSE_RE.search(content)
                if m and m.group(1) == uuid:
                    self.i += 1  # past close
                    break
            body_tokens.append(t)
            self.i += 1

        sub = _Emitter(body_tokens, self.sidecar)
        sub.run()
        body_xhtml = "".join(sub.out)

        entry = self.sidecar.get("panels", {}).get(uuid)
        if entry is None:
            # Validation pre-pass should have caught this; defensive raise.
            raise S.unknown_cp_uuid(uuid)

        shape = entry.get("shape")
        name = entry.get("name", "")
        if shape == "macro":
            parts = [f'<ac:structured-macro ac:name="{xml_escape(name)}" ac:schema-version="1">']
            for k, v in entry.get("params", {}).items():
                parts.append(
                    f'<ac:parameter ac:name="{xml_escape(k)}">{xml_escape(v)}</ac:parameter>'
                )
            parts.append(f"<ac:rich-text-body>{body_xhtml}</ac:rich-text-body>")
            parts.append("</ac:structured-macro>")
            self.out.append("".join(parts))
        elif shape == "adf":
            parts = ['<ac:adf-extension><ac:adf-node type="panel">']
            parts.append(
                f'<ac:adf-attribute key="panel-type">{xml_escape(name)}</ac:adf-attribute>'
            )
            for k, v in entry.get("adf_attrs", {}).items():
                parts.append(
                    f'<ac:adf-attribute key="{xml_escape(k)}">{xml_escape(v)}</ac:adf-attribute>'
                )
            parts.append(f"<ac:adf-content>{body_xhtml}</ac:adf-content>")
            parts.append("</ac:adf-node>")
            fallback = entry.get("adf_fallback", "")
            if fallback:
                parts.append(f"<ac:adf-fallback>{fallback}</ac:adf-fallback>")
            parts.append("</ac:adf-extension>")
            self.out.append("".join(parts))
        else:
            raise S.PushAbort("unknown-cp-shape", f"sidecar.panels[{uuid}].shape={shape!r} is unrecognized")

    # --- html_block (cb / cc / ct dispatch) ------------------------------

    def _emit_html_block(self, t: Token) -> None:
        content = (t.content or "").rstrip("\n")
        m = S.CB_RE.search(content)
        if m:
            h = m.group(1)
            xml = self.sidecar["blocks"][h]["xml"]
            self.out.append(_clean_sidecar_xml(xml))
            return
        # cc: should have been consumed by the fence emitter; if it appears
        # standalone, drop it (no fence to attach to).
        if S.CC_RE.search(content):
            return
        # ct: a stray task marker outside a list. Should not happen — drop.
        if S.CT_RE.search(content):
            return
        # other raw HTML pass through verbatim
        self.out.append(content)

    # --- table -----------------------------------------------------------

    def _emit_table(self) -> None:
        # Walk: table_open, thead_open, tr_open, (th_open, inline, th_close)+, tr_close, thead_close,
        #       tbody_open, (tr_open, (td_open, inline, td_close)+, tr_close)*, tbody_close, table_close
        self.i += 1  # past table_open
        self.out.append("<table><tbody>")
        # markdown-it emits thead/tbody; we collapse them into <tbody> for
        # plan-storage parity (modern editor doesn't use <thead>).
        while self.tokens[self.i].type != "table_close":
            t = self.tokens[self.i]
            if t.type in ("thead_open", "thead_close", "tbody_open", "tbody_close"):
                self.i += 1
            elif t.type == "tr_open":
                self.i += 1
                self.out.append("<tr>")
                while self.tokens[self.i].type != "tr_close":
                    cell_t = self.tokens[self.i]
                    cell_tag = "th" if cell_t.type == "th_open" else "td"
                    self.i += 1  # past td/th_open
                    inline = self.tokens[self.i]
                    self.i += 1
                    self.i += 1  # past td/th_close
                    rendered = self._inline(inline.children or [])
                    self.out.append(f"<{cell_tag}><p>{rendered}</p></{cell_tag}>")
                self.i += 1  # past tr_close
                self.out.append("</tr>")
            else:
                self.i += 1
        self.i += 1  # past table_close
        self.out.append("</tbody></table>")

    # --- inline rendering -----------------------------------------------

    def _inline(self, toks: list[Token]) -> str:
        out: list[str] = []
        # We need to track cm marker state across html_inline tokens.
        i = 0
        while i < len(toks):
            t = toks[i]
            ty = t.type
            if ty == "text":
                out.append(xml_escape(t.content))
            elif ty == "softbreak":
                out.append(" ")
            elif ty == "hardbreak":
                out.append("<br/>")
            elif ty == "code_inline":
                out.append(f"<code>{xml_escape(t.content)}</code>")
            elif ty == "strong_open":
                out.append("<strong>")
            elif ty == "strong_close":
                out.append("</strong>")
            elif ty == "em_open":
                out.append("<em>")
            elif ty == "em_close":
                out.append("</em>")
            elif ty == "s_open":
                out.append("<s>")
            elif ty == "s_close":
                out.append("</s>")
            elif ty == "link_open":
                emitted, advance = self._inline_link(t, toks, i, out)
                if advance > 1:
                    # cl-link consumed the whole link_open..trailer span;
                    # del the in-between tokens so `i += 1` lands past the link.
                    del toks[i + 1 : i + advance]
                # `out` already updated by _inline_link (it may have appended).
                # Falls through to `i += 1`.
            elif ty == "link_close":
                out.append("</a>")
            elif ty == "image":
                out.append(self._inline_image(t, toks, i))
            elif ty == "html_inline":
                out.append(self._inline_html(t.content or "", toks, i, out))
            else:
                # ignore unknown
                pass
            i += 1
        return "".join(out)

    def _inline_link(
        self, t: Token, toks: list[Token], i: int, out: list[str]
    ) -> tuple[str, int]:
        """Handle a link_open. Returns (unused_str, tokens_to_consume).

        If a `<!--cl:HASH-->` trailer follows the matching link_close, emit
        the sidecar's stored XML verbatim and return advance = span-length
        (link_open through trailer, inclusive) so the caller can splice
        them out. Otherwise emit the normal `<a href=...>` open tag and
        return advance = 1.
        """
        # Locate matching link_close.
        j = i + 1
        while j < len(toks) and toks[j].type != "link_close":
            j += 1
        if j >= len(toks):
            # Malformed — fall back to default behavior.
            href = t.attrGet("href") or ""
            out.append(f"<a href={xml_quoteattr(href)}>")
            return "", 1

        # Look for cl: trailer immediately after link_close.
        trailer_idx = j + 1
        if trailer_idx < len(toks) and toks[trailer_idx].type == "html_inline":
            m = S.CL_RE.match((toks[trailer_idx].content or "").strip())
            if m:
                hash_ = m.group(1)
                entry = self.sidecar.get("links", {}).get(hash_)
                if entry is None:
                    raise S.unknown_cl_hash(hash_)
                out.append(entry["xml"])
                # Span: link_open(i)..trailer(trailer_idx) inclusive.
                return "", trailer_idx - i + 1

        # No cl trailer — abort if the path looks workspace-relative (a
        # tell-tale sign of an orphaned local link the user forgot to
        # delete fully). Plain external URLs pass through unchanged.
        href = t.attrGet("href") or ""
        if _looks_like_local_workspace_path(href):
            raise S.bad_marker_syntax(
                f"local-style link `{href}` is missing its <!--cl:HASH--> trailer; "
                "restore the trailer or replace the link with the original Confluence URL"
            )
        out.append(f"<a href={xml_quoteattr(href)}>")
        return "", 1

    def _inline_image(self, t: Token, toks: list[Token], i: int) -> str:
        src = t.attrGet("src") or ""
        alt = t.content or ""
        # peek next token for a ci: trailer
        hash_: str | None = None
        if i + 1 < len(toks):
            nxt = toks[i + 1]
            if nxt.type == "html_inline":
                m = S.CI_TRAILER_RE.match((nxt.content or "").strip())
                if m:
                    hash_ = m.group(1)
                    # remove the trailer from the stream so it's not re-emitted
                    toks.pop(i + 1)

        if src.startswith("./_meta/attachments/") and hash_:
            entry = self.sidecar.get("images", {}).get(hash_)
            if entry is None:
                raise S.unknown_ci_hash(hash_)
            return self._reemit_image(alt, entry)

        if src.startswith("./_meta/attachments/"):
            # New attachment — not supported in v1
            raise S.new_attachment(src)

        # external URL image (legacy / classic-editor compatibility)
        return f'<ac:image><ri:url ri:value={xml_quoteattr(src)}/></ac:image>'

    def _reemit_image(self, alt: str, entry: dict[str, Any]) -> str:
        ac_attrs = dict(entry.get("ac_attrs", {}))
        if alt:
            ac_attrs[f"{{http://atlassian.com/content}}alt"] = alt
        ri_attrs = entry.get("ri_attrs", {})
        ac_str = " ".join(
            f"{_prefix_name(k)}={xml_quoteattr(str(v))}" for k, v in ac_attrs.items()
        )
        ri_str = " ".join(
            f"{_prefix_name(k)}={xml_quoteattr(str(v))}" for k, v in ri_attrs.items()
        )
        ac_open = f"<ac:image{(' ' + ac_str) if ac_str else ''}>"
        ri_self = f"<ri:attachment{(' ' + ri_str) if ri_str else ''}/>"
        return f"{ac_open}{ri_self}</ac:image>"

    def _inline_html(self, content: str, toks: list[Token], i: int, out: list[str]) -> str:
        # cm open / close
        m = S.CM_OPEN_RE.fullmatch(content.strip())
        if m:
            return f'<ac:inline-comment-marker ac:ref={xml_quoteattr(m.group(1))}>'
        m = S.CM_CLOSE_RE.fullmatch(content.strip())
        if m:
            return "</ac:inline-comment-marker>"

        # <span data-ci="HASH"> — beginning of an inline opaque
        m = re.fullmatch(r'<span\s+data-ci="([0-9a-f]{12})">', content.strip())
        if m:
            h = m.group(1)
            entry = self.sidecar.get("inline_blocks", {}).get(h)
            if entry is None:
                raise S.unknown_ci_hash(h)
            # consume tokens up to the matching </span>; the cosmetic visible
            # text inside is ignored — sidecar XML is source of truth.
            j = i + 1
            while j < len(toks):
                tj = toks[j]
                if tj.type == "html_inline" and (tj.content or "").strip() == "</span>":
                    break
                j += 1
            # null out the consumed tokens so the outer loop skips them
            for k in range(i + 1, j + 1):
                toks[k].type = "text"
                toks[k].content = ""
            return _clean_sidecar_xml(entry["xml"])

        # standalone </span> — should have been consumed above. If it slips
        # through, ignore it.
        if content.strip() == "</span>":
            return ""

        # ci: trailer hit here means it wasn't attached to an image — drop
        if S.CI_TRAILER_RE.fullmatch(content.strip()):
            return ""

        # unrecognized inline HTML — pass through verbatim (the user pasted
        # raw XHTML, which is rare but supported)
        return content


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Strip the leading `xmlns:ac="..."` / `xmlns:ri="..."` attributes that
# C14N2 stamps onto every root element. Confluence's storage format expects
# these namespaces to be implicit at the body level.
_XMLNS_RE = re.compile(r"\s+xmlns:(ac|ri)=\"[^\"]+\"")


def _clean_sidecar_xml(xml: str) -> str:
    """Remove the xmlns:ac/xmlns:ri attributes that c14n2 always emits.
    Result is what Confluence's storage parser expects: a fragment that
    inherits namespaces from the document scope."""
    return _XMLNS_RE.sub("", xml)


def _prefix_name(qname: str) -> str:
    """`{http://atlassian.com/content}name` -> `ac:name` etc. for serialization."""
    if qname.startswith("{http://atlassian.com/content}"):
        return "ac:" + qname.split("}", 1)[1]
    if qname.startswith("{http://atlassian.com/resource/identifier}"):
        return "ri:" + qname.split("}", 1)[1]
    return qname


def _item_is_task(item_tokens: list[Token]) -> bool:
    """A list_item is a task if its first html_block/html_inline content
    contains a ct: marker."""
    for t in item_tokens:
        if t.type in ("html_block", "html_inline"):
            if S.CT_RE.search(t.content or ""):
                return True
        if t.children:
            if _item_is_task(t.children):
                return True
    return False


def _task_uuid_of(item_tokens: list[Token]) -> str | None:
    for t in item_tokens:
        if t.type in ("html_block", "html_inline"):
            m = S.CT_RE.search(t.content or "")
            if m:
                return m.group(1)
        if t.children:
            u = _task_uuid_of(t.children)
            if u:
                return u
    return None


def _task_body_text(item_tokens: list[Token]) -> str:
    """Extract the task body text — everything after the ct: marker on its
    line. Used to fill `<ac:task-body>`."""
    for t in item_tokens:
        if t.type == "html_block":
            content = (t.content or "").rstrip("\n")
            # ct marker is at the start; what follows is the body
            m = S.CT_RE.search(content)
            if m:
                tail = content[m.end():].lstrip()
                return xml_escape(tail)
        # also handle the case where ct: was inline inside a paragraph
        if t.type == "inline" and t.children:
            return xml_escape(_strip_ct_marker(t.content or ""))
    return ""


def _strip_ct_marker(s: str) -> str:
    m = S.CT_RE.search(s)
    if not m:
        return s.strip()
    return s[m.end():].strip()


def _looks_like_local_workspace_path(href: str) -> bool:
    """True for paths that look like cross-page local refs in this skill's
    workspace layout: no URL scheme, no leading `#`, and the path either is or
    contains `index.md`. The orphan-cl detector uses this to fail loudly when
    a user deletes the `cl:` trailer but leaves the local-style path in place.
    """
    if not href:
        return False
    if href.startswith(("http://", "https://", "mailto:", "tel:", "#", "javascript:")):
        return False
    return "index.md" in href
