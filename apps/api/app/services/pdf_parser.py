"""支付宝基金资产证明与交易明细 PDF 解析器。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import fitz

MONEY_RE = re.compile(r"^\d+\.\d{2}$")
NAV_RE = re.compile(r"^\d+\.\d{3,4}$")
DATE8_RE = re.compile(r"^20\d{6}$")
CODE_RE = re.compile(r"^\d{6}$")
ACCOUNT_RE = re.compile(r"^\d{15,20}$")
ORDER_RE = re.compile(r"^\d{32}(?:\d{8})?$")
DATETIME_RE = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}$")


class PdfParseError(ValueError):
    """PDF 内容无法通过结构校验。"""


@dataclass
class ParsedDocument:
    document_type: str
    snapshot_date: str | None
    positions: list[dict[str, Any]]
    transactions: list[dict[str, Any]]
    warnings: list[str]
    summary: dict[str, Any] | None = None


TRANSACTION_TYPE_MAP = {
    "定投买入": "buy",
    "用户买入": "buy",
    "用户认购": "buy",
    "用户卖出": "sell",
    "定投卖出": "sell",
    "机构分红": "dividend",
    "用户跨TA转换": "other",
}


def parse_pdf(content: bytes) -> ParsedDocument:
    try:
        document = fitz.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise PdfParseError("无法打开 PDF 文件") from exc

    try:
        cover = "".join((document[0].get_text("text") if document.page_count else "").split())
        if "基金资产证明" in cover or "资产证明" in cover:
            return _parse_positions(document)
        if "基金交易明细" in cover or "交易明细" in cover:
            return _parse_transactions(document)
        raise PdfParseError("不是受支持的支付宝基金资产证明或交易明细")
    finally:
        document.close()


def _extract_cover_total(text: str) -> Decimal | None:
    match = re.search(r"合计约为\s*([\d.]+)元", text)
    return Decimal(match.group(1)) if match else None


def _parse_positions(document: fitz.Document) -> ParsedDocument:
    positions: list[dict[str, Any]] = []
    pending_name = ""

    for page_index in range(1, document.page_count):
        words = document[page_index].get_text("words")
        codes = [w for w in words if 265 < w[0] < 305 and CODE_RE.fullmatch(w[4])]

        # 页首名称残片属于上一页末尾的跨页记录。
        first_code_y = min((w[1] for w in codes), default=842)
        fragments = [
            w
            for w in words
            if 160 < w[0] < 265 and w[3] < first_code_y - 2 and w[1] < 80
        ]
        if fragments and positions:
            positions[-1]["fund_name"] += "".join(w[4] for w in sorted(fragments, key=lambda x: (x[1], x[0])))

        for code_word in codes:
            center_y = (code_word[1] + code_word[3]) / 2

            def same_row(x_min: float, x_max: float, delta: float = 5) -> list:
                return [
                    w for w in words
                    if x_min < w[0] < x_max
                    and abs(((w[1] + w[3]) / 2) - center_y) < delta
                ]

            def join_cell(x_min: float, x_max: float) -> str:
                return "".join(w[4] for w in sorted(same_row(x_min, x_max), key=lambda x: x[0]))

            account = join_cell(85, 160)
            shares = join_cell(310, 360)
            nav = join_cell(365, 410)
            nav_date = join_cell(405, 470)
            market_value = join_cell(475, 530)

            name_words = [
                w for w in words
                if 160 < w[0] < 265
                and center_y - 12 <= ((w[1] + w[3]) / 2) <= center_y + 12
            ]
            name = "".join(w[4] for w in sorted(name_words, key=lambda x: (x[1], x[0])))
            name = pending_name + name
            pending_name = ""

            if not (
                ACCOUNT_RE.fullmatch(account)
                and MONEY_RE.fullmatch(shares)
                and NAV_RE.fullmatch(nav)
                and DATE8_RE.fullmatch(nav_date)
                and MONEY_RE.fullmatch(market_value)
                and name
            ):
                raise PdfParseError(f"资产证明第 {page_index + 1} 页存在无法识别的持仓记录")

            positions.append(
                {
                    "account_key": account,
                    "fund_code": code_word[4],
                    "fund_name": name,
                    "shares": shares,
                    "nav": nav,
                    "nav_date": datetime.strptime(nav_date, "%Y%m%d").date().isoformat(),
                    "market_value": market_value,
                }
            )

    if not positions:
        raise PdfParseError("资产证明中没有识别到持仓")

    total = sum(Decimal(item["market_value"]) for item in positions)
    cover_total = _extract_cover_total(document[0].get_text("text"))
    if cover_total is not None and abs(total - cover_total) > Decimal("0.01"):
        raise PdfParseError(f"资产明细合计与封面相差 {abs(total - cover_total)} 元")

    snapshot_date = max(item["nav_date"] for item in positions)
    return ParsedDocument(
        document_type="positions",
        snapshot_date=snapshot_date,
        positions=positions,
        transactions=[],
        warnings=["资产证明中的单位净值保留位数有限，份额乘净值可能与资产小计有少量舍入差异。"],
        summary={
            "total_market_value": str(total),
            "total_cost": "0.00",
            "total_profit": str(total),
            "profit_rate": None,
            "total_return_rate": None,
            "position_count": len(positions),
            "snapshot_date": snapshot_date,
        },
    )


def _horizontal_boundaries(page: fitz.Page) -> list[float]:
    values: list[float] = []
    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        if rect is None or rect.width <= 30 or rect.height > 1.5:
            continue
        values.append(round((rect.y0 + rect.y1) / 2, 1))
    return sorted(set(values))


def _row_bands(page: fitz.Page) -> list[tuple[float, float]]:
    boundaries = _horizontal_boundaries(page)
    bands: list[tuple[float, float]] = []
    for top, bottom in zip(boundaries, boundaries[1:]):
        if bottom - top > 10 and bottom < 806:
            bands.append((top, bottom))
    return bands


def _spans(page: fitz.Page) -> list[tuple[float, float, float, float, str]]:
    values: list[tuple[float, float, float, float, str]] = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "")
                if text:
                    x0, y0, x1, y1 = span["bbox"]
                    values.append((x0, y0, x1, y1, text))
    return values


def _parse_transactions(document: fitz.Document) -> ParsedDocument:
    column_edges = [59, 98.7, 137.7, 176.7, 215.7, 269.7, 308.7, 347.7, 386.7, 425.7, 464.7, 496.2, 535.5]
    records: list[list[str]] = []
    current: list[str] | None = None

    for page_index in range(1, document.page_count):
        page = document[page_index]
        spans = _spans(page)
        for top, bottom in _row_bands(page):
            cells = ["" for _ in range(12)]
            row_spans = sorted(
                (s for s in spans if top < s[1] and s[3] < bottom),
                key=lambda s: (s[1], s[0]),
            )
            for x0, _y0, _x1, _y1, text in row_spans:
                for index in range(12):
                    if column_edges[index] <= x0 < column_edges[index + 1]:
                        cells[index] += text.strip()
                        break

            if not any(cells):
                continue
            if re.match(r"^20(?:2[1-6])(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])", cells[0]):
                if current is not None:
                    records.append(current)
                current = cells
            elif current is not None:
                current = [old + extra for old, extra in zip(current, cells)]

    if current is not None:
        records.append(current)

    transactions: list[dict[str, Any]] = []
    warnings: list[str] = []
    duplicate_orders: set[str] = set()
    seen_orders: set[str] = set()

    for index, cells in enumerate(records, 1):
        order_no, trade_time, trade_type, fund_name, _group, fund_code, *amounts, confirm_time = cells
        if not (
            ORDER_RE.fullmatch(order_no)
            and DATETIME_RE.fullmatch(trade_time)
            and CODE_RE.fullmatch(fund_code)
            and DATETIME_RE.fullmatch(confirm_time)
            and all(
                value == "/" or MONEY_RE.fullmatch(value) or re.fullmatch(r"0(?:\.00)?", value)
                for value in amounts
            )
        ):
            raise PdfParseError(f"交易明细第 {index} 条记录未通过结构校验：{cells}")
        if trade_type not in TRANSACTION_TYPE_MAP:
            raise PdfParseError(f"发现不支持的交易类型：{trade_type}")
        if order_no in seen_orders:
            duplicate_orders.add(order_no)
        seen_orders.add(order_no)

        apply_amount, apply_shares, confirmed_amount, confirmed_shares, fee = amounts
        normalized_type = TRANSACTION_TYPE_MAP[trade_type]
        signed_shares = None if confirmed_shares == "/" else Decimal(confirmed_shares)
        if normalized_type == "sell" and signed_shares is not None:
            signed_shares = -signed_shares
        amount = Decimal("0") if confirmed_amount == "/" else Decimal(confirmed_amount)

        transactions.append(
            {
                "order_no": order_no,
                "transaction_date": datetime.strptime(trade_time, "%Y/%m/%d %H:%M").date().isoformat(),
                "confirmation_date": datetime.strptime(confirm_time, "%Y/%m/%d %H:%M").date().isoformat(),
                "transaction_type": normalized_type,
                "source_type": trade_type,
                "fund_code": fund_code,
                "fund_name": fund_name,
                "amount": str(amount),
                "shares": None if signed_shares is None else str(signed_shares),
                "nav": None,
                "fee": fee,
                "apply_amount": None if apply_amount == "/" else apply_amount,
                "apply_shares": None if apply_shares == "/" else apply_shares,
            }
        )

    if not transactions:
        raise PdfParseError("交易明细中没有识别到交易")
    if duplicate_orders:
        warnings.append(
            f"检测到 {len(duplicate_orders)} 个订单号对应多笔真实确认记录，导入时将按订单、基金和交易类型联合去重。"
        )

    return ParsedDocument(
        document_type="transactions",
        snapshot_date=max(item["confirmation_date"] for item in transactions),
        positions=[],
        transactions=transactions,
        warnings=warnings,
        summary=None,
    )
