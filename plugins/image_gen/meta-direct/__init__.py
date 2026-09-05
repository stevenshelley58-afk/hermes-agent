"""Meta Muse Image API provider."""
from __future__ import annotations

import base64
import io
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    normalize_reference_images,
    resolve_aspect_ratio,
    save_b64_image,
    success_response,
)
from agent.secret_scope import get_secret

logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.meta.ai/v1"
MODEL = "muse-image-1.0"
_MAX_REFERENCES = 4
_MAX_OUTPUT_TOKENS = 2048
_MAX_REFERENCE_BYTES = 25 * 1024 * 1024


def _extract_result(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        if value.get("type") == "image_generation_call":
            result = value.get("result")
            if isinstance(result, str) and result.strip():
                return result.strip()
        for child in value.values():
            found = _extract_result(child)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for child in value:
            found = _extract_result(child)
            if found:
                return found
    else:
        kind = getattr(value, "type", None)
        if kind == "image_generation_call":
            result = getattr(value, "result", None)
            if isinstance(result, str) and result.strip():
                return result.strip()
        output = getattr(value, "output", None)
        if output is not None:
            return _extract_result(output)
    return None


def _input_image(value: str) -> Dict[str, str]:
    value = value.strip()
    if value.lower().startswith("data:image/"):
        header, sep, payload = value.partition(",")
        if not sep or ";base64" not in header.lower():
            raise ValueError("reference image data URL must be base64 encoded")
        raw = base64.b64decode(payload, validate=True)
    else:
        from agent.file_safety import raise_if_read_blocked
        raise_if_read_blocked(value)
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise ValueError("reference image path does not exist or is not a file")
        raw = path.read_bytes()
    if not raw or len(raw) > _MAX_REFERENCE_BYTES:
        raise ValueError("reference image is empty or exceeds 25MB")
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as image:
        image.verify()
        fmt = (image.format or "").upper()
    mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}.get(fmt)
    if mime is None:
        raise ValueError("reference image format is unsupported")
    encoded = base64.b64encode(raw).decode("ascii")
    return {"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"}


def _build_input(prompt: str, image_url: Optional[str], references: Optional[List[str]]) -> List[Dict[str, Any]]:
    content: List[Dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    values = []
    if image_url:
        values.append(image_url)
    values.extend(references or [])
    if len(values) > _MAX_REFERENCES:
        raise ValueError(f"Meta Muse accepts at most {_MAX_REFERENCES} reference images")
    for value in values:
        content.append(_input_image(value))
    return [{"role": "user", "content": content}]


class MetaDirectImageGenProvider(ImageGenProvider):
    @property
    def name(self) -> str:
        return "meta-direct"

    @property
    def display_name(self) -> str:
        return "Meta Muse Image"

    def is_available(self) -> bool:
        if not (get_secret("META_MODEL_API_KEY", "") or "").strip():
            return False
        try:
            import openai  # noqa: F401
        except ImportError:
            return False
        return True

    def list_models(self) -> List[Dict[str, Any]]:
        return [{"id": MODEL, "display": "Muse Image 1.0", "speed": "varies", "strengths": "Text-to-image and image references", "price": "varies"}]

    def default_model(self) -> Optional[str]:
        return MODEL

    def capabilities(self) -> Dict[str, Any]:
        return {"modalities": ["text", "image"], "max_reference_images": _MAX_REFERENCES}

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Meta Muse Image",
            "badge": "paid",
            "tag": "Muse Image 1.0 via api.meta.ai — text and image references",
            "env_vars": [{"key": "META_MODEL_API_KEY", "prompt": "Meta Model API key", "url": "https://www.meta.ai/"}],
        }

    def generate(self, prompt: str, aspect_ratio: str = DEFAULT_ASPECT_RATIO, *, image_url: Optional[str] = None, reference_image_urls: Optional[List[str]] = None, **kwargs: Any) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)
        if not prompt:
            return error_response(error="Prompt is required and must be a non-empty string", error_type="invalid_argument", provider=self.name, model=MODEL, aspect_ratio=aspect)
        requested = kwargs.get("model")
        if requested is not None and requested != MODEL:
            return error_response(error="Meta Muse provider supports only muse-image-1.0", error_type="invalid_model", provider=self.name, model=str(requested), prompt=prompt, aspect_ratio=aspect)
        api_key = (get_secret("META_MODEL_API_KEY", "") or "").strip()
        if not api_key:
            return error_response(error="META_MODEL_API_KEY is not configured", error_type="auth_required", provider=self.name, model=MODEL, prompt=prompt, aspect_ratio=aspect)
        try:
            refs = normalize_reference_images(reference_image_urls)
            if image_url:
                refs = [image_url, *(refs or [])]
            guidance = {
                "landscape": "Compose for a landscape frame; exact dimensions are provider-controlled.",
                "portrait": "Compose for a portrait frame; exact dimensions are provider-controlled.",
                "square": "Compose for a square frame; exact dimensions are provider-controlled.",
            }[aspect]
            content = _build_input(f"{prompt}\n\nAspect guidance: {guidance}", None, refs)
            import openai
            client = openai.OpenAI(api_key=api_key, base_url=API_BASE_URL, max_retries=0, timeout=180.0)
            response = client.responses.create(model=MODEL, input=content, max_output_tokens=_MAX_OUTPUT_TOKENS)
            status = getattr(response, "status", None)
            if isinstance(response, dict):
                status = response.get("status")
            if status is not None and status != "completed":
                return error_response(error="Meta Muse response was not completed", error_type="provider_error", provider=self.name, model=MODEL, prompt=prompt, aspect_ratio=aspect)
            b64 = _extract_result(response)
            if not b64:
                return error_response(error="Meta Muse response contained no image_generation_call result", error_type="provider_error", provider=self.name, model=MODEL, prompt=prompt, aspect_ratio=aspect)
            try:
                raw = base64.b64decode(b64, validate=True)
                if not raw:
                    raise ValueError("empty image result")
                from PIL import Image
                import io
                with Image.open(io.BytesIO(raw)) as image:
                    image.verify()
            except Exception:
                return error_response(error="Meta Muse returned an invalid image result", error_type="provider_error", provider=self.name, model=MODEL, prompt=prompt, aspect_ratio=aspect)
            with Image.open(io.BytesIO(raw)) as image:
                fmt = (image.format or "PNG").upper()
                width, height = image.size
            extension = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif"}.get(fmt, "png")
            path = save_b64_image(b64, prefix="meta_direct", extension=extension)
            usage = getattr(response, "usage", None)
            if isinstance(response, dict):
                usage = response.get("usage", usage)
            extra = {"image_count": 1, "actual_width": width, "actual_height": height}
            if usage is not None:
                extra["usage"] = usage.to_dict() if hasattr(usage, "to_dict") else usage
            return success_response(image=str(path), model=MODEL, prompt=prompt, aspect_ratio=aspect, provider=self.name, modality="image" if refs else "text", extra=extra)
        except Exception as exc:
            logger.debug("Meta Muse request failed: %s", type(exc).__name__)
            return error_response(error=f"Meta Muse request failed: {type(exc).__name__}", error_type="provider_error", provider=self.name, model=MODEL, prompt=prompt, aspect_ratio=aspect)


def register(ctx) -> None:
    """Register the bundled backend through Hermes' existing plugin loader."""
    ctx.register_image_gen_provider(MetaDirectImageGenProvider())
