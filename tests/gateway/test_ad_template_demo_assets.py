import base64
import copy
from types import SimpleNamespace

from PIL import Image
import pytest
import gateway.exact_clone_process as process


def fixture(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    generated = tmp_path / "generated.webp"
    Image.new("RGB", (200, 250), "white").save(source)
    Image.new("RGB", (128, 160), "blue").save(generated)
    candidate = {"template": {
        "templateId": "test",
        "imageInputs": [{"key": "hero", "label": "Exterior", "defaultAssetKey": "photo"}],
        "assets": {"photo": {"fileName": "source.png", "mimeType": "image/png"}},
        "feedLayout": {"layers": [{
            "type": "image_slot", "inputKey": "hero",
            "geometry": {"x": 0, "y": 0, "width": 1080, "height": 1350},
        }]},
    }, "assets": [{"assetKey": "photo", "fileName": "source.png", "mimeType": "image/png"}]}
    monkeypatch.setattr(process, "_runtime_catalog", lambda: SimpleNamespace(
        assets={"source.png": SimpleNamespace(usage="photo-default")},
    ))
    arguments = dict(source=str(source), source_placement="feed", workspace=tmp_path,
                     route={"provider": "meta-direct", "model": "muse-image-1.0"},
                     emit=lambda *args: None)
    return candidate, generated, arguments


def test_demo_assets_resume_once_and_do_not_mutate_candidate(tmp_path, monkeypatch):
    candidate, generated, args = fixture(tmp_path, monkeypatch)
    original = copy.deepcopy(candidate)
    calls = []
    def generate(*values):
        calls.append(values)
        return str(generated)
    first, first_bytes = process.prepare_demo_assets(candidate, call_image_model=generate, **args)
    second, second_bytes = process.prepare_demo_assets(first, call_image_model=generate, **args)
    assert candidate == original
    assert first == second
    assert first_bytes == second_bytes
    assert len(calls) == 1
    assert first_bytes["photo"].startswith(b"\x89PNG")
    assert first["assets"][0]["fileName"] == first["template"]["assets"]["photo"]["fileName"]


def test_unknown_image_call_is_not_blindly_retried(tmp_path, monkeypatch):
    candidate, generated, args = fixture(tmp_path, monkeypatch)
    calls = []
    def fail(*values):
        calls.append(values)
        raise RuntimeError("connection ended after submission")
    with pytest.raises(RuntimeError):
        process.prepare_demo_assets(candidate, call_image_model=fail, **args)
    with pytest.raises(process.AdTemplateProcessError, match="outcome is unknown"):
        process.prepare_demo_assets(candidate, call_image_model=fail, **args)
    assert len(calls) == 1


def test_import_sends_exact_generated_photo_bytes(tmp_path, monkeypatch):
    candidate, generated, args = fixture(tmp_path, monkeypatch)
    document, overrides = process.prepare_demo_assets(candidate, call_image_model=lambda *a: str(generated), **args)
    monkeypatch.setattr(process, "_candidate_envelope", lambda value: value)
    monkeypatch.setattr(process, "resolve_declared_assets", lambda catalog, declarations: [])
    sent = []
    def post(url, payload, **kwargs):
        sent.append(payload)
        return {"templateId": "test", "libraryStatus": "quarantined", "assetCount": 1}
    monkeypatch.setattr(process, "_post_blockwise", post)
    monkeypatch.setenv("BLOCKWISE_TEMPLATE_IMPORT_URL", "https://example.test/api/internal/adstudio/template-artifacts")
    result = process.import_template(document, run_id="run", project_id="project", asset_overrides=overrides)
    assert result["library_status"] == "quarantined"
    assert base64.b64decode(sent[0]["assets"][0]["bytesBase64"]) == overrides["photo"]
    assert sent[0]["template"]["assets"]["photo"]["mimeType"] == "image/png"
