"""Single-page workspace I/O.

The shape on disk is plan §"On-disk layout":

    <root-slug>/
      index.md
      _meta/
        index.md.orig
        index.conf.json
        attachments/...

This module owns reading/writing those files. The converters operate on
strings; the CLI glues converters + workspace + the HTTP client together.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import sentinels as S


SIDECAR_NAME = "index.conf.json"
ORIG_NAME = "index.md.orig"


@dataclass
class PageWorkspace:
    """Represents one page directory on disk. Loads everything the push
    pipeline needs into memory; writers go through dedicated methods so
    the disk layout stays consistent."""

    root: Path  # the page directory (contains index.md + _meta/)
    page_id: str
    title: str
    space_id: str
    parent_id: str | None
    base_version: int
    base_storage_sha256: str
    base_md_sha256: str
    fetched_at: str
    # Sidecar contents — these are written verbatim to JSON.
    blocks: dict[str, Any] = field(default_factory=dict)
    inline_blocks: dict[str, Any] = field(default_factory=dict)
    tasks: dict[str, Any] = field(default_factory=dict)
    code_blocks: dict[str, Any] = field(default_factory=dict)
    images: dict[str, Any] = field(default_factory=dict)
    panels: dict[str, Any] = field(default_factory=dict)
    links: dict[str, Any] = field(default_factory=dict)
    metadata_preserve: dict[str, Any] = field(default_factory=dict)

    # ---- on-disk paths ---------------------------------------------------

    @property
    def index_md_path(self) -> Path:
        return self.root / "index.md"

    @property
    def meta_dir(self) -> Path:
        return self.root / "_meta"

    @property
    def orig_path(self) -> Path:
        return self.meta_dir / ORIG_NAME

    @property
    def sidecar_path(self) -> Path:
        return self.meta_dir / SIDECAR_NAME

    @property
    def attachments_dir(self) -> Path:
        return self.meta_dir / "attachments"

    # ---- io --------------------------------------------------------------

    def write_initial(self, index_md_text: str) -> None:
        """Write a fresh `index.md` plus its byte-identical `.orig`. Used by pull."""
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        self.attachments_dir.mkdir(parents=True, exist_ok=True)
        self.index_md_path.write_text(index_md_text, encoding="utf-8")
        self.orig_path.write_text(index_md_text, encoding="utf-8")
        self.base_md_sha256 = hashlib.sha256(index_md_text.encode("utf-8")).hexdigest()
        self.write_sidecar()

    def re_pull(self, index_md_text: str) -> str:
        """Pull again into an existing workspace.

        Plan §"Re-pull on existing directory":
        - If `index.md` is byte-equal to `_meta/index.md.orig` (clean), overwrite
          both with the new content and rewrite the sidecar.
        - If `index.md` differs (dirty), write the new content to a sibling
          `index.md.remote` and leave `index.md`, `_meta/`, and the sidecar
          untouched. The user merges manually.

        Returns: `"clean"` if overwritten in place, `"dirty"` if a .remote
        sibling was written.
        """
        # Cold path: no workspace yet → identical to write_initial.
        if not self.index_md_path.exists() or not self.orig_path.exists():
            self.write_initial(index_md_text)
            return "clean"

        existing_md = self.index_md_path.read_text(encoding="utf-8")
        existing_orig = self.orig_path.read_text(encoding="utf-8")

        if existing_md == existing_orig:
            self.index_md_path.write_text(index_md_text, encoding="utf-8")
            self.orig_path.write_text(index_md_text, encoding="utf-8")
            self.base_md_sha256 = hashlib.sha256(index_md_text.encode("utf-8")).hexdigest()
            self.write_sidecar()
            return "clean"

        remote_path = self.index_md_path.with_suffix(".md.remote")
        remote_path.write_text(index_md_text, encoding="utf-8")
        return "dirty"

    def write_sidecar(self) -> None:
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        self.sidecar_path.write_text(
            json.dumps(self.to_sidecar_json(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def to_sidecar_json(self) -> dict[str, Any]:
        return {
            "page_id": self.page_id,
            "space_id": self.space_id,
            "title": self.title,
            "parent_id": self.parent_id,
            "base_version": self.base_version,
            "base_storage_sha256": self.base_storage_sha256,
            "base_md_sha256": self.base_md_sha256,
            "fetched_at": self.fetched_at,
            "blocks": self.blocks,
            "inline_blocks": self.inline_blocks,
            "tasks": self.tasks,
            "code_blocks": self.code_blocks,
            "images": self.images,
            "panels": self.panels,
            "links": self.links,
            "metadata_preserve": self.metadata_preserve,
        }

    @classmethod
    def from_disk(cls, root: Path) -> "PageWorkspace":
        """Load an existing workspace. Validates integrity (sidecar shape
        + .orig hash); raises `PushAbort` on tampering so the push pipeline
        can fail cleanly with the right rule-id."""
        meta = root / "_meta"
        sidecar_path = meta / SIDECAR_NAME
        orig_path = meta / ORIG_NAME
        if not sidecar_path.exists():
            raise S.meta_tampered(f"missing {sidecar_path}", file=str(root))
        try:
            data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise S.meta_tampered(f"sidecar {sidecar_path} not valid JSON: {e}", file=str(root))

        if not orig_path.exists():
            raise S.orig_tampered(f"missing {orig_path}", file=str(root))
        orig_text = orig_path.read_text(encoding="utf-8")
        orig_hash = hashlib.sha256(orig_text.encode("utf-8")).hexdigest()
        if orig_hash != data.get("base_md_sha256"):
            raise S.orig_tampered(
                f"sha256({orig_path}) does not match sidecar.base_md_sha256",
                file=str(root),
            )

        return cls(
            root=root,
            page_id=str(data["page_id"]),
            title=data["title"],
            space_id=str(data.get("space_id", "")),
            parent_id=str(data["parent_id"]) if data.get("parent_id") else None,
            base_version=int(data["base_version"]),
            base_storage_sha256=data["base_storage_sha256"],
            base_md_sha256=data["base_md_sha256"],
            fetched_at=data.get("fetched_at", ""),
            blocks=data.get("blocks", {}),
            inline_blocks=data.get("inline_blocks", {}),
            tasks=data.get("tasks", {}),
            code_blocks=data.get("code_blocks", {}),
            images=data.get("images", {}),
            panels=data.get("panels", {}),
            links=data.get("links", {}),
            metadata_preserve=data.get("metadata_preserve", {}),
        )

    def read_index_md(self) -> str:
        return self.index_md_path.read_text(encoding="utf-8")

    def read_orig(self) -> str:
        return self.orig_path.read_text(encoding="utf-8")

    def sidecar_dict(self) -> dict[str, Any]:
        """Shape used by `md_to_storage.md_to_storage`."""
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


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def download_referenced_attachments(client: Any, page_id: str, ws: "PageWorkspace") -> list[str]:
    """Fetch binaries for every attachment referenced in ws.images.

    Returns a list of "<title> (<status>)" strings for attachments whose
    fetch failed; callers may surface as a warning. Uses the v1 child-
    attachment download endpoint, which accepts Basic auth.
    """
    from .api import APIError  # local import: avoid cycle at import time

    needed_filenames = {entry["filename"] for entry in ws.images.values() if entry.get("filename")}
    if not needed_filenames:
        return []
    failed: list[str] = []
    for att in client.list_attachments(page_id):
        if att.title not in needed_filenames:
            continue
        try:
            data = client.download_attachment_v1(page_id, att.id)
        except APIError as e:
            failed.append(f"{att.title} ({e.status})")
            continue
        target = ws.attachments_dir / att.title
        target.write_bytes(data)
    return failed
