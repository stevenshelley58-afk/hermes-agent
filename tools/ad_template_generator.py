"""Deterministic layered Feed/Story renderer used by Hermes."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from typing import Any
import cv2
import numpy as np

SIZES = {"feed": (1080, 1350), "story": (1080, 1920)}

def _color(value: Any, default=(245, 245, 245)):
    if isinstance(value, list) and len(value) >= 3:
        try: return tuple(max(0, min(255, int(value[i]))) for i in range(3))
        except (TypeError, ValueError): pass
    return default

def _source_path(layer: dict, source: str) -> Path | None:
    for key in ("src", "path", "media", "image", "source"):
        value = layer.get(key)
        if isinstance(value, str) and value.strip():
            path = Path(value).expanduser()
            if path.is_file(): return path
    path = Path(source).expanduser() if source else None
    return path if path and path.is_file() else None

def _draw_document(doc: dict, placement: str, source: str) -> np.ndarray:
    width, height = SIZES[placement]
    canvas = np.full((height, width, 3), _color(doc.get("background"), (245, 245, 245)), dtype=np.uint8)
    layers = sorted(enumerate(doc.get("layers") or []), key=lambda pair: (pair[1].get("z", pair[0]), pair[0]))
    for _, layer in layers:
        if not isinstance(layer, dict): continue
        kind = str(layer.get("type") or "").lower()
        x, y = int(layer.get("x", 0) or 0), int(layer.get("y", 0) or 0)
        w, h = int(layer.get("width", width) or width), int(layer.get("height", height) or height)
        x, y, w, h = max(0, x), max(0, y), max(1, min(width-x, w)), max(1, min(height-y, h))
        if kind in {"image", "media", "photo", "bitmap"}:
            path = _source_path(layer, source)
            image = cv2.imread(str(path), cv2.IMREAD_COLOR) if path else None
            if image is not None:
                canvas[y:y+h, x:x+w] = cv2.resize(image, (w, h), interpolation=cv2.INTER_AREA)
        elif kind in {"background", "rect", "rectangle", "shape", "box"}:
            cv2.rectangle(canvas, (x, y), (x+w-1, y+h-1), _color(layer.get("color"), (230, 230, 230)), thickness=-1)
        elif kind in {"text", "headline", "copy", "label"}:
            text = str(layer.get("text") or layer.get("value") or layer.get("content") or "")
            if text:
                scale = max(0.4, float(layer.get("font_size", 48) or 48) / 36.0)
                cv2.putText(canvas, text[:120], (x + 12, min(height-12, y + int(42*scale))), cv2.FONT_HERSHEY_SIMPLEX, scale, _color(layer.get("color"), (25, 25, 25)), max(1, int(scale*2)), cv2.LINE_AA)
    return canvas

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="-")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    raw = __import__("sys").stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
    candidate = json.loads(raw)
    template = candidate.get("template") if isinstance(candidate, dict) else None
    if not isinstance(template, dict) or not isinstance(template.get("feed"), dict) or not isinstance(template.get("story"), dict):
        raise SystemExit("candidate must contain Feed and Story documents")
    root = Path(args.output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    preview_root = root / "previews"
    preview_root.mkdir(parents=True, exist_ok=True)
    feed, story = template["feed"], template["story"]
    for name, doc in (("feed", feed), ("story", story)):
        layers = doc.get("layers")
        if not isinstance(layers, list) or not layers or any(not isinstance(layer, dict) or not layer.get("id") or not layer.get("type") for layer in layers):
            raise SystemExit(f"{name} layers are invalid")
    encode = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    (root / "feed.json").write_text(encode(feed), encoding="utf-8")
    (root / "story.json").write_text(encode(story), encoding="utf-8")
    (root / "template.json").write_text(encode({"feed": feed, "story": story}), encoding="utf-8")
    previews = []
    for placement, doc in (("feed", feed), ("story", story)):
        path = preview_root / (placement + ".png")
        if not cv2.imwrite(str(path), _draw_document(doc, placement, str(candidate.get("source_path") or ""))):
            raise SystemExit(f"could not write {placement} preview")
        previews.append({"name": path.name, "path": str(path), "placement": placement, "width": SIZES[placement][0], "height": SIZES[placement][1]})
    result = {"template": {"feed": feed, "story": story}, "previews": previews, "documents": {"feed": str(root / "feed.json"), "story": str(root / "story.json"), "template": str(root / "template.json")}, "render": {"feed": str(preview_root / "feed.png"), "story": str(preview_root / "story.png")}, "template_path": str(root / "template.json")}
    print(json.dumps(result, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
