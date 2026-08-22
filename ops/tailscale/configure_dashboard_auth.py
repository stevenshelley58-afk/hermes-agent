#!/usr/bin/env python3
"""Safely configure or disable the Hermes Tailscale dashboard identity allowlist.

This helper intentionally requires an explicit ``HERMES_HOME`` and must run
as the owner of that Hermes profile. It changes only the
``dashboard.tailscale_auth`` mapping and uses Hermes' comment-preserving,
atomic YAML writers.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Sequence


if __package__ in (None, ""):
    # Make the committed helper executable by absolute path without relying
    # on the caller's current working directory or PYTHONPATH.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


_MAX_LOGIN_LEN = 320
_PUBLIC_HOST_RE = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)


class HelperError(RuntimeError):
    """A safe, non-sensitive helper failure."""


def _valid_login(value: str) -> bool:
    if not value or len(value) > _MAX_LOGIN_LEN or value != value.strip():
        return False
    if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in value):
        return False
    return "," not in value and ";" not in value


def _valid_public_host(value: str) -> bool:
    return bool(
        value
        and len(value) <= 253
        and value == value.strip()
        and _PUBLIC_HOST_RE.fullmatch(value)
    )


def _explicit_hermes_home() -> Path:
    raw = os.environ.get("HERMES_HOME", "").strip()
    if not raw:
        raise HelperError("HERMES_HOME must be explicitly set")
    home = Path(raw)
    if not home.is_absolute():
        raise HelperError("HERMES_HOME must be an absolute directory")
    try:
        resolved = home.resolve(strict=True)
    except OSError as exc:
        raise HelperError("HERMES_HOME is not an existing directory") from exc
    if resolved != home or not home.is_dir():
        raise HelperError("HERMES_HOME must not be a symlink")
    return home


def _target_config() -> tuple[Path, os.stat_result]:
    home = _explicit_hermes_home()
    # Set the context explicitly as well as the environment. This prevents a
    # caller's in-process profile override from redirecting the write.
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    override_token = set_hermes_home_override(home)
    from hermes_cli.config import get_config_path

    try:
        config_path = get_config_path()
    finally:
        reset_hermes_home_override(override_token)
    expected = home / "config.yaml"
    if config_path != expected or config_path.is_symlink():
        raise HelperError("config path is not the explicit Hermes config.yaml")
    try:
        metadata = config_path.stat()
    except OSError as exc:
        raise HelperError("config.yaml must already exist") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise HelperError("config.yaml must be a regular file")
    if metadata.st_uid != os.geteuid() or metadata.st_gid != os.getegid():
        raise HelperError("config.yaml owner does not match the executing user")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise HelperError("config.yaml must have mode 0600")
    return config_path, metadata


def _validated_dashboard(raw: dict[str, Any]) -> dict[str, Any]:
    if "dashboard" in raw and not isinstance(raw["dashboard"], dict):
        raise HelperError("dashboard is malformed")
    return raw.get("dashboard") or {}


def _validated_tailscale_section(dashboard: dict[str, Any]) -> dict[str, Any]:
    if "tailscale_auth" in dashboard and not isinstance(
        dashboard["tailscale_auth"], dict
    ):
        raise HelperError("dashboard.tailscale_auth is malformed")
    section = dict(dashboard.get("tailscale_auth") or {})
    if "allowed_users" in section and not isinstance(section["allowed_users"], list):
        raise HelperError("dashboard.tailscale_auth.allowed_users is malformed")
    if "public_host" in section and not isinstance(section["public_host"], str):
        raise HelperError("dashboard.tailscale_auth.public_host is malformed")
    return section


def _updated_section(
    raw: dict[str, Any], login: str, public_host: str
) -> dict[str, Any]:
    dashboard = _validated_dashboard(raw)
    section = _validated_tailscale_section(dashboard)
    section["allowed_users"] = [login]
    section["public_host"] = public_host
    return section


def _read_valid_raw(config_path: Path) -> dict[str, Any]:
    from hermes_cli.config import read_user_config_raw
    from utils import fast_safe_load

    raw = read_user_config_raw(config_path)
    # read_user_config_raw intentionally returns {} for a non-mapping root;
    # distinguish that malformed case before mutating the document.
    with config_path.open(encoding="utf-8") as stream:
        parsed_root = fast_safe_load(stream)
    if not isinstance(parsed_root, dict):
        raise HelperError("config.yaml root is malformed")
    return raw


def _verify_metadata(config_path: Path, before: os.stat_result) -> None:
    after = config_path.stat()
    if (
        after.st_uid != before.st_uid
        or after.st_gid != before.st_gid
        or stat.S_IMODE(after.st_mode) != stat.S_IMODE(before.st_mode)
    ):
        raise HelperError("config.yaml ownership or mode changed unexpectedly")


def configure(operator_login: str, public_host: str) -> bool:
    """Apply the two Tailscale settings; return whether a write occurred."""
    if not _valid_login(operator_login):
        raise HelperError("operator login is malformed")
    if not _valid_public_host(public_host):
        raise HelperError("public hostname is malformed")

    config_path, before = _target_config()
    from hermes_cli.config import read_user_config_raw
    from utils import atomic_roundtrip_yaml_update

    raw = _read_valid_raw(config_path)
    section = _updated_section(raw, operator_login, public_host)
    dashboard = raw.get("dashboard") or {}
    current = dashboard.get("tailscale_auth") or {}
    if (
        isinstance(current, dict)
        and current.get("allowed_users") == [operator_login]
        and current.get("public_host") == public_host
    ):
        return False

    atomic_roundtrip_yaml_update(
        config_path,
        "dashboard.tailscale_auth",
        section,
    )

    _verify_metadata(config_path, before)
    verify = read_user_config_raw(config_path)
    verified = (verify.get("dashboard") or {}).get("tailscale_auth")
    if not isinstance(verified, dict):
        raise HelperError("config.yaml verification failed")
    if (
        verified.get("allowed_users") != [operator_login]
        or verified.get("public_host") != public_host
    ):
        raise HelperError("config.yaml verification failed")
    return True


def disable() -> bool:
    """Remove only ``dashboard.tailscale_auth``; return whether it existed."""
    config_path, before = _target_config()
    from hermes_cli.config import read_user_config_raw
    from utils import atomic_roundtrip_yaml_save

    raw = _read_valid_raw(config_path)
    if "dashboard" not in raw:
        return False
    dashboard = _validated_dashboard(raw)
    if "tailscale_auth" not in dashboard:
        return False
    # Apply the same structural validation as the enable path before deleting
    # anything. A malformed security mapping is an operator-visible error, not
    # an excuse to rewrite the surrounding config.
    _validated_tailscale_section(dashboard)
    del dashboard["tailscale_auth"]

    atomic_roundtrip_yaml_save(config_path, raw)
    _verify_metadata(config_path, before)
    verify = read_user_config_raw(config_path)
    verified_dashboard = verify.get("dashboard")
    if (
        not isinstance(verified_dashboard, dict)
        or "tailscale_auth" in verified_dashboard
    ):
        raise HelperError("config.yaml verification failed")
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Configure or disable the Hermes Tailscale dashboard identity allowlist."
    )
    parser.add_argument(
        "--disable",
        action="store_true",
        help="remove only dashboard.tailscale_auth",
    )
    parser.add_argument("operator_login", nargs="?")
    parser.add_argument("public_host", nargs="?")
    args = parser.parse_args(argv)
    if args.disable and (
        args.operator_login is not None or args.public_host is not None
    ):
        parser.error("--disable does not accept operator_login or public_host")
    if not args.disable and (args.operator_login is None or args.public_host is None):
        parser.error(
            "operator_login and public_host are required unless --disable is used"
        )
    try:
        if args.disable:
            changed = disable()
        else:
            assert args.operator_login is not None
            assert args.public_host is not None
            changed = configure(args.operator_login, args.public_host)
    except HelperError as exc:
        print(f"error: {exc}")
        return 2
    state = "updated" if changed else "unchanged"
    if args.disable:
        print(f"tailscale dashboard auth {state} (tailscale_auth=absent, mode=0600)")
    else:
        print(
            "tailscale dashboard auth "
            f"{state} (allowed_users=1, public_host=configured, mode=0600)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
