"""Tests for the entity layer and config loader (spec §2, §1).

The `acquirer_data` tests matter most: Razorpay's own samples show three
different shapes for the same field, and the whole leg-4 claim depends on
reading them correctly.
"""

import copy
from datetime import date

import pytest

from rlc.config import REPO_ROOT, Config
from rlc.entities import (
    CLOSURE_STATES,
    Dispute,
    Payment,
    ReconRow,
    Refund,
    RefundVerdict,
    ReturnsLedgerRow,
    Settlement,
)

# Verbatim from Razorpay's "Fetch Settlement Recon Details" sample response.
RECON_PAYMENT_SAMPLE = {
    "entity_id": "pay_DEXrnipqTmWVGE",
    "type": "payment",
    "debit": 0,
    "credit": 97100,
    "amount": 100000,
    "currency": "INR",
    "fee": 2900,
    "tax": 0,
    "on_hold": False,
    "settled": True,
    "created_at": 1567692556,
    "settled_at": 1568176960,
    "settlement_id": "setl_DGlQ1Rj8os78Ec",
    "payment_id": None,
    "settlement_utr": "1568176960vxp0rj",
    "order_id": "order_DEXrnRiR3SNDHA",
    "method": "card",
    "card_network": "MasterCard",
    "card_issuer": "KARB",
    "card_type": "credit",
    "dispute_id": None,
}

RECON_REFUND_SAMPLE = {
    "entity_id": "rfnd_DGRcGzZSLyEdg1",
    "type": "refund",
    "debit": 242500,
    "credit": 0,
    "amount": 242500,
    "currency": "INR",
    "fee": 0,
    "tax": 0,
    "on_hold": False,
    "settled": True,
    "created_at": 1568107224,
    "settled_at": 1568176960,
    "settlement_id": "setl_DGlQ1Rj8os78Ec",
    "payment_id": "pay_DEXq1pACSqFxtS",
    "settlement_utr": "1568176960vxp0rj",
    "order_id": "order_DEXpmZgffXNvuI",
    "method": "card",
    "dispute_id": None,
}


# ------------------------------------------------------------------- Payment


def test_payment_fee_is_gst_inclusive():
    p = Payment(id="pay_x", amount=10_000_000, fee=236_000, tax=36_000)
    assert p.fee_ex_gst == 200_000
    assert p.net_credited == 10_000_000 - 236_000


def test_payment_round_trips_through_api_shape():
    p = Payment(id="pay_x", amount=150_000, fee=3_540, tax=540, created_at=1_756_000_000)
    assert Payment.from_api(p.to_api()) == p


def test_payment_from_api_tolerates_nulls():
    p = Payment.from_api({"id": "pay_x", "amount": 100, "fee": None, "tax": None,
                          "amount_refunded": None, "created_at": None})
    assert (p.fee, p.tax, p.amount_refunded, p.created_at) == (0, 0, 0, 0)


# -------------------------------------------------------------------- Refund


@pytest.mark.parametrize(
    "acquirer_data, expected",
    [
        ({}, None),                                  # Fetch All Refunds sample
        ({"arn": None}, None),                       # Create Normal Refund sample
        ({"arn": "10000000000000"}, "10000000000000"),  # Fetch Refund With ID sample
    ],
)
def test_acquirer_data_normalises_all_three_documented_shapes(acquirer_data, expected):
    r = Refund.from_api({
        "id": "rfnd_x", "payment_id": "pay_x", "amount": 6000,
        "status": "processed", "acquirer_data": acquirer_data,
    })
    assert r.arn == expected


def test_processed_without_arn_is_representable():
    """The whole project exists because this state is reachable in Razorpay."""
    r = Refund.from_api({
        "id": "rfnd_FP8QHiV938haTz", "payment_id": "pay_29QQoUBi66xm2f",
        "amount": 500100, "status": "processed", "acquirer_data": {"arn": None},
    })
    assert r.status == "processed"
    assert r.arn is None


def test_refund_defaults_speed_when_absent():
    """Razorpay omits speed_* unless `speed` was set on the request."""
    r = Refund.from_api({"id": "rfnd_x", "payment_id": "pay_x", "amount": 100})
    assert r.speed_requested == "normal"
    assert r.speed_processed == "normal"


def test_failed_refund_is_excluded_from_parent_totals():
    r = Refund(id="rfnd_x", payment_id="pay_x", amount=100, status="failed")
    assert r.counts_toward_parent_total is False
    assert Refund(id="r", payment_id="p", amount=1, status="processed").counts_toward_parent_total


def test_instant_fee_only_applies_when_actually_processed_instantly():
    """optimum -> normal means the fee was credited back, so it is not leakage."""
    fell_back = Refund(id="r1", payment_id="p", amount=1,
                       speed_requested="optimum", speed_processed="normal")
    instant = Refund(id="r2", payment_id="p", amount=1,
                     speed_requested="optimum", speed_processed="instant")
    assert fell_back.instant_fee_applies is False
    assert instant.instant_fee_applies is True


def test_refund_has_no_settlement_fields():
    """A Refund carries no settlement_id/settled_at/fee/tax — hence the join."""
    for absent in ("settlement_id", "settled_at", "fee", "tax"):
        assert not hasattr(Refund(id="r", payment_id="p", amount=1), absent)


def test_refund_round_trips():
    r = Refund(id="rfnd_x", payment_id="pay_x", amount=6000, arn="10000000000000",
               receipt="RMA-1", created_at=1_756_000_000)
    assert Refund.from_api(r.to_api()) == r


# ------------------------------------------------------------------- Dispute


def test_dispute_sub_states():
    lost = Dispute(id="d", payment_id="p", amount=10_000, amount_deducted=10_000, status="lost")
    review = Dispute(id="d", payment_id="p", amount=10_000, status="under_review")
    won = Dispute(id="d", payment_id="p", amount=10_000, status="won")
    assert lost.is_realized_loss and not lost.is_at_risk
    assert review.is_at_risk and not review.is_realized_loss
    assert won.is_resolved_favourably and not won.is_realized_loss


def test_dispute_lost_with_zero_deduction_is_not_a_realized_loss():
    d = Dispute(id="d", payment_id="p", amount=10_000, amount_deducted=0, status="lost")
    assert d.is_realized_loss is False


def test_dispute_round_trips():
    d = Dispute(id="disp_x", payment_id="pay_x", amount=10_000, status="lost",
                amount_deducted=10_000, phase="chargeback", created_at=1_590_059_211)
    assert Dispute.from_api(d.to_api()) == d


# ------------------------------------------------------ ReconRow, Settlement


def test_recon_payment_sample_parses_and_nets_correctly():
    row = ReconRow.from_api(copy.deepcopy(RECON_PAYMENT_SAMPLE))
    assert row.is_payment and not row.is_refund
    assert row.payment_id is None            # null for payment rows
    assert row.credit == row.amount - row.fee
    assert row.net == 97_100


def test_recon_refund_sample_carries_no_fee():
    """Refund rows have fee = 0 and tax = 0: leakage must come from the payment."""
    row = ReconRow.from_api(copy.deepcopy(RECON_REFUND_SAMPLE))
    assert row.is_refund
    assert row.fee == 0 and row.tax == 0
    assert row.debit == row.amount and row.credit == 0
    assert row.payment_id == "pay_DEXq1pACSqFxtS"
    assert row.net == -242_500


def test_recon_row_round_trips():
    row = ReconRow.from_api(copy.deepcopy(RECON_REFUND_SAMPLE))
    assert ReconRow.from_api(row.to_api()) == row


def test_settlement_round_trips():
    s = Settlement(id="setl_x", amount=9_973_635, utr="1568176960vxp0rj", created_at=1_568_176_960)
    assert Settlement.from_api(s.to_api()) == s
    assert s.fees == 0 and s.tax == 0


def test_returns_ledger_round_trips():
    r = ReturnsLedgerRow(rma_id="RMA-1", order_id="order_x", payment_id="pay_x",
                         expected_refund_paise=200_000, rma_created_at=1_756_000_000)
    assert ReturnsLedgerRow.from_row(r.to_row()) == r


# -------------------------------------------------------------- RefundVerdict


def test_verdict_rejects_unknown_state():
    with pytest.raises(ValueError):
        RefundVerdict(refund_id="r", payment_id="p", amount=1, closure_state="MAYBE")


def test_verdict_accepts_every_declared_state():
    for state in CLOSURE_STATES:
        assert RefundVerdict(refund_id="r", payment_id="p", amount=1, closure_state=state)


def test_legs_verified_counts_three_not_four():
    """Leg 4 is evidenced, never verified — it must not be summed with the rest."""
    v = RefundVerdict(refund_id="r", payment_id="p", amount=1, closure_state="CLOSED_MATCHED",
                      leg2_gateway_processed=True, leg3_settlement_deducted=True,
                      leg4_bank_evidenced=True)
    assert v.legs_verified == 3


def test_verdict_serialises_to_a_row():
    v = RefundVerdict(refund_id="r", payment_id="p", amount=100, closure_state="EXCEPTION",
                      exception_codes=["NEVER_DEDUCTED"], leakage_paise=236)
    row = v.to_row()
    assert row["closure_state"] == "EXCEPTION"
    assert row["exception_codes"] == ["NEVER_DEDUCTED"]
    assert row["legs"]["4_bank_evidenced"] is False


# --------------------------------------------------------------------- Config


def test_shipped_config_loads_and_validates():
    cfg = Config.load(REPO_ROOT / "config.yaml")
    assert cfg.run.period_start == date(2026, 8, 1)
    assert cfg.pricing.fee_bps == 200 and cfg.pricing.gst_bps == 1800
    assert cfg.thresholds.settle_threshold_wd >= cfg.settlement.cycle_wd
    assert cfg.calendar().is_working_day(date(2026, 8, 15)) is False


def test_config_rejects_a_recon_window_that_ends_at_period_end_minus_a_day():
    """Guards the most likely bug: a narrow pull inventing false NEVER_DEDUCTED."""
    raw = _raw_config()
    raw["run"]["recon_window_end"] = "2026-08-30"
    with pytest.raises(ValueError, match="recon_window_end"):
        Config.from_dict(raw)


def test_config_rejects_payments_window_starting_after_the_period():
    raw = _raw_config()
    raw["run"]["payments_window_start"] = "2026-08-10"
    with pytest.raises(ValueError, match="payments_window_start"):
        Config.from_dict(raw)


def test_config_rejects_a_maturity_gate_shorter_than_the_settlement_cycle():
    raw = _raw_config()
    raw["thresholds"]["settle_threshold_wd"] = 1
    with pytest.raises(ValueError, match="settle_threshold_wd"):
        Config.from_dict(raw)


def test_assumptions_table_flags_the_lag_as_an_assumption():
    cfg = Config.load(REPO_ROOT / "config.yaml")
    rows = {r["key"]: r["note"] for r in cfg.assumptions_table()}
    assert "ASSUMPTION" in rows["settlement.refund_deduction_lag_wd"]
    assert "DOCUMENTED" in rows["settlement.cycle_wd"]


def _raw_config() -> dict:
    import yaml
    return yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
