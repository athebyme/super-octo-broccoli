"""Bind exact local decision plans to durable, single-attempt Decisions calls."""

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Optional, Sequence, Tuple

from services.marketplace_decisions_preparation import (
    CategoryProductContext,
    CharacteristicProductContext,
    DecisionItemResult,
    DecisionPlan,
    DecisionPreparationError,
    decode_decision_plan_answers,
    plan_category_phase,
    plan_characteristic_phase,
)
from services.openai_decisions import (
    DecisionAnswer,
    DecisionOutcome,
    OpenAIDecisionsClient,
)
from services.openai_decisions_cache import (
    DecisionBatchFinish,
    DecisionBatchReservation,
    DecisionWorkItem,
    OpenAIDecisionsCache,
)


_QUESTION_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_CACHE_BLOCK_REASONS = {
    "unknown": "unknown_response",
    "busy": "cache_busy",
    "deferred": "cache_deferred",
    "budget_exhausted": "budget_exhausted",
    "cache_full": "cache_full",
}


@dataclass(frozen=True)
class DecisionsPipelineResult:
    """Safe local results for one category or characteristic phase."""

    phase: str
    results: Tuple[DecisionItemResult, ...]
    reservation_state: str
    finish_state: Optional[str] = None
    physical_request_started: bool = False
    retry_at: Optional[datetime] = None
    safe_code: Optional[str] = None


class OpenAIDecisionsPipeline:
    """Plan locally, reserve exact pending questions, call at most once, merge."""

    def __init__(
        self,
        cache: OpenAIDecisionsCache,
        client: OpenAIDecisionsClient,
    ):
        self._cache = cache
        self._client = client

    def run_category_phase(
        self,
        contexts: Sequence[CategoryProductContext],
        *,
        now: Optional[datetime] = None,
    ) -> DecisionsPipelineResult:
        return self.run_plan(plan_category_phase(contexts), now=now)

    def run_characteristic_phase(
        self,
        contexts: Sequence[CharacteristicProductContext],
        *,
        now: Optional[datetime] = None,
    ) -> DecisionsPipelineResult:
        return self.run_plan(plan_characteristic_phase(contexts), now=now)

    def run_plan(
        self,
        plan: DecisionPlan,
        *,
        now: Optional[datetime] = None,
    ) -> DecisionsPipelineResult:
        if type(plan) is not DecisionPlan:
            raise DecisionPreparationError("invalid_decision_plan")
        if plan.phase not in {"category", "characteristic"}:
            raise DecisionPreparationError("invalid_decision_phase")

        questions = tuple(plan.questions)
        fingerprints = tuple(plan.question_fingerprints)
        if (
            len(questions) != len(fingerprints)
            or len(set(fingerprints)) != len(fingerprints)
            or any(
                type(value) is not str
                or _QUESTION_FINGERPRINT.fullmatch(value) is None
                for value in fingerprints
            )
        ):
            raise DecisionPreparationError("invalid_question_fingerprints")
        if not questions:
            results = decode_decision_plan_answers(plan, {})
            return DecisionsPipelineResult(
                phase=plan.phase,
                results=results,
                reservation_state="not_required",
            )

        requested = tuple(
            DecisionWorkItem(fingerprint=fingerprint, question=question)
            for fingerprint, question in zip(fingerprints, questions)
        )
        try:
            reservation = self._cache.reserve_batch(requested, now=now)
        except Exception:
            return self._finish_local_result(
                plan=plan,
                answers_by_fingerprint={},
                missing_reasons={
                    fingerprint: "decision_reservation_unavailable"
                    for fingerprint in fingerprints
                },
                reservation_state="unavailable",
                safe_code="ledger_unavailable",
            )
        if type(reservation) is not DecisionBatchReservation:
            reservation = DecisionBatchReservation(
                "unavailable",
                safe_code="invalid_reservation_result",
            )

        item_by_fingerprint = {
            item.fingerprint: item for item in requested
        }
        answers_by_fingerprint = self._cached_answers(
            reservation,
            item_by_fingerprint,
        )
        missing_reasons = self._blocked_reasons(
            reservation,
            fingerprints,
            answers_by_fingerprint,
        )
        finish: Optional[DecisionBatchFinish] = None
        physical_request_started = False
        safe_code = reservation.safe_code
        retry_at = reservation.retry_at

        if reservation.kind == "ready":
            claimed = self._validated_claimed_items(
                reservation,
                item_by_fingerprint,
            )
            if not claimed:
                outcome = DecisionOutcome(
                    "configuration_error",
                    safe_code="invalid_claimed_questions",
                )
                missing_reasons.update({
                    item.fingerprint: "decision_reservation_invalid"
                    for item in reservation.claimed
                    if item.fingerprint in fingerprints
                })
            else:
                scoped_input = self._input_for_claimed_questions(
                    plan,
                    claimed,
                )
                if scoped_input is None:
                    outcome = DecisionOutcome(
                        "configuration_error",
                        safe_code="invalid_scoped_decision_input",
                    )
                    missing_reasons.update({
                        item.fingerprint: "decision_input_scope_invalid"
                        for item in claimed
                    })
                else:
                    try:
                        physical_lock = self._cache.acquire_physical_call_lock()
                    except Exception:
                        physical_lock = None
                        lock_safe_code = "provider_lock_unavailable"
                    else:
                        lock_safe_code = "provider_call_busy"

                    if physical_lock is None:
                        outcome = DecisionOutcome(
                            "configuration_error",
                            safe_code=lock_safe_code,
                        )
                    else:
                        try:
                            try:
                                outcome = self._client.decide(
                                    input_json=scoped_input,
                                    questions=tuple(item.question for item in claimed),
                                )
                            except Exception:
                                # A client exception may follow a physical request.
                                # Finalize terminally; never replay it automatically.
                                outcome = DecisionOutcome(
                                    "unknown_response",
                                    safe_code="transport_unknown",
                                )
                        finally:
                            physical_lock.release()
                    physical_request_started = (
                        type(outcome) is not DecisionOutcome
                        or outcome.kind != "configuration_error"
                    )

            try:
                finish = self._cache.finish_batch(
                    reservation,
                    outcome,
                    now=now,
                )
            except Exception:
                finish = DecisionBatchFinish(
                    "unavailable",
                    safe_code="ledger_finalize_unavailable",
                )
            if type(finish) is not DecisionBatchFinish:
                finish = DecisionBatchFinish(
                    "unavailable",
                    safe_code="invalid_finish_result",
                )
            safe_code = (
                outcome.safe_code
                if finish.kind == "released" and type(outcome) is DecisionOutcome
                else finish.safe_code or safe_code
            )
            retry_at = finish.retry_at or retry_at

            if (
                finish.kind in {"success", "partial_refusal", "refused"}
                and type(outcome) is DecisionOutcome
                and outcome.kind == finish.kind
                and len(outcome.answers) == len(claimed)
            ):
                for item, answer in zip(claimed, outcome.answers):
                    if type(answer) is DecisionAnswer:
                        answers_by_fingerprint[item.fingerprint] = (
                            self._rebind_answer(answer, item.question.name)
                        )
            else:
                finish_reason = self._finish_reason(finish, outcome)
                missing_reasons.update({
                    item.fingerprint: finish_reason
                    for item in claimed
                })

        missing_reasons = {
            fingerprint: reason
            for fingerprint, reason in missing_reasons.items()
            if fingerprint not in answers_by_fingerprint
        }
        for fingerprint in fingerprints:
            if fingerprint not in answers_by_fingerprint:
                missing_reasons.setdefault(
                    fingerprint,
                    "provider_outcome_unavailable",
                )

        results = decode_decision_plan_answers(
            plan,
            answers_by_fingerprint,
            missing_reasons=missing_reasons,
        )
        return DecisionsPipelineResult(
            phase=plan.phase,
            results=tuple(results),
            reservation_state=reservation.kind,
            finish_state=finish.kind if finish is not None else None,
            physical_request_started=physical_request_started,
            retry_at=retry_at,
            safe_code=safe_code,
        )

    @staticmethod
    def _rebind_answer(
        answer: DecisionAnswer,
        question_name: str,
    ) -> DecisionAnswer:
        return DecisionAnswer(
            name=question_name,
            status=answer.status,
            choice=answer.choice,
            confidence=answer.confidence,
        )

    @classmethod
    def _cached_answers(
        cls,
        reservation: DecisionBatchReservation,
        item_by_fingerprint: dict,
    ) -> dict:
        result = {}
        for fingerprint, answer in reservation.cached_answers:
            item = item_by_fingerprint.get(fingerprint)
            if (
                item is None
                or type(answer) is not DecisionAnswer
                or answer.name != item.question.name
            ):
                continue
            result[fingerprint] = cls._rebind_answer(
                answer,
                item.question.name,
            )
        return result

    @staticmethod
    def _blocked_reasons(
        reservation: DecisionBatchReservation,
        fingerprints: Tuple[str, ...],
        answers_by_fingerprint: dict,
    ) -> dict:
        result = {}
        for blocked in reservation.blocked:
            if blocked.fingerprint in fingerprints:
                result[blocked.fingerprint] = _CACHE_BLOCK_REASONS.get(
                    blocked.state,
                    "decision_reservation_blocked",
                )
        for fingerprint in fingerprints:
            if fingerprint not in answers_by_fingerprint:
                result.setdefault(
                    fingerprint,
                    _CACHE_BLOCK_REASONS.get(
                        reservation.kind,
                        "cache_unavailable",
                    ),
                )
        return result

    @staticmethod
    def _validated_claimed_items(
        reservation: DecisionBatchReservation,
        item_by_fingerprint: dict,
    ) -> Tuple[DecisionWorkItem, ...]:
        if not reservation.claimed:
            return ()
        result = []
        seen = set()
        for claimed in reservation.claimed:
            expected = item_by_fingerprint.get(
                getattr(claimed, "fingerprint", None)
            )
            if (
                type(claimed) is not DecisionWorkItem
                or expected is None
                or claimed.fingerprint in seen
                or claimed.question != expected.question
            ):
                return ()
            seen.add(claimed.fingerprint)
            result.append(expected)
        return tuple(result)

    @staticmethod
    def _input_for_claimed_questions(
        plan: DecisionPlan,
        claimed: Tuple[DecisionWorkItem, ...],
    ) -> Optional[dict]:
        try:
            return plan.input_json_for(tuple(
                item.fingerprint for item in claimed
            ))
        except Exception:
            return None

    @staticmethod
    def _finish_reason(
        finish: DecisionBatchFinish,
        outcome: DecisionOutcome,
    ) -> str:
        if finish.kind == "unknown":
            return "unknown_response"
        if finish.kind == "deferred":
            return "rate_limited"
        if finish.kind == "released":
            if type(outcome) is DecisionOutcome:
                if outcome.safe_code == "provider_call_busy":
                    return "cache_busy"
                if outcome.safe_code == "provider_lock_unavailable":
                    return "cache_unavailable"
            return "configuration_error"
        if type(outcome) is DecisionOutcome and outcome.kind == "rate_limited":
            return "unknown_response"
        if finish.kind == "stale":
            return "response_mismatch"
        if finish.kind in {"busy", "unavailable"}:
            return "cache_unavailable"
        if finish.kind == "invalid":
            return "invalid_response"
        return "provider_outcome_unavailable"

    @staticmethod
    def _finish_local_result(
        *,
        plan: DecisionPlan,
        answers_by_fingerprint: dict,
        missing_reasons: dict,
        reservation_state: str,
        safe_code: Optional[str] = None,
    ) -> DecisionsPipelineResult:
        results = decode_decision_plan_answers(
            plan,
            answers_by_fingerprint,
            missing_reasons=missing_reasons,
        )
        return DecisionsPipelineResult(
            phase=plan.phase,
            results=tuple(results),
            reservation_state=reservation_state,
            safe_code=safe_code,
        )


__all__ = ["DecisionsPipelineResult", "OpenAIDecisionsPipeline"]
