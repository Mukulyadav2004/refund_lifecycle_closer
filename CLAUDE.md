# CLAUDE.md — Refund Lifecycle Closer

Read this fully before writing code. It is the contract for this repo.

Project: an agent that closes the refund lifecycle for one month of merchant
activity, for the Razorpay AI Buildathon, AI Finance Controller track.
Submission is due **5 September 2026**. Optimise for a working, honest,
well-measured pipeline over features.

---

## 0. Prime directives

1. **Never invent a Razorpay field, endpoint, error string, status value, or
   SLA.** Every field name in this repo already appears in §3 below, which was
   transcribed from Razorpay's own documentation. If you need a field that is
   not in §3, stop and ask. Do not guess plausible names like
   `refund.settled_at`, `refund.settlement_id`, `payment.mdr`, or
   `recon.refund_fee` — none of those exist.
2. **Never convert a stated assumption into a stated fact.** §4 lists
   assumptions (thresholds, fee rates, lag). They live in `config.yaml`, are
   printed in the report, and are described in the README as assumptions.
3. **Integer paise everywhere.** No float, no Decimal, no division that returns
   a float. Money helpers live in `src/rlc/money.py`. If you find yourself
   writing `/` on money, use `//` with an explicit rounding helper instead.
4. **Deterministic money and matching.** No LLM anywhere in classification,
   joining, or arithmetic. The LLM only writes prose explanations for already
   classified exceptions (§9). Judging explicitly rewards choosing deterministic
   solutions where AI is unnecessary.
5. **Zero silent drops.** Every input refund resolves to exactly one
   `closure_state`. The evaluator asserts
   `N_in == N_closed + N_open + N_exception + N_rejected` and the run fails if
   it does not hold.
6. **When unsure, write the check as an assertion, not a comment.**

## 1. Vocabulary (use these exact names; do not rename)

- `closure_state` ∈ `CLOSED_MATCHED` | `OPEN` | `EXCEPTION` | `REJECTED_INPUT`.
  Exactly one per refund.
- `exception_codes[]`, `open_reasons[]`, `timing_flags[]` — lists, 0..n.
- Leakage and timing are **attributes computed on every record**, including
  matched ones. They are not buckets. An earlier version of this design called
  them "three buckets, one per refund" — that was wrong and must not come back:
  leakage applies to ~100% of refunds, so routing a refund into an "exception
  bucket" would remove it from the leakage total and break the numbers.
- Do not use the word **"orphan"**. It is ambiguous. Use `NO_PARENT_PAYMENT`
  (an integrity rejection) and `NEVER_DEDUCTED` (a real exception).
- Do not say **"4-leg lifecycle"** anywhere — repo, README, report, UI, video.
  The correct claim is **"3 legs verified, 1 leg evidenced"** (§5).

## 2. Layout

```
config.yaml                  all thresholds and assumptions
data/bank_holidays_IN_2026.json
src/rlc/
  config.py         Config dataclass + loader          [DONE]
  calendar_utils.py IST dates, working days            [DONE]
  money.py          paise arithmetic, allocation       [DONE]
  ids.py            deterministic Razorpay-style ids   [DONE]
  entities.py       dataclasses mirroring the API      [DONE]
  generate.py       synthetic data generator           [step 2]
  loader.py         read + normalise + index           [step 3]
  invariants.py     integrity checks                   [step 3]
  engine.py         closure state machine              [step 3]
  attributes.py     leakage, timing, leg evidence      [step 4]
  evaluate.py       metrics vs ground truth            [step 5]
  explain.py        LLM explanation layer              [step 6]
  report.py         report.md writer                   [step 7]
  cli.py            entry points
tests/
out/                          generated at runtime, gitignored
```

Run with `make data`, `make close`, `make eval`, `make test`.

## 3. Verified API surface — the ONLY fields that exist

Transcribed from Razorpay docs. Keep synthetic field names identical so a judge
can trace each one back.

### Payment (`GET /v1/payments`, `/v1/payments/:id`)
`id` `entity` `amount` `currency` `status` `method` `order_id` `description`
`international` `refund_status` `amount_refunded` `captured` `email` `contact`
`fee` `tax` `error_code` `error_description` `error_source` `error_step`
`error_reason` `notes` `created_at` `card_id`

- `status` ∈ `created` `authorized` `captured` `refunded` `failed`
- `refund_status` ∈ `null` `partial` `full`
- **`fee` is GST-inclusive** ("Fee (including GST) charged by Razorpay") and
  `tax` is the GST portion of it. **Never compute `fee + tax`** — that
  double-counts. Net settled for a payment is `amount - fee`.

### Refund (`GET /v1/refunds`, `/v1/refunds/:id`, `POST /v1/payments/:id/refund`)
`id` `entity` `amount` `currency` `payment_id` `notes` `receipt`
`acquirer_data` `created_at` `batch_id` `status` `speed_requested`
`speed_processed`

- `status` ∈ `pending` `processed` `failed`. `processed` is final.
- `acquirer_data` appears in three shapes in Razorpay's own samples: `{}`,
  `{"arn": null}`, `{"arn": "10000000000000"}`. Normalise all three to
  `arn: str | None`.
- `speed_requested` ∈ `normal` `optimum`; `speed_processed` ∈ `normal`
  `instant`. Both are absent unless `speed` was set on the request.
- `receipt` is optional and acts as a per-payment idempotency key
  (400 "Duplicate receipt found for this refund request").
- **There is NO `settlement_id`, `settled_at`, `fee`, or `tax` on a refund.**

### Dispute (`GET /v1/disputes`, `/v1/disputes/:id`)
`id` `entity` `payment_id` `amount` `currency` `amount_deducted` `reason_code`
`reason_description` `respond_by` `status` `phase` `created_at` `evidence`
(`evidence` includes a `refund_confirmation` slot), `comments`, `lifecycle`

- `status` ∈ `open` `under_review` `won` `lost` `closed`
- `phase` ∈ `fraud` `retrieval` `chargeback` `pre_arbitration` `arbitration`
- `amount_deducted` is documented as the amount deducted **when the dispute is
  lost**, and is 0 otherwise. Treat `lost` as realised loss and
  `open`/`under_review` as exposure at risk. Razorpay's blog says the amount is
  withheld at initiation — that is why "at risk" exists as a sub-state.

### Settlement (`GET /v1/settlements`, `/v1/settlements/:id`)
`id` `entity` `amount` `status` `fees` `tax` `utr` `created_at`
- `status` ∈ `created` `processed` `failed`; `fees`/`tax` are 0 for normal
  settlements. `amount` is the net paid to the bank.

### Settlement recon (`GET /v1/settlements/recon/combined?year=&month=[&day=]`)
`entity_id` `type` `debit` `credit` `amount` `currency` `fee` `tax` `on_hold`
`settled` `created_at` `settled_at` `settlement_id` `posted_at` `credit_type`
`description` `notes` `payment_id` `settlement_utr` `order_id` `order_receipt`
`method` `card_network` `card_issuer` `card_type` `dispute_id`

- `type` ∈ `payment` `refund` `transfer` `adjustment`. v1 generates only
  `payment` and `refund`.
- Refund rows: `debit = amount`, `credit = 0`, **`fee = 0`, `tax = 0`**,
  `payment_id` set. Payment rows: `credit = amount - fee`, `debit = 0`,
  `payment_id = null`.
- The endpoint is keyed on **settlement date**, not transaction date.
- Pagination `count` up to 1000.

### Returns ledger — NOT Razorpay
`rma_id` `order_id` `payment_id` `expected_refund_paise` `rma_created_at`
`reason`. This is the merchant's own OMS/RMA export. It is the only reason
`AMOUNT_MISMATCH` is computable, and the README must say so.

### Documented API errors that shape the logic
- 400 "The refund amount provided is greater than amount captured" — so a
  refund exceeding the parent is **impossible**; if it appears in data, that is
  an integrity failure, not a merchant exception.
- 400 "The refund on this payment is blocked due to ongoing dispute
  investigation" — so only the **refund-then-chargeback** ordering is reachable.
- 400 "Your account does not have enough balance…" — refunds are paid from the
  merchant's Razorpay balance, not from the original payment.
- 400 "Duplicate receipt found…" — `receipt` is an idempotency key, opt-in.

## 4. Assumptions (config-driven, never stated as fact)

| Key | Default | Why it is an assumption |
|---|---|---|
| `fee_bps` | 200 | Public base pricing; real merchants negotiate |
| `gst_bps` | 1800 | 18% GST on the fee |
| `settlement_cycle_wd` | 2 | Domestic T+2 working days (documented) |
| `refund_deduction_lag_wd` | 1 (90%), 2–4 (10%) | Razorpay's own recon sample shows a refund netted ~19h after creation. **Do not hardcode "T+5 to T+7"** — that number comes from a marketing blog and conflates the customer-receipt SLA with the settlement clock |
| `settle_threshold_wd` | 3 | Maturity gate = cycle + 1 grace day |
| `arn_threshold_wd` | 10 | No documented ARN SLA |
| `pending_threshold_wd` | 10 | No documented SLA |
| `duplicate_window_seconds` | 86400 | Heuristic; report sensitivity at 1800 / 86400 / 259200 |
| `chargeback_window_days` | 120 | Card-network dispute window, industry figure |
| `timezone` | Asia/Kolkata | Razorpay timestamps are Unix UTC; month boundaries are IST |

Working days exclude Sundays, **2nd and 4th Saturdays**, and listed bank
holidays. 1st/3rd/5th Saturdays are working days in Indian banking. This is
already implemented in `calendar_utils.py`; do not replace it with a naive
Mon–Fri rule.

## 5. The claim, stated precisely

- Leg 1 initiated — the refund record exists.
- Leg 2 gateway processed — `refund.status == "processed"`.
- Leg 3 deducted from settlement — exactly one recon refund row whose `debit`
  equals `refund.amount`.
- Leg 4 landed in the customer's bank — **evidenced only** by
  `acquirer_data.arn`. Razorpay's docs state a refund usually moves to
  `processed` before the ARN/RRN arrives, and their Create Refund sample shows
  `"status": "processed"` with `"arn": null`. An ARN proves a bank reference was
  issued, not that the customer's account was credited.

So: **3 legs verified, 1 leg evidenced.** Report `ARN_OVERDUE` counts
separately from settlement failures.

## 6. Closure state machine — implement in this exact order

Stage 0 integrity → `REJECTED_INPUT`, stop.
Stage 1 status: `failed` (superseded → matched, else `REFUND_FAILED`);
`pending` (`PENDING_OVERDUE` past threshold, else `AWAITING_PROCESSING`, and
skip stages 2–3).
Stage 2 existence: 0 recon rows → `AWAITING_SETTLEMENT` before the maturity
gate, `NEVER_DEDUCTED` after; >1 row → `DOUBLE_DEDUCTED`.
Stage 3 amount: `debit != amount` or `credit != 0` → `SETTLEMENT_AMOUNT_DELTA`.
Stage 4 cross-record on the parent payment: `DUPLICATE_SUSPECT` (heuristic),
`AMOUNT_MISMATCH` (needs returns ledger), `REFUND_PLUS_CHARGEBACK`
(sub `REALIZED` / `AT_RISK`).
Stage 5 evidence: no ARN → `AWAITING_ARN` before the threshold, `ARN_OVERDUE`
after.
Stage 6 decide: rejected → `REJECTED_INPUT`; any codes → `EXCEPTION`; any open
reasons → `OPEN`; else `CLOSED_MATCHED`.

**The maturity gate in stage 2 is not optional.** Without it every young refund
becomes a false `NEVER_DEDUCTED` and precision collapses.

Duplicate rule detail: flag only the later refund, only when at least one of the
pair has `receipt is None`. Two refunds with distinct receipts that Razorpay
accepted are legitimate. Attach a confidence and `needs_human_review = True`.
It is the only non-arithmetic rule in the engine; say so in the report.

## 7. Leakage formula (the part everyone gets wrong)

The recon **refund** row has `fee = 0, tax = 0`. The money lost is the original
**payment** row's fee, which is already GST-inclusive. Pro-rate by refund share
using largest-remainder allocation across all non-failed refunds of that payment
so partials sum exactly.

```
fee_alloc = allocate_largest_remainder(payment.fee, [r.amount ...], payment.amount)
tax_alloc = allocate_largest_remainder(payment.tax, [r.amount ...], payment.amount)
leakage        = fee_alloc[i]              # total, GST-inclusive
leakage_gst    = tax_alloc[i]
leakage_mdr    = fee_alloc[i] - tax_alloc[i]
```

Independent per-refund rounding is a bug: three partials on one payment will
miss the parent fee by 1–2 paise and the control total stops tying.

Timing: `CROSS_PERIOD` is the binary test
`(created_d.year, created_d.month) != (settled_d.year, settled_d.month)` in IST.
It needs no SLA assumption. Report the lag distribution; do not assert a number.

## 8. Metrics to print literally

```
N_in == N_closed + N_open + N_exception + N_rejected          (assert)
match_rate_strict = N_closed / (N_closed + N_exception)       (state the exclusion)
match_rate_all    = N_closed / N_in
false_auto_match_rate = seeded failures that ended CLOSED_MATCHED / seeded failures
```
Plus per-code precision/recall/F1, a state confusion matrix, duplicate-window
sensitivity, leakage totals with `leakage_bps`, timing counts, throughput
(records/sec), and data-error channel counts. Every percentage prints its
numerator and denominator beside it.

The evaluator is the only module allowed to read `ground_truth.json`. If the
engine imports it, that is grading your own homework — fail the review.

## 9. LLM boundary

Allowed: given a structured facts object for an already-classified exception,
produce 2–3 sentences plus one recommended action. Every number in the output
must appear in the facts object; otherwise fall back to a template. A
template-only mode must work with no API key so the demo always runs.

Not allowed: classification, matching, arithmetic, date logic, deciding states.

## 10. Style

Python 3.12, standard library plus `pyyaml` and `pytest` only (an LLM SDK is
optional and lazily imported). Dataclasses over dicts once data is loaded.
Type hints on public functions. No global mutable state. Every module gets a
docstring naming the spec section it implements. Tests are `pytest`, fast, no
network.

## 11. What is already done

All seven steps are complete, with 281 tests passing. Run `make test` before
changing anything and again after.

* Step 1: `config.py`, `calendar_utils.py`, `money.py`, `ids.py`, `entities.py`,
  the holiday calendar, `config.yaml`.
* Step 2: `generate.py` and `cli.py`. `make data` writes 3,400 payments, 380
  refunds, 359 RMAs, 13 disputes, 73 settlements and 3,657 recon rows into
  `data/synthetic/`, plus `ground_truth.json` and `manifest.json`. 52 seeded
  failures across all nine exception codes. Byte-for-byte deterministic.

The generator self-checks before writing: settlement control totals must tie, no
payment may be over-refunded, failed refunds must not settle, `AWAITING_SETTLEMENT`
seeds must have no recon row, and NEVER_DEDUCTED seeds must have no recon row. If
a self-check fires, fix the generator — do not relax the check.

* Step 3: `loader.py`, `invariants.py`, `engine.py` and `make close`. The engine
  reproduces all 380 seeded labels exactly — state, exception codes, open reasons
  and annotations — and asserts the identity equation on every run. Population at
  seed 42: 279 CLOSED_MATCHED, 49 OPEN, 49 EXCEPTION, 3 REJECTED_INPUT.

Two rules the engine added that are not in SPEC.md §6, both recorded in
`docs/what-broke.md`:

* `AMOUNT_MISMATCH` matches a refund to its RMA by `receipt == rma_id` first and
  falls back to the spec's date-proximity rule only when no receipt resolves.
  The date rule alone swaps expectations between two returns refunded out of
  order and reports two mismatches where there are none.
* `NO_RMA_MATCH` is informational and is listed in
  `engine.INFORMATIONAL_ANNOTATIONS`. The generator does not seed it, so the
  evaluator must not score it as a false positive.

* Step 4: `attributes.py`. Leakage, timing and leg evidence are computed on every
  non-rejected record and folded into `make close`. At seed 42: leakage
  ₹10,424.41 on ₹4,41,736.36 refunded (236 bps, ₹1,589.69 GST / ₹8,834.72 MDR),
  22 `CROSS_PERIOD`, 11 `LATE_VS_THRESHOLD`, and the §8 settlement control total
  closes with 0 paise unexplained.

Timing flags in `ground_truth.json` are derived from the settlement dates the
generator emitted, not from seeded intent, so all 380 records carry the flags the
data implies. The 10 deliberately steered cases are still identifiable by their
`cross_period` scenario.

* Step 5: `evaluate.py` and `make eval`. Confusion matrix, per-code
  precision/recall/F1, both false-auto-match denominators, duplicate-window
  sensitivity, and the §8 identity plus settlement control total. Every rate is a
  `Ratio` that prints its numerator and denominator. At seed 42 the engine
  reproduces all 380 labels, so every code scores 1.000 — which measures
  generator/engine consistency, not real-world accuracy, and `make eval` says so
  in its own output.

`false_auto_match_rate` is reported against two denominators. The manifest's
"seeded failures" counts only EXCEPTION + REJECTED_INPUT (52); the inclusive
variant adds OPEN (101), because an OPEN refund called CLOSED_MATCHED tells a
controller money landed when it has not.

* Step 6: `explain.py`, wired into `make close`. Provider is **Google Gemini**
  (`gemini-3.6-flash`) over `urllib` — no new dependency, per §10. `llm.enabled`
  is false by default so a clone runs keyless; the key is read from
  `$GEMINI_API_KEY` and is never stored in the repo.

Two guards sit between the model and the report, and both were earned:

* `verify` — every number in the output must appear in the facts object.
* `verify_claims` — no "four legs", no "orphan", no assertion that the customer
  was credited, and anything the engine marked `needs_human_review` must be
  hedged. Gemini claimed four verified legs on its second live output; the number
  guard could not see it (see `docs/what-broke.md` entry 11).

A rejected explanation falls back to that code's template, so a model that
invents something degrades the prose and never the numbers. Template mode is the
floor, not a degraded mode: `make close` with no API key produces a full
explanation for every exception, deterministically.

* Step 7: `report.py`. Writes the four §10 artifacts into `out/` — `report.md`,
  `results.jsonl`, `exceptions.csv` and `run.log`. `make close` writes all four
  (report.md without the ground-truth sections, so a run over real unlabelled
  data still produces one); `make eval` rewrites report.md with the match rates,
  confusion matrix, per-code scores and sensitivity table, and adds
  `evaluation.json`.

Two report disciplines are enforced by tests rather than by care:
`test_every_percentage_carries_its_denominator` and
`test_the_report_never_uses_banned_vocabulary` (`4-leg`, `four-leg`, `orphan`,
`all four legs`). The second one caught two live violations in the first draft —
see `docs/what-broke.md` entry 14.

The build is feature-complete. What remains is the README (§10 lists the ten
required sections), the video, and whatever the run turns up.

Engine authors: `data/synthetic/ground_truth.json` exists and is readable. The
engine must never open it. Only `evaluate.py` may.

## 12. Things that will break, and the fix

- **Month boundaries computed in UTC.** A refund created 31 Aug 23:30 IST is
  30 Aug 18:00 UTC — different month. Always go through
  `calendar_utils.to_ist_date`.
- **Single-month recon pull.** A refund created 30 Aug settles in September.
  Pull recon for the period **plus at least one settlement cycle after it**, and
  pull payments from ~6 months before the period start. A narrow window
  manufactures false `NEVER_DEDUCTED`. Write a regression test for this.
- **Float rounding on money.** Use `money.py` only.
- **Duplicate false positives on legitimate multi-partial refunds.** The
  `receipt` clause is what prevents it.
- **Failed refunds counted in ΣR.** Exclude `status == "failed"` from parent
  totals, duplicate pairs, and leakage allocation.

Record whichever of these actually bites you: the submission explicitly asks
what broke and how you fixed it.
