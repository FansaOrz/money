"""研究运行的确定性环境、随机种子与平台指纹。"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import random
import sys
from typing import Any

import numpy as np


DETERMINISTIC_ENVIRONMENT = {
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "PYTHONHASHSEED": "0",
}


def configure_determinism(seed: int = 0) -> dict[str, object]:
    for name, value in DETERMINISTIC_ENVIRONMENT.items():
        os.environ[name] = value
    random.seed(seed)
    np.random.seed(seed)
    return environment_fingerprint(seed=seed)


def environment_fingerprint(*, seed: int) -> dict[str, object]:
    dependencies = {
        name: importlib.metadata.version(name)
        for name in ("numpy", "pandas", "scipy", "cvxpy", "sqlalchemy")
    }
    payload: dict[str, Any] = {
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "dependencies": dependencies,
        "seed": seed,
        "thread_environment": {
            key: os.environ.get(key) for key in DETERMINISTIC_ENVIRONMENT
        },
        "limitations": [
            "跨 CPU/BLAS 实现的浮点末位可能不同",
            "并行求解器必须另行声明确定性保证",
        ],
    }
    payload["sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return payload
