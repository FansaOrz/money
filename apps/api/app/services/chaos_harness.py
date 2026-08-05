"""可注入故障的安全停止与幂等恢复演练器。"""

from __future__ import annotations


FAILURE_TYPES = {
    "source_timeout",
    "empty_response",
    "database_lock",
    "disk_full",
    "process_crash",
    "duplicate_schedule",
}


def run_drill(failure: str, operation, *, idempotency_key: str) -> dict[str, object]:
    if failure not in FAILURE_TYPES:
        raise ValueError("未知故障类型")
    try:
        operation(failure, idempotency_key)
    except Exception as exc:  # noqa: BLE001
        return {
            "failure": failure,
            "safe_stopped": True,
            "alert_required": True,
            "retry_idempotency_key": idempotency_key,
            "error_type": type(exc).__name__,
            "unresolved": [],
        }
    return {
        "failure": failure,
        "safe_stopped": failure in {"empty_response", "duplicate_schedule"},
        "alert_required": True,
        "retry_idempotency_key": idempotency_key,
        "unresolved": [],
    }
