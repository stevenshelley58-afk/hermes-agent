"""Executable orchestration for the sole ad-template process."""
from __future__ import annotations
import base64, json, mimetypes, os, shlex, shutil, subprocess, sys, urllib.error, urllib.request, uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping

THRESHOLD = 9.5
STAGES = ("source", "analyse", "decompose", "restyle", "story-draft", "render", "compare", "qa", "final-review", "import")

class AdTemplateProcessError(ValueError):
    pass

def _number(value: Any) -> float:
    try: result = float(value)
    except (TypeError, ValueError): raise AdTemplateProcessError("score must be numeric") from None
    if not 0 <= result <= 10: raise AdTemplateProcessError("score must be between 0 and 10")
    return result

RUBRIC_FIELDS = ("layout_geometry", "hierarchy_typography", "colour_tone", "editable_decomposition", "native_story")

def _assessment(value: Any, role: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise AdTemplateProcessError(f"{role} returned invalid evidence")
    rubric = value.get("rubric")
    if not isinstance(rubric, dict):
        raise AdTemplateProcessError(f"{role} must provide all rubric subscores")
    scores = {field: _number(rubric.get(field)) for field in RUBRIC_FIELDS}
    hard_failures = value.get("hard_failures") or value.get("hard_fail") or []
    if isinstance(hard_failures, str): hard_failures = [hard_failures]
    if not isinstance(hard_failures, list): raise AdTemplateProcessError(f"{role} hard_failures must be a list")
    reason = str(value.get("reason") or value.get("mismatches") or "").strip()
    if len(reason) < 3: raise AdTemplateProcessError(f"{role} must explain its decision")
    score = round(sum(scores.values()) / len(scores), 2)
    if hard_failures: score = 0.0
    return {"score": score, "reason": reason, "rubric": scores, "hard_failures": [str(item)[:240] for item in hard_failures]}

def validate_iterations(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > 30: raise AdTemplateProcessError("iterations must contain 1 to 30 records")
    result, accepted, retry_after_review = [], False, False
    for index, raw in enumerate(value, 1):
        if not isinstance(raw, dict) or raw.get("iteration", index) != index: raise AdTemplateProcessError("iterations must be consecutive and one-based")
        comparison = raw.get("comparison")
        if not isinstance(comparison, dict): raise AdTemplateProcessError("each iteration requires one comparator")
        if any(key in raw or key in comparison for key in ("reviewers", "reviewer", "primary", "strict")): raise AdTemplateProcessError("final reviewers are only allowed after the comparator passes")
        evidence = _assessment(comparison, "comparator")
        score = evidence["score"]
        decision = str(raw.get("decision") or ("accepted" if score >= THRESHOLD else "revise"))
        expected = "accepted" if score >= THRESHOLD else "revise"
        if decision != expected: raise AdTemplateProcessError("iteration decision does not match comparator score")
        reason = evidence["reason"]
        if accepted and not retry_after_review:
            raise AdTemplateProcessError("no iteration may follow an accepted candidate unless final reviewers requested revision")
        accepted = score >= THRESHOLD
        retry_after_review = bool(raw.get("final_review_failed"))
        item = dict(raw); item.update(iteration=index, comparison=evidence, decision=decision); result.append(item)
    return result

def validate_final_review(value: Any, *, accepted: bool) -> Dict[str, Any]:
    if not accepted:
        if value not in (None, {}, []): raise AdTemplateProcessError("final reviewers cannot run before a passing comparator")
        return {}
    if not isinstance(value, dict) or not isinstance(value.get("reviewers"), list) or len(value["reviewers"]) != 2: raise AdTemplateProcessError("exactly two final reviewers are required")
    normalized = []
    for item in value["reviewers"]:
        if not isinstance(item, dict): raise AdTemplateProcessError("reviewer record must be an object")
        identity = str(item.get("id") or item.get("name") or "").strip()
        if not identity: raise AdTemplateProcessError("reviewer identity is required")
        evidence = _assessment(item, "final reviewer")
        score, reason = evidence["score"], evidence["reason"]
        route = str(item.get("route") or "").strip()
        if not route: raise AdTemplateProcessError("reviewer route is required")
        normalized.append({"id": identity, "route": route, **evidence})
    if normalized[0]["id"] == normalized[1]["id"] or normalized[0]["route"] == normalized[1]["route"]: raise AdTemplateProcessError("final reviewers must use independent instances and routes")
    passed = all(item["score"] >= THRESHOLD for item in normalized); decision = "accepted" if passed else "revise"
    if str(value.get("decision") or decision) != decision: raise AdTemplateProcessError("final review decision does not match scores")
    return {"reviewers": normalized, "decision": decision, "threshold": THRESHOLD}

def deterministic_documents(template: Any) -> Dict[str, str]:
    if not isinstance(template, dict): raise AdTemplateProcessError("template candidate must be an object")
    feed = template.get("feedLayout") if template.get("schema") == "blockwise.ad-template" else template.get("feed")
    story = template.get("storyLayout") if template.get("schema") == "blockwise.ad-template" else template.get("story")
    feed = feed if isinstance(feed, dict) else {}
    story = story if isinstance(story, dict) else {}
    if not feed or not story: raise AdTemplateProcessError("template must contain Feed and Story documents")
    for name, doc in (("feed", feed), ("story", story)):
        layers = doc.get("layers")
        if not isinstance(layers, list) or not layers or any(not isinstance(layer, dict) or not (layer.get("layerId") or layer.get("id")) or not layer.get("type") for layer in layers):
            raise AdTemplateProcessError(f"{name} document has invalid layers")
    encode = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"feed.json": encode(feed), "story.json": encode(story), "template.json": encode({"feed": feed, "story": story})}

def _under(path: Path, root: Path) -> bool:
    try: path.relative_to(root); return True
    except ValueError: return False

def validate_artifacts(output: Mapping[str, Any], workspace: Path) -> Dict[str, Any]:
    previews = output.get("previews")
    if not isinstance(previews, list) or not previews: raise AdTemplateProcessError("renderer produced no previews")
    safe = []
    for item in previews:
        if not isinstance(item, dict): raise AdTemplateProcessError("preview record must be an object")
        raw = Path(str(item.get("path") or "")).resolve()
        if not _under(raw, workspace) or not raw.is_file(): raise AdTemplateProcessError("preview artifact is missing or outside the run workspace")
        placement = str(item.get("placement") or "").lower()
        if placement not in {"feed", "story"}: raise AdTemplateProcessError("preview placement must be Feed or Story")
        safe.append({"name": raw.name, "placement": placement})
    render = output.get("render")
    if not isinstance(render, dict) or any(not isinstance(render.get(place), str) for place in ("feed", "story")):
        raise AdTemplateProcessError("renderer receipt must contain Feed and Story paths")
    for place in ("feed", "story"):
        render_path = Path(render[place]).resolve()
        if not _under(render_path, workspace) or not render_path.is_file(): raise AdTemplateProcessError("render artifact is missing or outside the run workspace")
    return {"previews": safe}

def validate_template_artifact(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != "blockwise.ad-template":
        raise AdTemplateProcessError("builder must return the unversioned Blockwise template schema")
    for placement, width, height in (("feedLayout", 1080, 1350), ("storyLayout", 1080, 1920)):
        layout = value.get(placement)
        if not isinstance(layout, dict) or layout.get("placement") not in {"feed", "story"} or not isinstance(layout.get("layers"), list) or not layout["layers"]:
            raise AdTemplateProcessError(f"{placement} is invalid")
        ids = set()
        for layer in layout["layers"]:
            if not isinstance(layer, dict) or not layer.get("layerId") or layer["layerId"] in ids or not isinstance(layer.get("geometry"), dict):
                raise AdTemplateProcessError(f"{placement} has invalid or duplicate layer ids")
            ids.add(layer["layerId"])
            geometry = layer["geometry"]
            if any(not isinstance(geometry.get(key), (int, float)) for key in ("x", "y", "width", "height")):
                raise AdTemplateProcessError(f"{placement} layer geometry is invalid")
            if geometry["x"] < 0 or geometry["y"] < 0 or geometry["width"] <= 0 or geometry["height"] <= 0 or geometry["x"] + geometry["width"] > width or geometry["y"] + geometry["height"] > height:
                raise AdTemplateProcessError(f"{placement} layer geometry exceeds canvas")
    if not isinstance(value.get("assets"), dict) or not isinstance(value.get("imageInputs"), list) or not isinstance(value.get("textInputs"), list) or not isinstance(value.get("fonts"), list) or not isinstance(value.get("metadata"), dict):
        raise AdTemplateProcessError("template is missing exact Blockwise fields")
    for key, asset in value["assets"].items():
        if not isinstance(key, str) or not isinstance(asset, dict) or not asset.get("fileName") or not asset.get("mimeType"): raise AdTemplateProcessError("template asset declarations are invalid")
    return value

def run_generator_cli(candidate: Mapping[str, Any], workspace: Path) -> Dict[str, Any]:
    command = os.environ.get("AD_TEMPLATE_GENERATOR_CMD", "").strip()
    if not command: raise AdTemplateProcessError("AD_TEMPLATE_GENERATOR_CMD must point to the shared Blockwise renderer CLI")
    workspace.mkdir(parents=True, exist_ok=True)
    artifact_path = workspace / "artifact.json"
    template = validate_template_artifact(candidate.get("template") if isinstance(candidate, Mapping) else None)
    assets = candidate.get("assets") if isinstance(candidate, Mapping) and isinstance(candidate.get("assets"), list) else []
    artifact_path.write_text(json.dumps({"template": template, "assets": assets}, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    out_dir = workspace / "rendered"
    argv = shlex.split(command) + ["--input", str(artifact_path), "--assets-dir", str(workspace), "--out-dir", str(out_dir)]
    proc = subprocess.run(argv, text=True, capture_output=True, timeout=600, check=False)
    if proc.returncode: raise AdTemplateProcessError(f"shared Blockwise renderer failed ({proc.returncode})")
    receipt_path = out_dir / "receipt.json"
    try: receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): raise AdTemplateProcessError("shared renderer returned no receipt") from None
    outputs = receipt.get("outputs") if isinstance(receipt, dict) else {}
    if not isinstance(outputs, dict) or not all(isinstance(outputs.get(place), dict) and isinstance(outputs[place].get("path"), str) for place in ("feed", "story")):
        raise AdTemplateProcessError("shared renderer receipt is incomplete")
    previews = [{"name": Path(outputs[place]["path"]).name, "path": outputs[place]["path"], "placement": place, "width": outputs[place].get("width"), "height": outputs[place].get("height")} for place in ("feed", "story")]
    result = {"template": template, "assets": assets, "previews": previews, "render": {place: outputs[place]["path"] for place in ("feed", "story")}, "receipt": receipt, "template_path": str(artifact_path)}
    validate_artifacts(result, workspace)
    return result

def blockwise_artifact_template(template: Any, *, run_id: str) -> Dict[str, Any]:
    return validate_template_artifact(template)

def import_template(output: Mapping[str, Any], *, run_id: str, project_id: str) -> Dict[str, Any]:
    url = os.environ.get("BLOCKWISE_TEMPLATE_IMPORT_URL", "").strip()
    token = (os.environ.get("BLOCKWISE_INTERNAL_AUTH_SECRET") or "").strip()
    if not url or not token: raise AdTemplateProcessError("Blockwise template import endpoint and BLOCKWISE_INTERNAL_AUTH_SECRET are required")
    template = blockwise_artifact_template(output.get("template"), run_id=run_id)
    assets = output.get("assets") if isinstance(output.get("assets"), list) else []
    if not assets: raise AdTemplateProcessError("Blockwise import requires declared assets")
    for asset in assets:
        if not isinstance(asset, dict) or not all(asset.get(key) for key in ("assetKey", "fileName", "mimeType", "bytesBase64")):
            raise AdTemplateProcessError("Blockwise asset payload is invalid")
    body = json.dumps({"template": template, "assets": assets}).encode()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response: payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc: raise AdTemplateProcessError("Blockwise template import failed") from exc
    if not isinstance(payload, dict) or not payload.get("templateId"): raise AdTemplateProcessError("Blockwise import returned no templateId")
    replayed = bool(payload.get("replayed"))
    return {"template_id": str(payload["templateId"]), "status": "replayed" if replayed else "imported", "asset_count": int(payload.get("assetCount") or len(assets)), "replayed": replayed}

def generator_prompt(*, run_id: str, project_id: str, brief: str, placements: Any, source: str, feedback: str = "") -> str:
    return f"""Run the sole ad-template process as the builder agent. Inspect the attached source pixels, remove advertiser identity (names, logos, phones, URLs, portraits), and return candidate JSON only. Hermes renders and reviews it; never self-score or invent review evidence.
Exact candidate shape: {{"template": {{"feed": {{"width":1080,"height":1350,"layers":[...]}}, "story": {{"width":1080,"height":1920,"layers":[...]}}}}}}.
Every layer must have a unique id, type, x,y,width,height,z. Allowed types: background, rect, shape, image/media/photo, text/headline/copy/label. Editable image layers use source-free assetKey/placeholder only; never source_path, original source bytes, or a full-source plate. Text uses text, font_family, font_size, color, align, max_lines, line_height. Keep all bounds inside the exact canvas and preserve safe margins; make Story a native composition, not a square crop. Include replaceable media/text slots and metadata. Hermes calls one separate comparator per iteration and two fresh final reviewers only after comparator >= {THRESHOLD}. Run {run_id}; project {project_id}; fixed placements {json.dumps(placements)}; brief {brief[:4000]}; prior reviewer feedback {feedback[:3000]}"""

def vision_message(text: str, paths: List[str]) -> List[Dict[str, Any]]:
    parts: List[Dict[str, Any]] = [{"type": "text", "text": text}]
    for raw in paths:
        path = Path(str(raw)).expanduser().resolve()
        if not path.is_file():
            raise AdTemplateProcessError("vision input image is missing")
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"}})
    if len(parts) == 1:
        raise AdTemplateProcessError("vision role requires attached image pixels")
    return parts

class SoleProcessOrchestrator:
    """Runs builder, comparator, final reviewers, renderer, and importer as separate roles."""
    def __init__(self, *, call_agent: Callable[[str, Any, str], Dict[str, Any]], workspace: Path, run_id: str, project_id: str, emit: Callable[[str, str, Dict[str, Any]], None]):
        self.call_agent, self.workspace, self.run_id, self.project_id, self.emit = call_agent, workspace, run_id, project_id, emit

    def run(self, *, source: str, brief: str, placements: Any, routes: List[Dict[str, str]], review_round: int = 0, total_iterations: int = 0, feedback: str = "", history: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
        if len(routes) < 4: raise AdTemplateProcessError("builder, comparator, and two final reviewers require four configured roles")
        history = list(history or [])
        iterations = []
        candidate: Dict[str, Any] = {}
        for offset in range(31 - total_iterations - 1):
            index = total_iterations + offset + 1
            self.emit("stage.started", "analyse", {"iteration": index, "role": "builder"})
            candidate = self.call_agent("builder-%d" % index, vision_message(generator_prompt(run_id=self.run_id, project_id=self.project_id, brief=brief, placements=placements, source=source, feedback=feedback), [source]), f"{routes[0].get('provider')}/{routes[0].get('model')}")
            if not isinstance(candidate, dict): raise AdTemplateProcessError("builder returned invalid candidate")
            self.emit("iteration.started", "analyse", {"iteration": index, "role": "builder"})
            # Render every candidate before comparison. This makes each
            # iteration preview a real generator artifact, not an agent claim.
            iteration_workspace = self.workspace / "iterations" / f"{index:02d}"
            rendered = run_generator_cli(candidate, iteration_workspace)
            preview_receipt = validate_artifacts(rendered, iteration_workspace)
            public_preview_root = self.workspace / "previews"
            public_preview_root.mkdir(parents=True, exist_ok=True)
            public_previews = []
            for item in rendered.get("previews", []):
                placement = str(item.get("placement") or "preview").lower()
                destination = public_preview_root / f"iteration-{index:02d}-{placement}.png"
                shutil.copyfile(str(item["path"]), destination)
                public_previews.append({"name": destination.name, "path": str(destination), "placement": placement})
            public_render = {}
            for placement in ("feed", "story"):
                destination = public_preview_root / f"iteration-{index:02d}-{placement}.png"
                public_render[placement] = str(destination)
            rendered["previews"] = public_previews
            rendered["render"] = public_render
            validate_artifacts(rendered, self.workspace)
            self.emit("iteration.rendered", "render", {"iteration": index, "previews": [{"name": str(x.get("name") or ""), "placement": str(x.get("placement") or "")} for x in public_previews]})
            candidate = {**candidate, **rendered}
            comparison = self.call_agent("comparator-%d" % index, vision_message("Compare the attached source image against the attached rendered Feed and Story previews. Return JSON with numeric score and concrete reason.", [source, str((rendered.get("render") or {}).get("feed") or ""), str((rendered.get("render") or {}).get("story") or "")]), f"{routes[1].get('provider')}/{routes[1].get('model')}")
            evidence = _assessment(comparison, "comparator")
            score, reason = evidence["score"], evidence["reason"]
            decision = "accepted" if score >= THRESHOLD else "revise"
            record = {"iteration": index, "candidate": candidate, "comparison": evidence, "decision": decision}
            iterations.append(record)
            self.emit("iteration.compared", "compare", {"iteration": index, "score": score, "reason": reason, "decision": decision, "preview_names": [str(x.get("name")) for x in candidate.get("previews", []) if isinstance(x, dict)]})
            if score >= THRESHOLD: break
            feedback = reason
        if not iterations or iterations[-1]["decision"] != "accepted": raise AdTemplateProcessError("comparator never reached threshold")
        reviewers = []
        for n, route in enumerate(routes[2:4], 1):
            identity = f"final-reviewer-{self.run_id}-{n}-{uuid.uuid4().hex[:8]}"
            provider_route = f"{route.get('provider')}/{route.get('model')}"
            route_identity = f"final-review-{n}:{provider_route}"
            self.emit("final-review.started", "final-review", {"reviewer": identity, "route": route_identity})
            review = self.call_agent(identity, vision_message("Perform an independent final review of the attached source, Feed, and native Story renders. Return JSON with numeric score and concrete reason.", [source, str((candidate.get("render") or {}).get("feed") or ""), str((candidate.get("render") or {}).get("story") or "")]), provider_route)
            if not isinstance(review, dict): raise AdTemplateProcessError("final reviewer returned invalid result")
            evidence = _assessment(review, "final reviewer")
            reviewers.append({"id": identity, "route": route_identity, **evidence})
        final_review = validate_final_review({"reviewers": reviewers}, accepted=True)
        if final_review["decision"] != "accepted":
            self.emit("final-review.completed", "final-review", {"decision": "revise", "reviewers": final_review["reviewers"]})
            if review_round >= 5 or total_iterations + len(iterations) >= 30:
                raise AdTemplateProcessError("final reviewers failed after the bounded automatic revision loop")
            iterations[-1]["final_review_failed"] = True
            reasons = "; ".join(f"{item['id']}: {item['reason']}" for item in final_review["reviewers"])
            return self.run(source=source, brief=brief, placements=placements, routes=routes, review_round=review_round + 1, total_iterations=total_iterations + len(iterations), feedback=reasons, history=history + iterations)
        self.emit("final-review.completed", "final-review", {"decision": "accepted", "reviewers": final_review["reviewers"]})
        generated = run_generator_cli(candidate, self.workspace)
        documents = deterministic_documents(generated.get("template") or candidate.get("template"))
        validate_artifacts(generated, self.workspace)
        imported = import_template({**generated, "documents": documents}, run_id=self.run_id, project_id=self.project_id)
        self.emit("template.imported", "import", imported)
        return {"template": generated.get("template") or candidate.get("template"), "iterations": history + iterations, "final_review": final_review, "previews": generated.get("previews"), "documents": documents, "template_path": generated.get("template_path"), "render_path": generated.get("render_path") or generated.get("render", {}).get("feed"), "import": imported, "process": "only-ad-template-process"}
