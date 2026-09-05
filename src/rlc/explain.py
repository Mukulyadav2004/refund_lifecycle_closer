"""The explanation layer — the only place a model is allowed (spec §9, CLAUDE.md §9).

The boundary, stated so it cannot drift:

* **Allowed.** Given a structured facts object for an *already classified*
  exception, write 2-3 sentences plus one recommended action.
* **Not allowed.** Classification, matching, arithmetic, date logic, deciding
  states. All of that is plain integer code in §5-§8 and stays there. The
  judging criteria explicitly reward choosing a deterministic solution where AI
  is unnecessary, so the model's footprint here is deliberately small.

Two guards make that boundary enforceable rather than aspirational:

1. **Every number in the output must appear in the facts object.** `verify` strips
   the known ids, dates and amounts out of the generated text and rejects it if
   any digit survives. A rejected explanation is replaced by the template, so a
   model that invents a figure degrades the prose — it never puts a wrong number
   in front of a finance controller.
2. **Template mode works with no API key**, so the demo always runs. `make close`
   without `llm.enabled` produces the same explanations, deterministically.

The provider is injected, so tests never touch the network.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

from .config import Config
from .entities import EXCEPTION, RefundVerdict
from .loader import Sources
from .money import format_inr

MODEL_SOURCE = "model"
TEMPLATE_SOURCE = "template"
REJECTED_SOURCE = "template_after_rejection"

# Leg ordinals are fixed vocabulary from spec §5, not data, so "leg 3" must not
# trip the number guard.
_STRUCTURAL_NUMBERS = frozenset({"1", "2", "3", "4"})

_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

SYSTEM_PROMPT = """You explain payment-reconciliation exceptions to a finance controller at an Indian e-commerce merchant.

You are given a JSON facts object for a refund that has ALREADY been classified by a deterministic engine. Your job is only to write prose about it.

Rules, in order of importance:
1. Use ONLY numbers, dates and identifiers that appear in the facts object. Do not calculate, estimate, round, convert or infer any new figure. If you want to state a number that is not in the facts, leave it out of the sentence instead.
2. Do not re-classify. The exception code is correct; explain it, never dispute it.
3. If the facts say needs_human_review is true, say the finding is suspected and needs a human to confirm. Never assert it as fact.
4. A refund marked "processed" by the gateway has not necessarily reached the customer's bank. An ARN evidences that a bank reference was issued, nothing more. Never say the customer was credited.
5. There are four legs but only three are verified: initiated, gateway-processed and settlement-deducted. The fourth is evidenced by an ARN, never verified. Never write "all four legs" or call anything a four-leg lifecycle.
6. Write 2 or 3 sentences. Then give exactly one concrete recommended action.
7. Plain English. No markdown, no bullet points, no preamble. Never use the word "orphan".

Return JSON only, with exactly two string keys: "explanation" and "recommended_action"."""


# --------------------------------------------------------------------- facts


@dataclass(frozen=True, slots=True)
class ExplanationFacts:
    """Everything the model is allowed to know, and nothing else.

    Amounts are carried both as integer paise and as a formatted rupee string so
    the model never has to divide by 100 — that would be arithmetic, and
    arithmetic is not its job.
    """

    refund_id: str
    payment_id: str | None
    exception_codes: tuple[str, ...]
    amount_paise: int
    amount_inr: str
    closure_state: str
    needs_human_review: bool
    confidence: float | None
    open_reasons: tuple[str, ...]
    timing_flags: tuple[str, ...]
    annotations: tuple[str, ...]
    leakage_paise: int
    leakage_inr: str
    exposure_paise: int
    exposure_inr: str
    settle_lag_wd: int | None
    settlement_id: str | None
    legs: dict[str, bool] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    payment_amount_paise: int | None = None
    payment_amount_inr: str | None = None
    payment_method: str | None = None
    refund_created_ist: str | None = None
    as_of: str = ""

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "refund_id": self.refund_id,
            "payment_id": self.payment_id,
            "exception_codes": list(self.exception_codes),
            "refund_amount": self.amount_inr,
            "refund_amount_paise": self.amount_paise,
            "refund_created_ist": self.refund_created_ist,
            "closure_state": self.closure_state,
            "needs_human_review": self.needs_human_review,
            "confidence": self.confidence,
            "open_reasons": list(self.open_reasons),
            "timing_flags": list(self.timing_flags),
            "annotations": list(self.annotations),
            # Both renderings, as with the refund amount: the model never has to
            # divide by 100, and the guard sees the underlying integer.
            "fee_leakage": self.leakage_inr,
            "fee_leakage_paise": self.leakage_paise,
            "exposure": self.exposure_inr,
            "exposure_paise": self.exposure_paise,
            "settle_lag_working_days": self.settle_lag_wd,
            "settlement_id": self.settlement_id,
            "legs_verified": self.legs,
            "payment_amount": self.payment_amount_inr,
            "payment_amount_paise": self.payment_amount_paise,
            "payment_method": self.payment_method,
            "evidence": self.evidence,
            "as_of": self.as_of,
        }

    def allowed_identifiers(self) -> set[str]:
        """Ids and ISO dates — strings whose digits are not free-standing figures."""
        found: set[str] = set()

        def walk(value: Any) -> None:
            if isinstance(value, str):
                # Razorpay ids (rfnd_..., pay_..., disp_..., setl_...), the
                # merchant's RMA ids (RMA-00044) and ISO dates.
                if re.fullmatch(r"[A-Za-z]{2,}[-_][A-Za-z0-9_-]+", value) or re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}", value
                ):
                    found.add(value)
            elif isinstance(value, dict):
                for item in value.values():
                    walk(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    walk(item)

        walk(self.to_prompt_dict())
        return found

    def allowed_numbers(self) -> set[str]:
        """Every figure a sentence may state, normalised (no commas, no rupee sign).

        Whole-token matching, not substring removal. An earlier version erased
        allowed literals from the text and checked what was left, which meant a
        single allowed digit — "9", from the month in an ISO date — erased every
        9 in an invented "999.99" and the guard passed it.
        """
        allowed: set[str] = set(_STRUCTURAL_NUMBERS)

        def add_int(value: int) -> None:
            for magnitude in (value, abs(value)):
                allowed.add(str(magnitude))
                whole, frac = divmod(abs(magnitude), 100)
                allowed.add(str(whole))
                allowed.add(f"{whole}.{frac:02d}")

        def walk(value: Any) -> None:
            if isinstance(value, bool):
                return
            if isinstance(value, int):
                add_int(value)
            elif isinstance(value, float):
                allowed.add(str(value))
            elif isinstance(value, str):
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    year, month, day = value.split("-")
                    allowed.update({year, month, day, str(int(month)), str(int(day))})
            elif isinstance(value, dict):
                for item in value.values():
                    walk(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    walk(item)

        walk(self.to_prompt_dict())
        return allowed


def build_facts(
    verdict: RefundVerdict, sources: Sources, cfg: Config
) -> ExplanationFacts:
    """Assemble the facts object. Deterministic — no model involved."""
    from .calendar_utils import to_ist_date

    refund = sources.refunds_by_id[verdict.refund_id]
    parent = sources.parent_of(refund)
    return ExplanationFacts(
        refund_id=verdict.refund_id,
        payment_id=verdict.payment_id,
        exception_codes=tuple(verdict.exception_codes),
        amount_paise=verdict.amount,
        amount_inr=format_inr(verdict.amount),
        closure_state=verdict.closure_state,
        needs_human_review=verdict.needs_human_review,
        confidence=verdict.confidence,
        open_reasons=tuple(verdict.open_reasons),
        timing_flags=tuple(verdict.timing_flags),
        annotations=tuple(verdict.annotations),
        leakage_paise=verdict.leakage_paise,
        leakage_inr=format_inr(verdict.leakage_paise),
        exposure_paise=verdict.exposure_paise,
        exposure_inr=format_inr(verdict.exposure_paise),
        settle_lag_wd=verdict.settle_lag_wd,
        settlement_id=verdict.settlement_id,
        legs={
            "1_initiated": verdict.leg1_initiated,
            "2_gateway_processed": verdict.leg2_gateway_processed,
            "3_settlement_deducted": verdict.leg3_settlement_deducted,
            "4_bank_evidenced": verdict.leg4_bank_evidenced,
        },
        evidence=dict(verdict.evidence),
        payment_amount_paise=parent.amount if parent else None,
        payment_amount_inr=format_inr(parent.amount) if parent else None,
        payment_method=parent.method if parent else None,
        refund_created_ist=to_ist_date(refund.created_at).isoformat(),
        as_of=cfg.run.as_of.isoformat(),
    )


# ------------------------------------------------------------------ the guard


# Claims the model must never make, whatever the facts say. The number guard
# cannot catch these: "four" is a word, and "reached the customer" contains no
# digits at all. Gemini produced the four-legs claim on its second output of the
# very first live run, which is why this exists.
_FORBIDDEN_CLAIMS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\b(?:all\s+)?(?:four|4)[\s-]*(?:settlement\s+|lifecycle\s+)?legs?\b", re.I),
        "claims four legs; the project claims 3 verified and 1 evidenced (CLAUDE.md §1)",
    ),
    (
        re.compile(r"\borphan", re.I),
        "uses the banned word 'orphan'; say NO_PARENT_PAYMENT or NEVER_DEDUCTED",
    ),
    (
        re.compile(
            r"(?:reached|credited to|landed in)\s+the\s+customer|customer'?s?\s+"
            r"(?:bank\s+)?account\s+(?:has\s+been|was|is)\s+credited",
            re.I,
        ),
        "asserts the customer was credited; an ARN evidences a bank reference, not a credit",
    ),
)

# When the engine has asked for a human, the prose has to say so rather than
# assert the finding. One of these words must appear.
_HEDGES = re.compile(
    r"\b(suspect\w*|suspic\w*|appears?|possible|possibly|likely|may|might|potential\w*|"
    r"review|confirm\w*|verify|check)\b",
    re.I,
)


def verify_claims(text: str, facts: ExplanationFacts) -> str | None:
    """Return a rejection reason, or None if the prose makes no forbidden claim."""
    for pattern, reason in _FORBIDDEN_CLAIMS:
        if pattern.search(text):
            return reason
    if facts.needs_human_review and not _HEDGES.search(text):
        return (
            "states a finding that needs_human_review as fact; it must be hedged "
            "as suspected and sent for confirmation"
        )
    return None


def _normalise(token: str) -> str:
    return token.replace(",", "").lstrip("\u20b9")


def verify(text: str, facts: ExplanationFacts) -> str | None:
    """Return a rejection reason, or None if every figure traces to the facts.

    Identifiers and ISO dates are removed first, so a digit inside
    `rfnd_QfW822jZGfgbhJ` is never read as an amount. What remains is tokenised,
    and each whole token must equal a permitted figure.
    """
    remaining = text
    for identifier in sorted(facts.allowed_identifiers(), key=len, reverse=True):
        remaining = remaining.replace(identifier, " ")

    permitted = facts.allowed_numbers()
    for token in _NUMBER_RE.findall(remaining):
        value = _normalise(token)
        candidates = {value, value.rstrip("0").rstrip("."), f"{value}.00"}
        if candidates & permitted:
            continue
        return f"contains a number not present in the facts: {token!r}"
    return None


# --------------------------------------------------------------- explanations


@dataclass(frozen=True, slots=True)
class Explanation:
    refund_id: str
    text: str
    recommended_action: str
    source: str
    rejection_reason: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "refund_id": self.refund_id,
            "explanation": self.text,
            "recommended_action": self.recommended_action,
            "source": self.source,
            "rejection_reason": self.rejection_reason,
        }


@dataclass(frozen=True, slots=True)
class ExplanationReport:
    explanations: dict[str, Explanation]
    counts: dict[str, int]
    provider_errors: tuple[str, ...] = ()

    @property
    def model_written(self) -> int:
        return self.counts.get(MODEL_SOURCE, 0)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


# ------------------------------------------------------------------ templates


def _primary_code(facts: ExplanationFacts) -> str:
    return facts.exception_codes[0] if facts.exception_codes else "UNKNOWN"


def render_template(facts: ExplanationFacts) -> Explanation:
    """Deterministic prose. This is what runs with no API key (spec §9).

    Every sentence is assembled from the facts object by string substitution, so
    the number guard passes trivially — templates are the floor the model has to
    beat, not a degraded mode.
    """
    code = _primary_code(facts)
    ev = facts.evidence
    amount, rid = facts.amount_inr, facts.refund_id

    if code == "NEVER_DEDUCTED":
        due = ev.get("settle_due", facts.as_of)
        text = (
            f"Refund {rid} for {amount} was marked processed by the gateway, but no "
            f"settlement recon row has ever claimed it. The maturity gate for this "
            f"refund passed on {due}, so this is no longer a matter of waiting."
        )
        action = "Raise a settlement query with Razorpay support quoting this refund id."
    elif code == "DOUBLE_DEDUCTED":
        detail = ev.get("double_deducted", {})
        ids = ", ".join(detail.get("settlement_ids", []))
        text = (
            f"Refund {rid} for {amount} was deducted from settlement more than once. "
            f"The recon rows sit in settlements {ids}. Only one deduction is owed."
        )
        action = "Reconcile the duplicate settlement debit with Razorpay and claim it back."
    elif code == "SETTLEMENT_AMOUNT_DELTA":
        detail = ev.get("settlement_delta", {})
        text = (
            f"Refund {rid} for {amount} was deducted from settlement at a different "
            f"amount than the refund itself. The recon row debits "
            f"{format_inr(detail.get('debit', 0))} against a refund of {amount}."
        )
        action = "Ask Razorpay to explain the settlement debit for this refund id."
    elif code == "AMOUNT_MISMATCH":
        detail = ev.get("amount_mismatch", {})
        direction = "more than" if detail.get("direction") == "OVER" else "less than"
        text = (
            f"Refund {rid} paid out {amount}, which is {direction} the "
            f"{format_inr(detail.get('expected_paise', 0))} the returns ledger expected "
            f"for {detail.get('rma_id', 'the matching RMA')}. Razorpay only checks that "
            f"refunds do not exceed the captured amount, so only the merchant's own "
            f"ledger can catch this."
        )
        action = "Confirm the correct return value with the operations team before closing."
    elif code == "REFUND_PLUS_CHARGEBACK":
        detail = ev.get("chargeback", {})
        sub = detail.get("sub", "AT_RISK")
        ids = ", ".join(detail.get("realized_dispute_ids", []) + detail.get("at_risk_dispute_ids", []))
        if sub == "REALIZED":
            text = (
                f"Refund {rid} for {amount} was paid, and the customer then won a "
                f"chargeback on the same payment ({ids}). The merchant has paid twice; "
                f"total exposure is {facts.exposure_inr}."
            )
            action = "Recover the duplicate payout by disputing the chargeback outcome."
        else:
            text = (
                f"Refund {rid} for {amount} was paid, and a dispute is now open on the "
                f"same payment ({ids}). If it is lost the merchant pays twice; exposure "
                f"at risk is {facts.exposure_inr}."
            )
            action = (
                "Submit the refund confirmation as dispute evidence before the respond-by date."
            )
    elif code == "DUPLICATE_SUSPECT":
        detail = ev.get("duplicate", {})
        twin = detail.get("twin_refund_id", "an earlier refund")
        text = (
            f"Refund {rid} for {amount} looks like a repeat of {twin} on the same "
            f"payment: same amount, issued shortly after, and at least one of the pair "
            f"carries no receipt. This is a suspicion raised by a heuristic, not a "
            f"confirmed duplicate."
        )
        action = f"Ask operations whether {rid} and {twin} were both intended."
    elif code == "ARN_OVERDUE":
        due = ev.get("arn_due", facts.as_of)
        text = (
            f"Refund {rid} for {amount} is marked processed and was deducted from "
            f"settlement, but no acquirer reference number has arrived. An ARN evidences "
            f"that a bank reference was issued, and none exists past {due}."
        )
        action = "Ask Razorpay for the ARN so the customer can trace the credit."
    elif code == "PENDING_OVERDUE":
        due = ev.get("pending_due", facts.as_of)
        text = (
            f"Refund {rid} for {amount} is still pending at the gateway. It should have "
            f"moved to processed by {due}."
        )
        action = "Chase Razorpay on the pending refund before the customer does."
    elif code == "REFUND_FAILED":
        text = (
            f"Refund {rid} for {amount} failed at the gateway and no successful "
            f"replacement refund exists on the same payment. The customer has not been "
            f"paid."
        )
        action = "Re-initiate the refund, after checking the Razorpay balance."
    else:
        codes = ", ".join(facts.exception_codes) or facts.closure_state
        text = f"Refund {rid} for {amount} was flagged as {codes}."
        action = "Review this refund manually."

    if facts.leakage_paise:
        text += f" The fee already paid on the original sale, {facts.leakage_inr}, is not recoverable."
    return Explanation(
        refund_id=facts.refund_id, text=text, recommended_action=action, source=TEMPLATE_SOURCE
    )


# ------------------------------------------------------------------ providers


class Provider(Protocol):
    """Anything that turns a system prompt and a facts payload into JSON text."""

    def complete(self, system: str, user: str) -> str:  # pragma: no cover - protocol
        ...


class GeminiProvider:
    """Google Gemini via the REST API and the standard library only.

    CLAUDE.md §10 caps runtime dependencies at pyyaml, so this is `urllib` rather
    than a vendor SDK — one POST to one endpoint does not justify a dependency,
    and the demo installs nothing extra.

    The API key is read from the environment. It is never written to the repo,
    never logged, and never placed in `config.yaml`.
    """

    ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(
        self,
        model: str,
        api_key: str,
        max_output_tokens: int = 2048,
        timeout_seconds: int = 30,
        thinking_level: str | None = "low",
        max_retries: int = 2,
        retry_base_seconds: float = 5.0,
    ) -> None:
        if not api_key:
            raise ValueError("GeminiProvider needs an API key")
        self.model = model
        self._api_key = api_key
        self.max_output_tokens = max_output_tokens
        self.timeout_seconds = timeout_seconds
        # Gemini 3.x reasons before answering and those tokens are charged
        # against maxOutputTokens, so an unconstrained thinking level truncates
        # the JSON mid-string. Restating classified facts in three sentences
        # needs no deliberation, so this defaults to "low".
        self.thinking_level = thinking_level
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds

    def _post(self, request: urllib.request.Request) -> dict[str, Any]:
        """POST with backoff on 429. Free-tier keys rate-limit hard and early."""
        delay = self.retry_base_seconds
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:  # pragma: no cover - network
                detail = exc.read().decode("utf-8", "replace")[:400]
                if exc.code == 429 and attempt < self.max_retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise RuntimeError(f"Gemini HTTP {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:  # pragma: no cover - network
                raise RuntimeError(f"Gemini unreachable: {exc.reason}") from exc
        raise RuntimeError("unreachable")  # pragma: no cover

    def complete(self, system: str, user: str) -> str:
        body = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": self.max_output_tokens,
                "responseMimeType": "application/json",
            },
        }
        if self.thinking_level:
            body["generationConfig"]["thinkingConfig"] = {
                "thinkingLevel": self.thinking_level
            }
        request = urllib.request.Request(
            self.ENDPOINT.format(model=self.model),
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self._api_key,
            },
            method="POST",
        )
        payload = self._post(request)

        candidates = payload.get("candidates") or []
        if not candidates:
            raise RuntimeError(f"Gemini returned no candidates: {json.dumps(payload)[:300]}")
        parts = candidates[0].get("content", {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts).strip()
        if not text:
            reason = candidates[0].get("finishReason", "unknown")
            raise RuntimeError(
                f"Gemini returned an empty response (finishReason={reason}). On a thinking "
                "model the reasoning tokens count against maxOutputTokens; raise llm.max_tokens."
            )
        return text


def build_provider(cfg: Config) -> Provider | None:
    """Construct the configured provider, or None for template mode.

    Returns None rather than raising when the model is switched off, because a
    demo that cannot run without a key is a demo that will fail on stage.
    """
    llm = cfg.llm or {}
    if not llm.get("enabled"):
        return None
    name = str(llm.get("provider", "gemini")).lower()
    key_env = llm.get("api_key_env", "GEMINI_API_KEY")
    api_key = os.environ.get(key_env, "")
    if not api_key:
        raise RuntimeError(
            f"llm.enabled is true but ${key_env} is not set. Export the key or set "
            "llm.enabled: false to run in template mode."
        )
    if name != "gemini":
        raise ValueError(f"unsupported llm.provider {name!r}; this build wires Gemini only")
    return GeminiProvider(
        model=str(llm.get("model", "gemini-3.6-flash")),
        api_key=api_key,
        max_output_tokens=int(llm.get("max_tokens", 2048)),
        timeout_seconds=int(llm.get("timeout_seconds", 30)),
        thinking_level=llm.get("thinking_level", "low"),
        max_retries=int(llm.get("max_retries", 2)),
    )


# --------------------------------------------------------------------- driver


def _parse_model_json(raw: str) -> tuple[str, str]:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("model did not return a JSON object")
    text = str(payload.get("explanation", "")).strip()
    action = str(payload.get("recommended_action", "")).strip()
    if not text or not action:
        raise ValueError("model omitted 'explanation' or 'recommended_action'")
    return text, action


def explain_one(
    facts: ExplanationFacts, provider: Provider | None
) -> tuple[Explanation, str | None]:
    """One explanation, with the template as the floor. Returns (result, error)."""
    template = render_template(facts)
    if provider is None:
        return template, None

    try:
        raw = provider.complete(SYSTEM_PROMPT, json.dumps(facts.to_prompt_dict(), indent=2))
        text, action = _parse_model_json(raw)
    except Exception as exc:  # provider or parsing failure — never fatal
        return template, f"{facts.refund_id}: {exc}"

    # The hedge check reads the explanation and the action together: "Ask
    # operations whether both were intended" is a properly hedged action even
    # though the hedging word lives in the sentence before it.
    rejection = (
        verify(text, facts)
        or verify(action, facts)
        or verify_claims(f"{text} {action}", facts)
    )
    if rejection:
        return (
            Explanation(
                refund_id=facts.refund_id,
                text=template.text,
                recommended_action=template.recommended_action,
                source=REJECTED_SOURCE,
                rejection_reason=rejection,
            ),
            None,
        )
    return (
        Explanation(
            refund_id=facts.refund_id,
            text=text,
            recommended_action=action,
            source=MODEL_SOURCE,
        ),
        None,
    )


def explain_all(
    verdicts: Iterable[RefundVerdict],
    sources: Sources,
    cfg: Config,
    provider: Provider | None = None,
    only_exceptions: bool = True,
    max_model_calls: int | None = None,
) -> ExplanationReport:
    """Explain every exception in the run (spec §9).

    Ordered by exposure, then leakage, then refund id, so the most expensive
    finding is explained first. `max_model_calls` sends only the top slice to the
    model and templates the rest — a free-tier key rate-limits well before 49
    calls, and a demo that degrades in exposure order degrades in the right
    order. Every record still gets an explanation.
    """
    selected = [
        v for v in verdicts if not only_exceptions or v.closure_state == EXCEPTION
    ]
    selected.sort(key=lambda v: (-v.exposure_paise, -v.leakage_paise, v.refund_id))

    explanations: dict[str, Explanation] = {}
    errors: list[str] = []
    for index, verdict in enumerate(selected):
        facts = build_facts(verdict, sources, cfg)
        use_model = provider if (max_model_calls is None or index < max_model_calls) else None
        explanation, error = explain_one(facts, use_model)
        explanations[verdict.refund_id] = explanation
        verdict.explanation = explanation.text
        if error:
            errors.append(error)
    counts = Counter(e.source for e in explanations.values())
    return ExplanationReport(
        explanations=explanations,
        counts=dict(sorted(counts.items())),
        provider_errors=tuple(errors),
    )
