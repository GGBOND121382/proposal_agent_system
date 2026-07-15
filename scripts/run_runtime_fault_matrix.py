from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.runtime_evidence import FaultInjector, InjectedFailure


FAULT_POINTS = [
    "before_request_persist",
    "after_request_persist",
    "before_model_request",
    "after_response_persist",
    "before_db_transaction",
    "after_db_transaction",
    "after_critic_commit",
    "after_repair_commit",
    "after_gate_created",
    "before_export",
    "after_export",
]


def run_self_test() -> dict[str, object]:
    results = []
    with tempfile.TemporaryDirectory(prefix="track-a-faults-") as temp_dir:
        injector = FaultInjector(Path(temp_dir))
        old_points = os.environ.get("RUNTIME_FAULT_POINT")
        old_action = os.environ.get("RUNTIME_FAULT_ACTION")
        try:
            os.environ["RUNTIME_FAULT_ACTION"] = "raise"
            for index, point in enumerate(FAULT_POINTS):
                call_key = f"self-test-{index}"
                os.environ["RUNTIME_FAULT_POINT"] = point
                try:
                    injector.hit(point, call_key)
                except InjectedFailure:
                    pass
                else:
                    raise AssertionError(f"Fault point {point} did not fire")
                injector.hit(point, call_key)
                results.append({"point": point, "first_fire": "PASS", "resume_once": "PASS"})
        finally:
            if old_points is None:
                os.environ.pop("RUNTIME_FAULT_POINT", None)
            else:
                os.environ["RUNTIME_FAULT_POINT"] = old_points
            if old_action is None:
                os.environ.pop("RUNTIME_FAULT_ACTION", None)
            else:
                os.environ["RUNTIME_FAULT_ACTION"] = old_action
    return {"status": "PASS", "points": results}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Track-A durable fault-injection self-test.")
    parser.add_argument("--self-test", action="store_true", default=True)
    parser.parse_args()
    report = run_self_test()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
