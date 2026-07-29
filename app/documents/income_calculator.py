"""
Qualifying-income calculation engine.

Implements common agency-style (Fannie Mae / Freddie Mac) qualifying-income
methodology at a general, publicly-documented level. This is NOT a
substitute for AUS (DU/LP) findings or underwriter sign-off -- every result
carries `flagged_for_underwriter` so a human always reviews before a
commitment is issued. Rules here are intentionally conservative and
simplified; wire in your investor/agency matrices for production use.
"""
from dataclasses import dataclass, field
from statistics import mean


@dataclass
class IncomeResult:
    income_type: str
    monthly_qualifying_income: float
    calculation_method: str
    inputs: dict
    flagged_for_underwriter: bool = False
    notes: list[str] = field(default_factory=list)


def calc_base_w2_income(hourly_rate: float | None, annual_salary: float | None,
                         hours_per_week: float = 40) -> IncomeResult:
    """Straight base salary or hourly base, no variable income."""
    if annual_salary:
        monthly = annual_salary / 12
        return IncomeResult(
            income_type="base_salary",
            monthly_qualifying_income=round(monthly, 2),
            calculation_method="annual_salary / 12",
            inputs={"annual_salary": annual_salary},
        )
    if hourly_rate:
        monthly = hourly_rate * hours_per_week * 52 / 12
        return IncomeResult(
            income_type="base_hourly",
            monthly_qualifying_income=round(monthly, 2),
            calculation_method="hourly_rate * hours_per_week * 52 / 12",
            inputs={"hourly_rate": hourly_rate, "hours_per_week": hours_per_week},
        )
    raise ValueError("Must supply either annual_salary or hourly_rate")


def calc_variable_income(yearly_amounts: list[float], income_type: str = "bonus") -> IncomeResult:
    """
    Overtime / bonus / commission: generally averaged over the most recent
    2 years, and flagged if trending downward (agency guidance requires
    stable-or-increasing history, or documented reason otherwise).
    """
    if len(yearly_amounts) < 1:
        raise ValueError("Need at least one year of variable income history")

    avg_annual = mean(yearly_amounts)
    monthly = avg_annual / 12

    declining = len(yearly_amounts) >= 2 and yearly_amounts[-1] < yearly_amounts[-2] * 0.8
    single_year_only = len(yearly_amounts) == 1

    notes = []
    flagged = False
    if declining:
        notes.append(
            "Most recent year is >20% below prior year -- declining variable "
            "income trend. Must be evaluated at the lower/most-recent level "
            "or excluded per agency guidance."
        )
        flagged = True
        monthly = yearly_amounts[-1] / 12  # conservative: use most recent year only
    if single_year_only:
        notes.append("Only one year of history available -- underwriter must confirm eligibility to use.")
        flagged = True

    return IncomeResult(
        income_type=income_type,
        monthly_qualifying_income=round(monthly, 2),
        calculation_method="2yr_average (or most-recent-year if declining >20%)",
        inputs={"yearly_amounts": yearly_amounts},
        flagged_for_underwriter=flagged,
        notes=notes,
    )


def calc_self_employed_income(
    net_profit_by_year: list[float],
    depreciation_addback_by_year: list[float] | None = None,
    business_use_pct: float = 1.0,
) -> IncomeResult:
    """
    Self-employed / 1099 qualifying income: 2-year average of net profit
    (Schedule C/K-1) plus non-cash add-backs (e.g. depreciation), pro-rated
    by ownership percentage. Requires signed 4506-C / tax transcripts to
    validate in production -- this function only does the arithmetic.
    """
    depreciation_addback_by_year = depreciation_addback_by_year or [0.0] * len(net_profit_by_year)
    if len(net_profit_by_year) != len(depreciation_addback_by_year):
        raise ValueError("net_profit_by_year and depreciation_addback_by_year must align")

    adjusted_years = [
        (np + da) * business_use_pct
        for np, da in zip(net_profit_by_year, depreciation_addback_by_year)
    ]
    avg_annual = mean(adjusted_years)
    monthly = avg_annual / 12

    notes = [
        "Self-employed income requires 2 years of signed personal (and "
        "business, if applicable) tax returns and typically 4506-C "
        "transcript validation before this figure can be relied upon."
    ]
    declining = len(adjusted_years) >= 2 and adjusted_years[-1] < adjusted_years[-2] * 0.8
    if declining:
        notes.append("Declining year-over-year trend -- underwriter must evaluate business stability.")

    return IncomeResult(
        income_type="self_employed",
        monthly_qualifying_income=round(monthly, 2),
        calculation_method="2yr_avg(net_profit + addbacks) * ownership_pct",
        inputs={
            "net_profit_by_year": net_profit_by_year,
            "depreciation_addback_by_year": depreciation_addback_by_year,
            "business_use_pct": business_use_pct,
        },
        flagged_for_underwriter=True,  # self-employed always gets human review
        notes=notes,
    )


def calc_rental_income(gross_monthly_rent: float, vacancy_factor: float = 0.25) -> IncomeResult:
    """
    Standard agency treatment: 75% of gross rent counts as qualifying
    income to account for vacancy/maintenance, unless a signed lease +
    Schedule E history supports a different factor.
    """
    monthly = gross_monthly_rent * (1 - vacancy_factor)
    return IncomeResult(
        income_type="rental",
        monthly_qualifying_income=round(monthly, 2),
        calculation_method="gross_monthly_rent * (1 - vacancy_factor)",
        inputs={"gross_monthly_rent": gross_monthly_rent, "vacancy_factor": vacancy_factor},
        notes=["Confirm against Schedule E / lease agreement; adjust vacancy factor per investor guide."],
    )


def calc_dti(total_monthly_debts: float, housing_payment: float, gross_monthly_income: float) -> dict:
    """
    Front-end (housing) and back-end (total) DTI ratios.
    Common agency guardrails (conventional, general): front-end ~28%,
    back-end ~36-45%+ with compensating factors / AUS approval; these are
    reference points, not hard caps -- actual limits come from the AUS
    findings and investor overlays.
    """
    if gross_monthly_income <= 0:
        raise ValueError("gross_monthly_income must be > 0")

    front_end = housing_payment / gross_monthly_income
    back_end = (total_monthly_debts + housing_payment) / gross_monthly_income

    return {
        "front_end_dti": round(front_end, 4),
        "back_end_dti": round(back_end, 4),
        "housing_payment": housing_payment,
        "total_monthly_debts": total_monthly_debts,
        "gross_monthly_income": gross_monthly_income,
        "reference_note": (
            "Typical guardrails: front-end ~28%, back-end ~36-45%+ with "
            "compensating factors and AUS approval. Confirm against current "
            "investor/product matrix -- not a substitute for AUS output."
        ),
    }
