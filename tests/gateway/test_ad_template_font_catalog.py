import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "fontcat", "gateway/ad_template_font_catalog.py"
)
m = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(m)


def write_tree(tmp_path, faces):
    cli = tmp_path / "release/bin/cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("")
    font_dir = tmp_path / "release/public/fonts/adstudio"
    font_dir.mkdir(parents=True)
    for name, content in faces:
        (font_dir / name).write_bytes(content)
    entries = []
    for name, _ in faces:
        content = (font_dir / name).read_bytes()
        entries.append(
            {
                "file": "/fonts/adstudio/" + name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "license": "SIL OFL",
                "licenseUrl": "https://openfontlicense.org",
            }
        )
    (font_dir / "manifest.json").write_text(json.dumps({"faces": entries}))
    return str(cli), font_dir / "manifest.json"


def test_valid_extra_face_and_sorted(tmp_path):
    cmd, _ = write_tree(tmp_path, [("zeta.woff2", b"z"), ("alpha.woff2", b"a")])
    assert m.available_font_files(cmd, ["/fallback.woff2"]) == (
        "/fonts/adstudio/alpha.woff2",
        "/fonts/adstudio/zeta.woff2",
    )


def test_missing_manifest_fallback(tmp_path):
    cli = tmp_path / "cli.js"
    cli.write_text("")
    assert m.available_font_files(str(cli), ["/b", "/a"]) == ("/a", "/b")


def test_bad_digest_missing_file_traversal_duplicate(tmp_path):
    cmd, manifest = write_tree(tmp_path, [("a.woff2", b"a")])
    data = json.loads(manifest.read_text())
    data["faces"][0]["sha256"] = "0" * 64
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="digest"):
        m.available_font_files(cmd, [])

    cmd, manifest = write_tree(tmp_path / "missing", [("a.woff2", b"a")])
    (manifest.parent / "a.woff2").unlink()
    with pytest.raises(ValueError, match="missing"):
        m.available_font_files(cmd, [])

    cmd, manifest = write_tree(tmp_path / "traversal", [("a.woff2", b"a")])
    data = json.loads(manifest.read_text())
    data["faces"][0]["file"] = "/fonts/adstudio/../a.woff2"
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="path"):
        m.available_font_files(cmd, [])

    cmd, manifest = write_tree(tmp_path / "duplicate", [("a.woff2", b"a")])
    data = json.loads(manifest.read_text())
    data["faces"].append(dict(data["faces"][0]))
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="duplicate"):
        m.available_font_files(cmd, [])

def test_node_prefix_still_finds_cli_manifest(tmp_path):
    cmd, _ = write_tree(tmp_path, [("regular-400.woff2", b"font")])
    fake_node = tmp_path / "node"
    fake_node.write_text("")
    actual_cmd = str(fake_node) + " " + cmd
    assert m.available_font_files(actual_cmd, ["/fallback"]) == (
        "/fonts/adstudio/regular-400.woff2",
    )


@pytest.mark.parametrize("payload", [[], "not-an-object", {"faces": []}])
def test_manifest_shape_rejected(tmp_path, payload):
    cmd, manifest = write_tree(tmp_path, [("regular.woff2", b"font")])
    manifest.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        m.available_font_files(cmd, [])


def test_symlink_manifest_rejected(tmp_path):
    cmd, manifest = write_tree(tmp_path, [("regular.woff2", b"font")])
    target = manifest.with_name("real-manifest.json")
    manifest.rename(target)
    manifest.symlink_to(target.name)
    with pytest.raises(ValueError, match="symlink"):
        m.available_font_files(cmd, [])


@pytest.mark.parametrize("name", ["bad name.woff2", "bad\\\\name.woff2", "bad.woff", "../escape.woff2"])
def test_filename_edges_rejected(tmp_path, name):
    cmd, manifest = write_tree(tmp_path, [("regular.woff2", b"font")])
    data = json.loads(manifest.read_text())
    data["faces"][0]["file"] = "/fonts/adstudio/" + name
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="path"):
        m.available_font_files(cmd, [])
