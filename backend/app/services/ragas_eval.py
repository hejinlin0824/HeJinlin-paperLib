"""Optional Ragas bridge; deterministic synthetic evaluation remains the default."""
from __future__ import annotations

from typing import Any


def load_eval_fixture(path: str) -> dict[str, Any]:
    import json
    from pathlib import Path

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {"questions", "answers", "contexts", "ground_truths"}
    missing = required - set(data)
    if missing:
        raise ValueError(f"evaluation fixture missing fields: {sorted(missing)}")
    lengths = {len(data[key]) for key in required}
    if len(lengths) != 1:
        raise ValueError("questions/answers/contexts/ground_truths lengths must match")
    return data


def run_ragas(fixture: dict[str, Any], llm: Any = None, embeddings: Any = None) -> dict[str, Any]:
    """Run Ragas only with explicitly supplied evaluators; never select a network default."""
    if llm is None or embeddings is None:
        return {"status": "configuration_required", "dataset_rows": len(fixture["questions"]),
                "reason": "Pass an explicit local/provider LLM and embeddings adapter; no default network model is selected."}
    try:
        from datasets import Dataset
        from ragas import evaluate
        try:
            from ragas.metrics import context_precision, context_recall, faithfulness
            metrics = [context_precision, context_recall, faithfulness]
        except ImportError:
            from ragas.metrics.collections import ContextPrecision, ContextRecall, Faithfulness
            metrics = [ContextPrecision(), ContextRecall(), Faithfulness()]
    except ImportError as exc:
        return {
            "status": "unavailable",
            "reason": f"Install backend/requirements-eval.txt to enable Ragas: {exc}",
            "dataset_rows": len(fixture["questions"]),
        }
    dataset = Dataset.from_dict({
        "question": fixture["questions"],
        "answer": fixture["answers"],
        "contexts": fixture["contexts"],
        "ground_truth": fixture["ground_truths"],
    })
    try:
        result = evaluate(dataset, metrics=metrics, llm=llm, embeddings=embeddings)
        return {"status": "completed", "dataset_rows": len(fixture["questions"]),
                "metrics": dict(result)}
    except Exception as exc:  # Ragas/provider APIs evolve; never hide version incompatibility.
        return {"status": "error", "dataset_rows": len(fixture["questions"]),
                "reason": f"{type(exc).__name__}: {str(exc)[:500]}"}
