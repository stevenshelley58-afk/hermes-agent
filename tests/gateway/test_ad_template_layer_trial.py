from pathlib import Path
from PIL import Image
from scripts.ad_template_layer_trial import diff, outside, strict_ok

def test_diff_reports_only_changed_leaf():
    assert diff({'x': 1}, {'x': 2}) == {'/x'}

def test_diff_reports_list_length_broadly():
    assert diff([1], [1, 2], '/layers') == {'/layers'}

def test_strict_checks_accept_unchanged_story_and_feed_outside_title():
    assert strict_ok({'feed_changed_outside_allowed_heading_region': False, 'story_byte_identical': True})

def test_strict_checks_reject_changed_feed_outside_title():
    assert not strict_ok({'feed_changed_outside_allowed_heading_region': True, 'story_byte_identical': True})

def test_strict_checks_reject_changed_story():
    assert not strict_ok({'feed_changed_outside_allowed_heading_region': False, 'story_byte_identical': False})

def test_outside_detects_only_changes_beyond_heading_box(tmp_path):
    before = Path(tmp_path / 'before.png')
    inside = Path(tmp_path / 'inside.png')
    beyond = Path(tmp_path / 'beyond.png')
    Image.new('RGB', (1080, 1350), 'white').save(before)
    image = Image.new('RGB', (1080, 1350), 'white')
    image.putpixel((200, 100), (0, 0, 0))
    image.save(inside)
    image.putpixel((200, 300), (0, 0, 0))
    image.save(beyond)
    assert not outside(before, inside)
    assert outside(before, beyond)
