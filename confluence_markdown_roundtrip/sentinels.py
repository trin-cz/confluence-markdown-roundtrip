"""Marker (sentinel) syntax + identifier conventions for the round-trip skill.

A small, pure module: regexes, encode/decode helpers, validators,
hash function, slugify, and the canonical XML stripper used everywhere
a HASH is computed.

No I/O. No state. The rest of the package depends on this.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from lxml import etree


# ---------------------------------------------------------------------------
# Identifier shapes
# ---------------------------------------------------------------------------

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_HASH = r"[0-9a-f]{12}"

UUID_RE = re.compile(rf"^{_UUID}$")
HASH_RE = re.compile(rf"^{_HASH}$")


def is_uuid(s: str) -> bool:
    return UUID_RE.match(s) is not None


def is_hash(s: str) -> bool:
    return HASH_RE.match(s) is not None


# ---------------------------------------------------------------------------
# Marker regexes — these match the *MD-side* sentinel forms
# ---------------------------------------------------------------------------

CM_OPEN_RE = re.compile(rf"<!--cm:({_UUID})-->")
CM_CLOSE_RE = re.compile(rf"<!--/cm:({_UUID})-->")
CB_RE = re.compile(rf"<!--cb:({_HASH})-->")
CI_TRAILER_RE = re.compile(rf"<!--ci:({_HASH})-->")  # image trailer
CC_RE = re.compile(rf"<!--cc:({_UUID})-->")  # code block params trailer
CT_RE = re.compile(rf"<!--ct:({_UUID})-->")  # task
CL_RE = re.compile(rf"<!--cl:({_HASH})-->")  # cross-link trailer (in-tree page)
# Editable panel open/close. `style=NAME` is an inert hint for the reader;
# push side ignores it and uses sidecar.panels[UUID].name. Style is optional.
CP_OPEN_RE = re.compile(rf"<!--cp:({_UUID})(?:\s+style=([a-z]+))?-->")
CP_CLOSE_RE = re.compile(rf"<!--/cp:({_UUID})-->")

# Inline opaque span. data-ci value is a HASH.
CI_SPAN_RE = re.compile(rf'<span\s+data-ci="({_HASH})">(.*?)</span>', re.DOTALL)

# Catches a malformed marker before any of the strict ones do — used to
# produce a precise `bad-marker-syntax` abort rather than a silent miss.
ANY_MARKER_PREFIX_RE = re.compile(r"<!--/?(cm|cb|cc|ct|ci|cp|cl):([^>]*)-->")

# Union of macro (info/note/warning/tip) and adf (info/note/warning/success/error)
# panel types. The style hint in a cp marker should match one of these.
PANEL_TYPES = frozenset({"info", "note", "warning", "tip", "success", "error"})

# Confluence panel kind → GFM Alert tag. The MD-side visible wrapper is a
# GFM blockquote alert; the kind is derived deterministically from the
# sidecar panel name so the alert is cosmetic-on-pull, ignored-on-push.
# success has no GFM equivalent — green TIP is the closest visual match.
_PANEL_TO_GFM = {
    "info": "NOTE",
    "note": "IMPORTANT",
    "tip": "TIP",
    "success": "TIP",
    "warning": "WARNING",
    "error": "CAUTION",
}

GFM_ALERT_KINDS = frozenset({"NOTE", "TIP", "IMPORTANT", "WARNING", "CAUTION"})

GFM_ALERT_OPENER_RE = re.compile(r"^\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*$")


def panel_kind_to_gfm(name: str) -> str:
    """Return the GFM alert kind for a Confluence panel name.
    Unknown names fall back to NOTE (defensive — should never trigger
    since storage_to_md only invokes us for whitelisted panel types)."""
    return _PANEL_TO_GFM.get(name, "NOTE")


# ---------------------------------------------------------------------------
# Encoders — the canonical way to *write* a marker
# ---------------------------------------------------------------------------


def cm_open(uuid: str) -> str:
    _check_uuid(uuid)
    return f"<!--cm:{uuid}-->"


def cm_close(uuid: str) -> str:
    _check_uuid(uuid)
    return f"<!--/cm:{uuid}-->"


def cb(hash_: str) -> str:
    _check_hash(hash_)
    return f"<!--cb:{hash_}-->"


def ci_trailer(hash_: str) -> str:
    _check_hash(hash_)
    return f"<!--ci:{hash_}-->"


def cc(uuid: str) -> str:
    _check_uuid(uuid)
    return f"<!--cc:{uuid}-->"


def ct(uuid: str) -> str:
    _check_uuid(uuid)
    return f"<!--ct:{uuid}-->"


def ci_span(hash_: str, visible: str) -> str:
    _check_hash(hash_)
    return f'<span data-ci="{hash_}">{visible}</span>'


def cl(hash_: str) -> str:
    _check_hash(hash_)
    return f"<!--cl:{hash_}-->"


def cp_open(uuid: str, style: str | None = None) -> str:
    _check_uuid(uuid)
    if style is None:
        return f"<!--cp:{uuid}-->"
    return f"<!--cp:{uuid} style={style}-->"


def cp_close(uuid: str) -> str:
    _check_uuid(uuid)
    return f"<!--/cp:{uuid}-->"


# ---------------------------------------------------------------------------
# Push-abort exception hierarchy
# ---------------------------------------------------------------------------


@dataclass
class PushAbort(Exception):
    """Single exception type carrying a rule-id, optional file/line, message.

    See plan §"Push abort format" for the wire format. The CLI catches this,
    formats `error: <file>:<line>: <rule-id>: <message>` to stderr, and exits
    with the appropriate code.
    """

    rule_id: str
    message: str
    file: str | None = None
    line: int | None = None

    def __str__(self) -> str:
        loc = f"{self.file or '<unknown>'}:{self.line if self.line is not None else '?'}"
        return f"{loc}: {self.rule_id}: {self.message}"


# Convenience constructors keep call sites short.
def unmatched_cm(message: str, *, file: str | None = None, line: int | None = None) -> PushAbort:
    return PushAbort("unmatched-cm", message, file=file, line=line)


def unknown_cb_hash(h: str, *, file: str | None = None, line: int | None = None) -> PushAbort:
    return PushAbort("unknown-cb-hash", f"block hash {h} not in sidecar", file=file, line=line)


def unknown_ci_hash(h: str, *, file: str | None = None, line: int | None = None) -> PushAbort:
    return PushAbort("unknown-ci-hash", f"inline hash {h} not in sidecar", file=file, line=line)


def unknown_cl_hash(h: str, *, file: str | None = None, line: int | None = None) -> PushAbort:
    return PushAbort("unknown-cl-hash", f"cross-link hash {h} not in sidecar", file=file, line=line)


def new_attachment(path: str, *, file: str | None = None, line: int | None = None) -> PushAbort:
    return PushAbort("new-attachment", f"image path {path} not present in attachments", file=file, line=line)


def missing_h1(*, file: str | None = None) -> PushAbort:
    return PushAbort("missing-h1", "first non-blank line of index.md is not an H1", file=file, line=1)


def bad_marker_syntax(message: str, *, file: str | None = None, line: int | None = None) -> PushAbort:
    return PushAbort("bad-marker-syntax", message, file=file, line=line)


def unmatched_cp(message: str, *, file: str | None = None, line: int | None = None) -> PushAbort:
    return PushAbort("unmatched-cp", message, file=file, line=line)


def unknown_cp_uuid(uuid: str, *, file: str | None = None, line: int | None = None) -> PushAbort:
    return PushAbort("unknown-cp-uuid", f"panel uuid {uuid} not in sidecar", file=file, line=line)


def version_conflict(local: int, remote: int, *, file: str | None = None) -> PushAbort:
    return PushAbort(
        "version-conflict",
        f"local base_version={local}, remote version={remote}; .remote.md written",
        file=file,
    )


def orig_tampered(message: str, *, file: str | None = None) -> PushAbort:
    return PushAbort("orig-tampered", message, file=file)


def meta_tampered(message: str, *, file: str | None = None) -> PushAbort:
    return PushAbort("meta-tampered", message, file=file)


# ---------------------------------------------------------------------------
# Internal validators
# ---------------------------------------------------------------------------


def _check_uuid(s: str) -> None:
    if not is_uuid(s):
        raise bad_marker_syntax(f"not a UUID: {s!r}")


def _check_hash(s: str) -> None:
    if not is_hash(s):
        raise bad_marker_syntax(f"not a 12-hex hash: {s!r}")


# ---------------------------------------------------------------------------
# Hash + canonical XML
# ---------------------------------------------------------------------------

# Modern-editor bookkeeping attributes that change on every server response.
# Stripped before hashing so HASH is stable across pulls.
# See confluence/CLAUDE.md §"Modern editor surface artifacts".
_BOOKKEEPING_ATTRS = frozenset(
    {
        "local-id",
        "{http://atlassian.com/content}local-id",  # ac:local-id
        "{http://atlassian.com/content}macro-id",  # ac:macro-id
        "{http://atlassian.com/resource/identifier}local-id",  # ri:local-id
    }
)


def strip_bookkeeping(el: etree._Element) -> etree._Element:
    """Return a deep copy of `el` with bookkeeping attrs removed.

    Operates on a copy — never mutates the input. Used by `hash_xml` and
    by tests that compare canonical XML byte-equality.
    """
    copy = _deep_copy(el)
    for node in copy.iter():
        if not isinstance(node.tag, str):
            continue  # comment / PI
        for attr in list(node.attrib.keys()):
            if attr in _BOOKKEEPING_ATTRS:
                del node.attrib[attr]
    return copy


def _deep_copy(el: etree._Element) -> etree._Element:
    # round-trip through serialization is the only way to get a fully
    # detached copy that's safe to mutate without affecting the parent tree.
    # `with_tail=False` is critical: the original element may have trailing
    # text from its parent's text node, which would not parse standalone.
    return etree.fromstring(etree.tostring(el, with_tail=False))


def c14n2(el: etree._Element) -> bytes:
    """Canonical XML bytes (C14N 2.0). Used for hashing + byte-compare.

    Detaches `el` first so c14n2 sees the namespace decls — lxml's c14n2
    impl errors on elements whose namespaces are declared only on ancestors
    in the parent tree.
    """
    detached = _deep_copy(el)
    return etree.tostring(detached, method="c14n2", with_comments=True)


def hash_xml(el: etree._Element) -> str:
    """Stable 12-hex hash of an XML element, ignoring bookkeeping noise."""
    stripped = strip_bookkeeping(el)
    return hashlib.sha256(c14n2(stripped)).hexdigest()[:12]


def hash_bytes(data: bytes) -> str:
    """12-hex hash of an arbitrary byte blob — used for index.md / .orig."""
    return hashlib.sha256(data).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Slugify
# ---------------------------------------------------------------------------

_SLUG_NONWORD = re.compile(r"[^a-z0-9]+")
_SLUG_MAX = 60


def slugify(title: str) -> str:
    """Plan §"Conventions and constants".

    NFKD normalize -> strip combining marks -> lowercase -> non-[a-z0-9] runs
    to '-' -> trim leading/trailing '-' -> truncate at 60 chars on last '-'
    before 60 -> empty -> 'page'. Collision resolution (-2, -3, ...) is the
    caller's job.
    """
    nfkd = unicodedata.normalize("NFKD", title)
    no_marks = "".join(c for c in nfkd if not unicodedata.combining(c))
    lowered = no_marks.lower()
    dashed = _SLUG_NONWORD.sub("-", lowered).strip("-")
    if not dashed:
        return "page"
    if len(dashed) <= _SLUG_MAX:
        return dashed
    # Truncate at the last '-' at or before _SLUG_MAX so we don't cut a word.
    cut = dashed.rfind("-", 0, _SLUG_MAX)
    if cut <= 0:
        # No dash to cut on — hard-truncate.
        return dashed[:_SLUG_MAX]
    return dashed[:cut]


def slugify_unique(title: str, taken: Iterable[str]) -> str:
    """slugify + numeric suffix on collision. `taken` is the set of slugs
    already used in the destination directory."""
    base = slugify(title)
    taken_set = set(taken)
    if base not in taken_set:
        return base
    n = 2
    while True:
        candidate = f"{base}-{n}"
        if candidate not in taken_set:
            return candidate
        n += 1


# ---------------------------------------------------------------------------
# URL / id parsing — `pull <url-or-id>` argument
# ---------------------------------------------------------------------------

_PAGES_URL_RE = re.compile(r"/pages/(\d+)")


def page_id_from_arg(arg: str) -> int:
    """Accepts a Confluence page URL or a bare page id. Raises ValueError on
    anything else. Plan §"Conventions and constants" defines the rule."""
    m = _PAGES_URL_RE.search(arg)
    if m:
        return int(m.group(1))
    if arg.isdigit():
        return int(arg)
    raise ValueError(f"cannot extract page id from {arg!r}")
