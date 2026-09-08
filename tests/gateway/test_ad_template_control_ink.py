from PIL import Image, ImageDraw
from gateway.ad_template_control_ink import control_ink_alignment


def test_measures_painted_letters_not_invisible_box(tmp_path):
    path = tmp_path / "story.png"
    image = Image.new("RGB", (1080, 1920), "black")
    draw = ImageDraw.Draw(image)
    draw.rectangle((85, 1730, 384, 1869), fill="white")
    draw.rectangle((120, 1758, 340, 1785), fill="#222423")
    image.save(path)
    candidate = {"template": {"semanticColours": {"background": "#FFFFFF", "mainText": "#222423"},
        "storyLayout": {"layers": [
            {"type": "vector", "shape": "rect", "layerId": "button", "colourRole": "background", "geometry": {"x":85,"y":1730,"width":300,"height":140}},
            {"type": "text", "layerId": "label", "alignment": "center", "colourRole": "mainText", "geometry": {"x":85,"y":1758,"width":300,"height":88}},
        ]}}}
    before = control_ink_alignment(candidate, "story", path)
    assert before[0]["moveLabelDownByPx"] == 28
    assert before[0]["labelYForCurrentButton"] == 1786
    candidate["template"]["storyLayout"]["layers"][1]["geometry"]["height"] = 78
    assert control_ink_alignment(candidate, "story", path) == before
    candidate["template"]["storyLayout"]["layers"][1]["alignment"] = "left"
    assert control_ink_alignment(candidate, "story", path) == []


def test_unknown_pixels_do_not_claim_alignment(tmp_path):
    path = tmp_path / "empty.png"
    Image.new("RGB", (1080, 1350), "white").save(path)
    assert control_ink_alignment({}, "feed", path) == []
