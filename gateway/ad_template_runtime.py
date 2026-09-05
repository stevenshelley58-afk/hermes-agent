"""Narrow transport and renderer utilities for the exact-clone controller."""

from __future__ import annotations

import base64
import io
import json
import mimetypes
from pathlib import Path
from typing import Any, List, Dict

from PIL import Image, UnidentifiedImageError


VISION_MAX_SERIALIZED_IMAGE_BYTES = 96_000
VISION_MAX_LONG_EDGE = 1440
VISION_MAX_SERIALIZED_MESSAGE_BYTES = 1_500_000
MAX_RENDERER_REJECTIONS = 32
MAX_RENDERER_REJECTION_CHARS = 16_000


class AdTemplateProcessError(ValueError):
    pass


class AdTemplateRendererRejection(AdTemplateProcessError):
    def __init__(self, reasons: Any):
        raw = [reasons] if isinstance(reasons, str) else list(reasons or [])
        bounded = tuple(
            str(item).strip()[:MAX_RENDERER_REJECTION_CHARS]
            for item in raw if str(item).strip()
        )[:MAX_RENDERER_REJECTIONS]
        if not bounded:
            raise ValueError("renderer rejection requires at least one reason")
        self.reasons = bounded
        super().__init__("; ".join(bounded))


class AdTemplateStructuredOutputError(RuntimeError):
    pass


class AdTemplateTransportError(RuntimeError):
    pass


def _bounded_vision_image(path: Path) -> tuple[bytes, str]:
    raw = path.read_bytes()
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            opened.load()
            image = opened.convert("RGB")
    except (OSError, UnidentifiedImageError):
        return raw, mimetypes.guess_type(path.name)[0] or "image/png"
    if max(image.size) > VISION_MAX_LONG_EDGE:
        image.thumbnail((VISION_MAX_LONG_EDGE, VISION_MAX_LONG_EDGE), Image.Resampling.LANCZOS)
    encoded = b""
    for quality in (82, 70, 58):
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=quality, optimize=True, progressive=True)
        encoded = output.getvalue()
        if len(encoded) <= VISION_MAX_SERIALIZED_IMAGE_BYTES:
            return encoded, "image/jpeg"
    while len(encoded) > VISION_MAX_SERIALIZED_IMAGE_BYTES and max(image.size) > 480:
        image = image.resize(
            tuple(max(1, int(value * 0.8)) for value in image.size),
            Image.Resampling.LANCZOS,
        )
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=58, optimize=True, progressive=True)
        encoded = output.getvalue()
    if len(encoded) > VISION_MAX_SERIALIZED_IMAGE_BYTES:
        raise AdTemplateProcessError("bounded vision image exceeds transport budget")
    return encoded, "image/jpeg"


def vision_message(text: str, paths: List[str], *, bounded: bool = False) -> List[Dict[str, Any]]:
    parts: List[Dict[str, Any]] = [{"type": "text", "text": text}]
    for raw in paths:
        path = Path(str(raw)).expanduser().resolve()
        if not path.is_file():
            raise AdTemplateProcessError("vision input image is missing")
        payload, mime = (
            _bounded_vision_image(path)
            if bounded
            else (path.read_bytes(), mimetypes.guess_type(path.name)[0] or "image/png")
        )
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"},
        })
    if len(parts) == 1:
        raise AdTemplateProcessError("vision role requires attached image pixels")
    if bounded and len(json.dumps(parts, ensure_ascii=False).encode("utf-8")) >= VISION_MAX_SERIALIZED_MESSAGE_BYTES:
        raise AdTemplateProcessError("bounded vision message exceeds transport budget")
    return parts
