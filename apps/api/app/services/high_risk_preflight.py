"""高风险操作的可复核摘要、幂等键和双重确认门禁。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime


def evaluate_preflight(
    *,
    operation: str,
    target: str,
    impact: str,
    data_fresh: bool,
    clean_workspace: bool,
    evidence_consistent: bool,
    ledger_balanced: bool,
    idempotency_key: str,
    confirmation_digest: str | None = None,
) -> dict[str, object]:
    blockers = [
        message
        for condition, message in (
            (not data_fresh, "数据过期"),
            (not clean_workspace, "工作区未冻结"),
            (not evidence_consistent, "证据不一致"),
            (not ledger_balanced, "账户账本不平"),
            (not bool(idempotency_key.strip()), "缺少幂等键"),
        )
        if condition
    ]
    summary = {
        "operation": operation,
        "target": target,
        "impact": impact,
        "evaluated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "blockers": blockers,
        "idempotency_key": idempotency_key,
    }
    digest_payload = {key: value for key, value in summary.items() if key != "evaluated_at"}
    digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    summary["confirmation_digest"] = digest
    summary["allowed"] = not blockers and confirmation_digest == digest
    if not blockers and confirmation_digest != digest:
        summary["blockers"] = ["需要使用当前摘要哈希二次确认"]
    return summary
