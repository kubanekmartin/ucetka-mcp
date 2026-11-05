from decimal import ROUND_CEILING, Decimal

import pytest

from ucetka.tax import flat_rate_expenses, health_insurance_no_minimum, income_tax, social_insurance_side_activity
from ucetka.tax_params import params_for


def test_flat_rate_side_activity_2025() -> None:
    """Paušál 60 %, vedlejší činnost, penzijní spoření 2 400 Kč; hodnoty spočítané ručně podle ZDP a ČSSZ 2025."""
    p = params_for(2025)
    t = income_tax(Decimal(750_000), flat_rate_expenses(Decimal(750_000), p, 60), p, deductions=Decimal(2_400))
    assert t.expenses == Decimal(450_000)
    assert t.profit == Decimal(300_000)
    assert t.tax_base == Decimal(297_600)
    assert t.tax_before_credit == Decimal(44_640)
    assert t.tax == Decimal(13_800)
    social = social_insurance_side_activity(t.profit, p)
    assert social.assessment_base == Decimal(165_000)
    assert social.amount == Decimal(48_180)


def test_flat_rate_cap_applies_above_two_million() -> None:
    t = income_tax(Decimal(2_500_000), flat_rate_expenses(Decimal(2_500_000), params_for(2026), 60), params_for(2026))
    assert t.expenses == Decimal(1_200_000)


def test_higher_rate_above_threshold() -> None:
    p = params_for(2026)
    t = income_tax(Decimal(5_000_000), flat_rate_expenses(Decimal(5_000_000), p, 60), p)
    over = Decimal(3_800_000) - p.higher_rate_threshold
    expected = (Decimal(3_800_000) - over) * Decimal("0.15") + over * Decimal("0.23")
    assert t.tax_before_credit == expected.quantize(Decimal(1), rounding=ROUND_CEILING)


def test_social_not_due_below_threshold() -> None:
    assert not social_insurance_side_activity(Decimal(100_000), params_for(2026)).applies


def test_health_for_state_insured_student() -> None:
    h = health_insurance_no_minimum(Decimal(300_000), params_for(2026))
    assert h.assessment_base == Decimal(150_000)
    assert h.amount == Decimal(20_250)


def test_unknown_year_raises() -> None:
    with pytest.raises(ValueError, match="2030"):
        params_for(2030)


def test_actual_expenses_and_loss() -> None:
    p = params_for(2026)
    t = income_tax(Decimal(500_000), Decimal(180_000), p)
    assert t.profit == Decimal(320_000)
    assert t.tax == Decimal(320_000) * Decimal("0.15") - p.taxpayer_credit
    assert income_tax(Decimal(100_000), Decimal(150_000), p).tax == 0


def test_flat_rate_cap_scales_with_percent() -> None:
    p = params_for(2026)
    assert flat_rate_expenses(Decimal(2_500_000), p, 80) == Decimal(1_600_000)
    assert flat_rate_expenses(Decimal(2_500_000), p, 40) == Decimal(800_000)


def test_employee_pays_only_marginal_tax_without_second_credit() -> None:
    p = params_for(2026)
    t = income_tax(Decimal(300_000), Decimal(100_000), p, employment_income=Decimal(600_000))
    assert t.profit == Decimal(200_000)
    assert t.tax == Decimal(30_000)
    near_bracket = income_tax(Decimal(400_000), Decimal(0), p, employment_income=Decimal(1_600_000))
    over = Decimal(1_600_000 + 400_000) - p.higher_rate_threshold
    assert near_bracket.tax == ((Decimal(400_000) - over) * Decimal("0.15") + over * Decimal("0.23")).quantize(Decimal(1), rounding=ROUND_CEILING)
