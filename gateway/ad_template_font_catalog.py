from __future__ import annotations

import hashlib
import json
import re
import shlex
from pathlib import Path
from typing import Sequence


_FONT_BASENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.woff2")


def _manifest_for(cli: Path) -> Path | None:
    parents = tuple(cli.parents[:5])
    for parent in parents:
        candidate = parent / "public/fonts/adstudio/manifest.json"
        if candidate.is_symlink():
            raise ValueError(f"font manifest is symlinked: {candidate}")
        if candidate.exists():
            if not candidate.is_file():
                raise ValueError(f"font manifest is not a regular file: {candidate}")
            return candidate
    return None


def available_font_files(
    renderer_command: str, fallback: Sequence[str]
) -> tuple[str, ...]:
    """Return verified renderer font paths, or sorted legacy fallback paths."""
    fallback_paths = tuple(sorted(str(item) for item in fallback))
    if not renderer_command:
        return fallback_paths
    try:
        args = shlex.split(renderer_command)
    except ValueError:
        return fallback_paths

    for token in args:
        if not token.startswith("/"):
            continue
        candidate = Path(token)
        if not candidate.is_file() or candidate.is_symlink():
            continue
        manifest = _manifest_for(candidate.resolve())
        if manifest is None:
            continue

        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"invalid font manifest JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("font manifest must be a JSON object")
        faces = data.get("faces")
        if not isinstance(faces, list) or not faces:
            raise ValueError("font manifest faces must be a non-empty list")
        if len(faces) > 512:
            raise ValueError("font manifest faces must contain at most 512 entries")

        root = manifest.parent.resolve()
        paths: list[str] = []
        seen: set[str] = set()
        for face in faces:
            if not isinstance(face, dict):
                raise ValueError("font manifest face is invalid")
            path = face.get("file")
            digest = face.get("sha256")
            name = Path(path).name if isinstance(path, str) else ""
            if (
                not isinstance(path, str)
                or _FONT_BASENAME.fullmatch(name) is None
                or path != "/fonts/adstudio/" + name
            ):
                raise ValueError(f"font manifest face path is invalid: {path!r}")
            if path in seen:
                raise ValueError(f"duplicate font manifest path: {path}")
            seen.add(path)
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdefABCDEF" for char in digest)
            ):
                raise ValueError(f"font manifest digest is invalid: {path}")
            if (
                not isinstance(face.get("license"), str)
                or not face["license"].strip()
                or not isinstance(face.get("licenseUrl"), str)
                or not face["licenseUrl"].strip()
            ):
                raise ValueError(f"font manifest license metadata is missing: {path}")
            actual = root / name
            if actual.is_symlink() or not actual.is_file():
                raise ValueError(f"font manifest file is missing or symlinked: {path}")
            try:
                actual.resolve().relative_to(root)
            except ValueError as exc:
                raise ValueError(f"font manifest file escapes manifest directory: {path}") from exc
            observed = hashlib.sha256(actual.read_bytes()).hexdigest()
            if observed.lower() != digest.lower():
                raise ValueError(f"font manifest digest mismatch: {path}")
            paths.append(path)
        return tuple(sorted(paths))
    return fallback_paths
