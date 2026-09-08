"""Advisory painted-label centering measurements; never changes gates or pixels."""
from PIL import Image, ImageColor


def control_ink_alignment(candidate, placement, path):
    template = candidate.get("template", {})
    layers = template.get(placement + "Layout", {}).get("layers", [])
    colours = template.get("semanticColours", {})
    result = []
    with Image.open(path) as opened:
        image = opened.convert("RGB")
        if image.size != (1080, 1350 if placement == "feed" else 1920):
            return []
        for index, label in enumerate(layers):
            if label.get("type") != "text" or label.get("alignment") != "center":
                continue
            g = label.get("geometry", {})
            if not all(isinstance(g.get(k), (int, float)) for k in ("x", "y", "width", "height")):
                continue
            for background in reversed(layers[:index]):
                b = background.get("geometry", {})
                if background.get("type") != "vector" or background.get("shape") not in {"rect", "rounded"}:
                    continue
                if not all(isinstance(b.get(k), (int, float)) for k in ("x", "y", "width", "height")):
                    continue
                if (abs(b["x"] - g["x"]) > 2 or abs(b["width"] - g["width"]) > 2
                        or not b["y"] <= g["y"] < b["y"] + b["height"]
                        or not 20 <= b["height"] <= 240 or b["width"] > 800):
                    continue
                try:
                    fg = ImageColor.getrgb(colours[label["colourRole"]])
                    bg = ImageColor.getrgb(colours[background["colourRole"]])
                except (ValueError, KeyError, TypeError):
                    continue
                if sum(abs(a-z) for a, z in zip(fg, bg)) < 360:
                    continue
                left, top = max(0, round(b["x"] + 5)), max(0, round(b["y"] + 5))
                right = min(image.width, round(b["x"] + b["width"] - 5))
                bottom = min(image.height, round(b["y"] + b["height"] - 5))
                if right <= left or bottom <= top:
                    continue
                crop = image.crop((left, top, right, bottom))
                points = [(x, y) for y in range(crop.height) for x in range(crop.width)
                          if sum(abs(a-z) for a, z in zip(crop.getpixel((x, y)), fg)) < 90]
                if len(points) < 20:
                    continue
                xs, ys = zip(*points)
                if min(xs) == 0 or min(ys) == 0 or max(xs) == crop.width-1 or max(ys) == crop.height-1:
                    continue  # Clipping/border contamination is unknown, not a measurement.
                ink = {"x": left + min(xs), "y": top + min(ys),
                       "width": max(xs)-min(xs)+1, "height": max(ys)-min(ys)+1}
                dy = round(b["y"] + b["height"]/2 - ink["y"] - ink["height"]/2, 2)
                result.append({"layerId": label.get("layerId"), "backgroundId": background.get("layerId"),
                               "buttonBounds": b, "paintedInkBounds": ink,
                               "moveLabelDownByPx": dy, "labelYForCurrentButton": round(g["y"] + dy, 2),
                               "scope": "Advisory default glyph centering only. Remeasure after button/font changes; preserve replacement fit."})
                break
            if len(result) >= 8:
                break
    return result
