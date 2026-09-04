# Refund Lifecycle Closer

Razorpay AI Buildathon — **AI Finance Controller** track.

A deterministic agent that closes the refund lifecycle for one month of merchant
activity. It joins four Razorpay endpoints plus one merchant-side ledger,
resolves **every** refund to exactly one closure state, and reports measured
accuracy against seeded ground truth rather than a cherry-picked demo.

**The claim, stated precisely: three legs verified, one leg evidenced.**

| Leg | What it means | How it is established |
|---|---|---|
| 1 initiated | the refund exists | Refunds API record |
| 2 gateway processed | Razorpay marked it done | `refund.status == "processed"` |
| 3 deducted from settlement | the money left the payout | exactly one recon row with `debit == amount` |
| 4 landed in the customer's bank | — | **evidenced only** by `acquirer_data.arn` |

Razorpay's own documentation states a refund usually moves to `processed`
*before* the ARN/RRN arrives from the gateway, and their Create Refund sample
response shows `"status": "processed"` beside `"arn": null`. An ARN proves a
bank reference was issued, not that the customer's account was credited. This
repo never claims a four-leg lifecycle.

## Status

| Step | Module | State |
|---|---|---|
| 1 Skeleton, config, calendar, money, entities | `config.py` `calendar_utils.py` `money.py` `ids.py` `entities.py` | **done** |
| 2 Synthetic data generator | `generate.py` `cli.py` | **done — 380 refunds, 52 seeded failures** |
| 3 Loader, invariants, closure engine | `loader.py` `invariants.py` `engine.py` | to build |
| 4 Leakage, timing, leg evidence | `attributes.py` | to build |
| 5 Metrics vs ground truth | `evaluate.py` | to build |
| 6 Explanation layer | `explain.py` | to build |
| 7 Report writer | `report.py` | to build |

107 tests passing. `SPEC.md` is the algorithm. `CLAUDE.md` is the working contract — read it before
writing code.

## Run

```bash
make install     # pyyaml + pytest
make test        # unit tests
make data        # generate synthetic data + ground truth   (step 2)
make close       # run the engine over it                   (step 3)
make eval        # metrics, confusion matrix, report.md     (step 5)
```

## Where the data comes from

**Everything is synthetic and generated locally by `generate.py`.** No Razorpay
account, no API keys, no network. The brief asks for a 50+ record batch of
synthetic data; this generates ~600 payments and ~380 refunds from a fixed seed,
so any run is reproducible.

What makes it credible is not that it came from Razorpay, but that **every field
is shaped exactly like the documented API response**, so a judge can trace any
value back to a real endpoint:

| File | Mirrors | Endpoint |
|---|---|---|
| `payments.json` | Payment entity | `GET /v1/payments` |
| `refunds.json` | Refund entity | `GET /v1/refunds` |
| `disputes.json` | Dispute entity | `GET /v1/disputes` |
| `settlements.json` | Settlement entity | `GET /v1/settlements` |
| `settlement_recon.json` | recon rows | `GET /v1/settlements/recon/combined?year=&month=` |
| `returns_ledger.csv` | merchant RMA export | **not Razorpay** — see below |
| `ground_truth.json` | seeded labels | evaluator only |

The returns ledger is the one non-Razorpay source, and it is load-bearing.
Razorpay only enforces that total refunds never exceed the captured amount; it
has no idea what a specific return was *worth*. Refunding INR 20,000 for an
INR 2,000 item passes every Razorpay check. Only the merchant's own ledger can
catch it, which is why `AMOUNT_MISMATCH` needs this file and why the pitch
describes it as a merchant-side failure the PSP is structurally blind to.

Because the schemas match the API, swapping the generator for live test-mode API
calls is a loader change, not an engine change.

## Model

One exclusive axis plus two decorators. Leakage applies to nearly every refund,
so it cannot be a bucket that competes with exceptions — routing a refund into
"exceptions" would remove it from the leakage total and the numbers would stop
adding up.

```
closure_state   exactly one   CLOSED_MATCHED | OPEN | EXCEPTION | REJECTED_INPUT
exception_codes 0..n          non-empty iff EXCEPTION
open_reasons    0..n          AWAITING_PROCESSING | AWAITING_SETTLEMENT | AWAITING_ARN
leakage_paise   every record  fees Razorpay never returns, matched records included
timing_flags    every record  CROSS_PERIOD | LATE_VS_THRESHOLD
```

Zero silent drops: `N_in == N_closed + N_open + N_exception + N_rejected` is
asserted, and the run fails if it does not hold.

## Assumptions

Every threshold lives in `config.yaml`, is printed in the report, and is listed
as an assumption rather than a fact. Notably the refund settlement lag is **not**
hardcoded to "T+5 to T+7" — that figure comes from a marketing blog and
conflates the customer-receipt SLA with the settlement clock. Razorpay's own
recon sample nets a refund the next day. `CROSS_PERIOD` is a binary month
comparison in IST and needs no SLA assumption at all.

Working days follow Indian banking rules: closed on Sundays and on the **2nd and
4th Saturdays**; the 1st, 3rd and 5th Saturdays are working days. A naive
Monday-to-Friday calendar shifts every settlement date.

## AI judgment

No LLM in classification, matching, joining, or arithmetic — all of that is
deterministic integer-paise code. The LLM writes the human-readable explanation
for an already-classified exception, and every number it emits must appear in
the structured facts it was given, or the code falls back to a template. A
template-only mode runs with no API key.

## Limitations

- Leg 4 is evidenced by the ARN, never verified. `ARN_OVERDUE` is reported
  separately from settlement failures.
- Duplicate detection is the only heuristic rule; the other eight are arithmetic
  and route nothing to human review.
- `AMOUNT_MISMATCH` needs merchant order data Razorpay does not hold.
- Settlement and ARN thresholds are assumptions, printed in the report.
- Accuracy is measured against the generator's own ground truth. That is a real
  ceiling and it is stated before a judge has to say it.
- Below roughly 100 refunds a month with no partials and no disputes, the
  Razorpay dashboard is sufficient and this adds nothing.

See `docs/what-broke.md` for the failure-recovery log.
