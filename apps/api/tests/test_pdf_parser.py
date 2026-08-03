"""基于现有支付宝 PDF 样例的解析回归测试。"""

from pathlib import Path

import pytest

from app.services.pdf_parser import parse_pdf

PDF_DIR = Path("/root/Src/money/tmp")


def _pdfs() -> list[Path]:
    return sorted(PDF_DIR.glob("*.pdf"))


@pytest.mark.skipif(len(_pdfs()) < 2, reason="缺少支付宝 PDF 样例")
def test_position_pdf_parses_all_records_and_total() -> None:
    position_pdf = next(p for p in _pdfs() if "211851" in p.name)
    result = parse_pdf(position_pdf.read_bytes())
    assert result.document_type == "positions"
    assert len(result.positions) == 183
    assert result.summary is not None
    assert result.summary["total_market_value"] == "778655.69"


@pytest.mark.skipif(len(_pdfs()) < 2, reason="缺少支付宝 PDF 样例")
def test_transaction_pdf_parses_all_records() -> None:
    transaction_pdf = next(p for p in _pdfs() if "225019" in p.name)
    result = parse_pdf(transaction_pdf.read_bytes())
    assert result.document_type == "transactions"
    assert len(result.transactions) == 2489
    assert all(len(item["order_no"]) in (32, 40) for item in result.transactions)
