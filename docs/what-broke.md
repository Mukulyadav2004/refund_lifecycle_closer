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

---

## 8. The settlement control total did not balance (step 4)

**Symptom.** SPEC.md §8 requires that `Σdebit − Σamount` over settled refunds be
fully explained by `SETTLEMENT_AMOUNT_DELTA` plus the extra debits of
`DOUBLE_DEDUCTED`. The first run reported an unexplained −₹1,332.

**Cause.** My accounting, not the data. I added `refund.amount` once per *recon
row* instead of once per *refund*, so a double-deducted refund contributed its
amount twice on both sides of the subtraction. The two extra debits cancelled
themselves out of the difference and were then counted again as "explained",
producing a residual of exactly the double-deducted total.

**Fix.** Debits are summed per row, amounts per refund. That asymmetry is the
whole point of the control: it is what makes a second debit visible.

**Guard.** `test_every_paisa_of_settlement_difference_is_explained` asserts
`unexplained_paise == 0` on the full month, plus two synthetic cases that isolate
a delta and a double deduction. `unexplained_paise` is now printed by
`make close` with "(must be 0)" beside it.

## 9. Ground truth's timing flags described the intent, not the data

**Symptom.** The engine flagged 22 `CROSS_PERIOD` refunds and 11
`LATE_VS_THRESHOLD`. Ground truth listed 10 and 0.

**Cause.** Same shape as entry 4. Ten refunds were deliberately *steered* across
a month boundary and labelled. But any refund created near month end and
deducted in the next batch crosses one too, and a refund whose sampled deduction
lag ran to 4 working days is late against a 3-day threshold whether or not
anyone seeded it. `expected_timing_flags` was recording what the generator meant
to do rather than what it wrote.

**Fix.** `_label_timing` derives both flags from the `settled_at` values the
generator actually emitted, after step 5. The 10 steered cases stay identifiable
by their `cross_period` scenario, and an assertion checks each of them really did
cross — the seeder's intent is now verified rather than assumed.

**On circularity.** This label is computed from dates the generator chose, so it
cannot test whether the engine *guesses* a hidden intent. What it does test is
whether the engine joins the recon rows and converts to IST correctly — and that
is the first entry in CLAUDE.md §12, because 01:00 IST on 1 September is 31
August in UTC and a UTC month test silently reports the wrong month.
`test_a_month_boundary_is_measured_in_ist_not_utc` pins that case directly, with
no reference to ground truth at all.

**Guard.** `test_the_engine_agrees_with_the_seeded_timing_flags` compares all 380
records. An existing generator test that asserted `expected_timing_flags ==
["CROSS_PERIOD"]` exactly was relaxed to `in`, since a cross-period refund may
legitimately also be late.

---

## 10. `attributes.annotate` was not idempotent (step 5)

**Symptom.** A negative-control test in the evaluator failed for the wrong
reason. It silenced one `NEVER_DEDUCTED` and expected exactly one record to
disagree with ground truth; 29 disagreed. The extra ones all looked like
`expected ('LATE_VS_THRESHOLD',)  got ('LATE_VS_THRESHOLD', 'LATE_VS_THRESHOLD')`.

**Cause.** `annotate` appended to `verdict.timing_flags` instead of assigning to
it. Verdicts are enriched in place, so the second call — `make eval` re-scores a
run it has already annotated — gave every flagged record a second copy of its
flag. Leakage was unaffected because those fields are assigned, not accumulated;
`settle_lag_wd` had the same latent problem, keeping a stale value when a
re-annotated record no longer had exactly one recon row.

**Fix.** Flags are built in a local list and assigned; `settle_lag_wd` is reset
to `None` at the top of each record. `annotate` is now safe to call any number of
times.

**Lesson.** In-place enrichment across module boundaries needs the same
discipline as a database migration: write the whole field, never add to what is
already there. The bug was invisible in `make close`, which annotates once, and
would have surfaced first as inflated `CROSS_PERIOD` counts in the report.

**Guard.** `test_annotating_twice_changes_nothing` snapshots every verdict row,
re-annotates and compares. The evaluator's negative-control tests would also
have caught it, which is how it was found.

---

## 11. The model claimed four verified legs (step 6)

**Symptom.** The second explanation of the first live Gemini run read: *"All four
settlement legs for the refund have been verified."* That is the one claim
CLAUDE.md §1 forbids outright — the project claims three legs verified and one
evidenced, and says so in the repo, the README, the report and the video.

**Cause.** The guard specified in SPEC.md §9 checks that every **number** in the
output appears in the facts object. "four" is a word. The guard read the
sentence, found no digits, and passed it.

**Fix.** A second guard, `verify_claims`, with three forbidden patterns — any
claim of four legs, the banned word "orphan", and any assertion that the customer
was credited (an ARN evidences a bank reference, not a credit) — plus a positive
requirement: when the engine set `needs_human_review`, the prose must contain a
hedging word, so a `DUPLICATE_SUSPECT` can never be stated as a fact. The system
prompt now states all four rules as well, so the guard is a backstop rather than
the only line.

**Lesson.** "Every number must appear in the facts" is a good rule that reads as
a complete one. It is not: the expensive errors in this domain are claims, not
figures. A model that says *four legs verified* has said something false about
the product in a sentence containing no numbers at all.

**Guard.** `test_forbidden_claims_are_rejected` covers all four patterns, and
`test_every_template_passes_its_own_guards` holds the templates to the same bar
the model is held to.

## 12. The number guard could be erased into passing anything

**Symptom.** While writing the test for it, `"The customer should receive the
money within 3 to 5 business days"` was rejected as expected — but
`"999.99 rupees"` passed.

**Cause.** The guard removed every known literal from the text and then looked
for surviving digits. Among the known literals were the components of an ISO
date, including the bare `"9"` from `2026-09-04`. Erasing `"9"` everywhere turned
`999.99` into `.` and nothing was left to object to. Any invented figure made
only of digits that appear somewhere in the facts would have passed.

**Fix.** Whole-token matching instead of erasure. Identifiers and ISO dates are
removed first — so a digit inside `rfnd_QfW822jZGfgbhJ` is never read as an
amount — and every remaining number token must *equal* a permitted figure. The
facts object now also carries the paise integer beside each formatted rupee
string, so the permitted set covers both renderings.

**Lesson.** A guard built from "remove what is allowed, object to the remainder"
gets weaker with every literal you allow. One built from "tokenise, then check
each token" gets stronger.

**Guard.** `test_an_invented_number_is_rejected` uses the 999.99 case that
slipped, and `test_a_digit_inside_an_identifier_is_not_a_stray_number` pins the
reason erasure was there in the first place.

## 13. Gemini 2.5 Flash is not callable on a new key, and thinking truncated the JSON

**Symptom.** Two failures on the first live call. `models/gemini-2.5-flash`
returned `404 ... no longer available to new users`. After switching to the model
the API itself recommended, responses came back truncated mid-string:
`{"explanation": "Refund rfnd_QfW822jZGfgbhJ of ₹566.00 for payment pay_g`.

**Cause.** Two unrelated things. The 2.5 model is closed to new keys — the key
authenticates fine, the model just is not there. And Gemini 3.x reasons before
answering, charging those tokens against `maxOutputTokens`: a request with a
1024-token cap spent 391 on reasoning and ran out mid-JSON.

**Fix.** `gemini-3.6-flash`, with `thinkingConfig.thinkingLevel: "low"` — which
takes the reasoning tokens to zero — and a 2048-token cap. Restating
already-classified facts in three sentences requires no deliberation.
`thinkingBudget: 0`, the 2.5-era control, returns a 400 on this model.

**Also.** The free-tier key returns `429` after roughly a dozen calls, so 35 of
49 explanations fell back on the first full run. That is the designed behaviour
rather than a defect: every record still got an explanation, none were dropped,
and the count of model-written versus template output is printed. Exceptions are
explained in exposure order and `llm.max_model_calls` caps how many reach the
model, so when quota runs out it runs out on the cheapest findings.

**Guard.** `test_quota_exhaustion_still_explains_every_record` and
`test_capping_model_calls_spends_them_on_the_biggest_exposures`. The empty-response
path raises a message naming `maxOutputTokens` rather than failing silently.

---

## 14. The report explained the banned word by using it (step 7)

**Symptom.** `test_the_report_never_uses_banned_vocabulary[orphan]` failed
against `report.md`, twice.

**Cause.** Both occurrences were disclaimers. The data-errors section ended
"Neither is called an orphan", and the AI-judgment section listed the word among
the things the claim guard rejects. Both were *about* the rule rather than
breaking it, which felt like enough. It is not: CLAUDE.md §1 says do not use the
word, without an exception for talking about it, and a judge scanning the
document does not read intent.

**Fix.** The data-errors section now distinguishes `NO_PARENT_PAYMENT` from
`NEVER_DEDUCTED` on their own terms — different causes, different owners — which
makes the point better than naming the term it is avoiding ever did. The AI
section says "this project's banned vocabulary".

**Lesson.** A vocabulary rule that carves out an exception for meta-discussion is
not a vocabulary rule. Worth noting the test caught this and I would not have:
the sentences read as compliant while writing them.

**Guard.** `test_the_report_never_uses_banned_vocabulary`, parameterised over
`4-leg`, `four-leg`, `orphan` and `all four legs`.
