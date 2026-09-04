"""Dataclasses mirroring the verified Razorpay API surface (spec §2, CLAUDE.md §3).

Field names are identical to the API so every synthetic record traces back to a
documented endpoint. Nothing here invents a field. In particular:

* A Refund has NO `settlement_id`, `settled_at`, `fee` or `tax`. Those live on
  the recon row, which is why the join in §4 of the spec exists at all.
* A Payment's `fee` is GST-inclusive; `tax` is the GST portion of it.
* A recon refund row always carries `fee = 0` and `tax = 0`.
* `acquirer_data` appears in Razorpay's own samples as `{}`,
  `{"arn": null}` and `{"arn": "..."}`. `Refund.arn` normalises all three.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# ---------------------------------------------------------------- enumerations

PaymentStatus = Literal["created", "authorized", "captured", "refunded", "failed"]
RefundStatus = Literal["pending", "processed", "failed"]
RefundSpeed = Literal["normal", "optimum", "instant"]
DisputeStatus = Literal["open", "under_review", "won", "lost", "closed"]
DisputePhase = Literal["fraud", "retrieval", "chargeback", "pre_arbitration", "arbitration"]
SettlementStatus = Literal["created", "processed", "failed"]
ReconType = Literal["payment", "refund", "transfer", "adjustment"]
PaymentMethod = Literal["card", "netbanking", "wallet", "upi", "emi"]

PAYMENT_STATUSES = ("created", "authorized", "captured", "refunded", "failed")
REFUND_STATUSES = ("pending", "processed", "failed")
DISPUTE_STATUSES = ("open", "under_review", "won", "lost", "closed")
DISPUTE_PHASES = ("fraud", "retrieval", "chargeback", "pre_arbitration", "arbitration")
RECON_TYPES = ("payment", "refund", "transfer", "adjustment")

# Closure vocabulary (CLAUDE.md §1). Exactly one state per refund.
CLOSED_MATCHED = "CLOSED_MATCHED"
OPEN = "OPEN"
EXCEPTION = "EXCEPTION"
REJECTED_INPUT = "REJECTED_INPUT"
CLOSURE_STATES = (CLOSED_MATCHED, OPEN, EXCEPTION, REJECTED_INPUT)

EXCEPTION_CODES = (
    "REFUND_FAILED",
    "PENDING_OVERDUE",
    "NEVER_DEDUCTED",
    "DOUBLE_DEDUCTED",
    "SETTLEMENT_AMOUNT_DELTA",
    "AMOUNT_MISMATCH",
    "REFUND_PLUS_CHARGEBACK",
    "ARN_OVERDUE",
    "DUPLICATE_SUSPECT",
)

OPEN_REASONS = ("AWAITING_PROCESSING", "AWAITING_SETTLEMENT", "AWAITING_ARN")

REJECTION_REASONS = (
    "NEGATIVE_AMOUNT",
    "NO_PARENT_PAYMENT",
    "CURRENCY_MISMATCH",
    "REFUND_BEFORE_PAYMENT",
    "PARENT_NOT_CAPTURED",
    "OVER_CAPTURED_TOTAL",
    "TIMESTAMP_OUT_OF_RANGE",
)

TIMING_FLAGS = ("CROSS_PERIOD", "LATE_VS_THRESHOLD")


# ------------------------------------------------------------------- entities


@dataclass(slots=True)
class Payment:
    """Razorpay Payment entity. `fee` is GST-inclusive; `tax` is its GST part."""

    id: str
    amount: int
    currency: str = "INR"
    status: str = "captured"
    method: str = "card"
    order_id: str | None = None
    international: bool = False
    refund_status: str | None = None
    amount_refunded: int = 0
    captured: bool = True
    fee: int = 0
    tax: int = 0
    created_at: int = 0
    card_network: str | None = None
    card_type: str | None = None
    card_issuer: str | None = None
    notes: dict[str, Any] = field(default_factory=dict)
    entity: str = "payment"

    @property
    def fee_ex_gst(self) -> int:
        """MDR / platform-fee portion, i.e. `fee` minus its GST."""
        return self.fee - self.tax

    @property
    def net_credited(self) -> int:
        """What reaches the merchant for this payment: amount minus the fee."""
        return self.amount - self.fee

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Payment":
        return cls(
            id=d["id"],
            amount=int(d["amount"]),
            currency=d.get("currency", "INR"),
            status=d.get("status", "captured"),
            method=d.get("method", "card"),
            order_id=d.get("order_id"),
            international=bool(d.get("international", False)),
            refund_status=d.get("refund_status"),
            amount_refunded=int(d.get("amount_refunded") or 0),
            captured=bool(d.get("captured", True)),
            fee=int(d.get("fee") or 0),
            tax=int(d.get("tax") or 0),
            created_at=int(d.get("created_at") or 0),
            card_network=d.get("card_network"),
            card_type=d.get("card_type"),
            card_issuer=d.get("card_issuer"),
            notes=d.get("notes") or {},
        )

    def to_api(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "entity": "payment",
            "amount": self.amount,
            "currency": self.currency,
            "status": self.status,
            "method": self.method,
            "order_id": self.order_id,
            "international": self.international,
            "refund_status": self.refund_status,
            "amount_refunded": self.amount_refunded,
            "captured": self.captured,
            "fee": self.fee,
            "tax": self.tax,
            "created_at": self.created_at,
            "card_network": self.card_network,
            "card_type": self.card_type,
            "card_issuer": self.card_issuer,
            "notes": self.notes,
        }


@dataclass(slots=True)
class Refund:
    """Razorpay Refund entity.

    `arn` is a normalisation of `acquirer_data`, which Razorpay returns as `{}`,
    `{"arn": null}` or `{"arn": "..."}`. It evidences that a bank reference was
    issued; it does NOT prove the customer's account was credited.
    """

    id: str
    payment_id: str
    amount: int
    currency: str = "INR"
    status: str = "processed"
    receipt: str | None = None
    arn: str | None = None
    created_at: int = 0
    batch_id: str | None = None
    speed_requested: str = "normal"
    speed_processed: str = "normal"
    notes: dict[str, Any] = field(default_factory=dict)
    entity: str = "refund"

    @property
    def is_terminal(self) -> bool:
        return self.status in ("processed", "failed")

    @property
    def counts_toward_parent_total(self) -> bool:
        """Failed refunds move no money and are excluded from ΣR everywhere."""
        return self.status != "failed"

    @property
    def instant_fee_applies(self) -> bool:
        """True only when an instant refund actually completed instantly.

        If `optimum` was requested but processed at `normal` speed, Razorpay
        credits the levied instant-refund fee back, so it is not leakage.
        """
        return self.speed_processed == "instant"

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Refund":
        acquirer = d.get("acquirer_data") or {}
        arn = acquirer.get("arn") if isinstance(acquirer, dict) else None
        return cls(
            id=d["id"],
            payment_id=d.get("payment_id"),
            amount=int(d["amount"]),
            currency=d.get("currency", "INR"),
            status=d.get("status", "processed"),
            receipt=d.get("receipt"),
            arn=arn or None,
            created_at=int(d.get("created_at") or 0),
            batch_id=d.get("batch_id"),
            speed_requested=d.get("speed_requested") or "normal",
            speed_processed=d.get("speed_processed") or "normal",
            notes=d.get("notes") or {},
        )

    def to_api(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "entity": "refund",
            "amount": self.amount,
            "currency": self.currency,
            "payment_id": self.payment_id,
            "notes": self.notes,
            "receipt": self.receipt,
            "acquirer_data": {"arn": self.arn},
            "created_at": self.created_at,
            "batch_id": self.batch_id,
            "status": self.status,
            "speed_processed": self.speed_processed,
            "speed_requested": self.speed_requested,
        }


@dataclass(slots=True)
class Dispute:
    """Razorpay Dispute entity.

    `amount_deducted` is documented as the amount taken from the Razorpay
    balance when the dispute is LOST, and 0 otherwise. `open`/`under_review`
    therefore represent exposure at risk rather than a realised loss.
    """

    id: str
    payment_id: str
    amount: int
    currency: str = "INR"
    amount_deducted: int = 0
    reason_code: str = "chargeback"
    reason_description: str | None = None
    respond_by: int = 0
    status: str = "open"
    phase: str = "chargeback"
    created_at: int = 0
    evidence: dict[str, Any] = field(default_factory=dict)
    entity: str = "dispute"

    @property
    def is_realized_loss(self) -> bool:
        return self.status == "lost" and self.amount_deducted > 0

    @property
    def is_at_risk(self) -> bool:
        return self.status in ("open", "under_review")

    @property
    def is_resolved_favourably(self) -> bool:
        return self.status in ("won", "closed")

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Dispute":
        return cls(
            id=d["id"],
            payment_id=d["payment_id"],
            amount=int(d["amount"]),
            currency=d.get("currency", "INR"),
            amount_deducted=int(d.get("amount_deducted") or 0),
            reason_code=d.get("reason_code", "chargeback"),
            reason_description=d.get("reason_description"),
            respond_by=int(d.get("respond_by") or 0),
            status=d.get("status", "open"),
            phase=d.get("phase", "chargeback"),
            created_at=int(d.get("created_at") or 0),
            evidence=d.get("evidence") or {},
        )

    def to_api(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "entity": "dispute",
            "payment_id": self.payment_id,
            "amount": self.amount,
            "currency": self.currency,
            "amount_deducted": self.amount_deducted,
            "reason_code": self.reason_code,
            "reason_description": self.reason_description,
            "respond_by": self.respond_by,
            "status": self.status,
            "phase": self.phase,
            "created_at": self.created_at,
            "evidence": self.evidence,
        }


@dataclass(slots=True)
class Settlement:
    """Razorpay Settlement entity. `amount` is the net paid into the bank."""

    id: str
    amount: int
    status: str = "processed"
    fees: int = 0
    tax: int = 0
    utr: str = ""
    created_at: int = 0
    entity: str = "settlement"

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Settlement":
        return cls(
            id=d["id"],
            amount=int(d["amount"]),
            status=d.get("status", "processed"),
            fees=int(d.get("fees") or 0),
            tax=int(d.get("tax") or 0),
            utr=d.get("utr", ""),
            created_at=int(d.get("created_at") or 0),
        )

    def to_api(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "entity": "settlement",
            "amount": self.amount,
            "status": self.status,
            "fees": self.fees,
            "tax": self.tax,
            "utr": self.utr,
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class ReconRow:
    """One row of GET /v1/settlements/recon/combined.

    For `type == "refund"`: `entity_id` is the refund id, `debit == amount`,
    `credit == 0`, `fee == 0`, `tax == 0`, `payment_id` is the parent payment.
    For `type == "payment"`: `entity_id` is the payment id, `credit == amount -
    fee`, `debit == 0`, `payment_id` is null.
    """

    entity_id: str
    type: str
    debit: int = 0
    credit: int = 0
    amount: int = 0
    currency: str = "INR"
    fee: int = 0
    tax: int = 0
    on_hold: bool = False
    settled: bool = True
    created_at: int = 0
    settled_at: int = 0
    settlement_id: str = ""
    settlement_utr: str | None = None
    payment_id: str | None = None
    order_id: str | None = None
    order_receipt: str | None = None
    description: str | None = None
    notes: Any = None
    posted_at: int | None = None
    credit_type: str | None = "default"
    method: str | None = None
    card_network: str | None = None
    card_issuer: str | None = None
    card_type: str | None = None
    dispute_id: str | None = None

    @property
    def is_refund(self) -> bool:
        return self.type == "refund"

    @property
    def is_payment(self) -> bool:
        return self.type == "payment"

    @property
    def net(self) -> int:
        """Signed contribution to the settlement total."""
        return self.credit - self.debit

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "ReconRow":
        return cls(
            entity_id=d["entity_id"],
            type=d["type"],
            debit=int(d.get("debit") or 0),
            credit=int(d.get("credit") or 0),
            amount=int(d.get("amount") or 0),
            currency=d.get("currency", "INR"),
            fee=int(d.get("fee") or 0),
            tax=int(d.get("tax") or 0),
            on_hold=bool(d.get("on_hold", False)),
            settled=bool(d.get("settled", True)),
            created_at=int(d.get("created_at") or 0),
            settled_at=int(d.get("settled_at") or 0),
            settlement_id=d.get("settlement_id", ""),
            settlement_utr=d.get("settlement_utr"),
            payment_id=d.get("payment_id"),
            order_id=d.get("order_id"),
            order_receipt=d.get("order_receipt"),
            description=d.get("description"),
            notes=d.get("notes"),
            posted_at=d.get("posted_at"),
            credit_type=d.get("credit_type", "default"),
            method=d.get("method"),
            card_network=d.get("card_network"),
            card_issuer=d.get("card_issuer"),
            card_type=d.get("card_type"),
            dispute_id=d.get("dispute_id"),
        )

    def to_api(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "type": self.type,
            "debit": self.debit,
            "credit": self.credit,
            "amount": self.amount,
            "currency": self.currency,
            "fee": self.fee,
            "tax": self.tax,
            "on_hold": self.on_hold,
            "settled": self.settled,
            "created_at": self.created_at,
            "settled_at": self.settled_at,
            "settlement_id": self.settlement_id,
            "posted_at": self.posted_at,
            "credit_type": self.credit_type,
            "description": self.description,
            "notes": self.notes,
            "payment_id": self.payment_id,
            "settlement_utr": self.settlement_utr,
            "order_id": self.order_id,
            "order_receipt": self.order_receipt,
            "method": self.method,
            "card_network": self.card_network,
            "card_issuer": self.card_issuer,
            "card_type": self.card_type,
            "dispute_id": self.dispute_id,
        }


@dataclass(slots=True)
class ReturnsLedgerRow:
    """Merchant-side RMA record. NOT a Razorpay entity.

    Razorpay only enforces that total refunds do not exceed the captured amount.
    It has no idea what a specific return was worth. That number lives here, and
    it is the only thing that makes AMOUNT_MISMATCH computable.
    """

    rma_id: str
    order_id: str | None
    payment_id: str
    expected_refund_paise: int
    rma_created_at: int
    reason: str = "customer_return"

    @classmethod
    def from_row(cls, d: dict[str, Any]) -> "ReturnsLedgerRow":
        return cls(
            rma_id=d["rma_id"],
            order_id=d.get("order_id"),
            payment_id=d["payment_id"],
            expected_refund_paise=int(d["expected_refund_paise"]),
            rma_created_at=int(d["rma_created_at"]),
            reason=d.get("reason", "customer_return"),
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "rma_id": self.rma_id,
            "order_id": self.order_id,
            "payment_id": self.payment_id,
            "expected_refund_paise": self.expected_refund_paise,
            "rma_created_at": self.rma_created_at,
            "reason": self.reason,
        }


@dataclass(slots=True)
class BankStatementRow:
    """Optional merchant bank statement line, for the batch control total."""

    date: str
    narration: str
    utr: str
    credit_paise: int = 0
    debit_paise: int = 0


# ------------------------------------------------------- engine output record


@dataclass(slots=True)
class RefundVerdict:
    """The engine's output for one refund.

    `closure_state` is exclusive (zero silent drops). `leakage_*` and
    `timing_flags` are attributes computed on EVERY record, matched ones
    included, so they can be summed across the whole population.
    """

    refund_id: str
    payment_id: str | None
    amount: int
    closure_state: str
    exception_codes: list[str] = field(default_factory=list)
    open_reasons: list[str] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)
    timing_flags: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)

    leakage_paise: int = 0
    leakage_gst_paise: int = 0
    leakage_mdr_paise: int = 0
    exposure_paise: int = 0

    settle_lag_wd: int | None = None
    settlement_id: str | None = None
    settlement_utr: str | None = None

    leg1_initiated: bool = True
    leg2_gateway_processed: bool = False
    leg3_settlement_deducted: bool = False
    leg4_bank_evidenced: bool = False

    confidence: float | None = None
    needs_human_review: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)
    explanation: str | None = None

    def __post_init__(self) -> None:
        if self.closure_state not in CLOSURE_STATES:
            raise ValueError(f"unknown closure_state {self.closure_state!r}")

    @property
    def legs_verified(self) -> int:
        """Legs 1-3 only. Leg 4 is evidenced, never verified — never sum all four."""
        return sum((self.leg1_initiated, self.leg2_gateway_processed, self.leg3_settlement_deducted))

    def to_row(self) -> dict[str, Any]:
        return {
            "refund_id": self.refund_id,
            "payment_id": self.payment_id,
            "amount": self.amount,
            "closure_state": self.closure_state,
            "exception_codes": self.exception_codes,
            "open_reasons": self.open_reasons,
            "rejection_reasons": self.rejection_reasons,
            "timing_flags": self.timing_flags,
            "annotations": self.annotations,
            "leakage_paise": self.leakage_paise,
            "leakage_gst_paise": self.leakage_gst_paise,
            "leakage_mdr_paise": self.leakage_mdr_paise,
            "exposure_paise": self.exposure_paise,
            "settle_lag_wd": self.settle_lag_wd,
            "settlement_id": self.settlement_id,
            "settlement_utr": self.settlement_utr,
            "legs": {
                "1_initiated": self.leg1_initiated,
                "2_gateway_processed": self.leg2_gateway_processed,
                "3_settlement_deducted": self.leg3_settlement_deducted,
                "4_bank_evidenced": self.leg4_bank_evidenced,
            },
            "confidence": self.confidence,
            "needs_human_review": self.needs_human_review,
            "evidence": self.evidence,
            "explanation": self.explanation,
        }
