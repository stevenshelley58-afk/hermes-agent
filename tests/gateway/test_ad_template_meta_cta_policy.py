import gateway.ad_template_generator_process as process


def test_meta_image_cta_policy_reaches_all_generation_and_review_stages(monkeypatch):
    class Catalog:
        def prompt_lines(self):
            return []
    monkeypatch.setattr(process, '_runtime_catalog', lambda: Catalog())
    candidate = {'template': {'feedLayout': {'layers': []}, 'storyLayout': {'layers': []}}}
    reference = {'sourcePlacement': 'feed'}
    prompts = [
        process.aspect_reference_prompt(source_placement='feed', target_placement='story', canvas={'width': 1080, 'height': 1920}, brief=''),
        process.build_prompt(run_id='test', project_id='blockwise', brief='', placements=['feed', 'story'], reference=reference, source_map={}),
        process.patch_prompt(candidate=candidate, issues=[]),
        process.contract_repair_prompt(candidate=candidate, reasons=[]),
        *[process.review_prompt(final=final, candidate=candidate, reference=reference, metrics={}) for final in (False, True)],
    ]
    for prompt in prompts:
        assert process.META_IMAGE_CTA_RULE in prompt
        assert 'remaining embedded CTA button is an obvious production error' in prompt
        assert 'metaCopyDefaults.cta and publishRequirements.requiredCtaTypes' in prompt
        assert 'Keep informational website/contact details' in prompt
        assert 'square-cornered, fully opaque' in prompt
        assert 'Preserve interior rounded' in prompt
        assert 'Any outer corner cutout or frame is an obvious production error' in prompt


def test_old_cta_acceptance_evidence_requires_new_policy():
    assert process.EVALUATION_POLICY_VERSION > 10
    assert process.LIKENESS_THRESHOLD == 9.8
