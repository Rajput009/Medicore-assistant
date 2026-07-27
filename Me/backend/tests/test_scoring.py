"""NEWS2 scoring.

Thresholds are checked against the published Royal College of Physicians chart.
Getting a boundary wrong here changes escalation decisions, so every band
boundary is asserted explicitly.
"""

from __future__ import annotations

import pytest

from backend.services.cds.scoring import (
    ConsciousnessLevel,
    RiskBand,
    calculate_news2,
    legacy_band,
    normalised_score,
)

NORMAL = {
    "respiratory_rate": 16,
    "spo2": 98,
    "temperature": 37.0,
    "systolic_bp": 120,
    "pulse": 70,
}


def score_with(**overrides) -> int:
    return calculate_news2(**{**NORMAL, **overrides}).total


class TestRespiratoryRate:
    @pytest.mark.parametrize(
        "rr,expected",
        [(8, 3), (9, 1), (11, 1), (12, 0), (20, 0), (21, 2), (24, 2), (25, 3), (40, 3)],
    )
    def test_boundaries(self, rr, expected):
        assert score_with(respiratory_rate=rr) == expected


class TestSpO2Scale1:
    @pytest.mark.parametrize(
        "spo2,expected",
        [(91, 3), (92, 2), (93, 2), (94, 1), (95, 1), (96, 0), (100, 0)],
    )
    def test_boundaries(self, spo2, expected):
        assert score_with(spo2=spo2) == expected


class TestSpO2Scale2:
    """Scale 2 targets 88-92% for hypercapnic respiratory failure."""

    @pytest.mark.parametrize(
        "spo2,on_o2,expected",
        [
            (83, False, 3),
            (85, False, 2),
            (87, False, 1),
            (88, False, 0),
            (92, False, 0),
            (95, False, 0),  # above target on air is not penalised
            (93, True, 1),
            (96, True, 2),
            (98, True, 3),  # over-oxygenation on supplemental O2
        ],
    )
    def test_boundaries(self, spo2, on_o2, expected):
        result = calculate_news2(
            **{**NORMAL, "spo2": spo2},
            on_supplemental_oxygen=on_o2,
            use_spo2_scale2=True,
        )
        spo2_points = next(p.score for p in result.parameters if p.name == "spo2")
        assert spo2_points == expected

    def test_copd_target_saturation_is_not_penalised(self):
        """A COPD patient at 90% is at target; Scale 1 would wrongly score 3."""
        scale2 = calculate_news2(**{**NORMAL, "spo2": 90}, use_spo2_scale2=True)
        scale1 = calculate_news2(**{**NORMAL, "spo2": 90})
        assert scale2.total == 0
        assert scale1.total == 3


class TestTemperature:
    @pytest.mark.parametrize(
        "temp,expected",
        [(35.0, 3), (35.1, 1), (36.0, 1), (36.1, 0), (38.0, 0), (38.1, 1), (39.1, 2)],
    )
    def test_boundaries(self, temp, expected):
        assert score_with(temperature=temp) == expected


class TestSystolicBp:
    @pytest.mark.parametrize(
        "sbp,expected",
        [(90, 3), (91, 2), (100, 2), (101, 1), (110, 1), (111, 0), (219, 0), (220, 3)],
    )
    def test_boundaries(self, sbp, expected):
        assert score_with(systolic_bp=sbp) == expected


class TestPulse:
    @pytest.mark.parametrize(
        "hr,expected",
        [(40, 3), (41, 1), (50, 1), (51, 0), (90, 0), (91, 1), (111, 2), (131, 3)],
    )
    def test_boundaries(self, hr, expected):
        assert score_with(pulse=hr) == expected


class TestConsciousnessAndOxygen:
    def test_alert_scores_zero(self):
        assert score_with(consciousness=ConsciousnessLevel.ALERT) == 0

    @pytest.mark.parametrize(
        "level",
        [
            ConsciousnessLevel.CONFUSION,
            ConsciousnessLevel.VOICE,
            ConsciousnessLevel.PAIN,
            ConsciousnessLevel.UNRESPONSIVE,
        ],
    )
    def test_any_impairment_scores_three(self, level):
        assert score_with(consciousness=level) == 3

    def test_supplemental_oxygen_adds_two(self):
        assert (
            calculate_news2(**NORMAL, on_supplemental_oxygen=True).total == 2
        )


class TestBanding:
    def test_zero_is_low_risk(self):
        r = calculate_news2(**NORMAL)
        assert r.total == 0
        assert r.band is RiskBand.LOW
        assert r.red_flag is False

    def test_one_to_four_is_low_medium(self):
        # RR 21 => 2 points
        assert calculate_news2(**{**NORMAL, "respiratory_rate": 21}).band is RiskBand.LOW_MEDIUM

    def test_five_is_medium(self):
        # RR 21 (2) + SpO2 92 (2) + temp 38.5 (1) = 5
        r = calculate_news2(
            **{**NORMAL, "respiratory_rate": 21, "spo2": 92, "temperature": 38.5}
        )
        assert r.total == 5
        assert r.band is RiskBand.MEDIUM

    def test_seven_or_more_is_high(self):
        r = calculate_news2(
            **{**NORMAL, "respiratory_rate": 25, "spo2": 91, "pulse": 131}
        )
        assert r.total >= 7
        assert r.band is RiskBand.HIGH

    def test_single_parameter_of_three_escalates_despite_low_total(self):
        """The clinically critical rule: one extreme value must trigger review
        even when the aggregate is otherwise reassuring."""
        r = calculate_news2(**{**NORMAL, "pulse": 38})
        assert r.total == 3
        assert r.red_flag is True
        assert r.band is RiskBand.MEDIUM
        assert "urgent" in r.recommended_response.lower()

    def test_low_total_without_extremes_is_not_flagged(self):
        r = calculate_news2(**{**NORMAL, "pulse": 95})
        assert r.total == 1
        assert r.red_flag is False
        assert r.band is RiskBand.LOW_MEDIUM


class TestOutputShape:
    def test_every_parameter_is_explained(self):
        r = calculate_news2(**NORMAL)
        names = {p.name for p in r.parameters}
        assert names == {
            "respiratory_rate",
            "spo2",
            "supplemental_oxygen",
            "temperature",
            "systolic_bp",
            "pulse",
            "consciousness",
        }
        assert all(p.rationale for p in r.parameters)

    def test_total_equals_sum_of_parameters(self):
        r = calculate_news2(
            **{**NORMAL, "respiratory_rate": 22, "spo2": 93, "pulse": 120}
        )
        assert r.total == sum(p.score for p in r.parameters)

    def test_monitoring_frequency_tightens_with_risk(self):
        low = calculate_news2(**NORMAL)
        high = calculate_news2(
            **{**NORMAL, "respiratory_rate": 25, "spo2": 91, "pulse": 131}
        )
        assert low.monitoring_frequency != high.monitoring_frequency
        assert "continuous" in high.monitoring_frequency.lower()


class TestHelpers:
    @pytest.mark.parametrize(
        "total,expected", [(0, "low"), (3, "low"), (5, "medium"), (9, "high")]
    )
    def test_legacy_band(self, total, expected):
        assert legacy_band(total, red_flag=False) == expected

    def test_legacy_band_respects_red_flag(self):
        assert legacy_band(3, red_flag=True) == "medium"

    @pytest.mark.parametrize("total,expected", [(0, 0.0), (10, 0.5), (20, 1.0), (25, 1.0)])
    def test_normalised_score_is_bounded(self, total, expected):
        assert normalised_score(total) == expected

    def test_normalised_score_handles_zero_max(self):
        assert normalised_score(5, max_total=0) == 0.0
