"""OAuth server logic: PKCE, code/token lifecycle, discovery metadata.

The DynamoDB table is faked in-memory so nothing touches AWS.
"""

import base64
import hashlib

import pytest

from arte_suave_mcp import creds, oauth


class FakeTable:
    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        item = self.items.get(Key["pk"])
        return {"Item": item} if item else {}

    def put_item(self, Item):
        self.items[Item["pk"]] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(Key["pk"], None)


@pytest.fixture(autouse=True)
def _fake_table(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(oauth, "_table", lambda: table)
    oauth._token_cache.clear()
    yield table
    oauth._token_cache.clear()


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def test_user_id_is_deterministic_and_valid():
    a = oauth.user_id_for("Foo@Example.com ")
    b = oauth.user_id_for("foo@example.com")
    assert a == b
    assert creds._UID_RE.match(a)


def test_pkce_roundtrip():
    verifier = "abc123~verifier_string_long_enough"
    assert oauth.verify_pkce(verifier, _challenge(verifier))
    assert not oauth.verify_pkce("wrong", _challenge(verifier))
    assert not oauth.verify_pkce("", "")


def test_client_registration_roundtrip():
    reg = oauth.register_client(["https://claude.ai/cb"], "Claude")
    assert reg["client_id"].startswith(oauth.CLIENT_PREFIX)
    stored = oauth.get_client(reg["client_id"])
    assert stored["redirect_uris"] == ["https://claude.ai/cb"]
    assert oauth.get_client("nope") is None


def test_authorization_code_is_single_use():
    code = oauth.create_code("cid", "https://cb", "chal", "u123")
    first = oauth.consume_code(code)
    assert first and first["user_id"] == "u123"
    assert oauth.consume_code(code) is None  # already consumed


def test_expired_code_rejected(monkeypatch):
    code = oauth.create_code("cid", "https://cb", "chal", "u123")
    monkeypatch.setattr(oauth, "_now", lambda: 9_999_999_999)
    assert oauth.consume_code(code) is None


def test_access_token_resolves_to_user():
    access, refresh, ttl = oauth.issue_tokens("u456")
    assert access.startswith(oauth.ACCESS_PREFIX)
    assert ttl == oauth.ACCESS_TTL
    assert oauth.resolve_access_token(access) == "u456"
    assert oauth.resolve_access_token("asm_at_bogus") is None
    assert oauth.resolve_access_token("not-ours") is None


def test_refresh_issues_new_access_token():
    _, refresh, _ = oauth.issue_tokens("u789")
    result = oauth.refresh_access(refresh)
    assert result is not None
    new_access, ttl = result
    assert oauth.resolve_access_token(new_access) == "u789"
    assert oauth.refresh_access("asm_rt_bogus") is None


def test_metadata_shapes():
    base = "https://x.example.com"
    md = oauth._as_metadata(base)
    assert md["authorization_endpoint"] == f"{base}/authorize"
    assert md["code_challenge_methods_supported"] == ["S256"]
    pr = oauth._pr_metadata(base)
    assert pr["resource"] == f"{base}/mcp"
    assert pr["authorization_servers"] == [base]


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/.well-known/oauth-authorization-server", True),
        ("/.well-known/oauth-protected-resource/mcp", True),
        ("/.well-known/openid-configuration", True),
        ("/authorize", True),
        ("/token", True),
        ("/register", True),
        ("/mcp", False),
        ("/", False),
    ],
)
def test_is_oauth_path(path, expected):
    assert oauth.is_oauth_path(path) is expected


def test_login_page_escapes_params():
    page = oauth._login_page({"state": '"><script>x</script>', "client_id": "c"})
    assert "<script>x" not in page
    assert "&lt;script&gt;" in page
