from decimal import Decimal

import pytest

from pocketview.db import connect
from pocketview.explore import FilterError, Filters
from pocketview.store import Store


@pytest.mark.parametrize(
    "args",
    [
        {"start": "20261101"},
        {"start": "2026-02-30"},
        {"start": "2026-11-02", "end": "2026-11-01"},
        {"page": "0"},
        {"page": "-1"},
        {"page": "1.5"},
        {"page": "١"},
        {"page": "12345678"},
        {"q": "x" * 121},
        {"category": "x" * 101},
        {"chart": "pie"},
    ],
)
def test_invalid_filters_are_rejected(args):
    with pytest.raises(FilterError):
        Filters.parse(args)


def seed(app, item, rows):
    with connect(app.config["DATABASE_URL"]) as conn:
        for number, fields in enumerate(rows):
            values = {
                "date": "2026-11-20",
                "merchant": "Corner Cafe",
                "description": "Coffee",
                "amount": "1.00",
                "currency": "USD",
                "pending": False,
                "category": "FOOD_AND_DRINK",
                "detail": "FOOD_AND_DRINK_COFFEE",
                "confidence": "VERY_HIGH",
                **fields,
            }
            conn.execute(
                """INSERT INTO transactions (item_id, plaid_transaction_id, plaid_account_id,
                transaction_date, merchant_name, original_description, amount, currency_code,
                pending, category_primary, category_detailed, category_confidence)
                VALUES (%s, %s, 'account-1', %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (item["id"], f"tx-{number:03}", *values.values()),
            )


@pytest.mark.postgres
def test_filters_are_inclusive_literal_owner_scoped_and_paginated(
    connected_app, exchange_connection
):
    client, _, item = exchange_connection
    seed(
        connected_app,
        item,
        [
            *[{"merchant": "50%_ Coffee", "date": "2026-11-01"} for _ in range(53)],
            {"merchant": "50XX Coffee"},
            {"currency": "CAD"},
            {"date": "2026-10-31"},
        ],
    )
    filters = Filters.parse({"q": "50%_ coffee", "start": "2026-11-01", "end": "2026-11-01"})
    data = connected_app.extensions["store"].explore(filters)
    assert (data["total"], len(data["rows"]), data["pages"]) == (53, 25, 3)
    assert data["summary"]["amount"] == Decimal("53.00")  # All pages, not just visible rows.
    page = client.get("/", query_string={**filters.url_args(), "page": 2, "chart": "month"})
    assert page.status_code == 200
    assert "Page 2 of 3" in page.text
    assert "q=50%25_+coffee" in page.text
    last = connected_app.extensions["store"].explore(Filters(query="50%_", page=999))
    assert (last["page"], len(last["rows"])) == (3, 3)
    assert connected_app.extensions["store"].explore(Filters(query="' OR 1=1 --"))["total"] == 0
    assert connected_app.extensions["store"].explore(Filters(query="Coffee"))["total"] == 55
    with connect(connected_app.config["DATABASE_URL"]) as conn:
        conn.execute("INSERT INTO owners (id) VALUES ('other-owner')")
    other = Store(connected_app.config["DATABASE_URL"], "other-owner").explore(Filters())
    assert other["total"] == 0 and other["summary"]["amount"] == 0
    assert other["categories"] == []


@pytest.mark.postgres
def test_spending_sql_decimal_precision_exclusions_categories_and_months(
    connected_app, exchange_connection
):
    client, _, item = exchange_connection
    seed(
        connected_app,
        item,
        [
            {"amount": "12.34", "date": "2026-10-01"},
            {"amount": "27.66", "confidence": "LOW"},
            {"amount": "0.10", "category": None, "confidence": None},
            {"amount": "99", "pending": True},
            {"amount": "500", "category": "TRANSFER_OUT"},
            {
                "amount": "300",
                "category": "LOAN_PAYMENTS",
                "detail": "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
            },
            {"amount": "-5"},
            {"amount": "-1000", "category": "INCOME"},
            {"amount": "100", "currency": "CAD"},
        ],
    )
    data = connected_app.extensions["store"].explore(Filters())
    assert data["total"] == 8  # The table includes USD inflows, pending and transfers.
    assert data["summary"] == {"amount": Decimal("40.10"), "count": 3, "uncertain": 2}
    assert data["by_month"] == [
        {"label": "2026-10", "amount": Decimal("12.34")},
        {"label": "2026-11", "amount": Decimal("27.76")},
    ]
    unknown = connected_app.extensions["store"].explore(Filters(category="UNCATEGORIZED"))
    assert unknown["total"] == 1 and unknown["summary"]["amount"] == Decimal("40.10")
    searched = connected_app.extensions["store"].explore(Filters(query="not-a-merchant"))
    assert searched["total"] == 0 and searched["summary"]["amount"] == Decimal("40.10")
    november = connected_app.extensions["store"].explore(
        Filters.parse({"start": "2026-11-01", "q": "not-a-merchant"})
    )
    assert november["total"] == 0 and november["summary"]["amount"] == Decimal("27.76")
    assert client.get("/?chart=month").status_code == 200
    assert "40.10" in client.get("/").text
    assert client.get("/?q=does-not-exist").status_code == 200
    invalid = client.get("/?start=2026-11-31")
    assert invalid.status_code == 400 and "YYYY-MM-DD" in invalid.text
