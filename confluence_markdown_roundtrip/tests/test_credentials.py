"""Tests for credential loading — mode check + parse.

The token never enters argv or env. We verify the 0600 mode rule fires,
the TOML parse error is surfaced, missing keys are caught, and `__repr__`
never leaks the token (regression test for the load-bearing rule in
confluence/CLAUDE.md §"Token-handling protocol").
"""

from __future__ import annotations

import os
import stat

import pytest

from confluence_markdown_roundtrip.api import Credentials, CredentialsError, load_credentials


def _write_creds(tmp_path, content: str, mode: int = 0o600):
    p = tmp_path / "credentials.toml"
    p.write_text(content, encoding="utf-8")
    os.chmod(p, mode)
    return p


class TestLoadCredentials:
    def test_happy_path(self, tmp_path):
        p = _write_creds(
            tmp_path,
            'base_url = "https://x.atlassian.net"\nemail = "u@x"\napi_token = "secret"\n',
        )
        c = load_credentials(p)
        assert c.base_url == "https://x.atlassian.net"
        assert c.email == "u@x"
        assert c.api_token == "secret"

    def test_trailing_slash_stripped(self, tmp_path):
        p = _write_creds(
            tmp_path,
            'base_url = "https://x.atlassian.net/"\nemail = "u@x"\napi_token = "s"\n',
        )
        c = load_credentials(p)
        assert c.base_url == "https://x.atlassian.net"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(CredentialsError, match="not found"):
            load_credentials(tmp_path / "nope.toml")

    def test_too_permissive_raises(self, tmp_path):
        p = _write_creds(
            tmp_path,
            'base_url = "https://x"\nemail = "u"\napi_token = "s"\n',
            mode=0o644,
        )
        with pytest.raises(CredentialsError, match="permissive"):
            load_credentials(p)

    def test_group_read_also_too_permissive(self, tmp_path):
        p = _write_creds(
            tmp_path,
            'base_url = "https://x"\nemail = "u"\napi_token = "s"\n',
            mode=0o640,
        )
        with pytest.raises(CredentialsError):
            load_credentials(p)

    def test_invalid_toml_raises(self, tmp_path):
        p = _write_creds(tmp_path, "this is = not valid TOML = at all\n")
        with pytest.raises(CredentialsError, match="not valid TOML"):
            load_credentials(p)

    def test_missing_key_raises(self, tmp_path):
        p = _write_creds(tmp_path, 'base_url = "https://x"\nemail = "u"\n')
        with pytest.raises(CredentialsError, match="missing keys"):
            load_credentials(p)


class TestRedaction:
    def test_repr_never_shows_token(self):
        c = Credentials(base_url="https://x", email="u@x", api_token="VERY_SECRET_TOKEN")
        r = repr(c)
        assert "VERY_SECRET_TOKEN" not in r
        assert "***" in r

    def test_token_not_in_exception_message_for_permissive_file(self, tmp_path):
        p = _write_creds(
            tmp_path,
            'base_url = "https://x"\nemail = "u"\napi_token = "MUST_NOT_LEAK"\n',
            mode=0o644,
        )
        with pytest.raises(CredentialsError) as exc:
            load_credentials(p)
        assert "MUST_NOT_LEAK" not in str(exc.value)
