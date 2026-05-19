"""Pytest harness for online Phase 2 tests.

Wires up the three-phase fixture lifecycle from plan §"Fixture lifecycle":

1. Session bootstrap (once per pytest invocation):
   - Discover the 4 page IDs by walking descendants of root.
   - Verify the tree shape; abort if wrong.
   - For each page: reconcile inline comments (additive), assemble the
     template with real comment UUIDs, PUT body + title.
   - Capture the resulting canonical storage as the in-memory baseline.

2. Per-test restore (before every mutating test):
   - PUT the captured baseline back to each page involved in the test.

3. Per-test workspace helpers:
   - `make_workspace(page_id) -> Path`: fresh tmp dir, pull there.
   - `push(path) -> CompletedProcess`: invoke CLI's push.
   - `diff_md(a, b)`: structured diff for assertions.

The whole online tier is gated by `--integration`; without it, tests are
deselected. Offline tests in this directory run unconditionally.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from confluence_markdown_roundtrip.api import (
    ConfluenceClient,
    CredentialsError,
    VersionConflict,
    load_credentials,
)
from confluence_markdown_roundtrip import sentinels as S


FIXTURES_DIR = Path(__file__).parent / "fixtures"
PAGE_SPEC_PATH = FIXTURES_DIR / "page-spec.json"


def _live_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "confluence-markdown-roundtrip" / "live-tests.toml"


def _load_live_config() -> dict[str, Any]:
    p = _live_config_path()
    if not p.exists():
        pytest.skip(
            f"live-tests config not found: {p}. See live-tests.example.toml at the repo root."
        )
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        pytest.fail(f"{p} is not valid TOML: {e}")
    for key in ("root_page_id", "reference_page_id"):
        if key not in data:
            pytest.fail(f"{p} missing key: {key}")
    return data


# ---------------------------------------------------------------------------
# `--integration` flag + skip logic
# ---------------------------------------------------------------------------


def pytest_addoption(parser):
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="Run online integration tests against the live tenant.",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "online: test that requires the live tenant")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--integration"):
        return
    skip_online = pytest.mark.skip(reason="needs --integration")
    for item in items:
        if "online" in item.keywords:
            item.add_marker(skip_online)


# ---------------------------------------------------------------------------
# Page spec + templates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PageBootstrap:
    logical_name: str
    page_id: str
    expected_title: str
    template_path: Path
    comments: list[dict[str, Any]]  # [{slot, text_selection, body}]
    parent_logical: str | None


def _load_spec() -> dict[str, Any]:
    return json.loads(PAGE_SPEC_PATH.read_text(encoding="utf-8"))


def _norm_body(s: str) -> str:
    """Whitespace-normalize a comment body for stable matching across
    Confluence's possible reformatting on save."""
    return " ".join((s or "").split())


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def live_client() -> ConfluenceClient:
    """Single client for the entire session."""
    try:
        creds = load_credentials()
    except CredentialsError as e:
        pytest.skip(f"credentials unavailable: {e}")
    client = ConfluenceClient(creds)
    yield client
    client.close()


@pytest.fixture(scope="session")
def live_config() -> dict[str, Any]:
    return _load_live_config()


@pytest.fixture(scope="session")
def reference_page_id(live_config: dict[str, Any]) -> str:
    return str(live_config["reference_page_id"])


@pytest.fixture(scope="session")
def page_ids(live_client: ConfluenceClient, live_config: dict[str, Any]) -> dict[str, str]:
    """Resolve logical name -> page id via descendant walk of the root.

    Aborts the session if the tree shape doesn't match (suite never creates,
    deletes, or moves pages — see plan §"Hard constraint").
    """
    spec = _load_spec()
    root_id = str(live_config["root_page_id"])
    expected_titles = {name: page["expected_title"] for name, page in spec["pages"].items()}

    ids: dict[str, str] = {"root": root_id}
    descendants_by_id: dict[str, dict[str, Any]] = {}
    for d in live_client.list_descendants(root_id):
        descendants_by_id[d.id] = {"title": d.title, "parent_id": d.parent_id}

    # Match by title — the only stable identifier across runs (page IDs are
    # also stable but we want the suite to keep working after a UI rename
    # that bootstrap will repair).
    title_to_id = {info["title"]: pid for pid, info in descendants_by_id.items()}
    for logical, title in expected_titles.items():
        if logical == "root":
            continue
        pid = title_to_id.get(title)
        if pid is None:
            pytest.exit(
                f"page-spec.json expects a descendant titled {title!r} "
                f"under root {root_id}; not found. Descendants observed: "
                f"{[info['title'] for info in descendants_by_id.values()]}. "
                f"The suite cannot create pages — fix in the UI.",
                returncode=4,
            )
        ids[logical] = pid

    # Verify grandchild's parent is the right child.
    spec_pages = spec["pages"]
    for logical, page in spec_pages.items():
        parent_logical = page.get("parent_logical")
        if parent_logical:
            expected_parent_id = ids[parent_logical]
            actual_parent = descendants_by_id[ids[logical]]["parent_id"]
            if actual_parent != expected_parent_id:
                pytest.exit(
                    f"{logical}'s parent is {actual_parent}, expected {expected_parent_id}",
                    returncode=4,
                )
    return ids


@pytest.fixture(scope="session")
def page_bootstrap_plan(page_ids: dict[str, str]) -> dict[str, PageBootstrap]:
    spec = _load_spec()
    plan: dict[str, PageBootstrap] = {}
    for logical, page in spec["pages"].items():
        plan[logical] = PageBootstrap(
            logical_name=logical,
            page_id=page_ids[logical],
            expected_title=page["expected_title"],
            template_path=FIXTURES_DIR / page["template"],
            comments=page.get("comments", []),
            parent_logical=page.get("parent_logical"),
        )
    return plan


@pytest.fixture(scope="session")
def baselines(
    live_client: ConfluenceClient,
    page_bootstrap_plan: dict[str, PageBootstrap],
) -> dict[str, dict[str, Any]]:
    """Bootstrap all 4 pages and capture in-memory baselines.

    Returns a dict keyed by logical name. Each entry:
      {"page_id": str, "title": str, "storage_body": str, "version": int}
    """
    out: dict[str, dict[str, Any]] = {}
    for logical, plan in page_bootstrap_plan.items():
        baseline = _bootstrap_page(live_client, plan)
        out[logical] = baseline
    return out


def _bootstrap_page(client: ConfluenceClient, plan: PageBootstrap) -> dict[str, Any]:
    """Idempotent: reconcile comments, render template, PUT, capture baseline.

    Comments are additive — we never delete existing comments. If a slot's
    `text_selection` already has a comment in the page, we reuse its UUID
    rather than creating a duplicate.

    The fast path (everything already exists) is one PUT. The cold path
    (first-time setup) PUTs a scaffold once, creates the missing comments,
    then PUTs the final body. Worst case N+1 PUTs where N is the number
    of missing comments.
    """
    target_title = plan.expected_title
    template = plan.template_path.read_text(encoding="utf-8")
    page = client.get_page(plan.page_id)

    slot_to_uuid: dict[str, str] = {}
    missing: list[dict[str, Any]] = []
    if plan.comments:
        # The inline-comments listing returns text_selection='' for any
        # comment whose marker isn't currently in the page body (i.e. orphaned
        # by a previous PUT). Match by body content instead — it survives
        # marker churn and is per-spec unique.
        existing = list(client.list_inline_comments(plan.page_id))
        # Match by comment body content; values are the `marker_ref` UUID
        # used in `<ac:inline-comment-marker ac:ref="...">`. NOTE: this is
        # distinct from the comment's content `id` — the marker_ref lives
        # in `properties.inlineMarkerRef`.
        by_body: dict[str, str] = {
            _norm_body(c.body_storage): c.marker_ref for c in existing if c.marker_ref
        }
        for entry in plan.comments:
            key = _norm_body(entry["body"])
            if key in by_body:
                slot_to_uuid[entry["slot"]] = by_body[key]
            else:
                missing.append(entry)

    if missing:
        # PUT a scaffold once so `create_inline_comment` can anchor to text
        # that's present on the page (markers themselves are absent).
        scaffold = template
        for entry in plan.comments:
            anchor = entry["text_selection"]
            scaffold = scaffold.replace(
                f'<ac:inline-comment-marker ac:ref="{entry["slot"]}">{anchor}</ac:inline-comment-marker>',
                anchor,
            )
        page = client.update_page(
            plan.page_id, title=target_title, storage_body=scaffold, base_version=page.version
        )
        # Create the missing comments — order doesn't matter, anchors are unique.
        for entry in missing:
            ic = client.create_inline_comment(
                plan.page_id,
                body_storage=entry["body"],
                text_selection=entry["text_selection"],
            )
            slot_to_uuid[entry["slot"]] = ic.marker_ref

    body = template
    for slot, uuid in slot_to_uuid.items():
        body = body.replace(slot, uuid)
    if "__CM_SLOT_" in body:
        pytest.fail(f"unresolved comment slot in {plan.template_path}")

    updated = client.update_page(
        plan.page_id, title=target_title, storage_body=body, base_version=page.version
    )
    return {
        "page_id": plan.page_id,
        "title": updated.title,
        "storage_body": updated.storage_body,
        "version": updated.version,
        "slot_to_uuid": slot_to_uuid,
    }


# ---------------------------------------------------------------------------
# Per-test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def restore(live_client: ConfluenceClient, baselines: dict[str, dict[str, Any]]):
    """Function-scoped: PUT the recorded baseline back to a page.

    Use as `restore("root")` from inside a test that mutates the root page.
    Tests that don't mutate (e.g. category A identity) don't need to call it
    explicitly — but doing so is harmless (returns immediately if already
    at baseline).
    """

    def _restore(logical: str) -> dict[str, Any]:
        baseline = baselines[logical]
        # Confluence has a small lag between PUT and the version surfacing on
        # GET (notes.md §3 / Phase 2 finding #6). Two problems flow from it:
        #   (a) `restore`'s GET may return the pre-push version → its PUT 409s.
        #       Retry on conflict.
        #   (b) After `restore`'s PUT succeeds, a follow-up subprocess pull
        #       may still observe the OLD version, then push with that as
        #       base_version → server's "current" has already advanced → 409.
        #       Poll until the new version is visible.
        import time

        target_version: int | None = None
        for attempt in range(4):
            current = live_client.get_page_version(baseline["page_id"])
            try:
                updated = live_client.update_page(
                    baseline["page_id"],
                    title=baseline["title"],
                    storage_body=baseline["storage_body"],
                    base_version=current,
                )
                baseline["version"] = updated.version
                target_version = updated.version
                break
            except VersionConflict:
                if attempt == 3:
                    raise
                time.sleep(1.0 + attempt)

        # Poll until GET reports the version we just PUT.
        for _ in range(20):
            if live_client.get_page_version(baseline["page_id"]) >= target_version:
                break
            time.sleep(0.25)
        return baseline

    return _restore


@pytest.fixture
def make_workspace(tmp_path: Path) -> Callable[[str], Path]:
    """Return a callable `make_workspace(page_id_or_url) -> Path` that does
    a fresh `confluence-markdown-roundtrip pull <id> --into <tmp>` and returns the
    resulting page directory."""

    def _make(page_arg: str) -> Path:
        # Use the CLI so we exercise the same code path users do.
        out_dir = tmp_path / f"ws-{page_arg}"
        out_dir.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            [sys.executable, "-m", "confluence_markdown_roundtrip.cli", "pull", page_arg, "--into", str(out_dir)],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            pytest.fail(f"pull failed: {r.stderr}")
        # CLI prints the page directory path on success.
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        if not lines:
            pytest.fail(f"pull produced no path: {r.stdout!r}")
        return Path(lines[-1])

    return _make


@pytest.fixture
def push() -> Callable[[Path], subprocess.CompletedProcess]:
    """Return a callable that invokes `confluence-markdown-roundtrip push` and
    returns the CompletedProcess (so tests can inspect exit code + stderr)."""

    def _push(path: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "confluence_markdown_roundtrip.cli", "push", str(path)],
            capture_output=True,
            text=True,
        )

    return _push


@pytest.fixture
def re_pull(make_workspace) -> Callable[[str], Path]:
    """Alias for `make_workspace` used by tests that want a second pull
    after a push, to a different tmp dir."""
    return make_workspace
