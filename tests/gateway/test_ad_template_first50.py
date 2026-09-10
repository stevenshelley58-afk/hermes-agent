import copy
import json
from pathlib import Path
import importlib.util

import pytest


spec = importlib.util.spec_from_file_location("first50", "scripts/ad_template_first50.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def _sources(root: Path, count: int = 50) -> list[Path]:
    root.mkdir(parents=True)
    paths = []
    for number in range(1, count + 1):
        path = root / f"meta_{number}.png"
        path.write_bytes(b"\\x89PNG\\r\\n\\x1a\\nsynthetic-" + str(number).encode())
        paths.append(path)
    return paths


def _quality_run(run_id: str, status: str = "ready_for_review") -> dict:
    scores = {
        "overall": 9.8, "geometry": 9.8, "typography": 9.8,
        "colourEffects": 9.8, "imageCrop": 9.8, "details": 9.8,
    }
    effects = {
        "shading": "match", "gradients": "match", "shadows": "match",
        "transparency": "match", "borders": "match", "masks": "match", "texture": "not_present",
    }
    reviewers = [
        {
            "id": "reviewer-a", "route": "gemini/a", "decision": "accept",
            "scores": scores.copy(), "issues": [], "warnings": [],
            "effects": effects.copy(), "fontSubstitution": None,
        },
        {
            "id": "reviewer-b", "route": "muse/b", "decision": "accept",
            "scores": scores.copy(), "issues": [], "warnings": [],
            "effects": effects.copy(), "fontSubstitution": None,
        },
    ]
    template_id = f"template-{run_id}"
    return {
        "run_id": run_id,
        "tool_id": "ad-template-generator",
        "action": "build-template",
        "status": status,
        "output": {
            "template": {
                "schema": "blockwise.ad-template.v1",
                "templateId": template_id,
                "title": "Synthetic template",
                "artifact": f"/tmp/{template_id}.json",
            },
            "scores": {"comparator": scores.copy(), "finalReviewers": [scores.copy(), scores.copy()]},
            "overall_check": {"no_obvious_errors": True},
            "final_review": {"decision": "accepted", "reviewers": reviewers},
            "import": {"template_id": template_id, "library_status": "quarantined"},
            "smoke_test": {"templateId": template_id, "status": "passed"},
            "reusable_validation": {
                "status": "passed", "scenarioLimit": 4,
                "counts": {"total": 4, "passed": 4, "failed": 0},
                "scenarios": [
                    {"name": name, "identity": f"identity-{name}", "status": "passed"}
                    for name in ("short", "max", "unicode", "optional-empty")
                ],
            },
        },
    }


def test_discover_prefers_latest_numeric_design_then_deduplicates_sha(tmp_path):
    latest = tmp_path / "latest"
    older = tmp_path / "older"
    _sources(latest)
    _sources(older)
    (older / "meta_1.png").write_bytes((latest / "meta_2.png").read_bytes())
    (latest / "meta_2.png").write_bytes((latest / "meta_1.png").read_bytes())

    selected = module.discover([latest, older])

    assert len(selected) == 49
    assert selected[0].name == "meta_1.png"
    assert selected[1].name == "meta_3.png"
    assert all(module._source_id(path) == int(path.stem.split("_", 1)[1]) for path in selected)


def test_dry_run_is_read_only_and_prints_exact_manifest(tmp_path, monkeypatch, capsys):
    source_root = tmp_path / "sources"
    _sources(source_root)
    state_root = tmp_path / "state"
    stage_root = tmp_path / "stage"
    monkeypatch.setattr(module, "DEFAULT_ROOTS", [source_root])

    assert module.main(["--root", str(state_root), "--stage-root", str(stage_root)]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["count"] == 50
    assert len(output["items"]) == 50
    assert not state_root.exists()
    assert not stage_root.exists()


class _FakeApi:
    instances = []

    def __init__(self, endpoint, api_key, timeout):
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout
        self.runs = {}
        self.created = []
        self.create_calls = 0
        self.list_calls = 0
        self.__class__.instances.append(self)

    def list_runs(self):
        self.list_calls += 1
        return list(self.runs.values())

    def get_run(self, run_id):
        run = self.runs[run_id]
        if run["status"] == "queued":
            run.update(_quality_run(run_id))
        return run

    def create_run(self, payload):
        self.create_calls += 1
        run_id = f"trun_{self.create_calls}"
        run = {
            "run_id": run_id,
            "tool_id": payload["tool_id"],
            "action": payload["action"],
            "status": "queued",
            "request_id": payload["request_id"],
            "idempotency_key": payload["idempotency_key"],
            "payload": payload["payload"],
        }
        self.runs[run_id] = run
        self.created.append(payload)
        return run


def test_run_stages_verified_sources_persists_manifest_and_caps_active_work(tmp_path, monkeypatch):
    source_root = tmp_path / "sources"
    _sources(source_root)
    monkeypatch.setattr(module, "_Api", _FakeApi)
    _FakeApi.instances.clear()

    assert module.run_batch(
        roots=[source_root], state_root=tmp_path / "state", stage_root=tmp_path / "stage",
        batch_id="test-batch", endpoint="http://172.16.1.1:8642", poll_seconds=0,
    ) == 0

    api = _FakeApi.instances[0]
    state_dir = tmp_path / "state" / "test-batch"
    document = json.loads((state_dir / "manifest.json").read_text())
    state = json.loads((state_dir / "state.json").read_text())
    assert document["schema"] == module.MANIFEST_SCHEMA
    assert len(document["items"]) == 50
    assert all(item["state"] == "passed" for item in state["items"])
    assert all(Path(item["staged_path"]).is_file() for item in document["items"])
    assert all(Path(item["staged_path"]).stat().st_mode & 0o777 == 0o644 for item in document["items"])
    assert len(api.created) == 50
    assert all(payload["payload"]["sources"][0]["path"].startswith(str(tmp_path / "stage")) for payload in api.created)
    assert all(payload["request_id"].startswith("ad-template-first50-test-batch-") for payload in api.created)


def test_uncertain_post_reconciles_same_identity_without_duplicate(tmp_path, monkeypatch):
    source_root = tmp_path / "sources"
    _sources(source_root)

    class LostPostApi(_FakeApi):
        def create_run(self, payload):
            if self.create_calls == 0:
                self.create_calls += 1
                run_id = "trun-lost"
                self.runs[run_id] = {
                    "run_id": run_id, "tool_id": payload["tool_id"], "action": payload["action"],
                    "status": "ready_for_review", "request_id": payload["request_id"],
                    "idempotency_key": payload["idempotency_key"], "payload": payload["payload"],
                    "output": _quality_run(run_id)["output"],
                }
                raise module.HttpFailure("lost response", uncertain=True)
            return super().create_run(payload)

    monkeypatch.setattr(module, "_Api", LostPostApi)
    assert module.run_batch(
        roots=[source_root], state_root=tmp_path / "state", stage_root=tmp_path / "stage",
        batch_id="lost-post", poll_seconds=0,
    ) == 0
    assert LostPostApi.instances[0].create_calls == 50


def test_restart_reconciles_completed_manifest_without_duplicate_posts(tmp_path, monkeypatch):
    source_root = tmp_path / "sources"
    _sources(source_root)
    monkeypatch.setattr(module, "_Api", _FakeApi)
    module.run_batch(
        roots=[source_root], state_root=tmp_path / "state", stage_root=tmp_path / "stage",
        batch_id="resume", poll_seconds=0,
    )
    first = _FakeApi.instances[-1]

    class ResumeApi(_FakeApi):
        def create_run(self, _payload):
            raise AssertionError("passed manifest must not submit duplicates")

    monkeypatch.setattr(module, "_Api", ResumeApi)
    assert module.run_batch(
        roots=[source_root], state_root=tmp_path / "state", stage_root=tmp_path / "stage",
        batch_id="resume", poll_seconds=0,
    ) == 0
    assert first.create_calls == 50


def test_failed_run_stops_new_scheduling_and_preserves_other_active_run(tmp_path, monkeypatch):
    source_root = tmp_path / "sources"
    _sources(source_root)

    class FailureApi(_FakeApi):
        def get_run(self, run_id):
            return self.runs[run_id]

        def create_run(self, payload):
            self.create_calls += 1
            run_id = f"trun-{self.create_calls}"
            if self.create_calls == 1:
                run = {
                    "run_id": run_id, "tool_id": payload["tool_id"], "action": payload["action"],
                    "status": "queued", "request_id": payload["request_id"],
                    "idempotency_key": payload["idempotency_key"], "payload": payload["payload"],
                }
            else:
                run = {
                    "run_id": run_id, "tool_id": payload["tool_id"], "action": payload["action"],
                    "status": "failed", "request_id": payload["request_id"],
                    "idempotency_key": payload["idempotency_key"], "payload": payload["payload"],
                }
            self.runs[run_id] = run
            self.created.append(payload)
            return run

    monkeypatch.setattr(module, "_Api", FailureApi)
    with pytest.raises(module.BatchError, match="ended in failed"):
        module.run_batch(
            roots=[source_root], state_root=tmp_path / "state", stage_root=tmp_path / "stage",
            batch_id="halt", poll_seconds=0,
        )
    api = FailureApi.instances[-1]
    assert len(api.created) == 2
    state = json.loads((tmp_path / "state" / "halt" / "state.json").read_text())
    assert state["status"] == "failed"
    assert state["items"][0]["state"] == "submitted"
    assert any(item["state"] == "failed" for item in state["items"])


def test_ready_for_review_without_final_evidence_fails_closed(tmp_path, monkeypatch):
    source_root = tmp_path / "sources"
    _sources(source_root)

    class BadReviewApi(_FakeApi):
        def create_run(self, payload):
            self.create_calls += 1
            run_id = f"trun-{self.create_calls}"
            run = {
                "run_id": run_id, "tool_id": payload["tool_id"], "action": payload["action"],
                "status": "ready_for_review", "request_id": payload["request_id"],
                "idempotency_key": payload["idempotency_key"], "payload": payload["payload"],
                "output": {"template": {"templateId": "missing-evidence"}},
            }
            self.runs[run_id] = run
            self.created.append(payload)
            return run

    monkeypatch.setattr(module, "_Api", BadReviewApi)
    with pytest.raises(module.BatchError, match="without final evidence"):
        module.run_batch(
            roots=[source_root], state_root=tmp_path / "state", stage_root=tmp_path / "stage",
            batch_id="review-gate", poll_seconds=0,
        )
    state = json.loads((tmp_path / "state" / "review-gate" / "state.json").read_text())
    assert state["status"] == "failed"


def test_manifest_rejects_source_hash_changes_on_resume(tmp_path, monkeypatch):
    source_root = tmp_path / "sources"
    _sources(source_root)
    monkeypatch.setattr(module, "_Api", _FakeApi)
    module.run_batch(
        roots=[source_root], state_root=tmp_path / "state", stage_root=tmp_path / "stage",
        batch_id="immutable", poll_seconds=0,
    )
    (source_root / "meta_7.png").write_bytes(b"changed-source")
    with pytest.raises(module.BatchError, match="immutable first50 manifest changed"):
        module.run_batch(
            roots=[source_root], state_root=tmp_path / "state", stage_root=tmp_path / "stage",
            batch_id="immutable", poll_seconds=0,
        )

def test_failed_item_reconciles_fixed_same_run_without_new_post(tmp_path, monkeypatch):
    source_root = tmp_path / "sources"
    _sources(source_root)

    class InitiallyFailedApi(_FakeApi):
        def get_run(self, run_id):
            return self.runs[run_id]

        def create_run(self, payload):
            self.create_calls += 1
            run_id = f"trun-{self.create_calls}"
            status = "queued" if self.create_calls == 1 else "failed"
            run = {
                "run_id": run_id, "tool_id": payload["tool_id"], "action": payload["action"],
                "status": status, "request_id": payload["request_id"],
                "idempotency_key": payload["idempotency_key"], "payload": payload["payload"],
            }
            self.runs[run_id] = run
            self.created.append(payload)
            return run

    monkeypatch.setattr(module, "_Api", InitiallyFailedApi)
    with pytest.raises(module.BatchError, match="ended in failed"):
        module.run_batch(
            roots=[source_root], state_root=tmp_path / "state", stage_root=tmp_path / "stage",
            batch_id="fixed-run", poll_seconds=0,
        )
    original_runs = InitiallyFailedApi.instances[-1].runs
    repaired_runs = {}
    for run_id, original in original_runs.items():
        repaired = copy.deepcopy(original)
        repaired["status"] = "ready_for_review"
        repaired["output"] = _quality_run(run_id)["output"]
        repaired_runs[run_id] = repaired

    class RepairedApi(_FakeApi):
        def __init__(self, endpoint, api_key, timeout):
            super().__init__(endpoint, api_key, timeout)
            self.runs = repaired_runs

        def get_run(self, run_id):
            run = self.runs[run_id]
            if run["status"] == "queued":
                run.update(_quality_run(run_id))
            return run

        def create_run(self, _payload):
            if _payload["request_id"] in {run["request_id"] for run in repaired_runs.values()}:
                raise AssertionError("repaired same-run resume must not POST a duplicate")
            return super().create_run(_payload)

    monkeypatch.setattr(module, "_Api", RepairedApi)
    assert module.run_batch(
        roots=[source_root], state_root=tmp_path / "state", stage_root=tmp_path / "stage",
        batch_id="fixed-run", poll_seconds=0,
    ) == 0


def test_quality_gate_requires_complete_generation_and_reusable_evidence():
    good = _quality_run("quality")
    assert module._quality_pass(good)

    missing_metadata = copy.deepcopy(good)
    del missing_metadata["output"]["scores"]["comparator"]
    assert not module._quality_pass(missing_metadata)

    missing_overall_check = copy.deepcopy(good)
    del missing_overall_check["output"]["overall_check"]
    assert not module._quality_pass(missing_overall_check)

    low_comparator = copy.deepcopy(good)
    low_comparator["output"]["scores"]["comparator"]["details"] = 9.49
    assert not module._quality_pass(low_comparator)

    at_gate_comparator = copy.deepcopy(good)
    at_gate_comparator["output"]["scores"]["comparator"]["details"] = 9.5
    assert module._quality_pass(at_gate_comparator)

    diagnostic_overall = copy.deepcopy(good)
    diagnostic_overall["output"]["scores"]["comparator"]["overall"] = 1.0
    assert module._quality_pass(diagnostic_overall)

    duplicate_reviewer = copy.deepcopy(good)
    duplicate_reviewer["output"]["final_review"]["reviewers"][1]["id"] = "reviewer-a"
    assert not module._quality_pass(duplicate_reviewer)

    failed_reusable = copy.deepcopy(good)
    failed_reusable["output"]["reusable_validation"]["counts"]["failed"] = 1
    assert not module._quality_pass(failed_reusable)


def test_api_uses_larger_bounded_collection_and_detail_reads(monkeypatch):
    class Response:
        def __init__(self, body):
            self.body = body
            self.read_size = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size):
            self.read_size = size
            return self.body

    collection = Response(json.dumps({"data": []}).encode())
    detail = Response(json.dumps({
        "run_id": "trun-detail", "tool_id": "ad-template-generator",
        "action": "build-template", "status": "running",
    }).encode())
    responses = iter((collection, detail))
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_args, **_kwargs: next(responses))
    api = module._Api("http://example.test", "", 1)
    assert api.list_runs() == []
    assert collection.read_size == module._MAX_COLLECTION_BYTES + 1
    assert api.get_run("trun-detail")["run_id"] == "trun-detail"
    assert detail.read_size == module._MAX_DETAIL_BYTES + 1
