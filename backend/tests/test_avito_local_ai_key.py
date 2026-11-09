from types import SimpleNamespace
import subprocess
import pytest
from app.avito import local_ai_key


@pytest.fixture(autouse=True)
def isolated_private_file(monkeypatch, tmp_path):
    monkeypatch.setattr(local_ai_key, "LOCAL_KEY_FILE", tmp_path / ".env.avito-ai")


def settings(environment="local", key=None):
    return SimpleNamespace(environment=environment, openai_api_key=key)


def test_environment_key_takes_priority(monkeypatch):
    monkeypatch.setattr(local_ai_key.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("No keychain lookup"))
    assert local_ai_key.avito_ai_key(settings(key="synthetic-env")) == "synthetic-env"


@pytest.mark.parametrize("environment,platform", [("production", "darwin"), ("staging", "darwin"), ("local", "linux")])
def test_local_key_never_used_in_production_or_other_platform(monkeypatch, environment, platform):
    monkeypatch.setattr(local_ai_key.sys, "platform", platform)
    monkeypatch.setattr(local_ai_key.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("No keychain lookup"))
    assert local_ai_key.avito_ai_key(settings(environment)) is None


@pytest.mark.parametrize("code,value", [(0, "sk-" + "synthetic" * 10), (44, ""), (0, "not-a-key"), (0, "sk-" + "x" * 600)])
def test_lookup_is_captured_bounded_and_validated(monkeypatch, code, value, capsys):
    monkeypatch.setattr(local_ai_key.sys, "platform", "darwin")
    calls = []
    def fake(args, **kwargs):
        calls.append((args, kwargs)); return SimpleNamespace(returncode=code, stdout=value, stderr="synthetic")
    monkeypatch.setattr(local_ai_key.subprocess, "run", fake)
    assert local_ai_key.avito_ai_key(settings()) == (value if code == 0 and 40 <= len(value) <= 512 and value.startswith("sk-") else None)
    assert calls[0][1]["capture_output"] and calls[0][1]["timeout"] == 10
    assert value not in calls[0][0] if value else True
    assert capsys.readouterr().out == ""


def test_timeout_does_not_leak_errors(monkeypatch):
    monkeypatch.setattr(local_ai_key.sys, "platform", "darwin")
    def fail(*_args, **_kwargs): raise subprocess.TimeoutExpired("synthetic", 10, output="private")
    monkeypatch.setattr(local_ai_key.subprocess, "run", fail)
    assert local_ai_key.avito_ai_key(settings()) is None


def test_private_local_file_read_without_keychain(monkeypatch, capsys):
    monkeypatch.setattr(local_ai_key.sys, "platform", "darwin")
    path = local_ai_key.LOCAL_KEY_FILE
    value = "sk-" + "synthetic" * 10
    path.write_text("# local only\nOPENAI_API_KEY=" + value + "\n")
    path.chmod(0o600)
    monkeypatch.setattr(local_ai_key.subprocess, "run", lambda *_a, **_k: pytest.fail("No Keychain read"))
    assert local_ai_key.avito_ai_key(settings()) == value
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("environment,platform", [("production", "darwin"), ("staging", "darwin"), ("local", "linux")])
def test_file_never_used_outside_local_mac(monkeypatch, environment, platform):
    monkeypatch.setattr(local_ai_key.sys, "platform", platform)
    monkeypatch.setattr(local_ai_key, "_local_file_key", lambda: pytest.fail("Must not read local file"))
    assert local_ai_key.avito_ai_key(settings(environment)) is None


def test_file_requires_private_permissions_and_unique_setting():
    path = local_ai_key.LOCAL_KEY_FILE
    value = "sk-" + "synthetic" * 10
    path.write_text("OPENAI_API_KEY=" + value)
    path.chmod(0o644)
    assert local_ai_key._local_file_key() is None
    path.chmod(0o600)
    path.write_text("OPENAI_API_KEY=" + value + "\nOPENAI_API_KEY=" + value)
    assert local_ai_key._local_file_key() is None
