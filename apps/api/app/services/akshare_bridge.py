"""受限 AkShare 子进程桥；第三方 SDK 退出时不得拖垮常驻调度器。"""

from __future__ import annotations

import json
import sys

ALLOWED = {
    "news_trade_notify_dividend_baidu",
    "news_trade_notify_suspend_baidu",
    "index_stock_cons_weight_csindex",
    "stock_dividend_cninfo",
    "stock_info_change_name",
    "stock_info_sh_name_code",
    "stock_info_sz_name_code",
    "stock_zh_a_spot_em",
}


def main() -> None:
    import akshare as ak

    request = json.loads(sys.stdin.read())
    result: list[dict[str, object]] = []
    for call in request["calls"]:
        name = str(call["function"])
        if name not in ALLOWED:
            raise ValueError(f"不允许调用 AkShare 接口：{name}")
        frame = getattr(ak, name)(**dict(call.get("kwargs") or {}))
        rows = frame.to_dict("records")
        for row in rows:
            normalized: dict[str, object] = {}
            for key, value in row.items():
                if hasattr(value, "isoformat"):
                    value = value.isoformat()
                elif hasattr(value, "item"):
                    value = value.item()
                normalized[str(key)] = value
            result.append(normalized)
    print("__AKSHARE_RESULT__" + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
