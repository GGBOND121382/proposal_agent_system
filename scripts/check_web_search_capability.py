from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.skills.browser_worker import BrowserWorker  # noqa: E402
from app.skills.search_gateway import SearchGateway, normalize_search_queries  # noqa: E402
from app.skills.search_providers import BrowserSearchProvider  # noqa: E402
from app.util import safe_filename, utc_now, write_json  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a real Browser Search capability check without invoking any LLM."
    )
    parser.add_argument(
        "--query",
        default="human AI collaborative decision making research",
    )
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()

    settings = replace(Settings.load(), public_search_provider="browser_search")
    timestamp = utc_now().replace(":", "").replace("-", "")
    output_dir = Path(args.output_dir).resolve() if args.output_dir else (
        settings.data_dir / "capability_tests" / "web_search" / safe_filename(timestamp)
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    worker = BrowserWorker(settings, cache_dir=output_dir / "cache")
    exit_code = 1
    try:
        batch = SearchGateway(
            [
                BrowserSearchProvider(
                    settings,
                    worker=worker,
                    evidence_dir=output_dir / "raw_search_pages",
                )
            ]
        ).search(
            normalize_search_queries(
                [{"query_id": "CAPABILITY-WEB-001", "query": args.query}]
            ),
            per_query_limit=max(1, min(args.limit, 20)),
            continue_on_error=True,
        )
        runs = [item.to_dict() for item in batch.runs]
        passed = bool(
            batch.hits
            and runs
            and all(item.get("status") == "PASS" for item in runs)
        )
        receipt = {
            "schema_version": "1.0",
            "capability": "REAL_WEB_SEARCH_WITHOUT_LLM",
            "created_at": utc_now(),
            "status": "PASS" if passed else "FAIL",
            "llm_invoked": False,
            "query": args.query,
            "search_url_template": settings.browser_search_url_template,
            "provider_runs": runs,
            "failures": batch.failures,
            "hits": [item.to_candidate() for item in batch.hits],
        }
        write_json(output_dir / "capability_receipt.json", receipt)
        print(json.dumps(receipt, ensure_ascii=True, indent=2))
        print(f"receipt={output_dir / 'capability_receipt.json'}")
        exit_code = 0 if passed else 1
    except Exception as exc:
        receipt = {
            "schema_version": "1.0",
            "capability": "REAL_WEB_SEARCH_WITHOUT_LLM",
            "created_at": utc_now(),
            "status": "FAIL",
            "llm_invoked": False,
            "query": args.query,
            "error": f"{type(exc).__name__}: {exc}",
        }
        write_json(output_dir / "capability_receipt.json", receipt)
        print(json.dumps(receipt, ensure_ascii=True, indent=2))
        print(f"receipt={output_dir / 'capability_receipt.json'}")
    finally:
        worker.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
