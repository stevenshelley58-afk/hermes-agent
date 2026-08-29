"""Sole Frank -> Hermes -> ad-template-generator process."""
from __future__ import annotations
import copy, json, re
from typing import Any, Dict, List

THRESHOLD = 9.5
STAGES = ("source", "analyse", "decompose", "restyle", "story-draft", "render", "compare", "qa", "final-review", "import")

class AdTemplateProcessError(ValueError):
    pass

def _number(value: Any) -> float:
    try: result = float(value)
    except (TypeError, ValueError): raise AdTemplateProcessError("score must be numeric") from None
    if not 0 <= result <= 10: raise AdTemplateProcessError("score must be between 0 and 10")
    return result

def validate_iterations(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > 30: raise AdTemplateProcessError("iterations must contain 1 to 30 records")
    result, accepted = [], False
    for index, raw in enumerate(value, 1):
        if not isinstance(raw, dict) or raw.get("iteration", index) != index: raise AdTemplateProcessError("iterations must be consecutive and one-based")
        comparison = raw.get("comparison")
        if not isinstance(comparison, dict): raise AdTemplateProcessError("each iteration requires one comparator")
        if any(key in raw or key in comparison for key in ("reviewers", "reviewer", "primary", "strict")): raise AdTemplateProcessError("final reviewers are only allowed after the comparator passes")
        score = _number(comparison.get("score"))
        decision = str(raw.get("decision") or ("accepted" if score >= THRESHOLD else "revise"))
        expected = "accepted" if score >= THRESHOLD else "revise"
        if decision != expected: raise AdTemplateProcessError("iteration decision does not match comparator score")
        reason = str(comparison.get("reason") or raw.get("revision_reason") or "").strip()
        if len(reason) < 3: raise AdTemplateProcessError("comparator must explain its decision")
        if accepted: raise AdTemplateProcessError("no iteration may follow an accepted candidate")
        accepted = score >= THRESHOLD
        item = copy.deepcopy(raw); item.update(iteration=index, comparison={"score": score, "reason": reason}, decision=decision); result.append(item)
    return result

def validate_final_review(value: Any, *, accepted: bool) -> Dict[str, Any]:
    if not accepted:
        if value not in (None, {}, []): raise AdTemplateProcessError("final reviewers cannot run before a passing comparison")
        return {}
    if not isinstance(value, dict) or not isinstance(value.get("reviewers"), list) or len(value["reviewers"]) != 2: raise AdTemplateProcessError("exactly two final reviewers are required")
    normalized = []
    for item in value["reviewers"]:
        if not isinstance(item, dict): raise AdTemplateProcessError("reviewer record must be an object")
        identity = str(item.get("id") or item.get("name") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._:-]{2,160}", identity): raise AdTemplateProcessError("reviewer identity is invalid")
        score = _number(item.get("score")); reason = str(item.get("reason") or "").strip()
        if len(reason) < 3: raise AdTemplateProcessError("reviewer must explain its decision")
        normalized.append({"id": identity, "score": score, "reason": reason})
    if normalized[0]["id"] == normalized[1]["id"]: raise AdTemplateProcessError("final reviewers must be independent")
    passed = all(item["score"] >= THRESHOLD for item in normalized); decision = "accepted" if passed else "revise"
    if str(value.get("decision") or decision) != decision: raise AdTemplateProcessError("final review decision does not match scores")
    return {"reviewers": normalized, "decision": decision, "threshold": THRESHOLD}

def deterministic_documents(template: Any) -> Dict[str, str]:
    if not isinstance(template, dict): raise AdTemplateProcessError("template candidate must be an object")
    feed = template.get("feed") if isinstance(template.get("feed"), dict) else {}
    story = template.get("story") if isinstance(template.get("story"), dict) else {}
    if not feed or not story: raise AdTemplateProcessError("template must contain Feed and Story documents")
    encode = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"feed.json": encode(feed), "story.json": encode(story), "template.json": encode({"feed": feed, "story": story})}

def generator_prompt(*, run_id: str, project_id: str, brief: str, placements: Any, source: str) -> str:
    return f"""Run the sole ad-template process on the VPS as Hermes' generator tool. Read {source}, then autonomously iterate through {', '.join(STAGES)}.
Every iteration creates Feed and Story candidates and exactly one comparator score. Scores below {THRESHOLD} revise and continue. Only after a passing comparator may exactly two independent final reviewers run; if either fails, return to the loop automatically. When both pass, create deterministic Feed, Story, template JSON documents and deterministic renders.
Do not use vaults, private upload protocols, source/artifact hashes, signatures, approvals, or release receipts. Return one JSON object with template, iterations, final_review, previews, template_path, render_path, and import.status=ready. Run {run_id}; project {project_id}; placements {json.dumps(placements)}; brief {brief[:4000]}"""
