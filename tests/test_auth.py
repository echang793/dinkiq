"""Auth model.

Before: the password was skipped whenever the *Host header* said localhost --
a header any client controls -- and the server listened on 0.0.0.0, so anyone
on the LAN/tailnet could send `Host: 127.0.0.1` and get full read/write/delete
access (it worked from 192.168.x.x, and the router was already probing the
port).

Now a request is trusted without the password only when it is
  (a) from the reverse proxy: carries the shared secret Caddy injects, or
  (b) genuinely local: loopback peer, no forwarding headers, loopback Host
      (the Host check keeps DNS-rebinding pages out).
Everything else needs the Basic password. The server also binds loopback.
"""

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

import server

PASSWORD = "correct horse"
SECRET = "proxy-secret-for-tests"
LOOPBACK = ("127.0.0.1", 50000)
LAN = ("192.168.1.50", 50000)
FUNNEL_HOST = "box.tail1234.ts.net"


@pytest.fixture(autouse=True)
def _auth_on(monkeypatch):
    monkeypatch.setattr(server, "_DINKIQ_PASSWORD", PASSWORD)
    monkeypatch.setattr(server, "_DINKIQ_PASSWORD_BYTES", PASSWORD.encode())
    monkeypatch.setattr(server, "_PROXY_SECRET_BYTES", SECRET.encode(), raising=False)


def _get(peer, headers=None, path="/api/sessions"):
    return TestClient(server.app, client=peer).get(path, headers=headers or {})


def _basic(pw, user="eric"):
    return {"authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_spoofed_localhost_host_from_lan_is_rejected():
    """The actual hole: a LAN client choosing its own Host header."""
    for host in ("127.0.0.1:8100", "localhost", "127.0.0.1", ""):
        r = _get(LAN, {"host": host})
        assert r.status_code == 401, f"Host={host!r} bypassed auth"


def test_no_credentials_from_lan_is_rejected():
    assert _get(LAN, {"host": "192.168.1.246:8100"}).status_code == 401


def test_genuine_local_request_needs_no_password():
    assert _get(LOOPBACK, {"host": "127.0.0.1:8100"}).status_code == 200
    assert _get(LOOPBACK, {"host": "localhost:8100"}).status_code == 200


def test_loopback_with_foreign_host_is_rejected():
    """DNS rebinding: a web page resolving its own name to 127.0.0.1."""
    assert _get(LOOPBACK, {"host": "evil.example"}).status_code == 401


def test_proxied_request_without_secret_is_rejected():
    """Anything that came through a proxy carries forwarding headers and must
    not ride the loopback exemption."""
    for h in ({"x-forwarded-for": "203.0.113.9"}, {"x-forwarded-host": FUNNEL_HOST},
              {"forwarded": "for=203.0.113.9"}):
        r = _get(LOOPBACK, {"host": "127.0.0.1:8100", **h})
        assert r.status_code == 401, h


def test_valid_proxy_secret_is_trusted():
    r = _get(LOOPBACK, {"host": FUNNEL_HOST, "x-forwarded-for": "203.0.113.9",
                        "x-dinkiq-proxy": SECRET})
    assert r.status_code == 200


def test_wrong_or_empty_proxy_secret_is_rejected():
    for bad in ("nope", "", SECRET + "x"):
        r = _get(LOOPBACK, {"host": FUNNEL_HOST, "x-forwarded-for": "203.0.113.9",
                            "x-dinkiq-proxy": bad})
        assert r.status_code == 401, repr(bad)


def test_proxy_secret_is_ignored_when_none_configured(monkeypatch):
    """Unset secret must never be satisfiable by sending an empty header."""
    monkeypatch.setattr(server, "_PROXY_SECRET_BYTES", None)
    r = _get(LOOPBACK, {"host": FUNNEL_HOST, "x-forwarded-for": "1.2.3.4", "x-dinkiq-proxy": ""})
    assert r.status_code == 401


def test_basic_password_still_works_remotely():
    assert _get(LAN, {"host": FUNNEL_HOST, **_basic(PASSWORD)}).status_code == 200
    assert _get(LAN, {"host": FUNNEL_HOST, **_basic("wrong")}).status_code == 401
    assert _get(LAN, {"host": FUNNEL_HOST, **_basic("")}).status_code == 401


def test_401_asks_for_basic_credentials():
    r = _get(LAN, {"host": FUNNEL_HOST})
    assert r.headers["www-authenticate"].startswith("Basic")


def test_malformed_authorization_header_is_rejected_not_500():
    for bad in ("Basic !!!notbase64", "Basic ", "Bearer abc", "Basic " + base64.b64encode(b"nocolon").decode()):
        assert _get(LAN, {"host": FUNNEL_HOST, "authorization": bad}).status_code == 401, bad


def test_no_password_configured_means_open(monkeypatch):
    """Dev mode with no DINKIQ_PASSWORD stays open (unchanged behaviour)."""
    monkeypatch.setattr(server, "_DINKIQ_PASSWORD", None)
    monkeypatch.setattr(server, "_DINKIQ_PASSWORD_BYTES", None)
    assert _get(LAN, {"host": FUNNEL_HOST}).status_code == 200


def test_server_binds_loopback_by_default():
    assert server.DEFAULT_HOST == "127.0.0.1"
