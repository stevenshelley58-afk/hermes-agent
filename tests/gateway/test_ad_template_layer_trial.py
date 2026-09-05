from pathlib import Path
from PIL import Image
import pytest
import scripts.ad_template_layer_trial as layer_trial
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


def test_main_loads_runtime_environment_only_when_invoked(monkeypatch):
    loaded = []
    monkeypatch.delenv('HERMES_HOME', raising=False)
    monkeypatch.delenv('AD_TEMPLATE_GENERATOR_CMD', raising=False)
    monkeypatch.setattr(layer_trial, 'load_dotenv', lambda path, override: loaded.append((path, override)))

    with pytest.raises(SystemExit, match='AD_TEMPLATE_GENERATOR_CMD required'):
        layer_trial.main()

    assert loaded == [
        ('/home/hermes/.hermes/.env', False),
        ('/srv/hermes/secrets/ad-template.env', False),
        ('/srv/hermes/secrets/ad-template-renderer-current.env', False),
    ]
    assert layer_trial.os.environ['HERMES_HOME'] == '/home/hermes/.hermes'
