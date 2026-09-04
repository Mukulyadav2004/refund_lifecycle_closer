"""Razorpay-shaped identifiers for synthetic data (spec §3).

Real Razorpay ids look like `pay_DEXrnipqTmWVGE`, `rfnd_DGRcGzZSLyEdg1`,
`setl_DGlQ1Rj8os78Ec`, `disp_AHfqOvkldwsbqt`, `order_DEXrnRiR3SNDHA`. The
prefix and the 14-character base62 body are what a reviewer recognises, so the
generator matches that shape exactly. Ids are drawn from a seeded `random.Random`
so a run is reproducible.
"""

from __future__ import annotations

import random
import string

ALPHABET = string.ascii_letters + string.digits

PREFIXES = {
    "payment": "pay",
    "refund": "rfnd",
    "settlement": "setl",
    "dispute": "disp",
    "order": "order",
    "batch": "batch",
    "adjustment": "adj",
    "transfer": "trf",
    "document": "doc",
}


class IdFactory:
    """Deterministic, collision-checked id generator."""

    def __init__(self, rng: random.Random, body_length: int = 14) -> None:
        self._rng = rng
        self._body_length = body_length
        self._seen: set[str] = set()

    def _body(self) -> str:
        return "".join(self._rng.choice(ALPHABET) for _ in range(self._body_length))

    def new(self, kind: str) -> str:
        try:
            prefix = PREFIXES[kind]
        except KeyError as exc:
            raise ValueError(f"unknown id kind {kind!r}; expected one of {sorted(PREFIXES)}") from exc
        while True:
            candidate = f"{prefix}_{self._body()}"
            if candidate not in self._seen:
                self._seen.add(candidate)
                return candidate

    def payment(self) -> str:
        return self.new("payment")

    def refund(self) -> str:
        return self.new("refund")

    def settlement(self) -> str:
        return self.new("settlement")

    def dispute(self) -> str:
        return self.new("dispute")

    def order(self) -> str:
        return self.new("order")

    def arn(self) -> str:
        """A bank reference number. Razorpay's samples show a 14-digit ARN."""
        return "".join(self._rng.choice(string.digits) for _ in range(14))

    def utr(self, yyyymmdd: str) -> str:
        """Settlement UTR. Razorpay's samples mix formats; this one is traceable."""
        tail = "".join(self._rng.choice(string.ascii_lowercase + string.digits) for _ in range(6))
        return f"{yyyymmdd}{tail}"
