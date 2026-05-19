"""Offline tests for sentinels.py — pure logic, no network.

Covers: marker encode/decode, identifier validation, slugify (incl.
collisions and Unicode), hash stability under bookkeeping-attr churn,
URL/id parsing, push-abort exception shape.
"""

from __future__ import annotations

import pytest
from lxml import etree

from confluence_markdown_roundtrip import sentinels as s


# ---------------------------------------------------------------------------
# Identifier shape
# ---------------------------------------------------------------------------


class TestIdentifierShape:
    def test_uuid_accepts_v4(self):
        assert s.is_uuid("194c349e-4b9c-45bd-b7a7-8cd9230aee1f")

    def test_uuid_rejects_short(self):
        assert not s.is_uuid("194c349e-4b9c-45bd-b7a7-8cd9230aee1")

    def test_uuid_rejects_non_hex(self):
        assert not s.is_uuid("194c349z-4b9c-45bd-b7a7-8cd9230aee1f")

    def test_uuid_rejects_no_dashes(self):
        assert not s.is_uuid("194c349e4b9c45bdb7a78cd9230aee1f")

    def test_hash_accepts_12_hex(self):
        assert s.is_hash("0123456789ab")

    def test_hash_rejects_uppercase(self):
        assert not s.is_hash("0123456789AB")

    def test_hash_rejects_wrong_length(self):
        assert not s.is_hash("0123456789a")
        assert not s.is_hash("0123456789abc")


# ---------------------------------------------------------------------------
# Marker regexes
# ---------------------------------------------------------------------------


UUID_X = "11111111-2222-3333-4444-555555555555"
UUID_Y = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
HASH_X = "deadbeefcafe"
HASH_Y = "0123456789ab"


class TestMarkerRegexes:
    def test_cm_open_matches(self):
        m = s.CM_OPEN_RE.search(f"prose <!--cm:{UUID_X}-->annotated text<!--/cm:{UUID_X}--> more")
        assert m is not None
        assert m.group(1) == UUID_X

    def test_cm_close_matches(self):
        m = s.CM_CLOSE_RE.search(f"<!--/cm:{UUID_X}-->")
        assert m is not None
        assert m.group(1) == UUID_X

    def test_cm_open_does_not_match_close(self):
        assert s.CM_OPEN_RE.fullmatch(f"<!--/cm:{UUID_X}-->") is None

    def test_cb_matches(self):
        m = s.CB_RE.search(f"prefix <!--cb:{HASH_X}--> suffix")
        assert m and m.group(1) == HASH_X

    def test_ci_trailer_matches(self):
        m = s.CI_TRAILER_RE.search(f"![alt](./x.png)<!--ci:{HASH_X}-->")
        assert m and m.group(1) == HASH_X

    def test_cc_matches(self):
        m = s.CC_RE.search(f"```python\nbody\n```\n<!--cc:{UUID_X}-->")
        assert m and m.group(1) == UUID_X

    def test_ct_matches(self):
        m = s.CT_RE.search(f"- <!--ct:{UUID_X}--> task text")
        assert m and m.group(1) == UUID_X

    def test_ci_span_matches(self):
        m = s.CI_SPAN_RE.search(f'before <span data-ci="{HASH_X}">[@mention]</span> after')
        assert m and m.group(1) == HASH_X and m.group(2) == "[@mention]"

    def test_ci_span_multiline_visible(self):
        # CI span visible text may contain newlines in pathological cases
        m = s.CI_SPAN_RE.search(f'<span data-ci="{HASH_X}">line1\nline2</span>')
        assert m and m.group(2) == "line1\nline2"


# ---------------------------------------------------------------------------
# Encoders
# ---------------------------------------------------------------------------


class TestEncoders:
    def test_cm_open_roundtrip(self):
        assert s.cm_open(UUID_X) == f"<!--cm:{UUID_X}-->"

    def test_cm_close_roundtrip(self):
        assert s.cm_close(UUID_X) == f"<!--/cm:{UUID_X}-->"

    def test_cb_roundtrip(self):
        assert s.cb(HASH_X) == f"<!--cb:{HASH_X}-->"

    def test_ci_span_roundtrip(self):
        assert s.ci_span(HASH_X, "[@mention]") == f'<span data-ci="{HASH_X}">[@mention]</span>'

    def test_encoder_rejects_bad_uuid(self):
        with pytest.raises(s.PushAbort) as exc:
            s.cm_open("not-a-uuid")
        assert exc.value.rule_id == "bad-marker-syntax"

    def test_encoder_rejects_bad_hash(self):
        with pytest.raises(s.PushAbort) as exc:
            s.cb("DEADBEEF")  # uppercase forbidden
        assert exc.value.rule_id == "bad-marker-syntax"


# ---------------------------------------------------------------------------
# Slugify
# ---------------------------------------------------------------------------


class TestSlugify:
    @pytest.mark.parametrize(
        "title, expected",
        [
            ("Hello World", "hello-world"),
            ("Already-slugged", "already-slugged"),
            ("  whitespace edges  ", "whitespace-edges"),
            ("punct!!!everywhere???", "punct-everywhere"),
            ("Numbers 123 in title", "numbers-123-in-title"),
            ("Mixed CASE and CaSe", "mixed-case-and-case"),
            ("é-Café-naïve-résumé", "e-cafe-naive-resume"),  # diacritic stripping
            ("中文 mixed", "mixed"),  # CJK has no NFKD decomposition; drops
            ("", "page"),
            ("!!!", "page"),
            ("---", "page"),
        ],
    )
    def test_slugify_table(self, title, expected):
        assert s.slugify(title) == expected

    def test_slugify_truncates_at_60_on_dash(self):
        # 70 chars of "a-" pattern; should cut at last dash <= 60
        title = "word " * 20  # "word word word ..." → "word-word-word-..."
        out = s.slugify(title)
        assert len(out) <= 60
        assert not out.endswith("-")
        # must be a prefix of the full untrimmed slug
        full = "-".join(["word"] * 20)
        assert full.startswith(out)

    def test_slugify_hard_truncates_when_no_dash(self):
        title = "x" * 100
        out = s.slugify(title)
        assert out == "x" * 60

    def test_slugify_unique_no_collision(self):
        assert s.slugify_unique("Hello", set()) == "hello"

    def test_slugify_unique_collision_appends_2(self):
        assert s.slugify_unique("Hello", {"hello"}) == "hello-2"

    def test_slugify_unique_collision_walks(self):
        assert s.slugify_unique("Hello", {"hello", "hello-2", "hello-3"}) == "hello-4"


# ---------------------------------------------------------------------------
# Hash + canonical XML
# ---------------------------------------------------------------------------


NS = "xmlns:ac='http://atlassian.com/content' xmlns:ri='http://atlassian.com/resource/identifier'"


class TestHashStability:
    def test_strip_bookkeeping_removes_local_id_variants(self):
        xml = f"""<root {NS}>
          <ac:structured-macro ac:local-id="abc123" ac:macro-id="def456" local-id="ghi789">
            <ac:parameter ac:name="x">v</ac:parameter>
          </ac:structured-macro>
        </root>"""
        el = etree.fromstring(xml)
        stripped = s.strip_bookkeeping(el)
        macro = stripped[0]
        assert "local-id" not in macro.attrib
        assert "{http://atlassian.com/content}local-id" not in macro.attrib
        assert "{http://atlassian.com/content}macro-id" not in macro.attrib

    def test_strip_bookkeeping_keeps_payload_attrs(self):
        xml = f"""<root {NS}>
          <ac:image ac:align="center" ac:local-id="bk">
            <ri:attachment ri:filename="x.png" ri:version-at-save="2"/>
          </ac:image>
        </root>"""
        el = etree.fromstring(xml)
        stripped = s.strip_bookkeeping(el)
        img = stripped[0]
        assert img.get("{http://atlassian.com/content}align") == "center"
        att = img[0]
        assert att.get("{http://atlassian.com/resource/identifier}filename") == "x.png"
        assert att.get("{http://atlassian.com/resource/identifier}version-at-save") == "2"

    def test_hash_stable_under_local_id_churn(self):
        a = etree.fromstring(
            f"""<m {NS} ac:local-id="abc" ac:macro-id="111"><p>x</p></m>"""
        )
        b = etree.fromstring(
            f"""<m {NS} ac:local-id="xyz" ac:macro-id="999"><p>x</p></m>"""
        )
        assert s.hash_xml(a) == s.hash_xml(b)

    def test_hash_changes_when_content_changes(self):
        a = etree.fromstring(f"""<m {NS}><p>x</p></m>""")
        b = etree.fromstring(f"""<m {NS}><p>y</p></m>""")
        assert s.hash_xml(a) != s.hash_xml(b)

    def test_hash_is_12_hex(self):
        el = etree.fromstring(f"""<m {NS}><p>hello</p></m>""")
        h = s.hash_xml(el)
        assert s.is_hash(h)

    def test_strip_does_not_mutate_input(self):
        xml = f"""<m {NS} ac:local-id="abc"><p>x</p></m>"""
        el = etree.fromstring(xml)
        s.strip_bookkeeping(el)
        assert el.get("{http://atlassian.com/content}local-id") == "abc"


# ---------------------------------------------------------------------------
# Page-id from arg
# ---------------------------------------------------------------------------


class TestPageIdFromArg:
    def test_bare_int(self):
        assert s.page_id_from_arg("1234567890") == 1234567890

    def test_pages_url(self):
        url = "https://example.atlassian.net/wiki/spaces/TEST/pages/1234567890/RoundTrip+Test"
        assert s.page_id_from_arg(url) == 1234567890

    def test_pages_url_trailing_slash(self):
        url = "https://x.atlassian.net/wiki/spaces/K/pages/12345/"
        assert s.page_id_from_arg(url) == 12345

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            s.page_id_from_arg("not a url or an id")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            s.page_id_from_arg("")


# ---------------------------------------------------------------------------
# PushAbort shape
# ---------------------------------------------------------------------------


class TestPushAbort:
    def test_str_includes_rule_id_and_location(self):
        a = s.PushAbort("unmatched-cm", "open without close", file="index.md", line=42)
        assert "index.md:42" in str(a)
        assert "unmatched-cm" in str(a)
        assert "open without close" in str(a)

    def test_factory_rule_ids(self):
        assert s.unmatched_cm("x").rule_id == "unmatched-cm"
        assert s.unknown_cb_hash(HASH_X).rule_id == "unknown-cb-hash"
        assert s.unknown_ci_hash(HASH_X).rule_id == "unknown-ci-hash"
        assert s.new_attachment("./x.png").rule_id == "new-attachment"
        assert s.missing_h1().rule_id == "missing-h1"
        assert s.bad_marker_syntax("x").rule_id == "bad-marker-syntax"
        assert s.version_conflict(1, 2).rule_id == "version-conflict"
        assert s.orig_tampered("x").rule_id == "orig-tampered"
        assert s.meta_tampered("x").rule_id == "meta-tampered"
