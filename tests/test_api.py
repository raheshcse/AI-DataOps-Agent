import logging

from app import __version__
from app.core.logging import RedactionFilter, redact
from app.main import app
from tests.conftest import FAKE_KEY


def test_health_returns_expected_body(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {
        "status": "ok",
        "service": "AI DataOps Agent",
        "version": __version__,
    }


def test_health_works_even_when_configuration_is_invalid(client, monkeypatch):
    monkeypatch.setenv("API_PORT", "bogus")
    assert client.get("/health").status_code == 200


def test_ready_with_valid_configuration(client, with_key):
    r = client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"configuration": "valid", "openai_api_key": "configured"}
    assert body["environment"] == "development"


def test_ready_reports_missing_api_key(client):
    r = client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["openai_api_key"] == "missing"


def test_ready_blank_api_key_is_not_ready(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    assert client.get("/ready").status_code == 503


def test_ready_reports_invalid_configuration_without_echoing_values(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    monkeypatch.setenv("API_PORT", "super-secret-looking-value")
    r = client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["invalid_fields"] == ["api_port"]
    assert "super-secret-looking-value" not in r.text
    assert FAKE_KEY not in r.text


def test_secret_never_returned_by_any_endpoint(client, with_key):
    for path in ("/health", "/ready", "/openapi.json", "/docs", "/does-not-exist"):
        assert FAKE_KEY not in client.get(path).text, path


def test_unknown_route_returns_404(client):
    assert client.get("/nope").status_code == 404


def test_unhandled_error_returns_generic_500(client, monkeypatch):
    import app.main as main

    def boom():
        raise RuntimeError(f"leaky detail {FAKE_KEY}")

    monkeypatch.setattr(main, "get_settings", boom)
    r = client.get("/ready")
    assert r.status_code == 500
    assert r.json() == {"detail": "Internal server error"}
    assert FAKE_KEY not in r.text


def test_app_metadata():
    assert app.title == "AI DataOps Agent"
    assert app.version == __version__


def test_redact_masks_keys_and_tokens():
    assert "sk-abcdefgh12345678" not in redact("key is sk-abcdefgh12345678 ok")
    assert "hunter2" not in redact("password=hunter2")
    assert "abc.def" not in redact("Authorization: Bearer abc.def")
    assert redact("plain message") == "plain message"


def test_request_log_contains_path_but_not_query_secrets(client, caplog):
    caplog.set_level(logging.INFO, logger="app.request")
    client.get("/health?api_key=sk-abcdefgh12345678")
    text = caplog.text
    assert "GET /health -> 200" in text
    assert "sk-abcdefgh12345678" not in text
    assert "api_key" not in text


def test_redaction_filter_scrubs_log_records():
    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "token=%s", ("sk-abcdefgh12345678",), None
    )
    RedactionFilter().filter(record)
    assert "sk-abcdefgh12345678" not in record.getMessage()


def test_configure_logging_redacts_uvicorn_access_log():
    """Uses uvicorn's real AccessFormatter: redaction must not break its formatting."""
    from uvicorn.logging import AccessFormatter

    from app.core.logging import configure_logging

    configure_logging("INFO")
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/health?api_key=sk-abcdefgh12345678", "1.1", 200), None,
    )
    for f in logging.getLogger("uvicorn.access").filters:
        f.filter(record)
    formatted = AccessFormatter("%(client_addr)s %(request_line)s %(status_code)s").format(record)
    assert "sk-abcdefgh12345678" not in formatted
    assert "/health" in formatted
