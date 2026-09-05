from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
import io

import importlib.util
_spec = importlib.util.spec_from_file_location(
    "meta_direct_plugin", Path(__file__).parents[3] / "plugins" / "image_gen" / "meta-direct" / "__init__.py"
)
assert _spec and _spec.loader
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)


_PNG = b""
_buf = io.BytesIO()
Image.new("RGB", (2, 2), "white").save(_buf, format="PNG")
_PNG = _buf.getvalue()
_B64 = base64.b64encode(_PNG).decode()


def _response(value, status="completed", usage=None):
    return SimpleNamespace(status=status, usage=usage, output=[SimpleNamespace(type="image_generation_call", result=value)])


def test_decodes_responses_image_generation_result(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(plugin, "get_secret", lambda *args: "secret")
    captured = {}

    class Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.responses = SimpleNamespace(create=lambda **kwargs: _response(_B64))

    with patch.dict("sys.modules", {"openai": SimpleNamespace(OpenAI=Client)}):
        result = plugin.MetaDirectImageGenProvider().generate("make a house")

    assert result["success"] is True
    assert Path(result["image"]).is_file()
    assert captured["base_url"] == plugin.API_BASE_URL
    assert captured["max_retries"] == 0
    assert Path(result["image"]).suffix == ".png"
    assert result["image_count"] == 1


def test_missing_or_invalid_image_result_fails_closed(monkeypatch):
    monkeypatch.setattr(plugin, "get_secret", lambda *args: "secret")

    class Client:
        def __init__(self, **kwargs):
            self.responses = SimpleNamespace(create=lambda **kwargs: _response(None))

    with patch.dict("sys.modules", {"openai": SimpleNamespace(OpenAI=Client)}):
        result = plugin.MetaDirectImageGenProvider().generate("make a house")
    assert result["success"] is False
    assert "no image_generation_call" in result["error"]


def test_missing_secret_and_unsupported_model_are_local_errors(monkeypatch):
    provider = plugin.MetaDirectImageGenProvider()
    monkeypatch.setattr(plugin, "get_secret", lambda *args: "")
    assert provider.is_available() is False
    missing = provider.generate("house")
    assert missing["error_type"] == "auth_required"

    monkeypatch.setattr(plugin, "get_secret", lambda *args: "secret")
    unsupported = provider.generate("house", model="other-model")
    assert unsupported["error_type"] == "invalid_model"


def test_local_reference_uses_file_safety_guard(monkeypatch, tmp_path):
    ref = tmp_path / "ref.png"
    ref.write_bytes(_PNG)
    monkeypatch.setattr(plugin, "raise_if_read_blocked", lambda *_: (_ for _ in ()).throw(ValueError("blocked"))) if hasattr(plugin, "raise_if_read_blocked") else None
    with patch("agent.file_safety.raise_if_read_blocked", side_effect=ValueError("blocked")):
        try:
            plugin._input_image(str(ref))
        except ValueError as exc:
            assert "blocked" in str(exc)
        else:
            raise AssertionError("expected local source guard")
def test_reference_mime_and_completed_status_are_preserved(tmp_path, monkeypatch):
    ref = tmp_path / "ref.webp"
    Image.new("RGB", (3, 2), "blue").save(ref, format="WEBP")
    item = plugin._input_image(str(ref))
    assert "data:image/webp;base64," in item["image_url"]
    monkeypatch.setattr(plugin, "get_secret", lambda *args: "secret")

    class Client:
        def __init__(self, **kwargs):
            self.responses = SimpleNamespace(create=lambda **kwargs: _response(_B64, status="in_progress"))

    with patch.dict("sys.modules", {"openai": SimpleNamespace(OpenAI=Client)}):
        result = plugin.MetaDirectImageGenProvider().generate("house", image_url=str(ref))
    assert result["success"] is False
    assert "not completed" in result["error"]
