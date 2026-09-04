"""Validated, source-free assets available to the ad-template builder.

The builder is allowed to name catalog entries, but it never supplies asset
bytes.  This module makes the committed manifest the sole allowlist and binds
every runtime byte to its reviewed SHA-256 digest.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping, Sequence


CATALOG_SCHEMA = "blockwise.ad-template-safe-asset-catalog"
_MIME_BY_SUFFIX = MappingProxyType({".png": "image/png", ".webp": "image/webp"})
_ALLOWED_USAGE = frozenset({"photo-default", "neutral-placeholder"})


class CatalogIntegrityError(ValueError):
    """Raised when a catalog or requested asset fails closed validation."""


@dataclass(frozen=True)
class CatalogAsset:
    file_name: str
    mime_type: str
    sha256: str
    byte_size: int
    width: int
    height: int
    roles: tuple[str, ...]
    visual_families: tuple[str, ...]
    usage: str


@dataclass(frozen=True)
class SafeAssetCatalog:
    root: Path
    version: str
    assets: Mapping[str, CatalogAsset]

    def prompt_lines(self) -> tuple[str, ...]:
        """Return stable role-aware declarations for the builder prompt."""

        return tuple(
            f"- {asset.file_name} ({asset.mime_type}; usage={asset.usage}; "
            f"roles={','.join(asset.roles)})"
            for asset in self.assets.values()
        )

    def read_asset(self, file_name: str, mime_type: str) -> bytes:
        """Resolve one declared entry and revalidate its bytes at use time."""

        asset = self.assets.get(file_name)
        if asset is None:
            raise CatalogIntegrityError("asset is not declared in the safe catalog")
        if mime_type != asset.mime_type:
            raise CatalogIntegrityError("asset mimeType does not match the safe catalog")

        path = _contained_file(self.root, file_name)
        payload = path.read_bytes()
        if len(payload) != asset.byte_size:
            raise CatalogIntegrityError("asset byte size does not match the safe catalog")
        if hashlib.sha256(payload).hexdigest() != asset.sha256:
            raise CatalogIntegrityError("asset digest does not match the safe catalog")
        return payload


def _normalized_relative_file_name(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise CatalogIntegrityError("asset fileName must be a non-empty string")
    if "\\" in value:
        raise CatalogIntegrityError("asset fileName must use normalized POSIX separators")
    relative = PurePosixPath(value)
    if relative.is_absolute() or relative.as_posix() != value:
        raise CatalogIntegrityError("asset fileName must be a normalized relative path")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise CatalogIntegrityError("asset fileName cannot traverse the catalog root")
    return value


def _contained_file(root: Path, file_name: str) -> Path:
    relative = Path(*PurePosixPath(file_name).parts)
    candidate = root / relative
    if candidate.is_symlink():
        raise CatalogIntegrityError("catalog assets cannot be symlinks")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise CatalogIntegrityError("catalog asset is missing or outside the catalog root") from exc
    if not resolved.is_file():
        raise CatalogIntegrityError("catalog asset must be a regular file")
    return resolved


def _string_tuple(value: Any, *, field: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
        or len(value) != len(set(value))
    ):
        raise CatalogIntegrityError(f"asset {field} must be a non-empty unique string list")
    return tuple(value)


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CatalogIntegrityError(f"asset {field} must be a positive integer")
    return value


def _parse_asset(raw: Any) -> CatalogAsset:
    required = {
        "fileName",
        "mimeType",
        "sha256",
        "byteSize",
        "width",
        "height",
        "roles",
        "visualFamilies",
        "usage",
        "provenance",
    }
    if not isinstance(raw, dict) or set(raw) != required:
        raise CatalogIntegrityError("catalog asset has an unexpected shape")

    file_name = _normalized_relative_file_name(raw["fileName"])
    mime_type = raw["mimeType"]
    if _MIME_BY_SUFFIX.get(PurePosixPath(file_name).suffix.lower()) != mime_type:
        raise CatalogIntegrityError("asset mimeType does not match an allowed file extension")
    digest = raw["sha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise CatalogIntegrityError("asset sha256 must be a lowercase digest")
    usage = raw["usage"]
    if usage not in _ALLOWED_USAGE:
        raise CatalogIntegrityError("asset usage is not recognized")
    provenance = raw["provenance"]
    if not isinstance(provenance, dict) or not provenance:
        raise CatalogIntegrityError("asset provenance is required")

    return CatalogAsset(
        file_name=file_name,
        mime_type=mime_type,
        sha256=digest,
        byte_size=_positive_int(raw["byteSize"], field="byteSize"),
        width=_positive_int(raw["width"], field="width"),
        height=_positive_int(raw["height"], field="height"),
        roles=_string_tuple(raw["roles"], field="roles"),
        visual_families=_string_tuple(raw["visualFamilies"], field="visualFamilies"),
        usage=usage,
    )


def load_safe_asset_catalog(root: str | Path) -> SafeAssetCatalog:
    """Load and fully validate an immutable runtime catalog directory."""

    root_path = Path(root).expanduser().resolve(strict=True)
    if not root_path.is_dir():
        raise CatalogIntegrityError("catalog root must be a directory")
    manifest_path = root_path / "manifest.json"
    if manifest_path.is_symlink():
        raise CatalogIntegrityError("catalog manifest cannot be a symlink")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogIntegrityError("catalog manifest is missing or invalid") from exc

    if not isinstance(raw, dict) or set(raw) != {"schema", "version", "assets"}:
        raise CatalogIntegrityError("catalog manifest has an unexpected shape")
    if raw["schema"] != CATALOG_SCHEMA:
        raise CatalogIntegrityError("catalog manifest schema is unsupported")
    if not isinstance(raw["version"], str) or not raw["version"].strip():
        raise CatalogIntegrityError("catalog manifest version is required")
    if not isinstance(raw["assets"], list) or not raw["assets"]:
        raise CatalogIntegrityError("catalog manifest must declare assets")

    parsed = [_parse_asset(asset) for asset in raw["assets"]]
    names = [asset.file_name for asset in parsed]
    if len(names) != len(set(names)):
        raise CatalogIntegrityError("catalog asset fileName values must be unique")
    if names != sorted(names):
        raise CatalogIntegrityError("catalog assets must be sorted by fileName")

    declared_files = set(names)
    runtime_files: set[str] = set()
    for path in root_path.rglob("*"):
        if path == manifest_path:
            continue
        if path.is_symlink():
            raise CatalogIntegrityError("catalog cannot contain symlinks")
        if path.is_file():
            runtime_files.add(path.relative_to(root_path).as_posix())
    if runtime_files != declared_files:
        raise CatalogIntegrityError("catalog files must exactly match the manifest")

    assets: dict[str, CatalogAsset] = {}
    for asset in parsed:
        path = _contained_file(root_path, asset.file_name)
        payload = path.read_bytes()
        if len(payload) != asset.byte_size:
            raise CatalogIntegrityError(f"asset byte size mismatch: {asset.file_name}")
        if hashlib.sha256(payload).hexdigest() != asset.sha256:
            raise CatalogIntegrityError(f"asset digest mismatch: {asset.file_name}")
        assets[asset.file_name] = asset

    return SafeAssetCatalog(
        root=root_path,
        version=raw["version"],
        assets=MappingProxyType(assets),
    )


def resolve_declared_assets(
    catalog: SafeAssetCatalog,
    declarations: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Resolve builder asset declarations into renderer-ready byte payloads."""

    import base64

    resolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for declaration in declarations:
        if set(declaration) != {"assetKey", "fileName", "mimeType"}:
            raise CatalogIntegrityError("builder asset declaration has an unexpected shape")
        asset_key = declaration["assetKey"]
        if not isinstance(asset_key, str) or not asset_key or asset_key in seen:
            raise CatalogIntegrityError("builder assetKey must be a unique non-empty string")
        seen.add(asset_key)
        file_name = declaration["fileName"]
        mime_type = declaration["mimeType"]
        if not isinstance(file_name, str) or not isinstance(mime_type, str):
            raise CatalogIntegrityError("builder asset metadata must be strings")
        payload = catalog.read_asset(file_name, mime_type)
        resolved.append(
            {
                "assetKey": asset_key,
                "fileName": file_name,
                "mimeType": mime_type,
                "bytesBase64": base64.b64encode(payload).decode("ascii"),
            }
        )
    return tuple(resolved)
