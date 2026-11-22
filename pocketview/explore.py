"""Validate dashboard filters before building parameterized PostgreSQL queries."""

from dataclasses import dataclass, replace
from datetime import date


class FilterError(ValueError):
    pass


@dataclass(frozen=True)
class Filters:
    query: str = ""
    start: date | None = None
    end: date | None = None
    category: str = ""
    page: int = 1
    page_size: int = 25
    chart: str = "category"

    @classmethod
    def parse(cls, args):
        query = args.get("q", "").strip()
        category = args.get("category", "").strip()
        if len(query) > 120:
            raise FilterError("Search must contain 120 characters or fewer.")
        if len(category) > 100:
            raise FilterError("Choose a shorter category value.")

        def parse_date(key):
            value = args.get(key, "").strip()
            if not value:
                return None
            try:
                parsed = date.fromisoformat(value)
                if parsed.isoformat() != value:
                    raise ValueError
                return parsed
            except ValueError:
                raise FilterError("Enter dates in YYYY-MM-DD format.") from None

        start, end = parse_date("start"), parse_date("end")
        if start and end and start > end:
            raise FilterError("The start date must be on or before the end date.")
        page = args.get("page", "1")
        if not page.isascii() or not page.isdecimal() or len(page) > 7 or int(page) < 1:
            raise FilterError("Page must be a positive whole number.")
        chart = args.get("chart", "category")
        if chart not in {"category", "month"}:
            raise FilterError("Choose the category or monthly spending view.")
        return cls(query, start, end, category, int(page), chart=chart)

    def where(self, owner_id):
        clauses = ["i.owner_id = %s", "t.currency_code = 'USD'"]
        values = [owner_id]
        if self.query:
            # Escape LIKE metacharacters so a user's search is literal text.
            escaped = self.query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("(t.merchant_name ILIKE %s OR t.original_description ILIKE %s)")
            values.extend([f"%{escaped}%"] * 2)
        if self.start:
            clauses.append("t.transaction_date >= %s")
            values.append(self.start)
        if self.end:
            clauses.append("t.transaction_date <= %s")
            values.append(self.end)
        if self.category:
            clauses.append("COALESCE(NULLIF(t.category_primary, ''), 'UNCATEGORIZED') = %s")
            values.append(self.category)
        return " AND ".join(clauses), values

    def url_args(self, page=None, chart=None):
        values = {
            "q": self.query,
            "category": self.category,
            "start": self.start.isoformat() if self.start else "",
            "end": self.end.isoformat() if self.end else "",
            "chart": chart or self.chart,
            "page": page or self.page,
        }
        return {key: value for key, value in values.items() if value != ""}

    def period(self):
        """The overview uses dates; search and category narrow only the table."""
        return replace(self, query="", category="", page=1)
