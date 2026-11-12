#!/usr/bin/env python3
"""Submit the current declared candidate to the trusted coordinator."""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import socket
import sys
import time
from pathlib import Path

MAX_NOTICES = 5
MAX_NOTICE_CHARS = 200


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        for attempt in range(3):
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(self.timeout)
            try:
                connection.connect(self.socket_path)
                self.sock = connection
                return
            except OSError:
                connection.close()
                if attempt == 2:
                    raise
                time.sleep(0.25 * (attempt + 1))


def fail(category: str, remaining_submissions: int | None = None) -> None:
    payload: dict[str, object] = {"error": category}
    if remaining_submissions is not None:
        payload["remaining_submissions"] = remaining_submissions
    print(json.dumps(payload, separators=(",", ":")), file=sys.stderr)
    raise SystemExit(2)


def parse_failure(payload: bytes) -> tuple[str, int | None]:
    try:
        failure = json.loads(payload)
        category = str(failure.get("error", "request_failed"))
        remaining = failure.get("remaining_submissions")
        if not isinstance(remaining, int) or isinstance(remaining, bool):
            remaining = None
        return category, remaining
    except Exception:
        return "request_failed", None


def valid_notices(value: object) -> list[str]:
    """Optional advisory notices; anything malformed is dropped, not an error."""
    if not isinstance(value, list):
        return []
    notices = [
        item
        for item in value
        if isinstance(item, str)
        and 0 < len(item) <= MAX_NOTICE_CHARS
        and item.strip()
        and all(" " <= character <= "~" for character in item)
    ]
    return notices[:MAX_NOTICES]


def validate_result(result: object) -> dict[str, object]:
    if not isinstance(result, dict):
        fail("invalid_response")
    try:
        score = float(result["score"])
        best_score = float(result["best_score"])
        is_best = result["is_best"]
        baseline = result.get("baseline", False)
        submission_index = result["submission_index"]
        remaining = result["remaining_submissions"]
    except (KeyError, TypeError, ValueError):
        fail("invalid_response")
    if (
        not math.isfinite(score)
        or not 0.0 <= score <= 1.0
        or not math.isfinite(best_score)
        or not 0.0 <= best_score <= 1.0
        or best_score < score
        or not isinstance(is_best, bool)
        or not isinstance(baseline, bool)
        or not isinstance(submission_index, int)
        or isinstance(submission_index, bool)
        or submission_index < 1
        or not isinstance(remaining, int)
        or isinstance(remaining, bool)
        or remaining < 0
    ):
        fail("invalid_response")
    output: dict[str, object] = {
        "score": round(score, 6),
        "best_score": round(best_score, 6),
        "is_best": is_best,
        "baseline": baseline,
        "submission_index": submission_index,
        "remaining_submissions": remaining,
    }
    notices = valid_notices(result.get("notices"))
    if notices:
        output["notices"] = notices
    return output


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Submit the current candidate for evaluation. A successful submission consumes one evaluation; --help does not submit."
    )
    parser.parse_args()
    try:
        task_id = Path("/etc/gamebench-autoresearch-task").read_text().strip()
    except OSError:
        fail("not_configured")
    body = json.dumps(
        {"protocol": 1, "task_id": task_id, "container_id": socket.gethostname()},
        separators=(",", ":"),
    ).encode()
    headers = {"Content-Type": "application/json"}
    if os.environ.get("GAMEBENCH_VERIFY_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GAMEBENCH_VERIFY_TOKEN"]
    socket_path = os.environ.get(
        "GAMEBENCH_VERIFY_SOCKET", "/run/gamebench/submit.sock"
    )
    connection = UnixHTTPConnection(socket_path, timeout=3600)
    try:
        connection.request("POST", "/v1/submit", body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        if response.status >= 400:
            fail(*parse_failure(payload))
        result = json.loads(payload)
    except SystemExit:
        raise
    except Exception:
        fail("unavailable")
    finally:
        connection.close()
    print(json.dumps(validate_result(result), separators=(",", ":")))


if __name__ == "__main__":
    main()
