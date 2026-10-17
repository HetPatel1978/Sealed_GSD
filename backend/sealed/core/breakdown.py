"""Price breakdowns: what a dealer itemised, checked by code.

Rules:
- Every amount must be grounded: it has to appear in the dealer's own text. Ungrounded amounts are dropped
  and flagged `breakdown_ungrounded` (the parser must never invent numbers).
- The parts must add up to the out-the-door total within $1; if not, flag `breakdown_mismatch`.
  The dealer's stated total is always kept as the quote's price.
"""

from sealed.core.numbers import numbers_in

LIST_FIELDS = ("dealer_fees", "add_ons", "other")
AMOUNT_FIELDS = ("vehicle_price", "taxes", "title_registration", "out_the_door_total")
TOLERANCE = 1.0


def empty() -> dict:
    return {"vehicle_price": None, "dealer_fees": [], "add_ons": [], "taxes": None, "title_registration": None,
            "other": [], "out_the_door_total": None, "valid_until": None}


def validate(raw: dict | None, dealer_text: str, stated_total: float | None) -> tuple[dict | None, list[str]]:
    if not raw:
        return None, []
    grounded_values = set(numbers_in(dealer_text))
    flags: list[str] = []
    out = empty()
    out["valid_until"] = raw.get("valid_until")

    def grounded(v) -> float | None:
        if v is None:
            return None
        v = float(v)
        if v not in grounded_values:
            if "breakdown_ungrounded" not in flags:
                flags.append("breakdown_ungrounded")
            return None
        return v

    for f in AMOUNT_FIELDS:
        out[f] = grounded(raw.get(f))
    for f in LIST_FIELDS:
        for item in raw.get(f) or []:
            amount = grounded(item.get("amount"))
            if amount is not None:
                out[f].append({"label": str(item.get("label") or f), "amount": amount})

    total = out["out_the_door_total"] if out["out_the_door_total"] is not None else stated_total
    if total is not None and out["vehicle_price"] is not None:
        parts = (out["vehicle_price"] + (out["taxes"] or 0) + (out["title_registration"] or 0)
                 + sum(i["amount"] for f in LIST_FIELDS for i in out[f]))
        if abs(parts - total) > TOLERANCE:
            flags.append("breakdown_mismatch")
    if stated_total is not None and out["out_the_door_total"] is not None \
            and abs(out["out_the_door_total"] - stated_total) > TOLERANCE:
        if "breakdown_mismatch" not in flags:
            flags.append("breakdown_mismatch")
    return out, flags


def summary(breakdown: dict | None) -> dict:
    """Totals per category, for the fee comparison."""
    b = breakdown or empty()
    return {
        "vehicle_price": b["vehicle_price"],
        "dealer_fees_total": round(sum(i["amount"] for i in b["dealer_fees"]), 2),
        "add_ons_total": round(sum(i["amount"] for i in b["add_ons"]), 2),
        "taxes": b["taxes"],
        "title_registration": b["title_registration"],
        "other_total": round(sum(i["amount"] for i in b["other"]), 2),
        "dealer_fees": b["dealer_fees"],
        "add_ons": b["add_ons"],
    }
