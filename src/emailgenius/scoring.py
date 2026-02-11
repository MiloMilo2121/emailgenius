from __future__ import annotations

from dataclasses import dataclass

from .types import CompanySignals, EligibilityResult


@dataclass(frozen=True, slots=True)
class CreditRule:
    min_facility_pct: float
    min_process_pct: float
    credit_rate: int


DEFAULT_CREDIT_RULES: tuple[CreditRule, ...] = (
    CreditRule(min_facility_pct=10.0, min_process_pct=15.0, credit_rate=45),
    CreditRule(min_facility_pct=6.0, min_process_pct=10.0, credit_rate=40),
    CreditRule(min_facility_pct=3.0, min_process_pct=5.0, credit_rate=35),
)


def _meets_threshold(value: float | None, threshold: float) -> bool:
    return value is not None and value >= threshold


def estimate_credit_rate(
    facility_reduction_pct: float | None,
    process_reduction_pct: float | None,
    rules: tuple[CreditRule, ...] = DEFAULT_CREDIT_RULES,
) -> int | None:
    for rule in rules:
        if _meets_threshold(facility_reduction_pct, rule.min_facility_pct):
            return rule.credit_rate
        if _meets_threshold(process_reduction_pct, rule.min_process_pct):
            return rule.credit_rate
    return None


def _build_trigger(facility_reduction_pct: float | None, process_reduction_pct: float | None) -> str | None:
    if _meets_threshold(facility_reduction_pct, 3.0):
        return f"facility_reduction={facility_reduction_pct:.2f}%"
    if _meets_threshold(process_reduction_pct, 5.0):
        return f"process_reduction={process_reduction_pct:.2f}%"
    return None


def _estimate_confidence(signals: CompanySignals, eligible: bool) -> float:
    confidence = 0.25
    if eligible:
        confidence += 0.25
    if signals.facility_reduction_pct is not None:
        confidence += 0.15
    if signals.process_reduction_pct is not None:
        confidence += 0.15
    if signals.has_esg_report:
        confidence += 0.1
    if signals.has_industry40_signals:
        confidence += 0.1
    if signals.sector_tags:
        confidence += 0.05
    return round(min(confidence, 0.95), 2)


def evaluate_transition50_eligibility(signals: CompanySignals) -> EligibilityResult:
    credit_rate = estimate_credit_rate(
        signals.facility_reduction_pct,
        signals.process_reduction_pct,
    )
    eligible = credit_rate is not None
    trigger = _build_trigger(signals.facility_reduction_pct, signals.process_reduction_pct)

    rationale: list[str] = []
    if signals.facility_reduction_pct is not None:
        rationale.append(f"facility_reduction_pct={signals.facility_reduction_pct:.2f}")
    if signals.process_reduction_pct is not None:
        rationale.append(f"process_reduction_pct={signals.process_reduction_pct:.2f}")
    if signals.has_esg_report:
        rationale.append("esg_report_signals=true")
    if signals.has_industry40_signals:
        rationale.append("industry40_signals=true")
    if signals.sector_tags:
        rationale.append(f"sector_tags={','.join(signals.sector_tags)}")
    if not rationale:
        rationale.append("limited_public_signals")

    return EligibilityResult(
        eligible=eligible,
        estimated_credit_rate=credit_rate,
        trigger=trigger,
        confidence=_estimate_confidence(signals, eligible),
        rationale=rationale,
    )
