"""Offline composer environment tests; no Hermes process or network calls."""
import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import compose as C
import config

DATE = "2026-11-14"
ESSENTIALS = {
    "PATH": "test-bin", "PATHEXT": ".EXE;.CMD", "SYSTEMROOT": "C:/Windows",
    "COMSPEC": "C:/Windows/System32/cmd.exe", "TEMP": "test-temp", "TMP": "test-tmp",
    "USERPROFILE": "test-profile", "APPDATA": "test-appdata",
    "LOCALAPPDATA": "test-localappdata", "HOME": "test-home",
    "HERMES_HOME": "test-hermes-home", "HERMES_PROFILE": "test-profile-name",
}


@pytest.fixture
def composer(monkeypatch, tmp_path):
    # Replace, rather than read, the real environment and runtime configuration.
    monkeypatch.setattr(os, "environ", dict(ESSENTIALS))
    monkeypatch.setattr(config, "_ENV_KEYS", set())
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(config, "STATE", tmp_path)
    monkeypatch.setattr(config, "BRIEFS", tmp_path)
    monkeypatch.setattr(config, "PROJECT", tmp_path)
    monkeypatch.setattr(config, "load_settings", dict)
    monkeypatch.setattr(config, "hermes_bin", lambda: "fake-hermes")
    (tmp_path / "2026-07-14.md").write_text("reference", encoding="utf-8")
    data = tmp_path / "data.md"
    data.write_text("test data", encoding="utf-8")
    out = tmp_path / "brief.md"
    log = tmp_path / "compose.log"
    process = Mock(return_value=Mock(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(C.subprocess, "run", process)

    def run():
        return C.run(DATE, data, out, log)

    return run, process, out, log


def test_parent_webhook_prefix_is_excluded(composer):
    run, process, _, _ = composer
    names = ("DISCORD_WEBHOOK_URL", "DISCORD_WEBHOOK_URLS", "discord_webhook_future")
    for name in names:
        os.environ[name] = "synthetic-webhook"
    run()
    child = process.call_args.kwargs["env"]
    assert all(name not in child for name in names)
    assert all(os.environ[name] == "synthetic-webhook" for name in names)


def test_runtime_config_key_is_excluded(composer):
    run, process, _, _ = composer
    config.ENV_PATH.write_text('# comment\n\nignored line\n FUTURE_SECRET = "synthetic"\n', encoding="utf-8")
    config.load_env()
    assert os.environ["FUTURE_SECRET"] == "synthetic"
    assert config.env_keys() == frozenset({"FUTURE_SECRET"})
    run()
    assert "FUTURE_SECRET" not in process.call_args.kwargs["env"]


def test_preexisting_config_key_is_excluded_case_insensitively(composer):
    run, process, _, _ = composer
    os.environ["PREEXISTING_SECRET"] = "parent-value"
    os.environ["preexisting_secret"] = "case-variant"
    config.ENV_PATH.write_text("PREEXISTING_SECRET=file-value\n", encoding="utf-8")
    config.load_env()
    assert os.environ["PREEXISTING_SECRET"] == "parent-value"
    run()
    child = process.call_args.kwargs["env"]
    assert "PREEXISTING_SECRET" not in child
    assert "preexisting_secret" not in child


def test_os_and_hermes_launcher_variables_are_preserved(composer):
    run, process, _, _ = composer
    run()
    child = process.call_args.kwargs["env"]
    assert {key: child[key] for key in ESSENTIALS} == ESSENTIALS


def test_unrelated_variable_passes_through(composer):
    run, process, _, _ = composer
    os.environ["HARMLESS_SETTING"] = "unchanged"
    run()
    assert process.call_args.kwargs["env"]["HARMLESS_SETTING"] == "unchanged"


def test_config_key_tracking_survives_reload_and_missing_file(composer):
    run, process, _, _ = composer
    config.ENV_PATH.write_text("FIRST_SECRET=first\n", encoding="utf-8")
    config.load_env()
    snapshot = config.env_keys()
    config.ENV_PATH.write_text("SECOND_SECRET=second\n", encoding="utf-8")
    config.load_env()
    config.ENV_PATH.unlink()
    config.load_env()
    assert snapshot == frozenset({"FIRST_SECRET"})
    assert config.env_keys() == frozenset({"FIRST_SECRET", "SECOND_SECRET"})
    run()
    assert not config.env_keys().intersection(process.call_args.kwargs["env"])


def test_nonzero_exit_keeps_failure_path_and_logs_filtering(composer):
    run, process, _, log = composer
    process.return_value.returncode = 7
    assert run() == (False, "no brief written (hermes exit 7)")
    assert "compose environment: filtered" in log.read_text(encoding="utf-8")
    process.assert_called_once()


def test_launch_failure_is_explicit_without_inherited_env_retry(composer):
    run, process, _, log = composer
    # Secret exclusions take priority even if a config file declares an OS key.
    config.ENV_PATH.write_text("PATH=untrusted-bin\n", encoding="utf-8")
    config.load_env()
    process.side_effect = FileNotFoundError(2, "synthetic launch error")
    ok, detail = run()
    assert ok is False
    assert "launch failed with filtered environment (FileNotFoundError, errno 2)" in detail
    assert detail in log.read_text(encoding="utf-8")
    assert "PATH" not in process.call_args.kwargs["env"]
    process.assert_called_once()


def test_success_keeps_command_and_output_contract(composer):
    run, process, out, log = composer

    def write_brief(*args, **kwargs):
        out.write_text("test brief", encoding="utf-8")
        return Mock(returncode=0, stdout="", stderr="")

    process.side_effect = write_brief
    assert run() == (True, "hermes exit 0, 10 bytes")
    assert process.call_args.args[0] == [
        "fake-hermes", "chat", "--query-file", str(config.STATE / f"prompt-{DATE}.txt"),
        "--oneshot", "-Q", "--in", str(config.PROJECT), "-t", "web,file",
        "-m", "deepseek/deepseek-v4.1-flash-1017", "--max-turns", "40", "--run-budget", "900",
    ]
    assert process.call_args.kwargs["timeout"] == 1020
    prompt = (config.STATE / f"prompt-{DATE}.txt").read_text(encoding="utf-8")
    assert str(out) in prompt
    assert str(config.STATE / f"claims-{DATE}.json") in prompt
    assert "compose environment: filtered" in log.read_text(encoding="utf-8")
    assert process.call_args.kwargs["check"] is False
    assert "Never silently omit a failed section" in prompt


def test_nonzero_exit_rejects_even_a_fresh_brief(composer):
    run, process, out, _ = composer

    def failed_child(*args, **kwargs):
        out.write_text("test brief", encoding="utf-8")
        return Mock(returncode=7, stdout="", stderr="failed")

    process.side_effect = failed_child
    assert run() == (False, "compose failed (hermes exit 7)")
    assert process.call_args.kwargs["check"] is False
