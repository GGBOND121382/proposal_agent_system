from __future__ import annotations

import argparse
import json
import os
from contextlib import contextmanager
from typing import Iterator

from app.runtime_policy import CapabilityModeError, CapabilityPolicy


@contextmanager
def patched_environment(values: dict[str, str | None]) -> Iterator[None]:
    before = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def self_test() -> dict[str, object]:
    cases = []
    with patched_environment({"CAPABILITY_ACCEPTANCE_MODE": "true", "MODEL_RUNTIME_MODE": "REPLAY"}):
        try:
            CapabilityPolicy.from_environment().assert_environment("REPLAY")
        except CapabilityModeError:
            cases.append({"case": "reject_replay", "status": "PASS"})
        else:
            raise AssertionError("REPLAY was not rejected")
    with patched_environment(
        {
            "CAPABILITY_ACCEPTANCE_MODE": "true",
            "MODEL_RUNTIME_MODE": "LIVE",
            "PUBLIC_SEARCH_PROVIDER": "recorded",
        }
    ):
        try:
            CapabilityPolicy.from_environment().assert_environment("LIVE")
        except CapabilityModeError:
            cases.append({"case": "reject_recorded_provider", "status": "PASS"})
        else:
            raise AssertionError("recorded provider was not rejected")
    with patched_environment(
        {
            "CAPABILITY_ACCEPTANCE_MODE": "true",
            "MODEL_RUNTIME_MODE": "LIVE",
            "PUBLIC_SEARCH_PROVIDER": "disabled",
            "MODEL_RESPONSE_AUTOMATION": "false",
            "SAMPLE_SECTION_FALLBACK": "false",
            "AUTO_RESPONSE_ENABLED": "false",
        }
    ):
        CapabilityPolicy.from_environment().assert_environment("LIVE")
        cases.append({"case": "accept_live_only", "status": "PASS"})
    return {"status": "PASS", "cases": cases}


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Track-A capability runtime hard gates.")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        report = self_test()
    else:
        policy = CapabilityPolicy.from_environment()
        policy.assert_environment(os.getenv("MODEL_RUNTIME_MODE", "REPLAY"))
        report = {
            "status": "PASS",
            "capability_acceptance_mode": policy.enabled,
            "runtime_mode": os.getenv("MODEL_RUNTIME_MODE", "REPLAY").upper(),
            "public_search_provider": os.getenv("PUBLIC_SEARCH_PROVIDER", "disabled"),
        }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
