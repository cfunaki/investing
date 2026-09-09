"""
Regression tests for the Secret Manager version leak.

_persist_refreshed_token() used to call add_secret_version() on every Gmail
token refresh. Access tokens expire hourly, so this added ~720 versions/month.
Every enabled version bills $0.06/month for as long as it exists and nothing
removed them, so the cost compounded: 697 versions had accumulated by
2026-09-08, costing ~$42/month and rising ~$43/month.

Secret Manager only needs the refresh_token (it bootstraps cold instances), and
the refresh_token rarely rotates.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("google.oauth2", reason="google auth libs not installed locally")

from src.signals.email_monitor import GmailMonitor  # noqa: E402


def _creds(refresh_token: str):
    c = SimpleNamespace(refresh_token=refresh_token)
    c.to_json = lambda: json.dumps({"refresh_token": refresh_token, "token": "access-xyz"})
    return c


@pytest.fixture
def monitor():
    return GmailMonitor.__new__(GmailMonitor)


def test_no_secret_write_when_refresh_token_unchanged(monitor):
    """The hourly access-token refresh must not create a new secret version."""
    stored = json.dumps({"refresh_token": "SAME", "token": "access-old"})
    client = MagicMock()

    with patch.object(monitor, "_load_latest_token_from_secret_manager", return_value=stored), \
         patch("google.cloud.secretmanager.SecretManagerServiceClient", return_value=client), \
         patch.object(monitor, "_prune_secret_versions"):
        monitor._persist_refreshed_token(_creds("SAME"))

    client.add_secret_version.assert_not_called()


def test_secret_written_when_refresh_token_rotates(monitor):
    """A genuine refresh_token rotation must still be persisted."""
    stored = json.dumps({"refresh_token": "OLD", "token": "access-old"})
    client = MagicMock()

    with patch.object(monitor, "_load_latest_token_from_secret_manager", return_value=stored), \
         patch("google.cloud.secretmanager.SecretManagerServiceClient", return_value=client), \
         patch.object(monitor, "_prune_secret_versions"):
        monitor._persist_refreshed_token(_creds("NEW"))

    client.add_secret_version.assert_called_once()
    payload = client.add_secret_version.call_args.kwargs["request"]["payload"]["data"]
    assert json.loads(payload.decode())["refresh_token"] == "NEW"


def test_prune_keeps_only_newest_versions():
    """Backstop: pruning destroys everything past `keep`, newest first."""
    client = MagicMock()
    client.list_secret_versions.return_value = [
        SimpleNamespace(name=f"v{i}", state=SimpleNamespace(name="ENABLED")) for i in range(10)
    ]

    GmailMonitor._prune_secret_versions(client, "projects/p/secrets/S", keep=3)

    destroyed = [c.kwargs["request"]["name"] for c in client.destroy_secret_version.call_args_list]
    assert destroyed == [f"v{i}" for i in range(3, 10)]


def test_prune_noop_when_under_limit():
    client = MagicMock()
    client.list_secret_versions.return_value = [
        SimpleNamespace(name=f"v{i}", state=SimpleNamespace(name="ENABLED")) for i in range(2)
    ]

    GmailMonitor._prune_secret_versions(client, "projects/p/secrets/S", keep=5)

    client.destroy_secret_version.assert_not_called()
