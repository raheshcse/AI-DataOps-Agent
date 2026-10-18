import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings


def test_defaults_without_environment():
    s = Settings()
    assert s.openai_api_key is None
    assert not s.openai_configured
    assert s.openai_model == "gpt-4o-mini"
    assert s.app_env == "development"
    assert s.log_level == "INFO"
    assert s.api_host == "127.0.0.1"
    assert s.api_port == 8000


def test_values_load_from_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-abcdefgh12345678")
    monkeypatch.setenv("OPENAI_MODEL", "some-model")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("LOG_LEVEL", "debug")  # case-insensitive input
    monkeypatch.setenv("API_HOST", "0.0.0.0")
    monkeypatch.setenv("API_PORT", "9001")
    s = Settings()
    assert s.openai_configured
    assert s.openai_api_key.get_secret_value() == "sk-test-abcdefgh12345678"
    assert s.openai_model == "some-model"
    assert s.app_env == "test"
    assert s.log_level == "DEBUG"
    assert s.api_host == "0.0.0.0"
    assert s.api_port == 9001


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_api_key_counts_as_missing(monkeypatch, blank):
    monkeypatch.setenv("OPENAI_API_KEY", blank)
    assert not Settings().openai_configured


def test_secret_is_masked_in_repr_and_str(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-abcdefgh12345678")
    s = Settings()
    assert "abcdefgh12345678" not in repr(s)
    assert "abcdefgh12345678" not in str(s)
    assert "abcdefgh12345678" not in s.model_dump_json()


@pytest.mark.parametrize(
    "name,value",
    [
        ("API_PORT", "0"),
        ("API_PORT", "70000"),
        ("API_PORT", "not-a-number"),
        ("LOG_LEVEL", "LOUD"),
        ("APP_ENV", "staging"),
        ("OPENAI_MODEL", ""),
    ],
)
def test_invalid_values_are_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings()


def test_dotenv_file_is_read_from_working_directory(tmp_path):
    (tmp_path / ".env").write_text("APP_ENV=production\nAPI_PORT=8123\n")
    s = Settings()
    assert s.app_env == "production"
    assert s.api_port == 8123


def test_environment_overrides_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("API_PORT=8123\n")
    monkeypatch.setenv("API_PORT", "8222")
    assert Settings().api_port == 8222


def test_get_settings_is_cached(monkeypatch):
    first = get_settings()
    monkeypatch.setenv("API_PORT", "9999")
    assert get_settings() is first
    get_settings.cache_clear()
    assert get_settings().api_port == 9999
