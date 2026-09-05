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
bank reference was issued, not that the customer's account was credited.
Nowhere in this repo, its report or its pitch is the fourth leg counted as
verified.

---

## 1. Run it

```bash
make install     # pyyaml + pytest, nothing else
make test        # 304 tests, no network
make data        # synthetic data + ground truth, seed 42
make close       # close every refund, write out/
make eval        # score against ground truth, write out/report.md
```

`make all` runs the last three in order. No Razorpay account, no API key, no
network — the LLM layer defaults to template mode so the demo always runs.

### The dashboard

```bash
make serve       # http://localhost:8000
```

A payments-console UI over the same pipeline — overview with the identity
equation, the exception list in exposure order with per-record evidence and
explanation, the confusion matrix, leakage and timing, and the generated report.
Built on stdlib `http.server` with vanilla JS and no CDN, so a container serves
everything from its own origin and the dependency list stays at `pyyaml`.

The whole close runs in ~40 ms, so the dashboard recomputes rather than caches.
That makes the **duplicate-window control on the Accuracy page** live: drag it to
30 minutes, hit re-run, and watch `DUPLICATE_SUSPECT` fall from 10 to 3, recall
drop to 30%, and seven refunds move from `EXCEPTION` to `CLOSED_MATCHED` — the
sensitivity table from the report, happening in front of you.

### Deploying

The repo ships `Procfile`, `railway.json` and `nixpacks.toml`. On Railway:
create a project from the repo and deploy — `$PORT` is read from the
environment, `/healthz` is the health check, and the synthetic dataset is
generated on first boot because it is gitignored. Nothing else to configure.

To let the deployed instance use the model, set `GEMINI_API_KEY` in the Railway
environment and `llm.enabled: true` in `config.yaml`. Left alone it runs in
template mode, which is the safer default for a live demo.

Sample output from `make eval`:

```
identity (spec §8)
  N_in 380 == 279 closed + 49 open + 49 exception + 3 rejected  ->  HOLDS
  settlement control: Σdebit - Σamount 130628 paise, unexplained 0 (must be 0)

headline rates (numerator/denominator beside every percentage)
  match_rate_strict         85.06%  (279/328)   excludes OPEN and REJECTED_INPUT
  match_rate_all            73.42%  (279/380)
  state accuracy vs truth  100.00%  (380/380)
  exact record agreement   100.00%  (380/380)
  FALSE AUTO-MATCH RATE      0.00%  (0/52)   seeded failures closed anyway
    including OPEN           0.00%  (0/101)

duplicate window sensitivity (the only heuristic rule)
  W=1800s     tp=3   fp=0   fn=7   P=100.00%  (3/3)   R= 30.00%  (3/10)
  W=86400s    tp=10  fp=0   fn=0   P=100.00%  (10/10) R=100.00%  (10/10)
  W=259200s   tp=10  fp=0   fn=0   P=100.00%  (10/10) R=100.00%  (10/10)
```

Four artifacts land in `out/`: `report.md` (the whole run, with denominators),
`results.jsonl` (one line per refund), `exceptions.csv` (the exception list in
exposure order) and `run.log` (stages with counts).

---

## 2. Where the data comes from

**Everything is synthetic and generated locally by `generate.py`** — 3,400
payments and 380 refunds from a fixed seed, byte-for-byte reproducible. What
makes it credible is not that it came from Razorpay, but that **every field is
shaped exactly like the documented API response**, so a judge can trace any value
back to a real endpoint.

| File | Endpoint | Fields the engine actually uses |
|---|---|---|
| `payments.json` | `GET /v1/payments` | `id` `amount` `currency` `status` `method` `fee` `tax` `amount_refunded` `refund_status` `created_at` |
| `refunds.json` | `GET /v1/refunds` | `id` `payment_id` `amount` `currency` `status` `receipt` `acquirer_data.arn` `speed_processed` `created_at` |
| `disputes.json` | `GET /v1/disputes` | `id` `payment_id` `amount` `amount_deducted` `status` `phase` `created_at` |
| `settlements.json` | `GET /v1/settlements` | `id` `amount` `status` `utr` `created_at` |
| `settlement_recon.json` | `GET /v1/settlements/recon/combined?year=&month=` | `entity_id` `type` `debit` `credit` `fee` `tax` `payment_id` `settled_at` `settlement_id` `settlement_utr` |
| `returns_ledger.csv` | **not Razorpay** — merchant OMS/RMA export | `rma_id` `payment_id` `expected_refund_paise` `rma_created_at` |
| `ground_truth.json` | seeded labels | read by `evaluate.py` **only** |

Three details from those schemas do most of the work:

- **`Payment.fee` is GST-inclusive** and `tax` is its GST portion, so leakage is
  `fee` alone. Computing `fee + tax` double-counts the GST.
- **A recon refund row carries `fee = 0, tax = 0`.** The money the merchant loses
  is on the *payment* row, which is why the join in §4 of the spec exists.
- **A refund has no `settlement_id`, `settled_at`, `fee` or `tax`.** Everything
  about the settlement leg comes from the recon endpoint, which is keyed on
  settlement date — not transaction date.

The returns ledger is the one non-Razorpay source, and it is load-bearing.
Razorpay only enforces that total refunds never exceed the captured amount; it
has no idea what a specific return was *worth*. Refunding ₹20,000 for a ₹2,000
item passes every Razorpay check. Only the merchant's own ledger catches it,
which is why `AMOUNT_MISMATCH` needs this file — it is a merchant-side failure
the PSP is structurally blind to.

Because the schemas match the API, swapping the generator for live test-mode API
calls is a loader change, not an engine change.

---

## 3. The model

One exclusive axis plus two attributes computed on every record.

```
closure_state   exactly one   CLOSED_MATCHED | OPEN | EXCEPTION | REJECTED_INPUT
exception_codes 0..n          non-empty iff EXCEPTION
open_reasons    0..n          AWAITING_PROCESSING | AWAITING_SETTLEMENT | AWAITING_ARN
leakage_paise   every record  fees Razorpay never returns, matched records included
timing_flags    every record  CROSS_PERIOD | LATE_VS_THRESHOLD
```

Leakage and timing are **attributes, not buckets**. Leakage applies to nearly
every refund, so making it a bucket that competes with exceptions would remove
routed refunds from the leakage total and the numbers would stop adding up.

Zero silent drops: `N_in == N_closed + N_open + N_exception + N_rejected` is
asserted on every run, and the run fails if it does not hold.

---

## 4. Exception codes

Nine codes. Eight are arithmetic or evidence; exactly one is a heuristic, and it
is the only one that routes to a human.

| Code | Nature | Fields used |
|---|---|---|
| `REFUND_FAILED` | deterministic | `refund.status`, sibling refunds |
| `PENDING_OVERDUE` | deterministic + threshold | `status`, `created_at` |
| `NEVER_DEDUCTED` | arithmetic + maturity gate | recon rows, `created_at` |
| `DOUBLE_DEDUCTED` | arithmetic | recon rows |
| `SETTLEMENT_AMOUNT_DELTA` | arithmetic | recon `debit` vs `refund.amount` |
| `AMOUNT_MISMATCH` | arithmetic, needs merchant data | returns ledger |
| `REFUND_PLUS_CHARGEBACK` | arithmetic, cross-system | disputes, `created_at` |
| `ARN_OVERDUE` | evidence-based + threshold | `acquirer_data.arn`, `created_at` |
| `DUPLICATE_SUSPECT` | **heuristic → human review** | `amount`, `created_at`, `receipt` |

**The maturity gate is not optional.** A refund created yesterday has not failed
to settle; it has not had the chance to. Without the gate every young refund
becomes a false `NEVER_DEDUCTED` and precision collapses.

**The receipt clause is what keeps legitimate partials out of the duplicate
list.** Two refunds Razorpay accepted under distinct idempotency keys are two
real refunds. The rule flags only the later refund of a pair, only when at least
one carries no receipt, and always with a confidence and `needs_human_review`.

Two vocabulary rules the code enforces: `NO_PARENT_PAYMENT` is an *integrity
rejection* (the parent is not in the pull — the window is probably wrong) and
`NEVER_DEDUCTED` is a *merchant exception* (the money never left a payout). They
are never conflated under one ambiguous term, because they have different owners.

---

## 5. Metrics, from a real run

Seed 42, period 1–31 August 2026 IST, as of 4 September 2026.

```
N_in 380 == 279 CLOSED_MATCHED + 49 OPEN + 49 EXCEPTION + 3 REJECTED_INPUT   HOLDS

Σdebit − Σamount over settled refunds  = 130,628 paise
  explained by SETTLEMENT_AMOUNT_DELTA =  −2,572
  explained by DOUBLE_DEDUCTED         = 133,200
  UNEXPLAINED                          =       0   ← must be zero
```

| Metric | Value | Denominator note |
|---|---|---|
| `match_rate_strict` | 85.06% (279/328) | excludes OPEN and REJECTED_INPUT |
| `match_rate_all` | 73.42% (279/380) | — |
| state accuracy vs ground truth | 100.00% (380/380) | — |
| exact record agreement | 100.00% (380/380) | state + codes + open reasons + annotations + timing |
| false auto-match rate | 0.00% (0/52) | seeded failures = EXCEPTION + REJECTED_INPUT |
| false auto-match, incl. OPEN | 0.00% (0/101) | everything that should not have closed |

All nine codes score precision = recall = F1 = 1.000 at seed 42.

**Leakage.** ₹10,424.41 on ₹4,41,736.36 refunded = **236 bps**, split ₹1,589.69
GST / ₹8,834.72 MDR. That is the 2% + 18% GST base rate falling out of the
allocation, not a number typed in. The rounding residual against the exact
real-valued total is −58 paise across 342 payments — under one paisa per payment,
which is what largest-remainder allocation buys you.

**Timing.** 22 `CROSS_PERIOD`, 11 `LATE_VS_THRESHOLD`, lag histogram
`{1: 283, 2: 15, 3: 6, 4: 11}` working days, measured on the 315 refunds with
exactly one recon row.

**Legs.** 380 initiated, 368 gateway-processed, 311 settlement-deducted, 318
ARN-evidenced. Evidence failures are reported apart from settlement failures and
never summed: 14 settlement-leg exceptions against 8 `ARN_OVERDUE`.

**Throughput.** ~195,000 records/second — 380 refunds closed in 1.9 ms against
3,400 payments and 3,657 recon rows. The whole pipeline including load, scoring
and report writing is 34 ms.

### What these numbers do and do not show

The accuracy figures score the engine against labels this repository's own
generator produced. They measure **internal consistency between generator and
engine**, not accuracy against a real merchant's books. `make eval` prints that
caveat itself. Two places the numbers demonstrably move, which is the evidence
they measure anything:

- **Duplicate window.** Recall is 30.00% (3/10) at a 30-minute window and 100.00%
  (10/10) at 24 hours. The threshold is doing real work, which is why it is
  reported at three windows instead of asserted at one.
- **Pull window.** A recon pull that stops at `period_end` manufactures
  `NEVER_DEDUCTED`, because the recon endpoint is keyed on settlement date and a
  30 August refund is deducted in September. `tests/test_loader.py` measures it.

The number worth leading with is **49 exceptions found**, not the 100%.

---

## 6. Assumptions

Every threshold lives in `config.yaml`, is printed in `out/report.md`, and is
listed here as an assumption rather than a fact.

| Key | Default | Why it is an assumption |
|---|---|---|
| `fee_bps` | 200 | Public base pricing; real merchants negotiate |
| `gst_bps` | 1800 | 18% GST on the fee |
| `settlement.cycle_wd` | 2 | **Documented**: domestic T+2 working days |
| `refund_deduction_lag_wd` | 1 (90%), 2–4 (10%) | Razorpay's own recon sample nets a refund next-day |
| `settle_threshold_wd` | 3 | Maturity gate = cycle + 1 grace day |
| `arn_threshold_wd` | 10 | No documented ARN SLA |
| `pending_threshold_wd` | 10 | No documented SLA |
| `duplicate_window_seconds` | 86400 | Heuristic; sensitivity reported at 1800 / 86400 / 259200 |
| `chargeback_window_days` | 120 | Card-network dispute window, industry figure |
| `timezone` | Asia/Kolkata | Razorpay timestamps are Unix UTC; month boundaries are IST |

The refund settlement lag is deliberately **not** hardcoded to "T+5 to T+7" —
that figure comes from a marketing blog and conflates the customer-receipt SLA
with the settlement clock. `CROSS_PERIOD` is a binary month comparison in IST and
needs no SLA assumption at all.

Working days follow Indian banking rules: closed on Sundays and on the **2nd and
4th Saturdays**; the 1st, 3rd and 5th Saturdays are working days. A naive
Monday-to-Friday calendar shifts every settlement date.

---

## 7. AI judgment

**No model touches classification, matching, joining, arithmetic, date logic or
state decisions.** All of that is deterministic integer-paise code — the judging
criteria reward choosing a deterministic solution where AI is unnecessary, and
reconciliation is the clearest case of that there is.

The model writes prose about an exception the engine has **already** classified:
2–3 sentences plus one recommended action, from a structured facts object.
Provider is Google Gemini over `urllib` — no SDK dependency. `llm.enabled` is
`false` by default and the key is read from `$GEMINI_API_KEY`, never stored here.

Two guards stand between the model and the report:

- **Number guard.** Every figure in the output must appear in the facts object.
  Identifiers and ISO dates are removed first, then each remaining number token
  must *equal* a permitted figure.
- **Claim guard.** Rejects any assertion of four verified legs, this project's
  banned vocabulary, any claim the customer was credited, and any unhedged
  statement of a finding the engine marked for human review.

A rejected explanation falls back to that code's per-exception template, so a
model that invents something degrades the prose and never the numbers. Template
mode is the floor, not a degraded mode: with no API key every exception still
gets a specific explanation and a specific recommended action.

Both guards were earned. On its second live output the model wrote *"All four
settlement legs for the refund have been verified"* — the one claim this project
forbids — in a sentence containing no numbers at all.

---

## 8. What broke, and how I fixed it

Fourteen entries in [`docs/what-broke.md`](docs/what-broke.md), written as they
happened. The ones that changed the design:

1. **A 57% refund rate made daily settlements net negative.** Not an arithmetic
   bug — a modelling error. 340 refunds against 600 payments is a rate no
   merchant has, and Razorpay never claws money back through a payout. Raised
   payments to 3,400 for a ~10% rate.
2. **Payments stopped at `period_end`, so every September settlement had refund
   debits and no offsetting credits.** A window that ends at the period boundary
   is always too narrow, because money keeps moving after the period does. Same
   shape as the recon pull-window trap, found the hard way.
3. **Ground truth labelled in-flight refunds `CLOSED_MATCHED`** — 31 of them.
   The generator modelled immaturity correctly (no recon row, no ARN past
   `as_of`) but never revisited the default label, so refunds with one verified
   leg were labelled as three plus bank evidence. This is the dangerous kind:
   every metric still prints, and every one flatters the engine. I would have
   reported ~92% accuracy while being right.
4. **`AMOUNT_MISMATCH` fired on five refunds that matched their return exactly.**
   The spec's nearest-unconsumed-RMA rule swaps expectations between two returns
   refunded out of order — one `OVER`, one `UNDER`, same delta. The signature
   gave it away. Now joins on `receipt == rma_id` first, falling back to date
   proximity only when no receipt resolves.
5. **The settlement control total did not balance.** I summed `refund.amount`
   once per recon *row* instead of once per *refund*, so a double deduction
   cancelled itself out of the difference and was then counted again as
   explained. Debits per row, amounts per refund — that asymmetry is the whole
   point of the control.
6. **The number guard could be erased into passing anything.** It removed known
   literals then looked for surviving digits, and the bare `"9"` from an ISO date
   erased every digit of an invented `999.99`. Rewritten to tokenise and match
   whole tokens.
7. **The report used the banned word to explain that it is banned.** Twice, both
   as disclaimers. A vocabulary rule that carves out an exception for
   meta-discussion is not a vocabulary rule. A test caught it; I would not have.

Every entry ends with the test that now guards it.

---

## 9. Limitations

- Leg 4 is evidenced by the ARN, never verified. `ARN_OVERDUE` is reported
  separately from settlement failures and the two are never summed.
- Duplicate detection is the only heuristic rule. The other eight codes are
  arithmetic or evidence and route nothing to human review.
- `AMOUNT_MISMATCH` needs merchant order data Razorpay does not hold. Without a
  returns ledger this exception is not computable at all.
- Settlement, ARN and pending thresholds are assumptions, printed in the report.
- Accuracy is measured against the generator's own ground truth. That is a real
  ceiling, and it is stated before a judge has to say it.
- v1 is domestic INR only; instant refunds are off; recon `transfer` and
  `adjustment` rows are not generated.
- Refund status is modelled as `pending` / `processed` / `failed`. Razorpay's
  refund FAQ also describes a `reversed` outcome delivered on the
  `refund.processed` webhook; v1 neither generates nor classifies it.
- Below roughly 100 refunds a month with no partials and no disputes, the
  Razorpay dashboard is sufficient and this adds nothing.

---

## 10. Sources

Razorpay documentation (`razorpay.com/docs`):

- **Refunds** — `/api/refunds/entity`, `/api/refunds/fetch-all`,
  `/api/refunds/fetch-with-id`, `/api/refunds/create-normal` (errors table),
  `/api/refunds/normal-refunds-idempotent`, `/api/refunds/create-instant`,
  `/payments/refunds/`, `/payments/refunds/issue/`, `/payments/refunds/instant/`,
  `/payments/refunds/faqs/`, `/payments/refunds/errors/`
- **Settlements** — `/api/settlements/entity`, `/api/settlements/fetch-all`,
  `/api/settlements/fetch-recon`, `/payments/settlements/`
- **Payments** — `/api/payments/entity`, `/api/payments/capture/`
- **Disputes** — `/api/disputes/entity/`, `/api/disputes/fetch-all/`,
  `/payments/disputes/`

Razorpay blogs — chargebacks (deduction at initiation); "Do you get MDR back on
refunds"; pricing explained (platform fee on UPI, 18% GST, T+2/T+7); UPI charges,
MDR vs platform fees; payment gateway refund process.

Other:

- Double-refund chargeback rate (~10%) is an **unaudited industry estimate** from
  chargeback-vendor sources (justt.ai, chargebacks911.com, chargebackgurus.com,
  chargeflow.io), cited as such and never used in a calculation.
- RBI *Harmonisation of TAT for failed transactions* (20 Sep 2019) governs
  failed-transaction reversals, not merchant refunds.
- RBI *Regulation of Payment Aggregators* Directions, 2025 (15 Sep 2025) — the
  settlement-timeline reading is contested across sources, so nothing here
  depends on it.

---

## Status

| Step | Module | State |
|---|---|---|
| 1 Skeleton, config, calendar, money, entities | `config.py` `calendar_utils.py` `money.py` `ids.py` `entities.py` | **done** |
| 2 Synthetic data generator | `generate.py` `cli.py` | **done — 380 refunds, 52 seeded failures** |
| 3 Loader, invariants, closure engine | `loader.py` `invariants.py` `engine.py` | **done — all 380 seeded labels reproduced** |
| 4 Leakage, timing, leg evidence | `attributes.py` | **done — 236 bps leakage, control total ties** |
| 5 Metrics vs ground truth | `evaluate.py` | **done — confusion matrix, per-code P/R/F1, sensitivity** |
| 6 Explanation layer | `explain.py` | **done — Gemini + template fallback, two output guards** |
| 7 Report writer | `report.py` | **done — report.md, results.jsonl, exceptions.csv, run.log** |
| 8 Dashboard and deploy | `server.py` `web/` | **done — stdlib server, live re-run, Railway-ready** |

304 tests, no network. `SPEC.md` is the algorithm; `CLAUDE.md` is the working
contract.
