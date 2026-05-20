"""Confluence Cloud v2 REST client.

Credential discipline:
- API token never enters argv or env vars.
- Token loaded from a 0600 TOML file (~/.config/confluence-markdown-roundtrip/credentials.toml).
- Token never written to stdout/stderr/logs. `__repr__` redacts.
- Authorization headers redacted in any exception message we raise.

See plan §"Conventions and constants" and §"Phase 1 — notes.md" for
the endpoint contracts implemented here.
"""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urljoin

import httpx


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def default_credentials_path() -> Path:
    """Plan §"Conventions and constants" — XDG_CONFIG_HOME with ~/.config fallback."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "confluence-markdown-roundtrip" / "credentials.toml"


@dataclass(frozen=True)
class Credentials:
    base_url: str
    email: str
    api_token: str

    def __repr__(self) -> str:
        return f"Credentials(base_url={self.base_url!r}, email={self.email!r}, api_token='***')"


class CredentialsError(Exception):
    """Raised when credentials can't be loaded. Message never includes the token."""


def load_credentials(path: Path | None = None) -> Credentials:
    p = (path or default_credentials_path()).expanduser()
    if not p.exists():
        raise CredentialsError(f"credentials file not found: {p}")

    st = p.stat()
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise CredentialsError(
            f"credentials file {p} has too-permissive mode "
            f"{oct(stat.S_IMODE(st.st_mode))}; chmod 0600"
        )

    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise CredentialsError(f"credentials file {p} is not valid TOML: {e}") from None

    missing = [k for k in ("base_url", "email", "api_token") if k not in data]
    if missing:
        raise CredentialsError(f"credentials file {p} missing keys: {missing}")

    base_url = data["base_url"].rstrip("/")
    return Credentials(base_url=base_url, email=data["email"], api_token=data["api_token"])


# ---------------------------------------------------------------------------
# Exceptions — server-side / API-layer
# ---------------------------------------------------------------------------


class APIError(Exception):
    """Server returned a non-success status. Authorization header is never
    serialized into the message."""

    def __init__(self, status: int, method: str, url: str, body_excerpt: str = ""):
        self.status = status
        self.method = method
        self.url = url
        self.body_excerpt = body_excerpt
        super().__init__(f"{method} {url} -> {status}: {body_excerpt[:300]}")


class VersionConflict(APIError):
    """HTTP 409 on PUT — local base_version is stale."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


# Fields we read from each endpoint. Documented in notes.md §"Concrete API
# contracts for api.py". Other fields are ignored — Confluence adds keys freely.
@dataclass
class Page:
    id: str
    title: str
    space_id: str
    parent_id: str | None
    version: int
    storage_body: str

    @classmethod
    def from_json(cls, j: Mapping[str, Any]) -> "Page":
        return cls(
            id=str(j["id"]),
            title=j["title"],
            space_id=str(j.get("spaceId", "")),
            parent_id=str(j["parentId"]) if j.get("parentId") else None,
            version=int(j["version"]["number"]),
            storage_body=j.get("body", {}).get("storage", {}).get("value", ""),
        )


@dataclass
class Descendant:
    id: str
    title: str
    type: str
    parent_id: str | None


@dataclass
class Attachment:
    id: str
    title: str
    media_type: str
    download_link: str  # relative URL — concatenate with base_url + "/wiki"


@dataclass
class InlineComment:
    id: str  # Confluence content id of the comment (NOT the marker ref).
    marker_ref: str  # `properties.inlineMarkerRef` — the UUID used in
                    # <ac:inline-comment-marker ac:ref="...">. Different
                    # from `id`; see plan §"Phase 1 - notes.md" + observed
                    # behavior during Phase 2 online tests.
    text_selection: str
    resolution_status: str
    body_storage: str


class ConfluenceClient:
    """Thin wrapper around httpx.Client. Caller owns lifetime (use as context manager).

    Auth: HTTP Basic (email + api_token). httpx never logs the auth header.
    """

    def __init__(self, creds: Credentials, *, timeout: float = 30.0):
        self._creds = creds
        self._client = httpx.Client(
            base_url=creds.base_url,
            auth=(creds.email, creds.api_token),
            headers={"Accept": "application/json"},
            timeout=timeout,
            follow_redirects=True,
        )

    def __enter__(self) -> "ConfluenceClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- private helpers ----------------------------------------------------

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        r = self._client.request(method, url, **kwargs)
        if r.status_code >= 400:
            excerpt = _safe_response_excerpt(r)
            if r.status_code == 409:
                raise VersionConflict(r.status_code, method, url, excerpt)
            raise APIError(r.status_code, method, url, excerpt)
        return r

    # -- pages --------------------------------------------------------------

    def get_page(self, page_id: int | str) -> Page:
        r = self._request("GET", f"/wiki/api/v2/pages/{page_id}", params={"body-format": "storage"})
        return Page.from_json(r.json())

    def get_page_version(self, page_id: int | str) -> int:
        """Cheap version check — same endpoint but we discard the body.
        Confluence v2 has no smaller endpoint that returns only version."""
        r = self._request("GET", f"/wiki/api/v2/pages/{page_id}")
        return int(r.json()["version"]["number"])

    def update_page(
        self,
        page_id: int | str,
        *,
        title: str,
        storage_body: str,
        base_version: int,
    ) -> Page:
        body = {
            "id": str(page_id),
            "status": "current",
            "title": title,
            "body": {"representation": "storage", "value": storage_body},
            "version": {"number": base_version + 1},
        }
        r = self._request(
            "PUT",
            f"/wiki/api/v2/pages/{page_id}",
            json=body,
            headers={"Content-Type": "application/json"},
        )
        return Page.from_json(r.json())

    # -- ancestors ----------------------------------------------------------

    def list_ancestors(self, page_id: int | str) -> list[str]:
        """Return ancestor page ids, topmost-ancestor first.

        Each item in the response is `{id, type}` only (no title, no
        parentId) — Plan §"Phase 8 — Ancestor pull" API note. Caller must
        fetch each id with `get_page` for title + body. Empty list means
        the page is the space homepage (verified against live tenant
        2026-05-19).

        Do NOT pass a `limit` query parameter: probe established that
        truncated responses return the wrong end of the chain (immediate
        parent, not topmost) and no `_links.next` is emitted for cursor
        pagination. Take the default response and trust it fits.
        """
        r = self._request("GET", f"/wiki/api/v2/pages/{page_id}/ancestors")
        results = r.json().get("results", [])
        return [str(item["id"]) for item in results if item.get("type") == "page"]

    # -- descendants --------------------------------------------------------

    def list_descendants(self, root_page_id: int | str, *, depth: int | None = None) -> Iterator[Descendant]:
        params: dict[str, Any] = {"limit": 250}
        if depth is not None:
            params["depth"] = depth
        for item in self._paginate(f"/wiki/api/v2/pages/{root_page_id}/descendants", params=params):
            if item.get("type") != "page":
                continue  # plan §"Subtree scope" — pages only in v1
            yield Descendant(
                id=str(item["id"]),
                title=item["title"],
                type=item["type"],
                parent_id=str(item["parentId"]) if item.get("parentId") else None,
            )

    # -- attachments --------------------------------------------------------

    def list_attachments(self, page_id: int | str) -> Iterator[Attachment]:
        for item in self._paginate(f"/wiki/api/v2/pages/{page_id}/attachments", params={"limit": 250}):
            yield Attachment(
                id=str(item["id"]),
                title=item["title"],
                media_type=item.get("mediaType", ""),
                download_link=item.get("downloadLink", ""),
            )

    def download_attachment(self, download_link: str) -> bytes:
        # v2 attachment `downloadLink` is rooted at the wiki context but
        # *without* the `/wiki` prefix (e.g. `/download/attachments/...`).
        # Confluence Cloud serves the static asset under `/wiki/download/...`,
        # so we prepend it for relative links.
        # NOTE: this path returns 401-OAuth under Basic auth on Cloud. Prefer
        # download_attachment_v1 for Basic-auth callers; this method is kept
        # for OAuth-equipped callers.
        if download_link.startswith("http"):
            url = download_link
        else:
            path = download_link if download_link.startswith("/wiki/") else "/wiki" + download_link
            url = self._creds.base_url + path
        r = self._request("GET", url)
        return r.content

    def download_attachment_v1(self, page_id: int | str, attachment_id: str) -> bytes:
        """Fetch attachment bytes via the v1 child-attachment download endpoint.
        Accepts Basic auth on Confluence Cloud (unlike the v2 `_links.download`
        URL which 401s with WWW-Authenticate: OAuth). Accepts attachment_id
        with or without the `att` prefix."""
        url = (
            f"{self._creds.base_url}/wiki/rest/api/content/"
            f"{page_id}/child/attachment/{attachment_id}/download"
        )
        r = self._request("GET", url)
        return r.content

    # -- inline comments ----------------------------------------------------
    # Used by the test bootstrap to seed comments. Not by pull/push (markers
    # are read straight from storage XHTML).

    def list_inline_comments(self, page_id: int | str) -> Iterator[InlineComment]:
        for item in self._paginate(
            f"/wiki/api/v2/pages/{page_id}/inline-comments",
            params={"limit": 250, "body-format": "storage"},
        ):
            props = item.get("properties") or {}
            # `inlineOriginalSelection` is the anchor text Confluence
            # remembered at creation time; it survives even when the marker
            # is orphaned (unlike the v1 `textSelection` field which clears).
            yield InlineComment(
                id=str(item["id"]),
                marker_ref=props.get("inlineMarkerRef", ""),
                text_selection=props.get("textSelection") or props.get("inlineOriginalSelection", ""),
                resolution_status=item.get("resolutionStatus", ""),
                body_storage=item.get("body", {}).get("storage", {}).get("value", ""),
            )

    def create_inline_comment(
        self,
        page_id: int | str,
        *,
        body_storage: str,
        text_selection: str,
        match_count: int = 1,
        match_index: int = 0,
    ) -> InlineComment:
        body = {
            "pageId": str(page_id),
            "body": {"representation": "storage", "value": body_storage},
            "inlineCommentProperties": {
                "textSelection": text_selection,
                "textSelectionMatchCount": match_count,
                "textSelectionMatchIndex": match_index,
            },
        }
        r = self._request(
            "POST",
            "/wiki/api/v2/inline-comments",
            json=body,
            headers={"Content-Type": "application/json"},
        )
        j = r.json()
        props = j.get("properties") or {}
        return InlineComment(
            id=str(j["id"]),
            marker_ref=props.get("inlineMarkerRef", ""),
            text_selection=props.get("textSelection") or props.get("inlineOriginalSelection", text_selection),
            resolution_status=j.get("resolutionStatus", ""),
            body_storage=j.get("body", {}).get("storage", {}).get("value", ""),
        )

    # -- pagination ---------------------------------------------------------

    def _paginate(self, url: str, *, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        next_url: str | None = url
        next_params = params
        while next_url:
            r = self._request("GET", next_url, params=next_params)
            j = r.json()
            for item in j.get("results", []):
                yield item
            nxt = (j.get("_links") or {}).get("next")
            if not nxt:
                return
            next_url = nxt  # already has query string baked in
            next_params = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_response_excerpt(r: httpx.Response) -> str:
    """Return a short body excerpt safe to surface in exceptions.
    Never includes request headers (which contain Authorization)."""
    try:
        text = r.text
    except Exception:
        return ""
    return text[:500]
