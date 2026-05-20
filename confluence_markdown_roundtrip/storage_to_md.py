"""Confluence storage XHTML -> Markdown + sidecar.

Reads a storage-format body and a page title; emits an `index.md` string
and a sidecar metadata dict. The dispatch table is small (plan §3:
"medium scope"); anything outside the whitelist round-trips opaquely.

See plan §"Storage -> MD mapping" for the conversion rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.entities import name2codepoint
from typing import Any

from lxml import etree

from . import sentinels as S


# ---------------------------------------------------------------------------
# Namespaces + tag helpers
# ---------------------------------------------------------------------------

ACNS = "http://atlassian.com/content"
RINS = "http://atlassian.com/resource/identifier"
NS_MAP = {"ac": ACNS, "ri": RINS}

# Editable-panel type sets. Legacy macro panels and ADF panels overlap on
# {info, note, warning}; macro adds `tip`; adf adds `success`, `error`.
_MACRO_PANEL_TYPES = frozenset({"info", "note", "warning", "tip"})
_ADF_PANEL_TYPES = frozenset({"info", "note", "warning", "success", "error"})


def _ac(name: str) -> str:
    return f"{{{ACNS}}}{name}"


def _ri(name: str) -> str:
    return f"{{{RINS}}}{name}"


def _local(el: etree._Element) -> str:
    """Return the localname of an element regardless of namespace."""
    if not isinstance(el.tag, str):
        return ""
    return etree.QName(el).localname


def _ns(el: etree._Element) -> str:
    if not isinstance(el.tag, str):
        return ""
    return etree.QName(el).namespace or ""


# ---------------------------------------------------------------------------
# Sidecar
# ---------------------------------------------------------------------------


@dataclass
class Sidecar:
    """In-memory view of `_meta/index.conf.json`. The on-disk JSON shape is
    plan §"Sidecar schemas"; this dataclass is the writer's working set."""

    title: str = ""
    blocks: dict[str, dict[str, Any]] = field(default_factory=dict)
    inline_blocks: dict[str, dict[str, Any]] = field(default_factory=dict)
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    code_blocks: dict[str, dict[str, Any]] = field(default_factory=dict)
    images: dict[str, dict[str, Any]] = field(default_factory=dict)
    panels: dict[str, dict[str, Any]] = field(default_factory=dict)
    links: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "blocks": self.blocks,
            "inline_blocks": self.inline_blocks,
            "tasks": self.tasks,
            "code_blocks": self.code_blocks,
            "images": self.images,
            "panels": self.panels,
            "links": self.links,
        }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def storage_to_md(
    storage_xml: str,
    title: str,
    *,
    pid_to_relpath: dict[str, str] | None = None,
    title_to_pid: dict[tuple[str, str], str] | None = None,
    self_page_relpath: str | None = None,
) -> tuple[str, Sidecar]:
    """Convert a Confluence storage XHTML fragment + title to MD + sidecar.

    `storage_xml` is the raw `body.storage.value` from the v2 API. It is a
    fragment (no enclosing element, no xmlns decls); we wrap it before parsing.

    Subtree pulls pass `pid_to_relpath` / `title_to_pid` / `self_page_relpath`
    so that in-tree page links rewrite to relative paths with a `cl:` trailer.
    Single-page pulls omit those args (the link-rewrite path is a no-op).
    """
    root = _parse_fragment(storage_xml)
    sidecar = Sidecar(title=title)
    w = _Walker(
        sidecar,
        pid_to_relpath=pid_to_relpath or {},
        title_to_pid=title_to_pid or {},
        self_page_relpath=self_page_relpath or "",
    )

    lines: list[str] = [f"# {title}", ""]
    for node in root:
        out = w.block(node)
        if out:
            lines.extend(out)
            lines.append("")  # blank line between blocks

    # Trim trailing blanks, ensure single trailing newline.
    while lines and lines[-1] == "":
        lines.pop()
    md = "\n".join(lines) + "\n"
    return md, sidecar


def _parse_fragment(xhtml: str) -> etree._Element:
    """Wrap with namespace declarations + a synthetic root so lxml can parse
    fragments that reference ac:/ri: namespaces without declaring them.

    Confluence emits HTML named entities (&mdash;, &nbsp;, &hellip;, ...)
    in storage XHTML; we declare them in a DOCTYPE so the strict XML parser
    accepts them. The list below is the closed set of HTML5 named entities;
    practically only a handful appear in real pages but covering all is
    cheap insurance.
    """
    wrapped = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<!DOCTYPE root [{_HTML_ENTITY_DECLS}]>\n"
        f'<root xmlns:ac="{ACNS}" xmlns:ri="{RINS}">{xhtml}</root>'
    )
    parser = etree.XMLParser(strip_cdata=False, resolve_entities=True)
    return etree.fromstring(wrapped.encode("utf-8"), parser=parser)


def _build_html_entity_decls() -> str:
    """Build a string of `<!ENTITY name "&#N;">` declarations for the
    HTML4 named entities Confluence may emit. Numeric entities work
    out of the box; only named ones need DOCTYPE declarations."""
    # XML's predefined entities (amp, lt, gt, apos, quot) MUST NOT be
    # redeclared — they're built-in.
    _XML_BUILTINS = {"amp", "lt", "gt", "apos", "quot"}
    table = {n: c for n, c in name2codepoint.items() if n not in _XML_BUILTINS}
    return "".join(f'<!ENTITY {name} "&#{code};">' for name, code in table.items())


_HTML_ENTITY_DECLS = _build_html_entity_decls()


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------


class _Walker:
    """Holds the sidecar reference. Methods return lists of MD lines."""

    def __init__(
        self,
        sidecar: Sidecar,
        *,
        pid_to_relpath: dict[str, str] | None = None,
        title_to_pid: dict[tuple[str, str], str] | None = None,
        self_page_relpath: str = "",
    ):
        self.sidecar = sidecar
        self.pid_to_relpath = pid_to_relpath or {}
        self.title_to_pid = title_to_pid or {}
        self.self_page_relpath = self_page_relpath

    # ------------- block-level dispatch ----------------------------------

    def block(self, el: etree._Element) -> list[str]:
        if not isinstance(el.tag, str):
            return []  # comments / PIs — drop
        tag = _local(el)
        ns = _ns(el)

        # HTML5 block tags
        if ns == "":
            if tag == "p":
                return self._para(el)
            if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                return self._heading(el, tag)
            if tag == "ul":
                return self._list(el, ordered=False)
            if tag == "ol":
                return self._list(el, ordered=True)
            if tag == "blockquote":
                return self._blockquote(el)
            if tag == "table":
                return self._table(el)
            if tag == "hr":
                return ["---"]
            # any other HTML element block-level -> opaque
            return self._opaque_block(el)

        # ac:* / ri:* block constructs
        if ns == ACNS:
            if tag == "structured-macro":
                return self._macro(el)
            if tag == "task-list":
                return self._task_list(el)
            if tag == "image":
                return self._image(el)
            if tag == "adf-extension":
                return self._adf_extension(el)
            if tag in ("layout", "layout-section", "layout-cell"):
                return self._opaque_block(el)
        return self._opaque_block(el)

    # ------------- paragraph + headings ----------------------------------

    def _para(self, el: etree._Element) -> list[str]:
        text = self._inline(el).strip()
        if not text:
            return [""]  # empty <p/> from modern editor -> blank line
        return [text]

    def _heading(self, el: etree._Element, tag: str) -> list[str]:
        level = int(tag[1])
        # H1 in body collides with synthetic title H1. Downgrade to H2 to
        # keep MD parseable; users authoring H1 in body content is rare.
        if level == 1:
            level = 2
        return [f"{'#' * level} {self._inline(el).strip()}"]

    # ------------- lists ------------------------------------------------

    def _list(self, el: etree._Element, *, ordered: bool) -> list[str]:
        lines: list[str] = []
        for i, li in enumerate(el):
            if not isinstance(li.tag, str) or _local(li) != "li":
                continue
            marker = f"{i + 1}." if ordered else "-"
            text = self._li_inline(li).strip()
            lines.append(f"{marker} {text}")
        return lines

    def _li_inline(self, li: etree._Element) -> str:
        """Render a list-item's content as a single line of inline MD.

        Confluence (modern editor) wraps each list-item body in a `<p>`:
        `<li><p>text</p></li>`. We unwrap that single `<p>` so the inline
        renderer doesn't treat it as an opaque block."""
        children = [c for c in li if isinstance(c.tag, str)]
        if len(children) == 1 and _local(children[0]) == "p" and not _ns(children[0]):
            return self._inline(children[0])
        return self._inline(li)

    # ------------- blockquote -------------------------------------------

    def _blockquote(self, el: etree._Element) -> list[str]:
        inner = self._inline(el).strip()
        return [f"> {ln}" for ln in inner.split("\n")]

    # ------------- tables -----------------------------------------------

    def _table(self, el: etree._Element) -> list[str]:
        # GFM-eligible only if every <td>/<th> is "inline-only".
        if not self._table_is_simple(el):
            return self._opaque_block(el)
        rows = self._collect_rows(el)
        if not rows:
            return self._opaque_block(el)
        # First row -> header. GFM requires a header row.
        header, *body = rows
        ncols = len(header)
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(["---"] * ncols) + " |",
        ]
        for r in body:
            # pad short rows
            padded = r + [""] * (ncols - len(r))
            lines.append("| " + " | ".join(padded[:ncols]) + " |")
        return lines

    def _table_is_simple(self, el: etree._Element) -> bool:
        for cell in el.iter():
            if not isinstance(cell.tag, str):
                continue
            tag = _local(cell)
            if tag not in ("td", "th") or _ns(cell):
                continue
            for child in cell:
                if not isinstance(child.tag, str):
                    continue
                child_tag = _local(child)
                child_ns = _ns(child)
                # only one block kind allowed inside a cell: a single <p> wrapper
                if child_tag == "p" and child_ns == "":
                    continue
                # inline ac:* (e.g. ac:link with ri:user) is OK
                if child_ns == ACNS and child_tag in ("link", "inline-comment-marker"):
                    continue
                # anything else block-level -> opaque
                return False
        return True

    def _collect_rows(self, el: etree._Element) -> list[list[str]]:
        rows: list[list[str]] = []
        for tr in el.iter():
            if not isinstance(tr.tag, str) or _local(tr) != "tr" or _ns(tr):
                continue
            cells: list[str] = []
            for cell in tr:
                if not isinstance(cell.tag, str):
                    continue
                if _local(cell) not in ("td", "th") or _ns(cell):
                    continue
                # cell may contain a <p> wrapper; unwrap to inline
                cells.append(self._cell_text(cell))
            if cells:
                rows.append(cells)
        return rows

    def _cell_text(self, cell: etree._Element) -> str:
        # unwrap a single <p> if that's the only child
        children = [c for c in cell if isinstance(c.tag, str)]
        if len(children) == 1 and _local(children[0]) == "p" and not _ns(children[0]):
            return self._inline(children[0]).strip().replace("|", "\\|")
        return self._inline(cell).strip().replace("|", "\\|")

    # ------------- structured macros ------------------------------------

    def _macro(self, el: etree._Element) -> list[str]:
        name = el.get(_ac("name"), "")
        if name == "code":
            return self._code_macro(el)
        if name in _MACRO_PANEL_TYPES and _find_child(el, ACNS, "rich-text-body") is not None:
            return self._panel_macro(el, name)
        # everything else: opaque block. Tag with macro name for the label.
        return self._opaque_block(el, kind=f"macro:{name}")

    # ------------- editable panels (macro + adf) ------------------------

    def _panel_macro(self, el: etree._Element, name: str) -> list[str]:
        """Legacy panel: <ac:structured-macro ac:name="info|note|warning|tip"> with
        <ac:rich-text-body>. Body is rendered recursively as MD; macro params
        (none today on the classic four, reserved for future) preserved in
        sidecar."""
        params: dict[str, str] = {}
        for child in el:
            if not isinstance(child.tag, str):
                continue
            if _local(child) == "parameter" and _ns(child) == ACNS:
                params[child.get(_ac("name"), "")] = child.text or ""
        rtb = _find_child(el, ACNS, "rich-text-body")
        body_lines = self._render_panel_body(rtb) if rtb is not None else []
        uuid = _new_uuid()
        self.sidecar.panels[uuid] = {"shape": "macro", "name": name, "params": params}
        return _wrap_panel_with_alert(uuid, name, body_lines)

    def _adf_extension(self, el: etree._Element) -> list[str]:
        """ADF extension wrapper. We unwrap only when it's an editable panel
        (ac:adf-node type="panel" with non-custom panel-type). Anything else
        round-trips opaquely."""
        node = _find_child(el, ACNS, "adf-node")
        if node is None or node.get("type") != "panel":
            return self._opaque_block(el)
        panel_type = ""
        adf_attrs: dict[str, str] = {}
        content = None
        for child in node:
            if not isinstance(child.tag, str):
                continue
            ctag = _local(child)
            cns = _ns(child)
            if cns != ACNS:
                continue
            if ctag == "adf-attribute":
                key = child.get("key", "")
                val = child.text or ""
                if key == "panel-type":
                    panel_type = val
                elif key == "local-id":
                    continue  # bookkeeping; regenerated server-side
                else:
                    adf_attrs[key] = val
            elif ctag == "adf-content":
                content = child
        if panel_type not in _ADF_PANEL_TYPES:
            return self._opaque_block(el)

        fallback_el = _find_child(el, ACNS, "adf-fallback")
        adf_fallback = _inner_xml(fallback_el) if fallback_el is not None else ""

        body_lines = self._render_panel_body(content) if content is not None else []
        uuid = _new_uuid()
        self.sidecar.panels[uuid] = {
            "shape": "adf",
            "name": panel_type,
            "adf_attrs": adf_attrs,
            "adf_fallback": adf_fallback,
        }
        return _wrap_panel_with_alert(uuid, panel_type, body_lines)

    def _render_panel_body(self, container: etree._Element) -> list[str]:
        """Walk the panel body container's children as block elements, the
        same way the top-level loop does, and emit lines with single-blank
        separators between blocks."""
        lines: list[str] = []
        for node in container:
            out = self.block(node)
            if out:
                lines.extend(out)
                lines.append("")
        # Trim trailing blank — outer caller will reinsert as needed.
        while lines and lines[-1] == "":
            lines.pop()
        return lines

    def _code_macro(self, el: etree._Element) -> list[str]:
        # language: <ac:parameter ac:name="language">VAL</ac:parameter>
        # body:     <ac:plain-text-body><![CDATA[...]]></ac:plain-text-body>
        # All other <ac:parameter> children -> sidecar.code_blocks[UUID].params
        language = ""
        params: dict[str, str] = {}
        body = ""
        for child in el:
            if not isinstance(child.tag, str):
                continue
            ctag = _local(child)
            if ctag == "parameter" and _ns(child) == ACNS:
                pname = child.get(_ac("name"), "")
                pval = (child.text or "")
                if pname == "language":
                    language = pval
                else:
                    params[pname] = pval
            elif ctag == "plain-text-body" and _ns(child) == ACNS:
                body = child.text or ""

        uuid = _new_uuid()
        self.sidecar.code_blocks[uuid] = {"params": params}
        fence = f"```{language}" if language else "```"
        body_lines = body.splitlines() if body else [""]
        return [fence, *body_lines, "```", S.cc(uuid)]

    # ------------- task list --------------------------------------------

    def _task_list(self, el: etree._Element) -> list[str]:
        list_id = el.get(_ac("task-list-id"), "")
        lines: list[str] = []
        for task in el:
            if not isinstance(task.tag, str) or _local(task) != "task" or _ns(task) != ACNS:
                continue
            entry = self._task(task, list_id)
            if entry is not None:
                lines.append(entry)
        return lines

    def _task(self, el: etree._Element, list_id: str) -> str | None:
        task_id = ""
        task_uuid = ""
        status = "incomplete"
        body_inline = ""
        for child in el:
            if not isinstance(child.tag, str):
                continue
            ctag = _local(child)
            if _ns(child) != ACNS:
                continue
            if ctag == "task-id":
                task_id = (child.text or "").strip()
            elif ctag == "task-uuid":
                task_uuid = (child.text or "").strip()
            elif ctag == "task-status":
                status = (child.text or "").strip()
            elif ctag == "task-body":
                body_inline = self._inline(child).strip()

        if not S.is_uuid(task_uuid):
            # Confluence didn't include task-uuid? Synthesize one. Pull rare; tolerated.
            task_uuid = _new_uuid()
        self.sidecar.tasks[task_uuid] = {
            "status": status,
            "task_id": task_id,
            "task_list_id": list_id,
        }
        # Drop the "placeholder-inline-tasks" decoration text per notes.md §7.
        return f"- {S.ct(task_uuid)} {body_inline}".rstrip()

    # ------------- images -----------------------------------------------

    def _image(self, el: etree._Element) -> list[str]:
        # <ac:image attrs..><ri:attachment ri:filename="..." .../></ac:image>
        # or <ac:image><ri:url ri:value="..."/></ac:image>
        ref = next((c for c in el if isinstance(c.tag, str)), None)
        alt = el.get(_ac("alt"), "")
        if ref is None:
            return self._opaque_block(el, kind="image-empty")

        if _ns(ref) == RINS and _local(ref) == "attachment":
            filename = ref.get(_ri("filename"), "")
            ri_attrs = {k: v for k, v in ref.attrib.items()}
            ac_attrs = {k: v for k, v in el.attrib.items()}
            # Stable hash over canonical XML (bookkeeping attrs stripped).
            h = S.hash_xml(el)
            self.sidecar.images[h] = {
                "filename": filename,
                "ac_attrs": _strip_bookkeeping_keys(ac_attrs),
                "ri_attrs": _strip_bookkeeping_keys(ri_attrs),
            }
            path = f"./_meta/attachments/{filename}"
            line = f"![{alt}]({path}){S.ci_trailer(h)}"
            return [line]

        if _ns(ref) == RINS and _local(ref) == "url":
            url = ref.get(_ri("value"), "")
            return [f"![{alt}]({url})"]

        return self._opaque_block(el, kind="image-unknown")

    # ------------- opaque (block) ---------------------------------------

    def _opaque_block(self, el: etree._Element, *, kind: str | None = None) -> list[str]:
        if kind is None:
            kind = self._kind_of(el)
        h = S.hash_xml(el)
        xml = _serialize_for_sidecar(el)
        self.sidecar.blocks[h] = {"xml": xml, "kind": kind}
        return [
            f"> [confluence: {kind}]",
            S.cb(h),
        ]

    def _kind_of(self, el: etree._Element) -> str:
        ns = _ns(el)
        tag = _local(el)
        if ns == ACNS and tag == "structured-macro":
            name = el.get(_ac("name"), "?")
            return f"macro:{name}"
        if ns == ACNS:
            return f"ac:{tag}"
        return tag or "?"

    # ------------- inline rendering -------------------------------------

    def _inline(self, el: etree._Element) -> str:
        """Render the inline content of `el` to a single line of MD.
        Concatenates `el.text` + each child's inline rendering + child.tail."""
        parts: list[str] = []
        if el.text:
            parts.append(el.text)
        for child in el:
            parts.append(self._inline_node(child))
            if child.tail:
                parts.append(child.tail)
        return "".join(parts)

    def _inline_node(self, el: etree._Element) -> str:
        if not isinstance(el.tag, str):
            return ""
        ns = _ns(el)
        tag = _local(el)

        # HTML5 inline tags
        if ns == "":
            if tag == "strong" or tag == "b":
                return f"**{self._inline(el)}**"
            if tag == "em" or tag == "i":
                return f"*{self._inline(el)}*"
            if tag == "code":
                return f"`{self._inline(el)}`"
            if tag == "a":
                href = el.get("href", "")
                rewritten = self._maybe_rewrite_a_as_cl(el, href)
                if rewritten is not None:
                    return rewritten
                return f"[{self._inline(el)}]({href})"
            if tag == "br":
                return "  \n"
            if tag == "span":
                # ordinary span — passthrough, ignore attrs (it's likely
                # editor noise like placeholder-inline-tasks; we strip that
                # at the task-body site, but as a safety net here too)
                return self._inline(el)
            # Unknown HTML inline -> treat as inline opaque
            return self._inline_opaque(el)

        if ns == ACNS:
            if tag == "inline-comment-marker":
                ref = el.get(_ac("ref"), "")
                inner = self._inline(el)
                if S.is_uuid(ref):
                    return f"{S.cm_open(ref)}{inner}{S.cm_close(ref)}"
                return inner  # malformed ref — drop the marker rather than fail pull
            if tag == "link":
                rewritten = self._maybe_rewrite_ac_link_as_cl(el)
                if rewritten is not None:
                    return rewritten
                return self._inline_opaque(el, hint="link")
            if tag == "emoticon":
                return self._inline_opaque(el, hint="emoticon")
            if tag == "image":
                # rare: inline image. Treat same as block image but emit single line.
                lines = self._image(el)
                return lines[0] if lines else ""
            if tag == "structured-macro":
                # inline macros (status, inline-jira, ...) -> inline opaque
                name = el.get(_ac("name"), "?")
                return self._inline_opaque(el, hint=f"macro:{name}")

        # Default for anything unrecognized inline.
        return self._inline_opaque(el)

    def _inline_opaque(self, el: etree._Element, *, hint: str = "") -> str:
        h = S.hash_xml(el)
        xml = _serialize_for_sidecar(el)
        kind = hint or self._kind_of(el)
        self.sidecar.inline_blocks[h] = {"xml": xml, "kind": kind}
        # Pick a stable, human-readable placeholder. For user mentions the
        # display name isn't in storage (notes.md §11), so we use [@mention].
        visible = _placeholder_for(kind)
        return S.ci_span(h, visible)

    # ------------- cross-page links (Phase 7) ---------------------------

    def _maybe_rewrite_a_as_cl(self, el: etree._Element, href: str) -> str | None:
        """If `<a href=URL>` points to an in-tree page, emit `[text](rel)<!--cl:HASH-->`.
        Returns None if the link is not in-tree (caller emits the default form)."""
        if not self.pid_to_relpath or not href:
            return None
        pid, anchor = _parse_tenant_page_url(href)
        if pid is None or pid not in self.pid_to_relpath:
            return None
        text = self._inline(el)
        return self._emit_cl(el, text, pid, anchor)

    def _maybe_rewrite_ac_link_as_cl(self, el: etree._Element) -> str | None:
        """If `<ac:link>` wraps `<ri:page>` for an in-tree target, emit cl form.
        Recognized resolutions: ri:content-id (direct), or
        (ri:space-key, ri:content-title) via the title index."""
        if not self.pid_to_relpath:
            return None
        page_el = _find_child(el, RINS, "page")
        if page_el is None:
            return None
        pid = page_el.get(_ri("content-id"))
        if pid is None:
            title = page_el.get(_ri("content-title"))
            if title is not None:
                # title_to_pid is keyed by ("", title) — see subtree.py
                # for the rationale on the single-space key shape.
                pid = self.title_to_pid.get(("", title))
        if pid is None or pid not in self.pid_to_relpath:
            return None
        anchor = el.get(_ac("anchor")) or None
        text = _ac_link_visible_text(self, el)
        return self._emit_cl(el, text, pid, anchor)

    def _emit_cl(
        self,
        el: etree._Element,
        text: str,
        target_pid: str,
        anchor: str | None,
    ) -> str:
        """Record the original element verbatim, return the cl MD form."""
        h = S.hash_xml(el)
        self.sidecar.links[h] = {"xml": _serialize_for_sidecar(el)}
        target = self.pid_to_relpath[target_pid]
        # Compute display path relative to the current page's directory.
        import posixpath
        start = posixpath.dirname(self.self_page_relpath) if self.self_page_relpath else ""
        rel = posixpath.relpath(target, start=start) if start else target
        if anchor:
            rel = f"{rel}#{anchor}"
        return f"[{text}]({rel}){S.cl(h)}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_TENANT_PAGE_URL_RE = __import__("re").compile(r"/wiki/spaces/[^/]+/pages/(\d+)(?:/[^#?]*)?(?:\?[^#]*)?(?:#(.*))?$")


def _parse_tenant_page_url(href: str) -> tuple[str | None, str | None]:
    """Return (page_id, anchor) for a Confluence tenant pages URL, else (None, None).

    Matches both absolute (https://x/wiki/...) and relative (/wiki/...) forms.
    Anchor is the URL fragment minus the leading `#`.
    """
    if not href:
        return None, None
    m = _TENANT_PAGE_URL_RE.search(href)
    if not m:
        return None, None
    return m.group(1), (m.group(2) if m.group(2) else None)


def _ac_link_visible_text(walker: "_Walker", el: etree._Element) -> str:
    """Extract the human-visible text from an `<ac:link>` element.

    Body shapes (modern + legacy):
    - `<ac:plain-text-link-body>text</ac:plain-text-link-body>`
    - `<ac:link-body>text or inline content</ac:link-body>`
    - No body: fall back to the page title (best effort) or empty.
    """
    for child in el:
        if not isinstance(child.tag, str):
            continue
        if _ns(child) != ACNS:
            continue
        lt = _local(child)
        if lt == "plain-text-link-body":
            return child.text or ""
        if lt == "link-body":
            return walker._inline(child)
    return ""


def _placeholder_for(kind: str) -> str:
    if kind == "link":
        return "[@mention]"  # user mentions are the only common ac:link form
    if kind == "emoticon":
        return ":emoticon:"
    if kind.startswith("macro:"):
        return f"[{kind}]"
    return "[opaque]"


def _serialize_for_sidecar(el: etree._Element) -> str:
    """Serialize an element to a self-contained string for sidecar storage.

    Uses C14N 2.0 via sentinels.c14n2 (which detaches first for namespace
    safety). Bookkeeping attrs are preserved in the sidecar — they're
    accepted on PUT, and stripping them would slowly drift the page's
    collaboration state.
    """
    return S.c14n2(el).decode("utf-8")


def _strip_bookkeeping_keys(attrs: dict[str, str]) -> dict[str, str]:
    """Drop modern-editor bookkeeping keys from an attr dict (for the
    on-disk JSON sidecar form, where we don't need them)."""
    drop = {
        "local-id",
        _ac("local-id"),
        _ac("macro-id"),
        _ri("local-id"),
    }
    return {k: v for k, v in attrs.items() if k not in drop}


def _wrap_panel_with_alert(uuid: str, panel_name: str, body_lines: list[str]) -> list[str]:
    """Emit a Confluence panel as a cp marker pair wrapping a GFM alert.

    Layout:
        <!--cp:UUID-->

        > [!KIND]
        > body line 1
        >
        > body line 2

        <!--/cp:UUID-->

    Blank body lines become a bare `>` so the blockquote (and therefore the
    alert) stays a single block in the renderer. md_to_storage strips the
    alert prefix on push so the round trip is invertible.
    """
    kind = S.panel_kind_to_gfm(panel_name)
    quoted = [f"> {line}" if line else ">" for line in body_lines]
    return [S.cp_open(uuid), "", f"> [!{kind}]", *quoted, "", S.cp_close(uuid)]


def _new_uuid() -> str:
    import uuid as _u

    return str(_u.uuid4())


def _find_child(el: etree._Element, ns: str, local: str) -> etree._Element | None:
    for c in el:
        if isinstance(c.tag, str) and _ns(c) == ns and _local(c) == local:
            return c
    return None


def _inner_xml(el: etree._Element) -> str:
    """Serialize an element's children (text + sub-elements) as a single
    string, without the wrapping `<el>...</el>` tags. Used for capturing
    `<ac:adf-fallback>` body verbatim."""
    parts: list[str] = []
    if el.text:
        parts.append(el.text)
    for child in el:
        parts.append(etree.tostring(child, encoding="unicode", with_tail=True))
    return "".join(parts)
