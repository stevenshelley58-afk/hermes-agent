#!/usr/bin/env python3
"""One bounded Heading-only correction trial; never changes its parent run."""
import copy, hashlib, json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path
os.environ.setdefault('HERMES_HOME', '/home/hermes/.hermes')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from PIL import Image, ImageChops
load_dotenv('/home/hermes/.hermes/.env', override=False)
load_dotenv('/srv/hermes/secrets/ad-template.env', override=False)
load_dotenv('/srv/hermes/secrets/ad-template-renderer-current.env', override=False)
from agent.auxiliary_client import OpenAI
from gateway.ad_template_runtime import AdTemplateProcessError, vision_message
from gateway.exact_clone_process import apply_patch, run_renderer
from gateway.platforms.api_server import _resolve_request_runtime_agent_kwargs
from gateway.tool_run_api import ToolRunAPIMixin
ROOT = Path('/home/hermes/.hermes/tool_runs/ad-template-generator/trun_f2b0848ec6a14308a4c08cd513422d8c')

def dump(x):
    return json.dumps(x, sort_keys=True, indent=2)

def path(c):
    for i, x in enumerate(c['template']['feedLayout']['layers']):
        if x.get('layerId') == 'feed-title':
            return f'/template/feedLayout/layers/{i}'
    raise ValueError('feed-title absent')

def diff(a, b, p=''):
    if type(a) != type(b):
        return {p or '/'}
    if isinstance(a, dict):
        z = set()
        for k in set(a) | set(b):
            z |= {f'{p}/{k}'} if k not in a or k not in b else diff(a[k], b[k], f'{p}/{k}')
        return z
    if isinstance(a, list):
        return {p or '/'} if len(a) != len(b) else set().union(*(diff(x, y, f'{p}/{i}') for i, (x, y) in enumerate(zip(a, b))))
    return set() if a == b else {p or '/'}
ROI = (170, 30, 870, 180)

def crop(p, out):
    with Image.open(p) as im:
        im.crop(ROI).save(out)

def measure(p, threshold=120):
    with Image.open(p) as im:
        rgb = im.convert('RGB')
        pts = [(x, y) for y in range(ROI[1], ROI[3]) for x in range(ROI[0], ROI[2]) if sum(rgb.getpixel((x, y))) / 3 < threshold]
    if not pts:
        raise AdTemplateProcessError('heading ink not found')
    return {'bounds': {'xMin': min((x for x, y in pts)), 'yMin': min((y for x, y in pts)), 'xMax': max((x for x, y in pts)), 'yMax': max((y for x, y in pts))}, 'pixels': len(pts), 'threshold': threshold}

def outside(a, b):
    with Image.open(a).convert('RGB') as x, Image.open(b).convert('RGB') as y:
        d = ImageChops.difference(x, y)
        m = Image.new('L', x.size, 0)
        m.paste(255, (78, 20, 1002, 189))
        d.paste('black', mask=m)
        return d.getbbox() is not None

def strict_ok(checks):
    return not checks['feed_changed_outside_allowed_heading_region'] and checks['story_byte_identical']

def call(provider, model, name, prompt, pics, schema):
    r = _resolve_request_runtime_agent_kwargs(provider, target_model=model)
    if r.get('requested_provider') != provider or r.get('provider') != 'custom' or r.get('api_mode') != 'codex_responses':
        raise AdTemplateProcessError('configured structured route unavailable')
    c = OpenAI(api_key=r['api_key'], base_url=str(r['base_url']).rstrip('/'), timeout=150, max_retries=0)
    t = time.monotonic()
    try:
        q = c.responses.create(model=model, input=ToolRunAPIMixin._tool_responses_input(vision_message(prompt, [str(x) for x in pics], bounded=True)), text={'format': {'type': 'json_schema', 'name': name, 'strict': True, 'schema': schema}}, reasoning={'effort': 'minimal'}, max_output_tokens=8192)
        u = ToolRunAPIMixin._tool_response_usage(q, provider=provider, model=model, base_url=str(r['base_url']).rstrip('/'), api_key=r['api_key'])
        u.update({'provider': provider, 'model': model, 'instance': name, 'duration_ms': round((time.monotonic() - t) * 1000), 'outcome': 'ok'})
        return (ToolRunAPIMixin._tool_json_output(ToolRunAPIMixin._tool_response_text(q)), u)
    finally:
        c.close()

def main():
    if not os.getenv('AD_TEMPLATE_GENERATOR_CMD'):
        raise SystemExit('AD_TEMPLATE_GENERATOR_CMD required')
    root = ROOT
    trial = root / 'layer-trials' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    trial.mkdir(parents=True)
    calls = []
    results = {}
    try:
        ck = json.loads((root / 'exact-clone-checkpoint.json').read_text())
        before = copy.deepcopy(ck['bestCandidate'])
        allowed = path(before)
        (trial / 'baseline-candidate.json').write_text(dump(before))
        base = run_renderer(before, trial / 'baseline')['render']
        source = root / 'references/source-canvas.png'
        src, cur, ctx = (trial / 'source-crop.png', trial / 'current-crop.png', trial / 'context.png')
        crop(source, src)
        crop(base['feed'], cur)
        with Image.open(base['feed']) as im:
            im.resize((240, 300)).save(ctx)
        measurements = {'source': measure(source), 'baseline': measure(base['feed'])}
        (trial / 'measurements.json').write_text(dump(measurements))
        layer = before['template']['feedLayout']['layers'][int(allowed.rsplit('/', 1)[1])]
        fonts = [x['file'] for x in before['template']['fonts']]
        prompt = f'Return exactly one JSON patch. Only replace fields below {allowed}; no add/remove, no top-level field or input change. Correct feed-title only. Runtime measurements from source and actual checkpoint bestCandidate render: {dump(measurements)}. Match source position, face, weight, size and spacing. Use only an already-declared font file from {dump(fonts)}; do not request or declare another font. Layer={dump(layer)}. Images: source heading crop, current heading crop, small whole-frame context.'
        (trial / 'repair-prompt.txt').write_text(prompt)
        calls.append({'provider': 'meta-direct', 'model': 'muse-spark-1.3-contributor', 'instance': 'heading-repair', 'outcome': 'attempted'})
        patch, u = call('meta-direct', 'muse-spark-1.3-contributor', 'heading-repair', prompt, [src, cur, ctx], ToolRunAPIMixin._tool_patch_schema())
        calls[-1] = u
        (trial / 'repair-response.json').write_text(dump(patch))
        ops = patch.get('operations', [])
        if not ops or any((x.get('op') != 'replace' or not (str(x.get('path')) == allowed or str(x.get('path')).startswith(allowed + '/')) for x in ops)):
            raise AdTemplateProcessError('patch outside heading whitelist')
        after = apply_patch(before, patch)
        paths = sorted(diff(before, after))
        if not paths or any((not (x == allowed or x.startswith(allowed + '/')) for x in paths)):
            raise AdTemplateProcessError('candidate changed outside heading whitelist')
        (trial / 'after-candidate.json').write_text(dump(after))
        render = run_renderer(after, trial / 'after')['render']
        strict = {'feed_changed_outside_allowed_heading_region': outside(base['feed'], render['feed']), 'story_byte_identical': hashlib.sha256(Path(base['story']).read_bytes()).hexdigest() == hashlib.sha256(Path(render['story']).read_bytes()).hexdigest()}
        (trial / 'strict-checks.json').write_text(dump(strict))
        if not strict_ok(strict):
            raise AdTemplateProcessError('strict unchanged-element check failed')
        aft, small = (trial / 'after-crop.png', trial / 'after-context.png')
        crop(render['feed'], aft)
        with Image.open(render['feed']) as im:
            im.resize((240, 300)).save(small)
        judge = f'Judge heading correction only; no patch. Source is authority. changed={dump(paths)} strict={dump(strict)} measurements={dump(measurements)}. Verify visual improvement in alignment, face, weight, size and spacing; do not accept based only on score. Images: source heading crop, before heading crop, after heading crop, small whole-frame after context.'
        (trial / 'judge-prompt.txt').write_text(judge)
        calls.append({'provider': 'concentrate', 'model': 'gemini-3.8-flash', 'instance': 'heading-judge', 'outcome': 'attempted'})
        review, u = call('concentrate', 'gemini-3.8-flash', 'heading-judge', judge, [src, cur, aft, small], ToolRunAPIMixin._tool_review_schema(comparator=False))
        calls[-1] = u
        (trial / 'judge-response.json').write_text(dump(review))
        results = {'changed_paths': paths, 'strict': strict, 'review': review, 'beforeafter': {'before': base['feed'], 'after': render['feed']}}
        status = 'completed'
    except Exception as e:
        status = 'failed'
        results = {'failure': str(e)}
    report = {'status': status, 'baseline': 'exact-clone-checkpoint.json:bestCandidate', 'calls': calls, 'results': results}
    (trial / 'report.json').write_text(dump(report))
    print(dump({'trial': str(trial), 'status': status, 'calls': len(calls)}))
    return status == 'completed'
if __name__ == '__main__':
    raise SystemExit(0 if main() else 1)
