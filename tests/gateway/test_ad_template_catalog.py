from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from gateway.ad_template_catalog import (
    CatalogIntegrityError,
    load_safe_asset_catalog,
    resolve_declared_assets,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMMITTED_CATALOG = REPOSITORY_ROOT / "assets" / "ad-template-generator" / "catalog"


def _copy_catalog(tmp_path: Path) -> Path:
    root = tmp_path / "catalog"
    shutil.copytree(COMMITTED_CATALOG, root)
    return root


def test_committed_catalog_covers_required_roles_and_resolves_exact_bytes() -> None:
    catalog = load_safe_asset_catalog(COMMITTED_CATALOG)
    roles = {role for asset in catalog.assets.values() for role in asset.roles}

    assert {
        "high_rise",
        "aerial_estate",
        "land_parcel",
        "map",
        "agent_portrait",
        "kitchen",
        "patio",
        "lounge",
        "coastal_home",
        "sunset_exterior",
        "multi_peak",
        "two_gable",
    } <= roles
    assert len(catalog.prompt_lines()) == len(catalog.assets)
    assert any(
        {"brand_mark", "logo", "multi_peak", "two_gable"} <= set(asset.roles)
        for asset in catalog.assets.values()
    )

    resolved = resolve_declared_assets(
        catalog,
        (
            {
                "assetKey": "kitchen-default",
                "fileName": "interior/kitchen.webp",
                "mimeType": "image/webp",
            },
        ),
    )
    assert resolved[0]["assetKey"] == "kitchen-default"
    assert resolved[0]["bytesBase64"]

    logo = resolve_declared_assets(
        catalog,
        (
            {
                "assetKey": "multi-gable-logo",
                "fileName": "brand/neutral-multi-gable.png",
                "mimeType": "image/png",
            },
        ),
    )
    assert logo[0]["assetKey"] == "multi-gable-logo"
    assert logo[0]["bytesBase64"].startswith("iVBOR")


def test_catalog_rejects_tampered_asset_bytes(tmp_path: Path) -> None:
    root = _copy_catalog(tmp_path)
    target = root / "interior" / "kitchen.webp"
    target.write_bytes(target.read_bytes() + b"tamper")

    with pytest.raises(CatalogIntegrityError, match="byte size mismatch"):
        load_safe_asset_catalog(root)


def test_catalog_rejects_undeclared_files(tmp_path: Path) -> None:
    root = _copy_catalog(tmp_path)
    (root / "unreviewed.webp").write_bytes(b"not an image")

    with pytest.raises(CatalogIntegrityError, match="exactly match"):
        load_safe_asset_catalog(root)


def test_catalog_rejects_manifest_mime_drift(tmp_path: Path) -> None:
    root = _copy_catalog(tmp_path)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["assets"][0]["mimeType"] = "image/webp"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(CatalogIntegrityError, match="mimeType"):
        load_safe_asset_catalog(root)


def test_catalog_rejects_unknown_builder_path_and_mime() -> None:
    catalog = load_safe_asset_catalog(COMMITTED_CATALOG)

    with pytest.raises(CatalogIntegrityError, match="not declared"):
        catalog.read_asset("candidate/source.png", "image/png")
    with pytest.raises(CatalogIntegrityError, match="mimeType"):
        catalog.read_asset("interior/kitchen.webp", "image/png")
