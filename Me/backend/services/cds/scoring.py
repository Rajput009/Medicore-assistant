"""National Early Warning Score 2 (NEWS2).

Implements the Royal College of Physicians NEWS2 aggregate scoring system
rather than an invented formula, so the output is explainable, auditable and
matches an accepted published standard.

Reference: Royal College of Physicians, "National Early Warning Score (NEWS) 2:
Standardising the assessment of acute-illness severity in the NHS" (2017).

Clinical governance note: NEWS2 is a *track-and-trigger* aid for escalation. It
does not diagnose, and it is not validated for children or pregnancy. Local
policy governs the response to each escalation band.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ConsciousnessLevel(StrEnum):
    """ACVPU scale. Anything other than Alert scores 3 points."""

    ALERT = "A"
    CONFUSION = "C"
    VOICE = "V"
    PAIN = "P"
    UNRESPONSIVE = "U"


class RiskBand(StrEnum):
    LOW = "low"
    LOW_MEDIUM = "low-medium"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class ParameterScore:
    name: str
    value: float | str
    score: int
    rationale: str


@dataclass(frozen=True)
class News2Result:
    total: int
    band: RiskBand
    parameters: tuple[ParameterScore, ...]
    # True when any single parameter scores 3, which mandates urgent review
    # even at a low aggregate total.
    red_flag: bool
    recommended_response: str
    monitoring_frequency: str


def _score_respiratory_rate(rr: float) -> ParameterScore:
    if rr <= 8:
        s, why = 3, "<=8/min: severe bradypnoea"
    elif rr <= 11:
        s, why = 1, "9-11/min: below normal"
    elif rr <= 20:
        s, why = 0, "12-20/min: normal"
    elif rr <= 24:
        s, why = 2, "21-24/min: tachypnoea"
    else:
        s, why = 3, ">=25/min: severe tachypnoea"
    return ParameterScore("respiratory_rate", rr, s, why)


def _score_spo2_scale1(spo2: float) -> ParameterScore:
    if spo2 <= 91:
        s, why = 3, "<=91%: severe hypoxaemia"
    elif spo2 <= 93:
        s, why = 2, "92-93%: moderate hypoxaemia"
    elif spo2 <= 95:
        s, why = 1, "94-95%: mild hypoxaemia"
    else:
        s, why = 0, ">=96%: normal"
    return ParameterScore("spo2", spo2, s, why)


def _score_spo2_scale2(spo2: float, on_oxygen: bool) -> ParameterScore:
    """Scale 2 applies to patients with hypercapnic respiratory failure, whose
    target saturation is 88-92%. Using Scale 1 for them wrongly penalises a
    correct saturation."""
    if spo2 <= 83:
        s, why = 3, "<=83%: severe hypoxaemia (Scale 2)"
    elif spo2 <= 85:
        s, why = 2, "84-85% (Scale 2)"
    elif spo2 <= 87:
        s, why = 1, "86-87% (Scale 2)"
    elif spo2 <= 92:
        s, why = 0, "88-92%: target range (Scale 2)"
    elif not on_oxygen:
        s, why = 0, ">=93% on air (Scale 2)"
    elif spo2 <= 94:
        s, why = 1, "93-94% on oxygen (Scale 2)"
    elif spo2 <= 96:
        s, why = 2, "95-96% on oxygen (Scale 2)"
    else:
        s, why = 3, ">=97% on oxygen (Scale 2): over-oxygenation"
    return ParameterScore("spo2", spo2, s, why)


def _score_temperature(temp: float) -> ParameterScore:
    if temp <= 35.0:
        s, why = 3, "<=35.0C: hypothermia"
    elif temp <= 36.0:
        s, why = 1, "35.1-36.0C: below normal"
    elif temp <= 38.0:
        s, why = 0, "36.1-38.0C: normal"
    elif temp <= 39.0:
        s, why = 1, "38.1-39.0C: pyrexia"
    else:
        s, why = 2, ">=39.1C: high pyrexia"
    return ParameterScore("temperature", temp, s, why)


def _score_systolic_bp(sbp: float) -> ParameterScore:
    if sbp <= 90:
        s, why = 3, "<=90 mmHg: hypotension"
    elif sbp <= 100:
        s, why = 2, "91-100 mmHg: low"
    elif sbp <= 110:
        s, why = 1, "101-110 mmHg: borderline"
    elif sbp <= 219:
        s, why = 0, "111-219 mmHg: normal"
    else:
        s, why = 3, ">=220 mmHg: severe hypertension"
    return ParameterScore("systolic_bp", sbp, s, why)


def _score_pulse(hr: float) -> ParameterScore:
    if hr <= 40:
        s, why = 3, "<=40/min: severe bradycardia"
    elif hr <= 50:
        s, why = 1, "41-50/min: bradycardia"
    elif hr <= 90:
        s, why = 0, "51-90/min: normal"
    elif hr <= 110:
        s, why = 1, "91-110/min: mild tachycardia"
    elif hr <= 130:
        s, why = 2, "111-130/min: tachycardia"
    else:
        s, why = 3, ">=131/min: severe tachycardia"
    return ParameterScore("pulse", hr, s, why)


def _score_consciousness(level: ConsciousnessLevel) -> ParameterScore:
    alert = level is ConsciousnessLevel.ALERT
    return ParameterScore(
        "consciousness",
        level.value,
        0 if alert else 3,
        "Alert" if alert else f"New confusion or reduced consciousness ({level.value})",
    )


def _score_oxygen(on_oxygen: bool) -> ParameterScore:
    return ParameterScore(
        "supplemental_oxygen",
        on_oxygen,
        2 if on_oxygen else 0,
        "Receiving supplemental oxygen" if on_oxygen else "Breathing air",
    )


def _band(total: int, red_flag: bool) -> tuple[RiskBand, str, str]:
    """Map an aggregate score to the RCP escalation band."""
    if total >= 7:
        return (
            RiskBand.HIGH,
            "Emergency assessment by a critical-care-competent team; "
            "usually transfer to a higher level of care.",
            "Continuous monitoring",
        )
    if total >= 5 or red_flag:
        return (
            RiskBand.MEDIUM,
            "Urgent review by a clinician competent in acute illness.",
            "At least hourly",
        )
    if total >= 1:
        return (
            RiskBand.LOW_MEDIUM,
            "Assessment by a registered nurse, who decides on escalation.",
            "At least every 4-6 hours",
        )
    return (RiskBand.LOW, "Continue routine monitoring.", "At least every 12 hours")


def calculate_news2(
    *,
    respiratory_rate: float,
    spo2: float,
    temperature: float,
    systolic_bp: float,
    pulse: float,
    consciousness: ConsciousnessLevel = ConsciousnessLevel.ALERT,
    on_supplemental_oxygen: bool = False,
    use_spo2_scale2: bool = False,
) -> News2Result:
    """Compute the NEWS2 aggregate score with a per-parameter breakdown."""
    parameters = (
        _score_respiratory_rate(respiratory_rate),
        (
            _score_spo2_scale2(spo2, on_supplemental_oxygen)
            if use_spo2_scale2
            else _score_spo2_scale1(spo2)
        ),
        _score_oxygen(on_supplemental_oxygen),
        _score_temperature(temperature),
        _score_systolic_bp(systolic_bp),
        _score_pulse(pulse),
        _score_consciousness(consciousness),
    )

    total = sum(p.score for p in parameters)
    # A single parameter scoring 3 triggers escalation regardless of the total.
    red_flag = any(p.score == 3 for p in parameters)
    band, response, frequency = _band(total, red_flag)

    return News2Result(
        total=total,
        band=band,
        parameters=parameters,
        red_flag=red_flag,
        recommended_response=response,
        monitoring_frequency=frequency,
    )


def normalised_score(total: int, max_total: int = 20) -> float:
    """0..1 projection of the aggregate, for progress-bar style display."""
    if max_total <= 0:
        return 0.0
    return min(max(total / max_total, 0.0), 1.0)


def legacy_band(total: int, red_flag: bool) -> str:
    """Three-level label retained for the existing API contract."""
    band, _, _ = _band(total, red_flag)
    return {
        RiskBand.LOW: "low",
        RiskBand.LOW_MEDIUM: "low",
        RiskBand.MEDIUM: "medium",
        RiskBand.HIGH: "high",
    }[band]


__all__ = [
    "ConsciousnessLevel",
    "News2Result",
    "ParameterScore",
    "RiskBand",
    "calculate_news2",
    "legacy_band",
    "normalised_score",
]
