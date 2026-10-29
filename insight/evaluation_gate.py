"""Offline evaluation gate for summary writers (narrators). Owner: platform engineer
runs it in CI; the AI layer owner (Director) decides the cases and the bar.

A writer is promoted only if every case passes the validator. Exit code 1 blocks
the merge. Each run is saved (data/evaluation_runs.jsonl) for the page's Evaluation card.

Run: python3 -m insight.evaluation_gate --narrator template
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from insight.core.engine import Domain, InsightEngine
from insight.core.evaluations import EvaluationStore
from insight.core.semantic import open_warehouse
from insight.domains import pharmaceutical

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "data" / "evaluation_runs.jsonl"


def evaluate(domain: Domain, engine: InsightEngine, narrator_name: str) -> dict[str, Any]:
    """Run every case once with a fresh narrative and return the run record."""
    narrator = domain.narrators[narrator_name]
    members = [member["code"] for member in domain.scope_members(engine.warehouse)]
    cases = [(metric, scope, audience) for metric in domain.metrics
             for scope, audience in [(None, "brand")] + [(member, "regional") for member in members]]
    started_at, started = datetime.now(UTC).isoformat(timespec="seconds"), time.perf_counter()

    results, trace = [], {}
    for metric, scope, audience in cases:
        insight = engine.build(metric, scope, audience, narrator_name, fresh=True)["insight"]
        trace = insight["trace"] or trace
        drafts = insight["claims"] or insight["blocked_draft"]
        results.append({
            "metric": metric,
            "scope": scope or "ALL",
            "audience": audience,
            "status": insight["status"],
            "cost_usd": insight["trace"].get("cost_usd", 0),
            "duration_milliseconds": insight["trace"].get("duration_milliseconds"),
            "failures": [
                {"code": violation["code"], "detail": violation["detail"],
                 "sentence": drafts[violation["claim"]]["text"] if violation["claim"] is not None else None}
                for violation in insight["violations"]
            ],
        })

    passed = sum(result["status"] == "accepted" for result in results)
    return {
        "run_id": hashlib.sha256(f"{narrator_name}{started_at}".encode()).hexdigest()[:12],
        "narrator": narrator.name,
        "narrator_version": narrator.version,
        "title": narrator.title,
        "model": trace.get("model"),
        "prompt_hash": trace.get("prompt_hash"),
        "policy_version": domain.policy.version,
        "snapshot": engine.warehouse.snapshot()["source_hash"],
        "started_at": started_at,
        "duration_seconds": round(time.perf_counter() - started, 1),
        "passed": passed,
        "total": len(results),
        "cost_usd": round(sum(result["cost_usd"] or 0 for result in results), 6),
        "cases": results,
    }


def run(narrator: str, warehouse_path: Path, verbose: bool = False, store: EvaluationStore | None = None) -> int:
    domain = pharmaceutical.load()
    engine = InsightEngine(domain, open_warehouse(warehouse_path))
    record = evaluate(domain, engine, narrator)
    for case in record["cases"]:
        codes = sorted({failure["code"] for failure in case["failures"]})
        print(f"{'PASS' if case['status'] == 'accepted' else 'FAIL'}  {case['metric']:<22} "
              f"scope={case['scope']:<4} audience={case['audience']:<8} {', '.join(codes)}")
        if verbose:
            for failure in case["failures"]:
                print(f"        ! {failure['code']}: {failure['detail']}")
                if failure["sentence"]:
                    print(f"          \"{failure['sentence']}\"")
    (store or EvaluationStore(RUNS)).append(record)
    print(f"\n{narrator}: {record['passed']}/{record['total']} cases passed"
          + (f", LLM cost ${record['cost_usd']:.4f}" if record["cost_usd"] else ""))
    return 0 if record["passed"] == record["total"] else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--narrator", default="template")
    parser.add_argument("--warehouse", type=Path, default=ROOT / "data" / "warehouse.db",
                        help="SQLite file; ignored when INSIGHT_WAREHOUSE=databricks")
    parser.add_argument("-v", "--verbose", action="store_true", help="print each violation and its sentence")
    arguments = parser.parse_args()
    sys.exit(run(arguments.narrator, arguments.warehouse, arguments.verbose))


if __name__ == "__main__":
    main()
