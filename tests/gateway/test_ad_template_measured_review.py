"""Measured comparison diagnostics must remain advisory and source-specific."""

import pytest

from gateway import ad_template_generator_process as process


def _candidate(placement):
    return {"template": {
        "textInputs": [{"key": "headline", "placeholder": "OPEN HOUSE"}],
        f"{placement}Layout": {"layers": [{
            "type": "text", "layerId": f"{placement}-headline", "inputKey": "headline",
            "geometry": {"x": 100, "y": 450, "width": 850, "height": 200},
        }]},
    }}


def _map(y):
    return {
        "canvas": {"width": 1080, "height": 1350}, "ocrStatus": "completed",
        "ocr": [
            {"text": "OPEN", "x": 125, "y": y, "width": 150, "height": 80, "confidence": 95},
            {"text": "HOUSE", "x": 300, "y": y, "width": 230, "height": 80, "confidence": 95},
        ],
        "rectangleRegions": [
            {"kind": "edge-band", "x": 0, "y": 674, "width": 1080, "height": 6},
            {"kind": "edge-band", "x": 0, "y": 30, "width": 1080, "height": 300},
            {"kind": "edge-band", "x": 0, "y": float("nan"), "width": 1080, "height": 6},
        ],
    }


@pytest.mark.parametrize("placement", ["feed", "story"])
def test_comparison_measures_only_original_placement(monkeypatch, placement):
    target = "story" if placement == "feed" else "feed"
    source_map, rendered_map = _map(520), _map(560)
    if placement == "story":
        source_map["canvas"]["height"] = rendered_map["canvas"]["height"] = 1920
    observed = []
    monkeypatch.setattr(process, "deterministic_pixel_metrics", lambda *args: {"diagnostic": True})
    monkeypatch.setattr(process, "build_source_map", lambda path: observed.append(path) or rendered_map)
    result = process._comparison_metrics(
        source="/reference.png", reciprocal_reference="/reference.png",
        source_placement=placement, target_placement=target,
        rendered={"render": {"feed": "/feed.png", "story": "/story.png"}},
        candidate=_candidate(placement), source_map=source_map,
    )
    assert observed == [f"/{placement}.png"]
    evidence = result[placement]["textAlignment"]["entries"][0]
    assert evidence["offset"] == {"dx": 0, "dy": 40}
    assert evidence["layerId"] == f"{placement}-headline"
    assert result[placement]["sourceStructuralBands"] == [
        {"x": 0, "y": 674, "width": 1080, "height": 6},
    ]
    assert result[placement]["photoIdentityComparable"] is False
    assert result[target] == {"mode": "native-reflow", "pixelComparison": False}
    assert "score" not in result[placement]


def test_unavailable_ocr_does_not_fail_comparison(monkeypatch):
    monkeypatch.setattr(process, "deterministic_pixel_metrics", lambda *args: {"diagnostic": True})
    monkeypatch.setattr(process, "build_source_map", lambda _: (_ for _ in ()).throw(OSError("OCR unavailable")))
    kwargs = dict(
        source="/reference.png", reciprocal_reference="/reference.png",
        source_placement="feed", target_placement="story",
        rendered={"render": {"feed": "/feed.png", "story": "/story.png"}},
    )
    result = process._comparison_metrics(**kwargs, candidate=_candidate("feed"), source_map=_map(520))
    assert result["feed"]["renderOcrStatus"] == "unavailable"
    assert "textAlignment" not in result["feed"]
    legacy = process._comparison_metrics(**kwargs)
    assert "renderOcrStatus" not in legacy["feed"]


def test_font_capabilities_follow_active_renderer_and_fail_closed(monkeypatch):
    monkeypatch.setenv("AD_TEMPLATE_GENERATOR_CMD", "/usr/bin/node /release/cli.js")
    calls = []
    monkeypatch.setattr(process, "available_font_files", lambda command, fallback: calls.append((command, fallback)) or ("/fonts/adstudio/extra.woff2",))
    assert process._available_font_files() == ("/fonts/adstudio/extra.woff2",)
    assert calls == [("/usr/bin/node /release/cli.js", process.AVAILABLE_FONT_FILES)]
    monkeypatch.setattr(process, "available_font_files", lambda *_: (_ for _ in ()).throw(ValueError("digest mismatch")))
    with pytest.raises(process.AdTemplateProcessError, match="font catalog.*digest"):
        process._available_font_files()
