"""Run optional Ragas metrics on a small synthetic fixture; requires explicit adapters."""
from __future__ import annotations
import argparse
import importlib
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.services.ragas_eval import load_eval_fixture, run_ragas

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", default=str(ROOT / "tests/fixtures/synthetic_ragas_v1.json"))
    parser.add_argument("--adapters-module", help="Optional module exposing build_llm() and build_embeddings().")
    args = parser.parse_args()
    fixture = load_eval_fixture(args.fixture)
    llm = embeddings = None
    if args.adapters_module:
        module = importlib.import_module(args.adapters_module)
        llm, embeddings = module.build_llm(), module.build_embeddings()
    result = run_ragas(fixture, llm=llm, embeddings=embeddings)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["status"] in {"completed", "unavailable", "configuration_required"} else 2

if __name__ == "__main__":
    raise SystemExit(main())
