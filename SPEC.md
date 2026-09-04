# Refund Lifecycle Closer — final build spec

Track: AI Finance Controller (Razorpay AI Buildathon). Deadline: 5 Sep 2026.
Bar from the brief: throughput + measured accuracy + an honest exception list. "One cherry-picked match proves nothing."

This document is written so you can code from it top to bottom. Sections:

0. What you are building, in one paragraph, and the exact claims you may make
1. Facts vs assumptions (what is verified, what you must state as a convention)
2. Data sources and schemas, each traced to a real Razorpay endpoint
3. Synthetic data generator — build order, formulas, seeded failures, ground truth
4. Join graph
5. Integrity invariants (assert, do not assume)
6. Closure state machine — ordered rules with exact predicates
7. Attribute calculators — leakage, timing, leg evidence
8. Metrics, identity equations, evaluator
9. Where the LLM sits (and where it must not)
10. Output artifacts, README, video
11. Build order for one day
12. Pitch wording and limitations
13. Glossary
14. Sources

Amounts are integer paise everywhere. Never floats.

---

## 0. What you are building

An agent that closes the refund lifecycle for one month of merchant activity. It ingests five data sets shaped exactly like Razorpay's own API entities (Payments, Refunds, Disputes, Settlements, Settlement Recon) plus one merchant-side file (returns ledger), joins them on `refund_id` and `payment_id`, and resolves every refund into exactly one closure state: `CLOSED_MATCHED`, `OPEN`, `EXCEPTION`, or `REJECTED_INPUT`. On top of that exclusive state it computes two attributes on every record: rupee leakage (fees Razorpay never returns on a refund) and timing (settlement lag, cross-period drift). It reports match rate with stated denominators, per-exception precision and recall against seeded ground truth, a confusion matrix, and a false-auto-match rate.

### The model, corrected

Earlier we said "every refund lands in exactly one of three buckets." That was wrong and both reviewers caught it. Leakage applies to almost every refund; cross-period timing applies to many; exceptions apply to a few. If you force one bucket per refund you must pick a winner and the other totals become wrong. The correct shape is:

| Axis | Cardinality | Values |
|---|---|---|
| `closure_state` | exactly one per refund (MECE) | `CLOSED_MATCHED`, `OPEN`, `EXCEPTION`, `REJECTED_INPUT` |
| `exception_codes[]` | 0..n, non-empty iff state is `EXCEPTION` | listed in §6 |
| `open_reasons[]` | 0..n, non-empty iff state is `OPEN` | `AWAITING_PROCESSING`, `AWAITING_SETTLEMENT`, `AWAITING_ARN` |
| `leakage_paise` | computed on every record, matched ones included | integer |
| `timing_flags[]` | 0..n on every settled record | `CROSS_PERIOD`, `LATE_VS_THRESHOLD` |
| `leg_evidence` | on every record | legs 1–3 verified/not, leg 4 evidenced/not |

"Zero silent drops" applies to `closure_state` only. Leakage and timing are decorations that sum across the whole population.

### Claims you may make (and must not)

- Say: "Three legs verified (initiated, gateway-processed, deducted from settlement); the fourth leg (customer's bank credit) is evidenced by the ARN/RRN, not verified." Razorpay's own docs say a refund usually moves to `processed` before the ARN/RRN arrives from the gateway, and the Create Refund sample shows `status: processed` with `arn: null`.
- Say: "The data lives across four Razorpay endpoints and one merchant system, and nothing joins them on the refund axis." Do not say "the pain compounds at volume" — a Razorpay judge will answer that they already reconcile at volume.
- Say: "Eight rules are arithmetic and cannot be wrong; one (duplicate) is a heuristic and routes to human review."
- Say: "Amount mismatch catches a merchant-side failure Razorpay is structurally blind to, because Razorpay does not know what a return was worth."
- Do not say "4-leg lifecycle" anywhere: diagram, README, video, UI.

---

## 1. Facts vs assumptions

Verified against Razorpay documentation (see §14 for URLs):

| # | Fact | Where |
|---|---|---|
| F1 | Refund `status` ∈ {`pending`, `processed`, `failed`}; `processed` is the final state | Refund entity |
| F2 | Razorpay usually moves a refund to `processed` before receiving the ARN/RRN from the gateway; a support-enabled setting can delay `processed` until confirmation | Pay Refunds to Customers |
| F3 | `acquirer_data` holds one of RRN/ARN/UTR from the banking partner; samples show `{}`, `{"arn": null}`, and `{"arn": "..."}` — three shapes | Refund entity, Fetch All Refunds, Create Refund samples |
| F4 | Multiple partial refunds allowed while their sum ≤ captured amount; API returns 400 if refund amount > captured | Issue Refunds; Create Normal Refund errors |
| F5 | `receipt` is treated as an idempotency key per payment (400 "Duplicate receipt found") — and `receipt` is optional | Create Normal Refund errors |
| F6 | A separate header-based idempotency mechanism exists (`X-Refund-Idempotency`); it is not stored on the refund entity, so a reconciler cannot observe it | Idempotent refund pages |
| F7 | Refunds are paid from the merchant's Razorpay balance, not from the original payment (400 "not enough balance") | Create Normal Refund errors |
| F8 | Refund creation is blocked while a dispute is under investigation (400) | Create Normal Refund errors |
| F9 | Normal refund reaches the customer in 5–7 working days; Instant refund almost immediately; if an Instant refund falls back to normal, the instant fee is credited back; the platform fee is never refunded | About Refunds; About Instant Refunds |
| F10 | `speed_requested`/`speed_processed` appear only if `speed` was set; values `normal`/`optimum` and `instant`/`normal` | Refund entity |
| F11 | Failed refund reasons documented: payment older than 6 months; Instant refund bank/account issues | Refund entity `status` |
| F12 | Payment entity: `fee` = "Fee (including GST) charged by Razorpay"; `tax` = "GST charged for the payment"; also `amount_refunded`, `refund_status` ∈ {null, partial, full}, `captured` | Payments entity / Capture a Payment |
| F13 | Recon combined endpoint returns `payment`, `refund`, `transfer`, `adjustment` rows keyed by settlement date (`year`, `month`, optional `day`, `count` up to 1000). Refund rows carry `entity_id = rfnd_…`, `payment_id`, `debit = amount`, `credit = 0`, `fee = 0`, `tax = 0`, `settled_at`, `settlement_id`, `settlement_utr`, `dispute_id` | Fetch Settlement Recon Details |
| F14 | In Razorpay's own recon sample, the refund row's `settled_at` is about 19 hours after its `created_at` (next-day netting) | Fetch Settlement Recon Details sample |
| F15 | Settlement entity: `id`, `amount`, `status` ∈ {created, processed, failed}, `fees`, `tax`, `utr`, `created_at`; fees/tax are 0 for normal settlements | Settlements entity |
| F16 | Domestic settlement cycle T+2 working days; working days exclude bank holidays; international T+7 in the FAQ (product page has softened it) | About Settlements, FAQs |
| F17 | Dispute entity: `id`, `payment_id`, `amount`, `amount_deducted`, `reason_code`, `respond_by`, `status` ∈ {open, under_review, won, lost, closed}, `phase` ∈ {fraud, retrieval, chargeback, pre_arbitration, arbitration}, `created_at`, `evidence.refund_confirmation` | Disputes entity |
| F18 | `amount_deducted` is "deducted from your Razorpay current balance when the dispute is lost … 0 unless status is lost"; Razorpay's blog separately says the disputed amount is withheld when a dispute is initiated. Treat the API field as the realized loss and open/under_review as exposure at risk | Disputes entity; Chargebacks blog |
| F19 | MDR/transaction fee and its GST are not reversed on refunds, on any Indian gateway; UPI bank-to-bank carries 0% MDR but Razorpay charges a ~2% platform fee, also with 18% GST, also not reversed | Razorpay blogs on MDR and refunds, pricing page |
| F20 | Submission: public GitHub repo, 5-minute pitch video, a "what broke and how you fixed it" account; judged on problem taste, build quality, AI judgment (deterministic where AI is unnecessary), failure recovery | Buildathon coverage |

Assumptions you must state in the README (they are conventions, not facts):

| # | Assumption | Default | Why it is an assumption |
|---|---|---|---|
| A1 | Fee rate 2.00% flat on all domestic methods; GST 18% on the fee | 200 bps, 1800 bps | Public base pricing; real merchants have negotiated rates |
| A2 | Refund debit lands in the next settlement batch: 1 working day after creation, with a 10% tail of 2–4 days | lag_wd = 1 | Supported by F14 (a doc sample) and F7; not a published SLA. The "T+5–T+7 deduction" line from a Razorpay blog is marketing copy — do not hardcode it |
| A3 | Maturity gate for settlement = cycle + 1 grace working day | 3 wd | Engineering choice |
| A4 | ARN threshold: a `processed` refund with no ARN after N working days is flagged | 10 wd | No documented ARN SLA; chosen above the 5–7 wd customer-receipt window |
| A5 | Pending threshold: `pending` beyond N working days is flagged | 10 wd | Same reasoning |
| A6 | Duplicate window W | 24 h (report 30 min / 24 h / 72 h) | Heuristic |
| A7 | Chargeback window | 120 days from payment | Card-network dispute window, industry figure |
| A8 | v1 is domestic INR only; instant refunds off | — | Scope control |
| A9 | Month boundaries computed in Asia/Kolkata | IST | Razorpay timestamps are Unix UTC |

---

## 2. Data sources and schemas

Each synthetic file mirrors a real entity so a judge can trace every field to Razorpay's docs. Keep the field names identical to the API.

### 2.1 payments.json ↔ Payment entity (`GET /v1/payments`, `GET /v1/payments/:id`)

Fields used:

```
id              "pay_…"            string
amount          integer paise
currency        "INR"
status          "captured" | "refunded" | "failed" | "authorized"
method          "card" | "upi" | "netbanking" | "wallet"
order_id        "order_…"
international   false
captured        true/false
refund_status   null | "partial" | "full"
amount_refunded integer paise
fee             integer paise   GST-INCLUSIVE (F12)
tax             integer paise   GST portion of fee (F12)
created_at      unix seconds
card_network, card_type   optional, for reporting by method
```

Convention (state it): `fee = fee_ex + tax`, `tax = GST on fee_ex`, net settled for the payment = `amount − fee`.

### 2.2 refunds.json ↔ Refund entity (`GET /v1/refunds`, paginated `count ≤ 100`, `skip`, `from`, `to`)

```
id              "rfnd_…"
entity          "refund"
amount          integer paise
currency        "INR"
payment_id      "pay_…"
notes           {}
receipt         string | null        (idempotency key, optional — F5)
acquirer_data   {} | {"arn": null} | {"arn": "…"}   (F3)
created_at      unix seconds
batch_id        null
status          "pending" | "processed" | "failed"   (F1)
speed_requested "normal" | "optimum"   (may be absent — F10)
speed_processed "normal" | "instant"   (may be absent — F10)
```

Normalize on load: `arn = (acquirer_data or {}).get("arn")`; `speed_requested = speed_requested or "normal"`; `speed_processed = speed_processed or "normal"`.

### 2.3 disputes.json ↔ Dispute entity (`GET /v1/disputes`, `GET /v1/disputes/:id`)

```
id              "disp_…"
payment_id      "pay_…"
amount          integer paise
currency        "INR"
amount_deducted integer paise      (>0 only when status = lost — F18)
reason_code     string
respond_by      unix seconds
status          "open" | "under_review" | "won" | "lost" | "closed"
phase           "fraud" | "retrieval" | "chargeback" | "pre_arbitration" | "arbitration"
created_at      unix seconds
evidence        { "refund_confirmation": null | ["doc_…"], ... }
```

### 2.4 settlements.json ↔ Settlement entity (`GET /v1/settlements`)

```
id         "setl_…"
entity     "settlement"
amount     integer paise     (net paid to bank: Σcredit − Σdebit of its recon rows)
status     "processed"
fees       0
tax        0
utr        string
created_at unix seconds
```

### 2.5 settlement_recon.json ↔ `GET /v1/settlements/recon/combined?year=YYYY&month=MM[&day=DD]` (`count ≤ 1000`)

One row per settled transaction:

```
entity_id       "pay_…" | "rfnd_…"
type            "payment" | "refund"      (v1: no transfers/adjustments)
debit           integer paise             (refund rows: = amount)
credit          integer paise             (payment rows: = amount − fee)
amount          integer paise
currency        "INR"
fee             integer paise             (payment rows: GST-inclusive fee; refund rows: 0 — F13)
tax             integer paise             (payment rows: GST; refund rows: 0)
on_hold         false
settled         true
created_at      unix seconds              (transaction creation)
settled_at      unix seconds
settlement_id   "setl_…"
payment_id      null for payment rows; "pay_…" for refund rows (F13)
settlement_utr  string
order_id        "order_…"
method, card_network, card_issuer, card_type
dispute_id      null | "disp_…"
```

Join keys: payment rows join on `entity_id == payment.id`; refund rows join on `entity_id == refund.id`.

### 2.6 returns_ledger.csv ↔ merchant's own OMS/RMA system (NOT Razorpay)

```
rma_id, order_id, payment_id, expected_refund_paise, rma_created_at, reason
```

This is the only non-Razorpay source. It is what makes `AMOUNT_MISMATCH` computable. Say so.

### 2.7 ground_truth.json — generator labels, read only by the evaluator

```
{ "<refund_id>": { "expected_state": "...", "expected_codes": [...], "scenario": "..." }, ... }
```

Plus `bank_holidays_IN_2026.json` (list of dates) and `config.yaml` (§1 assumptions, `as_of`).

Optional stretch (§7.4): `merchant_bank_statement.csv` with `date, narration, utr, credit_paise` — one credit per settlement UTR, for a bank-side control total.

---

## 3. Synthetic data generator — build order

Deterministic seed (`--seed 42`). Target ≈ 600 payments and ≈ 380 refunds (competitive bar is 300–500; brief minimum is 50). Generate in this order because each step depends on the previous one.

### Step 0 — config and calendar

```
period_start = 2026-08-01, period_end = 2026-08-31, as_of = 2026-09-04 (IST)
payments_window_start = 2026-06-01     # parents up to ~3 months old, inside the 6-month refund limit (F11)
tz = Asia/Kolkata
fee_bps = 200 for card/upi/netbanking/wallet ; gst_bps = 1800
settlement_cycle_wd = 2
refund_deduction_lag_wd = 1 (90%), 2–4 (10%)
arn_lag_wd ~ Uniform{0..5}
```

Working-day helpers (date-level, IST):

```
is_working_day(d) = d.weekday() < 5 and d not in bank_holidays
add_working_days(d, n): step forward one calendar day at a time, count only working days
working_days_between(d1, d2): number of working days in (d1, d2]
```

### Step 1 — payments

For i in 1..600:

```
created_at  ~ uniform over [payments_window_start, period_end] (IST business hours)
method      ~ upi 55%, card 30%, netbanking 10%, wallet 5%
amount      ~ round(lognormal, ₹200–₹50,000) in paise, multiple of 100
status      = captured (later set to refunded if fully refunded)
fee_ex      = round_half_up(amount × fee_bps / 10000)
tax         = round_half_up(fee_ex × gst_bps / 10000)
fee         = fee_ex + tax                    # GST-inclusive, matches Payment entity (F12)
amount_refunded = 0 ; refund_status = null    # filled after refunds exist
```

### Step 2 — returns ledger (merchant truth for "what was owed")

Pick ≈ 340 payments for refund events (mostly with created_at in June–August so refunds fall in August):

```
full (70%):    expected_refund_paise = amount
partial (30%): expected_refund_paise = round(amount × U(0.10, 0.60)) rounded to 100 paise
rma_created_at = payment.created_at + U(1, 25) days, clipped ≤ as_of
```

For 5% of chosen payments create TWO partial RMAs (sum ≤ amount) — these become legitimate multi-partial refunds and test the duplicate rule for false positives.

### Step 3 — refunds, happy path

For each RMA:

```
id          = "rfnd_" + random16
amount      = expected_refund_paise
created_at  = rma_created_at + U(0, 2) days
status      = processed
speed_requested = speed_processed = "normal"
receipt     = "RMA-<rma_id>" with probability 0.7, else null   # merchants that skip receipts get no idempotency protection (F5)
arn         = "1000" + random10 if created_at + arn_lag_wd ≤ as_of else null   # ARN arrives later (F2)
```

Then per payment: `amount_refunded = Σ non-failed refund amounts`, `refund_status = full if Σ == amount else partial if Σ > 0 else null`, `status = refunded if full`.

### Step 4 — seeded scenarios (what the engine must catch, and what it must not)

Apply on top of the happy path. Record every seed in `ground_truth.json`. Suggested counts for ≈ 380 refunds:

| Scenario | Count | How to seed | Expected state / codes |
|---|---|---|---|
| DUPLICATE (true) | 10 | clone a processed refund: new id, same amount, `created_at + U(1, 90) min`, `receipt = null` on the clone; keep ΣR ≤ amount (choose partials or payments with room) | clone → `EXCEPTION` [`DUPLICATE_SUSPECT`]; original → `CLOSED_MATCHED` |
| DUPLICATE hard negative | 6 | two refunds, same amount, 1 day apart, both with distinct receipts | both `CLOSED_MATCHED` |
| AMOUNT_MISMATCH over | 5 | `amount = min(expected × 10, payment.amount − other refunds)`; if that equals expected, swap two digits instead | `EXCEPTION` [`AMOUNT_MISMATCH`] |
| AMOUNT_MISMATCH under | 3 | `amount = expected − U(500, 5000)` | `EXCEPTION` [`AMOUNT_MISMATCH`] |
| NEVER_DEDUCTED | 7 | processed, matured (created ≥ 5 wd before as_of), omit the recon refund row | `EXCEPTION` [`NEVER_DEDUCTED`] |
| DOUBLE_DEDUCTED | 3 | two recon refund rows for the same refund id on two settlement dates | `EXCEPTION` [`DOUBLE_DEDUCTED`] |
| SETTLEMENT_AMOUNT_DELTA | 4 | recon `debit = amount ± U(100, 2000)` paise | `EXCEPTION` [`SETTLEMENT_AMOUNT_DELTA`] |
| ARN_OVERDUE | 8 | processed, created ≥ 12 wd before as_of, `arn = null` forever | `EXCEPTION` [`ARN_OVERDUE`] |
| REFUND_PLUS_CHARGEBACK realized | 3 | dispute on the same payment, `phase = chargeback`, `created_at = refund.created_at + U(5, 40) days`, `status = lost`, `amount_deducted = dispute.amount = payment.amount` | `EXCEPTION` [`REFUND_PLUS_CHARGEBACK`] sub `REALIZED` |
| REFUND_PLUS_CHARGEBACK at risk | 2 | same but `status = under_review`, `amount_deducted = 0` | `EXCEPTION` [`REFUND_PLUS_CHARGEBACK`] sub `AT_RISK` |
| Dispute won after refund | 1 | same but `status = won`, `amount_deducted = 0` | `CLOSED_MATCHED`, annotation `DISPUTE_RESOLVED` |
| Chargeback-first (no refund possible) | 2 | dispute created before any refund; do NOT create a refund (API blocks it — F8) | no refund record; counted in "disputes without refunds" info only |
| Disputes on unrelated payments | 5 | payments with no refunds | nothing flagged |
| REFUND_FAILED, superseded | 2 | `status = failed`, then a second processed refund same amount 1–3 days later | failed one → `CLOSED_MATCHED` (`FAILED_SUPERSEDED`); second → `CLOSED_MATCHED` |
| REFUND_FAILED, not superseded | 2 | `status = failed`, no re-issue | `EXCEPTION` [`REFUND_FAILED`] |
| PENDING_OVERDUE | 2 | `status = pending`, created ≥ 12 wd before as_of, no recon row | `EXCEPTION` [`PENDING_OVERDUE`] |
| OPEN awaiting settlement/ARN | 15 | created within the last 1–2 working days before as_of, no recon row, `arn = null` | `OPEN` [`AWAITING_SETTLEMENT`, `AWAITING_ARN`] |
| Pending, young | 3 | `status = pending`, created 1–3 days before as_of | `OPEN` [`AWAITING_PROCESSING`] |
| CROSS_PERIOD (natural) | ≈ 10 | refunds created 28–31 Aug settle in September | `CLOSED_MATCHED` with timing flag `CROSS_PERIOD` |
| REJECTED_INPUT | 3 | one with `amount = −500`, one with `payment_id` missing, one created before its payment | `REJECTED_INPUT` |

Every other refund is expected `CLOSED_MATCHED`. Print the seeded counts at the end of generation.

### Step 5 — settlements and recon rows

```
settlement_dates = every working day in [payments_window_start, as_of]
for each captured payment:
    settled_date = add_working_days(created_date_IST, settlement_cycle_wd)
    recon row: type=payment, entity_id=id, credit=amount−fee, debit=0, fee, tax, payment_id=null,
               created_at, settled_at=settled_date 06:00 IST, order_id, method, card fields
for each processed refund not in NEVER_DEDUCTED:
    lag = 1 wd (90%) else U(2,4) wd
    settled_date = add_working_days(created_date_IST, lag)
    if settled_date > as_of: no row yet (this is how OPEN/awaiting settlement arises)
    recon row: type=refund, entity_id=refund.id, debit=amount (or ± delta for DELTA seeds), credit=0,
               fee=0, tax=0, payment_id=refund.payment_id, created_at=refund.created_at,
               settled_at, order_id, method, dispute_id = id of any dispute on that payment else null
    DOUBLE_DEDUCTED seeds: emit a second row on the next settlement date
group rows by settled_date → settlement_id "setl_"+random14, utr "UTR"+YYYYMMDD+random6
settlement.amount = Σcredit − Σdebit   (keep it positive: if a day goes negative, move a few payments earlier)
settlement.created_at = settled_at ; fees = tax = 0 ; status = processed
```

### Step 6 — ground truth and manifest

Write `ground_truth.json`, `manifest.json` (counts per scenario, seed, config hash), and, optionally, `merchant_bank_statement.csv` (one credit per settlement: `utr`, `credit_paise = settlement.amount`, `date`).

---

## 4. Join graph

```
refunds.id            → recon.entity_id  WHERE type='refund'     1 : 0..n   (leg 3; n>1 = double deduction)
refunds.payment_id    → payments.id                               n : 1      (parent; fee, tax, amount, amount_refunded)
refunds.payment_id    → recon.entity_id  WHERE type='payment'     n : 0..1   (settlement of the parent, optional)
refunds.payment_id    → disputes.payment_id                       1 : 0..n   (chargeback overlap)
refunds.payment_id    → returns_ledger.payment_id                 1 : 0..n   (expected amount; pick nearest RMA ≤ refund.created_at, consume once)
recon.settlement_id   → settlements.id                            n : 1      (batch control total)
settlements.utr       → merchant_bank_statement.utr (optional)    1 : 0..1   (bank-side control)
```

Two join levels: `refund_id` binds Refunds↔Recon; `payment_id` binds that result to Payments, Disputes and the returns ledger.

Pull-window rule (production and generator alike): the recon endpoint is keyed on settlement date, so a refund created on 30 Aug settles in September. Pull recon for the period plus at least one settlement cycle after it, and pull payments from 6 months before the period start (refunds are not possible on older payments, F11). A naive single-month pull manufactures false `NEVER_DEDUCTED` results. This is the most likely bug in your build; write a test for it.

---

## 5. Integrity invariants — assert, never assume

Violations go to a `data_errors` channel, are counted and reported, and are excluded from the match-rate denominator with the exclusion stated. They never enter exception counts.

| # | Invariant | On violation |
|---|---|---|
| I1 | `refund.amount > 0`, `currency == parent.currency`, `payment_id` present and found, `refund.created_at ≥ parent.created_at`, `parent.status ∈ {captured, refunded}` | refund → `REJECTED_INPUT` (reason code: `NEGATIVE_AMOUNT`, `NO_PARENT_PAYMENT`, `CURRENCY_MISMATCH`, `REFUND_BEFORE_PAYMENT`, `PARENT_NOT_CAPTURED`) |
| I2 | Per payment: Σ non-failed `refund.amount` ≤ `payment.amount` | impossible in Razorpay (F4) → all refunds of that payment `REJECTED_INPUT` (`OVER_CAPTURED_TOTAL`) |
| I3 | Per payment: `amount_refunded == Σ non-failed refund.amount`; `refund_status` consistent (`full` iff Σ == amount, `partial` iff 0 < Σ < amount) | `data_anomaly` note on the payment; refunds still processed |
| I4 | Recon refund row: `payment_id == refund.payment_id` | row ignored + `data_error` `JOIN_CORRUPTION` |
| I5 | Recon refund row: `fee == 0 and tax == 0` (F13) | `data_anomaly` note; do not interpret |
| I6 | Per settlement: Σ`credit` − Σ`debit` of its recon rows == `settlement.amount` | `SETTLEMENT_CONTROL_BREAK` at batch level, reported separately (not per refund) |
| I7 | Timestamps within [2000-01-01, as_of] | `REJECTED_INPUT` |

`NO_PARENT_PAYMENT` is deliberately an integrity failure, not a merchant exception: Razorpay cannot create a refund without a captured parent, so a missing parent means your pull window is too narrow or the data is corrupt. (One reviewer called this "orphan refund"; in our earlier discussions "orphan" meant "never deducted from settlement". The engine uses neither word — it uses `NO_PARENT_PAYMENT` and `NEVER_DEDUCTED`.)

---

## 6. Closure state machine

Evaluate per refund, in this order. Earlier stages gate later ones. `now = as_of`.

### 6.1 Derived dates

```
created_d   = to_IST_date(refund.created_at)
settle_due  = add_working_days(created_d, settle_threshold_wd)   # A3, default 3
arn_due     = add_working_days(created_d, arn_threshold_wd)      # A4, default 10
pending_due = add_working_days(created_d, pending_threshold_wd)  # A5, default 10
S(r)        = [row for row in recon if row.type == 'refund' and row.entity_id == r.id]
P           = payments[r.payment_id]
R(P)        = [x for x in refunds if x.payment_id == P.id and x.status != 'failed']
D(P)        = [d for d in disputes if d.payment_id == P.id]
```

### 6.2 Stage 0 — integrity (§5) → `REJECTED_INPUT` and stop

### 6.3 Stage 1 — status-driven

```
if r.status == 'failed':
    if exists r2 in R(P) with r2.amount == r.amount and r2.created_at > r.created_at:
        annotate FAILED_SUPERSEDED ; state CLOSED_MATCHED ; stop      # re-issued successfully
    else:
        codes += REFUND_FAILED ; stop                                  # needs re-initiation (F11 lists reasons)

if r.status == 'pending':
    if now > pending_due: codes += PENDING_OVERDUE
    else: open += AWAITING_PROCESSING
    skip stages 2–3 (no settlement expected yet) ; continue to stage 4 for cross-record checks
```

Failed refunds are excluded from `R(P)`, from ΣR, and from duplicate pairs.

### 6.4 Stage 2 — existence at the settlement leg (processed only)

```
if len(S(r)) == 0:
    if now < settle_due: open += AWAITING_SETTLEMENT          # maturity gate — young refunds are not exceptions
    else:                codes += NEVER_DEDUCTED
elif len(S(r)) > 1:
    codes += DOUBLE_DEDUCTED   (evidence: settlement_ids, Σdebit)
```

### 6.5 Stage 3 — amount at the settlement leg (if exactly one row)

```
row = S(r)[0]
if row.debit != r.amount or row.credit != 0:
    codes += SETTLEMENT_AMOUNT_DELTA   (delta = row.debit − r.amount)
```

### 6.6 Stage 4 — cross-record checks on the parent payment

Duplicate (the only heuristic rule):

```
for r2 in R(P), r2.id != r.id:
    if r2.amount == r.amount and abs(r2.created_at − r.created_at) ≤ W
       and (r.receipt is None or r2.receipt is None) and r.created_at > r2.created_at:
        codes += DUPLICATE_SUSPECT
        confidence = 0.9 if Δ ≤ 30 min else 0.7 if Δ ≤ 24 h else 0.5
        needs_human_review = True ; evidence = r2.id
```

Flag only the later refund. If both carry distinct receipts and Razorpay accepted both, they are legitimate separate refunds (F5) — do not flag. Report precision/recall at W ∈ {30 min, 24 h, 72 h}.

Amount mismatch (requires the merchant returns ledger):

```
rma = nearest unconsumed returns_ledger row with payment_id == P.id and rma_created_at ≤ r.created_at
if rma is None:
    annotate NO_RMA_MATCH          # informational in v1 (could be a goodwill refund)
elif abs(r.amount − rma.expected_refund_paise) > amount_tolerance_paise:
    codes += AMOUNT_MISMATCH (direction = OVER if r.amount > expected else UNDER, delta)
```

Note for the pitch: Razorpay's API only guarantees ΣR ≤ captured amount (F4). Refunding ₹20,000 for a ₹2,000 phone case passes that check. Only the merchant's own ledger can catch it.

Refund + chargeback (double payout):

```
for d in D(P):
    if d.created_at > r.created_at and (d.created_at − P.created_at) ≤ 120 days
       and d.phase in {chargeback, pre_arbitration, arbitration, fraud}:
        if d.status == 'lost' and d.amount_deducted > 0:
            codes += REFUND_PLUS_CHARGEBACK (sub = REALIZED, exposure = r.amount + d.amount_deducted)
        elif d.status in {open, under_review}:
            codes += REFUND_PLUS_CHARGEBACK (sub = AT_RISK, exposure_at_risk = d.amount)
        else:  # won, closed
            annotate DISPUTE_RESOLVED
```

Only the refund-first ordering is reachable: a refund attempted after a dispute is blocked at the API (F8), and an accepted dispute is itself resolved by a refund (dispute status `closed`). Say this in the pitch — most articles describe both orderings; you read the error table. The 10% figure ("about one in ten chargebacks becomes a double refund") is an unaudited industry estimate; cite it as such.

### 6.7 Stage 5 — bank-side evidence (processed only)

```
if r.arn is None:
    if now < arn_due: open += AWAITING_ARN
    else:             codes += ARN_OVERDUE      # evidence-based: "processed" without any bank reference past the threshold (F2)
```

### 6.8 Stage 6 — decide the state

```
if rejected:            state = REJECTED_INPUT
elif len(codes) > 0:    state = EXCEPTION
elif len(open) > 0:     state = OPEN
else:                   state = CLOSED_MATCHED
```

`CLOSED_MATCHED` therefore means: parent found, exactly one recon row, debit equals amount, no cross-record failure, and an ARN present. This is the "cautious internal ledger status" we designed earlier: the customer sees Razorpay's `processed`; the merchant's books close only on bank evidence.

### 6.9 Exception code table

| Code | Nature | Fields used | Seedable |
|---|---|---|---|
| `REFUND_FAILED` | deterministic (Razorpay status) | refund.status, R(P) | yes |
| `PENDING_OVERDUE` | deterministic + threshold A5 | status, created_at | yes |
| `NEVER_DEDUCTED` | arithmetic + threshold A3 | S(r), created_at | yes |
| `DOUBLE_DEDUCTED` | arithmetic | S(r) | yes |
| `SETTLEMENT_AMOUNT_DELTA` | arithmetic | S(r).debit, refund.amount | yes |
| `AMOUNT_MISMATCH` | arithmetic, needs merchant data | refund.amount, returns_ledger | yes |
| `REFUND_PLUS_CHARGEBACK` | arithmetic, cross-system | disputes, refund.created_at | yes |
| `ARN_OVERDUE` | evidence-based + threshold A4 | acquirer_data.arn, created_at | yes |
| `DUPLICATE_SUSPECT` | heuristic → human review | amount, created_at, receipt | yes |

If time is short, ship these first: `NEVER_DEDUCTED`, `DUPLICATE_SUSPECT`, `AMOUNT_MISMATCH`, `REFUND_PLUS_CHARGEBACK`, `ARN_OVERDUE`, `SETTLEMENT_AMOUNT_DELTA`. Add the other three if time permits; the generator seeds should match whatever the engine implements.

---

## 7. Attribute calculators (run on every non-rejected record)

### 7.1 Leakage — read the fee off the PAYMENT row, never the refund row

The recon refund row has `fee = 0, tax = 0` (F13). The money lost is the original payment's fee, which is GST-inclusive (F12). Pro-rate by refund share, and allocate with largest remainder across all refunds of the payment so partials sum exactly.

```
def allocate_largest_remainder(total, parts, base):
    # exact shares total*part/base ; floor ; hand out leftover paise to largest fractional parts
    raw     = [Fraction(total * p, base) for p in parts]
    floors  = [int(x) for x in raw]
    target  = int(sum(raw))              # floor of the exact total on the refunded portion
    leftover = target - sum(floors)
    order   = sorted(range(len(parts)), key=lambda i: raw[i] - floors[i], reverse=True)
    for i in order[:leftover]: floors[i] += 1
    return floors

parts = [x.amount for x in R(P)]                       # non-failed refunds on P, stable order by created_at
fee_alloc = allocate_largest_remainder(P.fee, parts, P.amount)   # total leakage per refund (GST-inclusive)
tax_alloc = allocate_largest_remainder(P.tax, parts, P.amount)   # GST portion
leakage(r)      = fee_alloc[r]
leakage_gst(r)  = tax_alloc[r]
leakage_mdr(r)  = fee_alloc[r] − tax_alloc[r]        # MDR / platform-fee portion
```

Do not compute `fee + tax` — on the Payment entity `fee` already includes `tax`. Full refund of a ₹1,00,000 card payment at 2% + 18% GST: `fee_ex = 200000`, `tax = 36000`, `fee = 236000` paise → leakage ₹2,360, of which ₹360 is GST. Priya gets her full ₹1,00,000; the merchant is out ₹2,360 on a sale that earned zero.

Instant refunds (off in v1): if `speed_processed == 'instant'`, add `instant_refund_fee_paise` from config; if `speed_requested == 'optimum' and speed_processed == 'normal'` the fee was credited back (F9) → add 0.

Leakage is reported on matched records too. Headline checks: `Σ leakage`, `leakage_bps = Σ leakage / Σ refunded × 10000` (should be ≈ 236 bps under A1), by method, and a reconciliation of Σ leakage against Σ over payments of `fee × ΣR / amount` (difference must be < number of payments, in paise, from rounding).

### 7.2 Timing

```
settled_d      = to_IST_date(S(r)[0].settled_at)        # only if exactly one row
settle_lag_wd  = working_days_between(created_d, settled_d)
CROSS_PERIOD   = (created_d.year, created_d.month) != (settled_d.year, settled_d.month)
LATE_VS_THRESHOLD = settle_lag_wd > settle_threshold_wd
```

`CROSS_PERIOD` is binary and needs no SLA assumption — it is exactly the "an August refund reduces September's settlement" problem. Report the lag distribution (histogram) instead of asserting a number. Keep the threshold configurable; do not hardcode "T+5–T+7".

### 7.3 Leg evidence

```
leg1_initiated          = True (record exists)
leg2_gateway_processed  = r.status == 'processed'
leg3_settlement_deducted = len(S(r)) == 1 and S(r)[0].debit == r.amount
leg4_bank_evidenced     = r.arn is not None          # evidenced, never verified
```

### 7.4 Batch controls (optional)

Per settlement: I6 above. If `merchant_bank_statement.csv` exists: `bank.credit_paise == settlement.amount` joined on `utr`. Report `settlements_tied / settlements_total`.

---

## 8. Metrics, identity equations, evaluator

Print these literally in the report — they prove nothing was dropped.

```
N_in = N_closed + N_open + N_exception + N_rejected                       # assert; fail the run otherwise
Σ_over settled refunds (S(r).debit) − Σ (r.amount)
      == Σ deltas explained by SETTLEMENT_AMOUNT_DELTA + Σ extra debits explained by DOUBLE_DEDUCTED   # unexplained difference must be 0
match_rate_strict = N_closed / (N_closed + N_exception)                   # excludes OPEN and REJECTED — say so
match_rate_all    = N_closed / N_in
```

Against `ground_truth.json` (evaluator only; the engine never reads it):

- State-level confusion matrix (CLOSED / OPEN / EXCEPTION / REJECTED).
- Per exception code: TP, FP, FN, precision, recall, F1.
- `false_auto_match_rate = (# seeded failures that ended CLOSED_MATCHED) / (# seeded failures)` — the only number that costs real money; make it prominent.
- Duplicate sensitivity: precision/recall at W = 30 min, 24 h, 72 h.
- Leakage: total, by method, bps, GST vs MDR split.
- Timing: count and ₹ of `CROSS_PERIOD`, lag histogram, `LATE_VS_THRESHOLD` count.
- Throughput: records/second and wall time for the batch (the brief asks for throughput).

Denominator discipline: every percentage printed states its numerator and denominator next to it.

---

## 9. Where the LLM sits

Judging explicitly rewards deterministic solutions where AI is unnecessary (F20). So:

- Never in classification, matching, or arithmetic. All of §5–§8 is plain code on integers.
- Explanation layer: for each `EXCEPTION`, pass a structured facts object (ids, amounts in ₹, dates, code, sub-state, evidence) to the model and ask for a 2–3 sentence explanation plus one recommended next action (for example: "submit `refund_confirmation` evidence on dispute disp_… before respond_by", "re-initiate refund", "confirm with ops whether rfnd_A and rfnd_B were both intended"). Guard: every number in the output must appear in the facts object, else fall back to a template sentence. Ship a template mode so the demo runs without an API key.
- Optional, if time remains: a question layer that turns "how much GST did we lose on card refunds in August?" into a filter/aggregate over the results table, executed deterministically.

State this split in the README under "AI judgment".

---

## 10. Output artifacts, README, video

Outputs per run (`out/`):

- `results.jsonl` — one line per refund: state, codes, open reasons, leakage (total/gst/mdr), timing, leg evidence, evidence ids, explanation.
- `exceptions.csv` — the honest exception list, sorted by exposure.
- `report.md` — identity equations, match rates with denominators, confusion matrix, per-code precision/recall, sensitivity table, leakage and timing summaries, throughput, data-error channel counts, settlement control results.
- `run.log` — structured log of every stage with counts.

README sections (required by the submission rules, F20):

1. What it does, in three sentences, with the three-legs-verified claim.
2. How to run: `make data`, `make close`, `make eval` (or three commands), sample output.
3. Data provenance table (§2): each file → Razorpay endpoint → fields used.
4. Assumptions (§1 table) and thresholds (config).
5. Exception codes (§6.9) with nature: arithmetic / heuristic / evidence.
6. Metrics and identity equations, with a real run pasted in.
7. AI judgment: where the LLM is and is not used.
8. What broke and how I fixed it — write real ones; candidates you will almost certainly hit: month boundary computed in UTC instead of IST (moves refunds across periods); single-month recon pull manufacturing false `NEVER_DEDUCTED`; float rounding leaving leakage off by paise until integer paise + largest remainder; duplicate rule flagging legitimate multi-partials until the receipt clause was added.
9. Limitations (§12).
10. Sources (§14).

Video (5 minutes): 30 s problem (Razorpay's own docs: processed before ARN); 60 s architecture and join graph; 120 s live run with the identity equation, exception list, confusion matrix; 60 s one exception explained end to end (refund + chargeback); 30 s limitations and what broke.

---

## 11. Build order for one day

1. (1.5 h) Repo skeleton, `config.yaml`, holiday calendar, working-day helpers with tests, entity dataclasses mirroring §2.
2. (2 h) Generator §3 with seeds and `ground_truth.json`; manifest printing seeded counts.
3. (2.5 h) Engine §5–§6: loader/normalizer, invariants, state machine, evidence.
4. (1 h) Attribute calculators §7 (largest remainder, timing, leg evidence) + batch controls.
5. (1 h) Evaluator §8: identity assertions, confusion matrix, per-code precision/recall, sensitivity, throughput.
6. (1 h) Explanation layer §9 with guard and template fallback.
7. (1.5 h) `report.md` writer, README, commit a real run's outputs, record the video.
8. Buffer (1 h). If behind: drop instant refunds, `PENDING_OVERDUE`, `REFUND_FAILED`, bank statement — keep the six core codes.

Reader test before you submit: paste this spec into a fresh Claude session and ask it to implement the generator. If it has to ask you questions, the spec has a gap — fix the spec, not the code.

---

## 12. Pitch wording and limitations

Opening: "Razorpay's own documentation says a refund usually moves to processed before the bank reference arrives. The refund's status lives in one endpoint, its settlement deduction in another, its chargeback in a third, and what the customer was actually owed in the merchant's system. Nothing joins them on the refund axis. This agent does, and it tells you exactly what it could not close."

Limitations to name out loud (they earn credit in this track):

- Leg 4 is evidenced by the ARN, never verified; the engine reports `ARN_OVERDUE` counts separately.
- Duplicate detection is heuristic; the other eight rules are arithmetic.
- `AMOUNT_MISMATCH` requires merchant order data Razorpay does not hold.
- Settlement and ARN thresholds are assumptions (§1), printed in the report, not documented constants.
- Accuracy is measured against the generator's own ground truth — a real ceiling.
- Below roughly 100 refunds a month with no partials and no disputes, the Razorpay dashboard is enough.

Two lines that show you read to the bottom: the recon row already carries `dispute_id`, and the dispute evidence schema has a dedicated `refund_confirmation` slot — Razorpay anticipates the double-payout scenario and leaves detection manual. And: UPI MDR is legally enabled by the August 2026 bill but not yet notified, so fee leakage tracking will matter more, not less.

Do not cite the 2019 RBI TAT circular for refund timing (it governs failed-transaction reversals). Do not cite "RBI PA Directions 2025 mandate T+1" — sources disagree on whether the directions mandate T+1 or allow negotiated terms.

---

## 13. Glossary

- Captured: the payment has been charged; refunds are only possible from this state.
- Settlement: the batch payout of net funds from Razorpay to the merchant's bank; identified by `setl_…` and a bank UTR.
- UTR: bank reference of the settlement payout; how you find it on the merchant's bank statement.
- ARN/RRN: reference number the banking partner issues for a refund; evidence it entered the bank rails, not proof of credit.
- MDR / transaction fee / TDR: the percentage fee on a payment; GST 18% is charged on it; neither is returned on refund.
- Platform fee: Razorpay's own fee, visible on UPI where MDR is 0%; also not returned.
- Chargeback: the customer's bank reverses a payment through a dispute; phases `chargeback`, `pre_arbitration`, `arbitration`.
- Working day: Mon–Fri excluding bank holidays; every Razorpay SLA is stated this way.

---

## 14. Sources

Razorpay documentation (razorpay.com/docs):

- Refunds API index — `/docs/api/refunds/`; Refund entity — `/docs/api/refunds/entity`; Fetch All Refunds — `/docs/api/refunds/fetch-all`; Fetch Refund With ID — `/docs/api/refunds/fetch-with-id`; Create a Normal Refund (errors table) — `/docs/api/refunds/create-normal`; Idempotent refund requests — `/docs/api/refunds/normal-refunds-idempotent`, `/docs/api/refunds/instant-refunds-idempotent`; Create an Instant Refund — `/docs/api/refunds/create-instant`
- About Refunds / Pay Refunds to Customers — `/docs/payments/refunds/`; Issue Refunds — `/docs/payments/refunds/issue/`; About Instant Refunds — `/docs/payments/refunds/instant/`; Refunds FAQs — `/docs/payments/refunds/faqs/`; Handle Refund Errors — `/docs/payments/refunds/errors/`
- Settlements API index — `/docs/api/settlements/`; Settlements entity — `/docs/api/settlements/entity`; Fetch All Settlements — `/docs/api/settlements/fetch-all`; Fetch Settlement Recon Details — `/docs/api/settlements/fetch-recon`; About Settlements — `/docs/payments/settlements/`; Settlements FAQs
- Payments entity — `/docs/api/payments/entity`; Capture a Payment — `/docs/api/payments/capture/`
- Disputes entity — `/docs/api/disputes/entity/`; Fetch All Disputes — `/docs/api/disputes/fetch-all/`; Fetch a Dispute (expanded transaction.settlement) — `/docs/api/disputes/fetch-dispute-expanded-transaction`; About Disputes — `/docs/payments/disputes/`
- Razorpay blogs: Chargebacks (deduction at initiation) — `razorpay.com/blog/chargebacks/`; Do you get MDR back on refunds — `razorpay.com/blog/do-you-get-mdr-back-on-refunds/`; Razorpay pricing explained (platform fee on UPI, 18% GST, T+2/T+7) — `razorpay.com/blog/razorpay-payment-gateway-pricing-explained/`; UPI charges: MDR vs platform fees — `razorpay.com/blog/upi-charges-explained-mdr-vs-platform-fees/`; Payment gateway refund process — `razorpay.com/blog/payment-gateway-refund-process`

Other:

- Double refund chargebacks (industry estimate ≈ 10%): justt.ai, chargebacks911.com, chargebackgurus.com, chargeflow.io
- RBI Harmonisation of TAT for failed transactions (20 Sep 2019) — governs failed-transaction reversals, not merchant refunds
- RBI (Regulation of Payment Aggregators) Directions, 2025 (15 Sep 2025) — settlement-timeline reading is contested across sources
- Buildathon requirements and judging (public repo, 5-minute video, what broke; problem taste, build quality, AI judgment, failure recovery): dev.to Dev Opportunity Radar #14; careersincloud.com; careerstn.com; jobseekershub.co.in
- Competing hackathon repo for differentiation: github.com/Sashank2006/Razorpay-Drift-Reconciler (fee-schedule drift, not refund lifecycle)
