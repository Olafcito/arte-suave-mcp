"""Identity resolution: legacy secret + self-identifying per-user tokens."""

import pytest

from arte_suave_mcp import creds


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # deterministic legacy secret; no AWS
    monkeypatch.delenv("ARTESUAVE_MCP_SECRET_PARAM", raising=False)
    monkeypatch.setenv("ARTESUAVE_MCP_SECRET", "legacy-secret-xyz")
    creds._identity_cache.clear()
    # fake user store: only "anders" exists, token secret "goodsecret"
    users = {("anders", "token"): "goodsecret"}
    monkeypatch.setattr(creds, "_user_param", lambda uid, field: users.get((uid, field)))
    yield
    creds._identity_cache.clear()


def test_legacy_secret_maps_to_default_user():
    assert creds.resolve_identity("legacy-secret-xyz") == creds.DEFAULT_USER


def test_valid_per_user_token():
    assert creds.resolve_identity("anders.goodsecret") == "anders"


def test_wrong_secret_rejected():
    assert creds.resolve_identity("anders.WRONG") is None


def test_unknown_user_rejected():
    assert creds.resolve_identity("mallory.whatever") is None


@pytest.mark.parametrize("bad", ["", "nope", "no-dot-here", "Anders.goodsecret", ".goodsecret"])
def test_malformed_tokens_rejected(bad):
    assert creds.resolve_identity(bad) is None


def test_cache_returns_same_result():
    # second call is served from cache (still correct)
    assert creds.resolve_identity("anders.goodsecret") == "anders"
    assert creds.resolve_identity("anders.goodsecret") == "anders"
