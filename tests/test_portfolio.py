"""Portfolio presentation helpers."""
from __future__ import annotations

from app import portfolio


def test_holdings_are_grouped_by_account_with_subtotals():
    rows = [
        {"account_name": "Roth", "symbol": "SPY", "value_cents": 100, "gain_cents": 10},
        {"account_name": "IRA", "symbol": "NVDA", "value_cents": 200, "gain_cents": -5},
        {"account_name": "Roth", "symbol": "TSM", "value_cents": 50, "gain_cents": 1},
    ]
    groups = portfolio.group_by_account(rows)

    assert [g["name"] for g in groups] == ["IRA", "Roth"]
    assert groups[0]["value_cents"] == 200
    assert groups[1]["value_cents"] == 150
    assert groups[1]["gain_cents"] == 11
    assert [r["symbol"] for r in groups[1]["rows"]] == ["SPY", "TSM"]


def test_group_by_account_on_an_empty_book():
    assert portfolio.group_by_account([]) == []
