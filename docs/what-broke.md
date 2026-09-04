# What broke and how I fixed it

Graded deliverable — Razorpay judges "failure recovery". Entries are appended the
moment something breaks. Format: symptom → cause → fix → the test that now guards it.

---

## 1. Daily settlements netted negative (generator, step 5)

**Symptom.** The first full generator run died on its own assertion:
`settlement on 2026-06-11 nets -3614347 paise`.

**Cause.** Not an arithmetic bug — a modelling error. The config asked for 600
payments and 340 refund events, a 57% refund rate. No merchant has that. On any
given day the refund debits outran the payment credits, so the batch payout came
out negative, which is impossible: Razorpay never claws money back through a
settlement, and refunds are paid from the merchant balance.

**Fix.** Raised `n_payments` to 3,400 against 340 refund events, a ~10% refund
rate. The graded population is still ~380 labelled refunds; the extra payments
are realistic context that make the settlement arithmetic possible.

**Guard.** `test_refund_rate_is_realistic` asserts the rate stays between 2% and
25%. `_group_into_settlements` keeps the hard assertion, so a bad config fails
loudly at generation instead of producing data the engine could never reconcile.

---

## 2. September settlements had refund debits but no payment credits

**Symptom.** Same assertion, moved to `2026-09-03`, after fix 1.

**Cause.** Payments were generated only up to `period_end` (31 August), but
refunds and their deductions continue into September up to `as_of`. Every
September settlement day therefore held debits with no offsetting credits.

**Fix.** Payments are now generated through `as_of`, not `period_end`. The
merchant keeps trading after the reporting month closes. Refund *events* are
still drawn only from payments created on or before `period_end`, so the graded
population stays tied to August activity.

**Guard.** `test_settlement_control_totals_tie` checks every batch's
`Σcredit − Σdebit` equals its payout and that no payout is negative.

This is the same class of mistake as the pull-window trap in CLAUDE.md §12: a
window that ends at the period boundary is always too narrow, because money
keeps moving after the period does.

---

## 3. Two test assertions were wrong, not the code (step 1)

**Symptom.** Two failures in the first test run: a UTC month-boundary test and a
rounding test.

**Cause.** Both were my errors in the tests. IST is UTC+5:30, so the date flips
for times *before* 05:30 IST, not after 23:00 as I had written — 31 Aug 23:30
IST is still 31 Aug in UTC. And my chosen partial-refund split happened to round
correctly by luck, so it did not demonstrate the failure it claimed to.

**Fix.** Rewrote the boundary test around 1 Sep 02:00 IST, which really is 31 Aug
in UTC. Replaced the rounding example with a ₹1,000 payment split three ways,
where naive rounding overshoots the parent fee by one paisa and largest-remainder
allocation does not.

**Lesson.** A green test proves nothing if the assertion encodes the wrong
expectation. Both tests now fail if the library regresses, which they previously
would not have.

---

## 4. Ground truth called in-flight refunds `CLOSED_MATCHED` (step 3)

**Symptom.** The first full engine run disagreed with the labels on 31 of 380
refunds. Every disagreement ran the same way: the engine said `OPEN`
(`AWAITING_SETTLEMENT`, `AWAITING_ARN`, or both) where ground truth said
`CLOSED_MATCHED`.

**Cause.** The generator models immaturity correctly and labels it wrongly. Step
5 skips emitting a recon row when the settlement date falls after `as_of`, and
`_set_arn` leaves `arn` null when the ARN would arrive after `as_of` — both
right. But `RefundPlan.expected_state` defaults to `CLOSED_MATCHED`, and nothing
revisited it, so a refund created *on* `as_of` with no recon row and no ARN was
labelled as three verified legs plus bank evidence. It had one leg and no
evidence.

A clamp made it worse. `_step3_refunds_happy_path` pinned `created_at` to
`ist_unix(as_of, 20)`, so every RMA raised near the end of the window piled its
refund onto a single day: 27 refunds on 4 September against 3-8 on every other
day.

**Fix.** A new `_label_immature` pass, run after step 5 and before ground truth,
derives the open reasons from the facts the generator actually emitted — "did I
write a recon row for this refund", "did I give it an ARN" — and flips the state
to `OPEN` only when the plan carries no seeded exception codes, matching stage
6's precedence. It reads no engine logic; those are facts about the file it just
wrote.

**Why this one mattered most.** It was a *ground truth* bug, and ground truth
bugs are silent. Every metric would still have printed, and every one would have
been wrong in the flattering direction: the engine looked 92% accurate while
being right, because the answer key was wrong. This is the mirror image of the
"grading your own homework" hazard in CLAUDE.md §8 — there the engine cheats, here
the answer key does.

**Guard.** `test_the_engine_reproduces_every_seeded_label` compares state, codes,
open reasons and annotations per record and prints the first five disagreements.
`_label_immature` carries two assertions of its own (see entry 5).

## 5. `arn_lag_wd_max` and the maturity gate can outrun each other

**Symptom.** The two assertions added in entry 4 both fired — one immediately, one
only under a different seed.

**Cause.** Two independent range mismatches, both invisible at seed 42.

* A seeded `REJECTED_INPUT` refund reached the ARN check. Stage 0 stops before
  stage 5, so a rejected refund can never be `AWAITING_ARN`. The pass had to skip
  them explicitly.
* `settlement.refund_deduction_lag_tail` goes up to 4 working days but
  `thresholds.settle_threshold_wd` is 3. A refund whose sampled lag was 4,
  observed exactly 3 working days later, has no recon row *and* has passed the
  maturity gate — indistinguishable from `NEVER_DEDUCTED`. The label would have
  been a coin toss.

**Fix.** `_effective_lag` pulls a sampled lag back inside the observation window:
if the deduction would land after `as_of` and the gate has already passed, the
refund settles at the latest lag that still lands by `as_of`. When even a
one-day lag overruns `as_of` the refund is provably young, so it stays in flight.
Both assertions remain, and both fail loudly if a threshold ever moves.

**Lesson.** Both thresholds are assumptions, so neither is "wrong" — but the data
must not sit in the gap between them. The generator now refuses to emit a record
whose correct label depends on which assumption you believe.

**Guard.** The two assertions inside `_label_immature`, plus
`test_a_different_seed_produces_different_output`, which is what caught the
second one.

## 6. `AMOUNT_MISMATCH` fired on refunds that matched their return exactly

**Symptom.** 13 `AMOUNT_MISMATCH` against 8 seeded. Five false positives, in
pairs.

**Cause.** An engine bug, not a data bug. SPEC.md §6.6 matches a refund to
"the nearest unconsumed returns-ledger row with `rma_created_at <=
refund.created_at`". Two returns raised a day apart and refunded out of order
each get handed the other's expected amount, and both look wrong — one `OVER`,
one `UNDER`, by exactly the same delta. That signature is what gave it away.

**Fix.** Two passes in `match_rmas`, exact before approximate. `refund.receipt`
is the merchant's own per-payment idempotency key, and this merchant's OMS
stamps the RMA id into it, so `receipt == rma_id` is an exact join. It is claimed
first, and only refunds with no receipt — or a receipt naming no ledger row —
fall through to the date heuristic, which can then never steal a row belonging to
a refund that named it outright.

**Lesson.** The date rule was in the spec, so it went in unquestioned. A
heuristic is worth writing only where no key exists; here one was sitting in a
field the engine was already reading for the duplicate rule.

**Guard.** `test_the_receipt_join_beats_date_proximity` rebuilds the exact
out-of-order pair and asserts neither refund is flagged.

## 7. `OPEN` was treated as a synonym for "not settled"

**Symptom.** After fix 4, a generator self-check failed:
`rfnd_ad2OnfVk7nx3sG: OPEN seed must not have settled`.

**Cause.** The assertion said any plan whose expected state is `OPEN` must have
no recon row. That is only true of `AWAITING_SETTLEMENT`. A refund can be
deducted from settlement — leg 3 verified — and still be waiting on its ARN,
which is leg 4 and a completely independent question.

**Fix.** The assertion now keys on the open *reason*, not the state:
`"AWAITING_SETTLEMENT" in plan.expected_open` implies no rows.

**Lesson.** This is the "three buckets" thinking CLAUDE.md §1 warns about,
resurfacing in an assertion instead of in a design doc. `open_reasons` is a list
for a reason, and the four legs are independent by design. Any code that treats a
state as shorthand for one specific reason will be wrong the moment a record has
two.
