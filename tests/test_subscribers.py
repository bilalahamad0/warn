from unittest.mock import patch, MagicMock
import warn_subscribers


def _clear_env(monkeypatch):
    for k in ("SUBSCRIBERS_ENDPOINT", "SIGNUP_ENDPOINT", "SUBSCRIBERS_TOKEN"):
        monkeypatch.delenv(k, raising=False)


def test_get_subscribers_unconfigured(monkeypatch):
    _clear_env(monkeypatch)
    assert warn_subscribers.get_subscribers() == []


@patch("warn_subscribers.requests.get")
def test_get_subscribers_missing_token_skips_request(mock_get, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SIGNUP_ENDPOINT", "https://example.com/exec")
    # No token -> should not even hit the network.
    assert warn_subscribers.get_subscribers() == []
    assert not mock_get.called


@patch("warn_subscribers.requests.get")
def test_get_subscribers_parses_and_dedupes(mock_get, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SUBSCRIBERS_ENDPOINT", "https://example.com/exec")
    monkeypatch.setenv("SUBSCRIBERS_TOKEN", "secret")

    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {
        "ok": True,
        "subscribers": [
            {"email": "A@Example.com"},
            {"email": "a@example.com"},   # case-insensitive duplicate
            {"email": "b@example.com"},
            {"email": ""},                # skipped
            {"email": "not-an-email"},    # skipped (no @)
        ],
    }
    mock_get.return_value = mock_resp

    subs = warn_subscribers.get_subscribers()
    assert subs == ["a@example.com", "b@example.com"]
    # Token must be passed through as a query param.
    _, kwargs = mock_get.call_args
    assert kwargs["params"] == {"token": "secret"}


@patch("warn_subscribers.requests.get")
def test_get_subscribers_handles_error(mock_get, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SIGNUP_ENDPOINT", "https://example.com/exec")
    monkeypatch.setenv("SUBSCRIBERS_TOKEN", "secret")
    mock_get.side_effect = Exception("boom")
    assert warn_subscribers.get_subscribers() == []


@patch("warn_subscribers.requests.get")
def test_get_subscribers_not_ok_payload(mock_get, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SIGNUP_ENDPOINT", "https://example.com/exec")
    monkeypatch.setenv("SUBSCRIBERS_TOKEN", "secret")
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"ok": False, "error": "forbidden"}
    mock_get.return_value = mock_resp
    assert warn_subscribers.get_subscribers() == []
