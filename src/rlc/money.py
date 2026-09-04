"""Integer-paise money arithmetic (spec §7.1).

Every amount in this project is an `int` number of paise. No floats, no
Decimal, no true division. Two rules carry the correctness of the whole report:

1. On the Razorpay Payment entity, `fee` is GST-INCLUSIVE and `tax` is the GST
   portion of it. Leakage is `fee` alone. Computing `fee + tax` double-counts
   the GST — that is the single most common way to get this wrong.
2. When a payment has several partial refunds, allocate the parent fee across
   them with largest-remainder allocation. Independent per-refund rounding
   leaves the total 1-2 paise off the parent fee and the control total in the
   report stops tying.
"""

from __future__ import annotations

from fractions import Fraction

BPS_DENOM = 10_000


def pct_paise(amount_paise: int, bps: int) -> int:
    """`bps` basis points of `amount_paise`, rounded half up, in paise."""
    if amount_paise < 0 or bps < 0:
        raise ValueError("pct_paise expects non-negative inputs")
    return (amount_paise * bps + BPS_DENOM // 2) // BPS_DENOM


def fee_breakdown(amount_paise: int, fee_bps: int, gst_bps: int) -> tuple[int, int, int]:
    """Return (fee_inclusive, fee_ex_gst, gst) for a payment.

    Mirrors the Payment entity: `fee` includes GST, `tax` is the GST part.
    """
    fee_ex = pct_paise(amount_paise, fee_bps)
    gst = pct_paise(fee_ex, gst_bps)
    return fee_ex + gst, fee_ex, gst


def allocate_largest_remainder(total: int, parts: list[int], base: int) -> list[int]:
    """Split `total * part / base` across `parts` with exact integer arithmetic.

    Each share is floored, then the leftover paise are handed to the largest
    fractional remainders. The result sums to `floor(total * sum(parts) / base)`,
    so when the parts cover the whole base the allocation sums exactly to
    `total`.

    Ties are broken by larger part first, then by earlier index, so the function
    is deterministic and order-stable.
    """
    if base <= 0:
        raise ValueError("base must be positive")
    if any(p < 0 for p in parts):
        raise ValueError("parts must be non-negative")
    if not parts:
        return []
    if sum(parts) > base:
        raise ValueError("sum(parts) exceeds base; check the ΣR <= captured invariant")

    exact = [Fraction(total * p, base) for p in parts]
    floors = [e.numerator // e.denominator for e in exact]
    target = sum(exact)
    target_floor = target.numerator // target.denominator
    leftover = target_floor - sum(floors)

    if leftover:
        order = sorted(
            range(len(parts)),
            key=lambda i: (exact[i] - floors[i], parts[i], -i),
            reverse=True,
        )
        for i in order[:leftover]:
            floors[i] += 1
    return floors


def format_inr(paise: int) -> str:
    """Render paise as a rupee string, e.g. -236000 -> '-₹2,360.00'."""
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(paise), 100)
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    return f"{sign}₹{digits}.{frac:02d}"


def bps_of(numerator_paise: int, denominator_paise: int) -> int:
    """Basis points of one amount against another; 0 when the base is 0."""
    if denominator_paise == 0:
        return 0
    return (numerator_paise * BPS_DENOM + denominator_paise // 2) // denominator_paise
