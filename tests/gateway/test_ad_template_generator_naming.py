import importlib
import json

from gateway import ad_template_generator_process
from gateway import ad_template_generator_layer_refinement
from gateway import ad_template_generator_photo_qa
from gateway.tool_runs import (
    AD_TEMPLATE_GENERATOR_OPTIONAL_ROUTE,
    AD_TEMPLATE_GENERATOR_ROUTE_ORDER,
    AD_TEMPLATE_OPTIONAL_ROUTE,
    AD_TEMPLATE_ROUTE_ORDER,
    ad_template_generator_model_catalog,
    ad_template_generator_profile_snapshot,
    ad_template_model_catalog,
    ad_template_profile_snapshot,
    default_ad_template_generator_policy,
    default_ad_template_policy,
    ToolRunStore,
)


def test_legacy_module_paths_are_canonical_module_aliases():
    assert importlib.import_module(
        "gateway.exact_clone_process"
    ) is ad_template_generator_process
    assert importlib.import_module(
        "gateway.exact_clone_layer_refinement"
    ) is ad_template_generator_layer_refinement
    assert importlib.import_module(
        "gateway.exact_clone_photo_qa"
    ) is ad_template_generator_photo_qa


def test_legacy_source_symbols_alias_canonical_implementations():
    assert (
        ad_template_generator_process.ExactCloneOrchestrator
        is ad_template_generator_process.AdTemplateGeneratorOrchestrator
    )
    assert (
        ad_template_generator_process.validate_exact_clone_output
        is ad_template_generator_process.validate_ad_template_generator_output
    )
    assert AD_TEMPLATE_ROUTE_ORDER is AD_TEMPLATE_GENERATOR_ROUTE_ORDER
    assert AD_TEMPLATE_OPTIONAL_ROUTE == AD_TEMPLATE_GENERATOR_OPTIONAL_ROUTE
    assert ad_template_model_catalog is ad_template_generator_model_catalog
    assert ad_template_profile_snapshot is ad_template_generator_profile_snapshot
    assert default_ad_template_policy is default_ad_template_generator_policy



def test_default_policy_uses_product_name_without_changing_seed():
    policy = default_ad_template_generator_policy()
    assert policy["name"] == "Ad Template Generator"
    assert policy["seed_revision"] == 15


def test_stored_seed_15_legacy_name_is_not_migrated(tmp_path):
    path = tmp_path / "legacy-name.db"
    store = ToolRunStore(str(path))
    stored = store.get_policy("ad-template-generator")
    policy = stored["policy"]
    policy["name"] = "Sole ad-template process"
    store._conn.execute(
        "UPDATE tool_model_policies SET policy_json=? WHERE tool_id=? AND revision=?",
        (
            json.dumps(policy, separators=(",", ":"), sort_keys=True),
            "ad-template-generator",
            stored["revision"],
        ),
    )
    store._conn.commit()
    store.close()

    reopened = ToolRunStore(str(path))
    current = reopened.get_policy("ad-template-generator")
    assert current["revision"] == stored["revision"]
    assert current["policy"]["name"] == "Sole ad-template process"
