"""Read-only entry point to the generator's output validator, not a second engine."""
import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--feed", type=Path, required=True)
    parser.add_argument("--story", type=Path, required=True)
    args = parser.parse_args()
    # This script is bundled with the immutable generator release. A copied
    # standalone skill should use its serving release, not silently import
    # unrelated user-installed code.
    root = Path(__file__).resolve().parents[4]
    if not (root / "gateway" / "ad_template_output_qa.py").is_file():
        parser.error("Run this helper from the verified Hermes source or release skill.")
    sys.path.insert(0, str(root))
    from gateway.ad_template_output_qa import run_ad_output_qa
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    result = run_ad_output_qa(candidate, {"feed": args.feed, "story": args.story})
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return {"pass": 0, "fail": 2, "needs_review": 3}.get(result.get("status"), 3)


if __name__ == "__main__":
    raise SystemExit(main())
