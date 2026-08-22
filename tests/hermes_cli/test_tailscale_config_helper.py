"""Tests for the Hermes Tailscale dashboard config helper."""

from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path

import pytest
import yaml


_ROOT = Path(__file__).resolve().parents[2]
_HELPER_PATH = _ROOT / "ops" / "tailscale" / "configure_dashboard_auth.py"
_SPEC = importlib.util.spec_from_file_location("configure_dashboard_auth", _HELPER_PATH)
assert _SPEC and _SPEC.loader
helper = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(helper)


HOST = "srv1625369.tail3084c0.ts.net"
LOGIN = "operator@example.com"


def _setup_home(tmp_path: Path, monkeypatch, text: str, mode: int = 0o600) -> Path:
    home = tmp_path / "hermes"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text(text, encoding="utf-8")
    config.chmod(mode)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return config


def _raw(config: Path):
    return yaml.safe_load(config.read_text(encoding="utf-8"))


def test_updates_real_yaml_list_and_preserves_unrelated_config(
    tmp_path, monkeypatch, capsys
):
    config = _setup_home(
        tmp_path,
        monkeypatch,
        """dashboard:\n  existing: keep\n  tailscale_auth:\n    session_ttl_seconds: 43200\nother:\n  keep: true\n""",
    )
    before = config.stat()
    assert helper.main([LOGIN, HOST]) == 0
    output = capsys.readouterr().out
    parsed = _raw(config)
    assert parsed["dashboard"]["tailscale_auth"]["allowed_users"] == [LOGIN]
    assert parsed["dashboard"]["tailscale_auth"]["public_host"] == HOST
    assert parsed["dashboard"]["tailscale_auth"]["session_ttl_seconds"] == 43200
    assert parsed["dashboard"]["existing"] == "keep"
    assert parsed["other"] == {"keep": True}
    after = config.stat()
    assert stat.S_IMODE(after.st_mode) == 0o600
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert "allowed_users=1" in output
    assert LOGIN not in output


def test_is_idempotent_without_second_write(tmp_path, monkeypatch, capsys):
    config = _setup_home(tmp_path, monkeypatch, "dashboard: {}\n")
    assert helper.main([LOGIN, HOST]) == 0
    capsys.readouterr()
    first = config.stat().st_mtime_ns
    assert helper.main([LOGIN, HOST]) == 0
    output = capsys.readouterr().out
    assert config.stat().st_mtime_ns == first
    assert "unchanged" in output


def test_disable_removes_only_tailscale_mapping_and_preserves_comments(
    tmp_path, monkeypatch, capsys
):
    config = _setup_home(
        tmp_path,
        monkeypatch,
        """# operator preferences
dashboard:
  existing: keep  # dashboard preference
  tailscale_auth:
    allowed_users:
      - operator@example.com
    public_host: srv1625369.tail3084c0.ts.net
    session_ttl_seconds: 43200
agent:
  reasoning_effort: high  # later reasoning preference
display:
  show_reasoning: true  # later display preference
  skin: hermes-classic
other:
  keep: true
""",
    )
    before = config.stat()

    assert helper.main(["--disable"]) == 0

    output = capsys.readouterr().out
    parsed = _raw(config)
    assert "tailscale_auth" not in parsed["dashboard"]
    assert parsed["dashboard"]["existing"] == "keep"
    assert parsed["agent"]["reasoning_effort"] == "high"
    assert parsed["display"] == {
        "show_reasoning": True,
        "skin": "hermes-classic",
    }
    assert parsed["other"] == {"keep": True}
    text = config.read_text(encoding="utf-8")
    assert "# operator preferences" in text
    assert "# dashboard preference" in text
    assert "# later reasoning preference" in text
    assert "# later display preference" in text
    after = config.stat()
    assert stat.S_IMODE(after.st_mode) == 0o600
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert "tailscale_auth=absent" in output
    assert LOGIN not in output
    assert HOST not in output


def test_disable_is_idempotent_when_mapping_is_absent(tmp_path, monkeypatch, capsys):
    config = _setup_home(
        tmp_path,
        monkeypatch,
        "dashboard:\n  existing: keep\ndisplay:\n  show_reasoning: true\n",
    )
    original = config.read_bytes()
    first = config.stat().st_mtime_ns

    assert helper.main(["--disable"]) == 0

    output = capsys.readouterr().out
    assert config.read_bytes() == original
    assert config.stat().st_mtime_ns == first
    assert "unchanged" in output
    assert "tailscale_auth=absent" in output


@pytest.mark.parametrize(
    "text, message",
    [
        ("not-a-mapping\n", "config.yaml root is malformed"),
        ("dashboard: wrong\n", "dashboard is malformed"),
        (
            "dashboard:\n  tailscale_auth: wrong\n",
            "dashboard.tailscale_auth is malformed",
        ),
        (
            "dashboard:\n  tailscale_auth:\n    allowed_users: wrong\n",
            "allowed_users is malformed",
        ),
        (
            "dashboard:\n  tailscale_auth:\n    public_host: []\n",
            "public_host is malformed",
        ),
    ],
)
def test_disable_aborts_on_malformed_config(
    tmp_path, monkeypatch, capsys, text, message
):
    config = _setup_home(tmp_path, monkeypatch, text)
    original = config.read_bytes()
    assert helper.main(["--disable"]) == 2
    assert config.read_bytes() == original
    assert message in capsys.readouterr().out


@pytest.mark.parametrize(
    "args",
    [
        ["--disable", LOGIN],
        ["--disable", LOGIN, HOST],
    ],
)
def test_disable_refuses_positional_identity_or_host(args, capsys):
    with pytest.raises(SystemExit) as exc:
        helper.main(args)
    assert exc.value.code == 2
    output = capsys.readouterr()
    assert "does not accept operator_login or public_host" in output.err
    assert LOGIN not in output.err
    assert HOST not in output.err


@pytest.mark.parametrize(
    "text, message",
    [
        ("not-a-mapping\n", "config.yaml root is malformed"),
        ("dashboard: wrong\n", "dashboard is malformed"),
        (
            "dashboard:\n  tailscale_auth: wrong\n",
            "dashboard.tailscale_auth is malformed",
        ),
        (
            "dashboard:\n  tailscale_auth:\n    allowed_users: wrong\n",
            "allowed_users is malformed",
        ),
    ],
)
def test_aborts_on_malformed_config(tmp_path, monkeypatch, capsys, text, message):
    config = _setup_home(tmp_path, monkeypatch, text)
    original = config.read_bytes()
    assert helper.main([LOGIN, HOST]) == 2
    assert config.read_bytes() == original
    assert message in capsys.readouterr().out


def test_refuses_symlink_config(tmp_path, monkeypatch, capsys):
    home = tmp_path / "hermes"
    home.mkdir()
    target = tmp_path / "target.yaml"
    target.write_text("dashboard: {}\n", encoding="utf-8")
    target.chmod(0o600)
    (home / "config.yaml").symlink_to(target)
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert helper.main([LOGIN, HOST]) == 2
    assert "config path" in capsys.readouterr().out


def test_refuses_wrong_mode(tmp_path, monkeypatch, capsys):
    config = _setup_home(tmp_path, monkeypatch, "dashboard: {}\n", mode=0o640)
    original = config.read_bytes()
    assert helper.main([LOGIN, HOST]) == 2
    assert config.read_bytes() == original
    assert "mode 0600" in capsys.readouterr().out


def test_disable_refuses_symlink_config(tmp_path, monkeypatch, capsys):
    home = tmp_path / "hermes"
    home.mkdir()
    target = tmp_path / "target.yaml"
    target.write_text("dashboard:\n  tailscale_auth: {}\n", encoding="utf-8")
    target.chmod(0o600)
    (home / "config.yaml").symlink_to(target)
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert helper.main(["--disable"]) == 2
    assert "config path" in capsys.readouterr().out


def test_disable_refuses_wrong_mode(tmp_path, monkeypatch, capsys):
    config = _setup_home(
        tmp_path,
        monkeypatch,
        "dashboard:\n  tailscale_auth: {}\n",
        mode=0o640,
    )
    original = config.read_bytes()
    assert helper.main(["--disable"]) == 2
    assert config.read_bytes() == original
    assert "mode 0600" in capsys.readouterr().out


@pytest.mark.skipif(
    os.geteuid() != 0, reason="requires root to create wrong-owner fixture"
)
def test_refuses_wrong_owner(tmp_path, monkeypatch, capsys):
    config = _setup_home(tmp_path, monkeypatch, "dashboard: {}\n")
    os.chown(config, 1, config.stat().st_gid)
    assert helper.main([LOGIN, HOST]) == 2
    assert "owner" in capsys.readouterr().out


@pytest.mark.skipif(
    os.geteuid() != 0, reason="requires root to create wrong-owner fixture"
)
def test_disable_refuses_wrong_owner(tmp_path, monkeypatch, capsys):
    config = _setup_home(
        tmp_path,
        monkeypatch,
        "dashboard:\n  tailscale_auth: {}\n",
    )
    os.chown(config, 1, config.stat().st_gid)
    assert helper.main(["--disable"]) == 2
    assert "owner" in capsys.readouterr().out
