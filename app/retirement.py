"""Retirement projection — Greg Clor's planner engine, fed by the Banana Stand's accounts.

The model is a year-by-year simulation from today to `max_age`:

  1. While a person works, salary flows into a 401(k) (employee + employer match).
  2. Social Security starts at each person's claiming age (PIA × early/delay × COLA).
  3. Spending inflates; any shortfall is withdrawn cash → brokerage → trad IRA → 401(k)
     → HSA → Roth.
  4. Taxes: MFJ brackets + standard deduction, LTCG stacking, taxable-SS rules, flat state.
  5. RMDs at SECURE 2.0 ages; growth applies to beginning balances (net flows get a half year).

Accounts are **not** entered by hand: they are read from the household's real accounts,
with holdings cost basis where we have it. Balances are Decimal dollars in the engine and
integer cents at the edges.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import copy
import time
from datetime import date
from decimal import Decimal
from typing import Any

from . import alerts, db, money, networth, portfolio, reports

ZERO = Decimal("0")
HALF = Decimal("0.5")

PLAN_KEY = "retirement_plan"

# Withdrawal priority: the order the engine drains accounts.
WITHDRAWAL_ORDER = ["cash", "brokerage", "trad-ira", "401k", "hsa", "roth-ira"]

# How many paychecks a year holds for each way of entering salary.
PERIODS_PER_YEAR = {"annual": 1, "monthly": 12, "semimonthly": 24, "biweekly": 26, "weekly": 52}

TYPE_LABELS = {
    "401k": "401(k) / 403(b)",
    "trad-ira": "Traditional IRA",
    "roth-ira": "Roth IRA",
    "hsa": "HSA",
    "brokerage": "Taxable brokerage",
    "cash": "Cash / savings",
}
TAX_CLASS = {
    "401k": "pretax",
    "trad-ira": "pretax",
    "roth-ira": "taxfree",
    "hsa": "taxfree",
    "brokerage": "taxable",
    "cash": "taxable",
}

# ---------------------------------------------------------------- tax tables (MFJ)
BRACKETS = [
    (Decimal("0.10"), Decimal("24500")),
    (Decimal("0.12"), Decimal("99600")),
    (Decimal("0.22"), Decimal("212400")),
    (Decimal("0.24"), Decimal("405400")),
    (Decimal("0.32"), Decimal("515000")),
    (Decimal("0.35"), Decimal("772400")),
    (Decimal("0.37"), Decimal("999999999")),
]
LTCG_BRACKETS = [
    (Decimal("0.00"), Decimal("99400")),
    (Decimal("0.15"), Decimal("616850")),
    (Decimal("0.20"), Decimal("999999999")),
]
STANDARD_DEDUCTION = Decimal("30800")
ADDITIONAL_65 = Decimal("1650")

_CLAIMING_MULTIPLIERS = {
    62: Decimal("0.70"), 63: Decimal("0.75"), 64: Decimal("0.80"), 65: Decimal("0.867"),
    66: Decimal("0.933"), 67: Decimal("1.000"), 68: Decimal("1.080"), 69: Decimal("1.160"),
    70: Decimal("1.240"),
}
_UNIFORM_LIFETIME = {
    72: Decimal("27.4"), 73: Decimal("26.5"), 74: Decimal("25.5"), 75: Decimal("24.6"),
    76: Decimal("23.7"), 77: Decimal("22.9"), 78: Decimal("22.0"), 79: Decimal("21.1"),
    80: Decimal("20.2"), 81: Decimal("19.4"), 82: Decimal("18.5"), 83: Decimal("17.7"),
    84: Decimal("16.8"), 85: Decimal("16.0"), 86: Decimal("15.2"), 87: Decimal("14.4"),
    88: Decimal("13.6"), 89: Decimal("12.8"), 90: Decimal("12.0"), 91: Decimal("11.2"),
    92: Decimal("10.4"), 93: Decimal("9.6"), 94: Decimal("8.9"), 95: Decimal("8.1"),
    96: Decimal("7.4"), 97: Decimal("6.7"), 98: Decimal("6.0"), 99: Decimal("5.3"),
    100: Decimal("4.6"),
}


# ---------------------------------------------------------------- plan storage
def blank_plan() -> dict[str, Any]:
    return {
        "people": [
            {"name": "", "birth_year": None, "retire_age": 65},
            {"name": "", "birth_year": None, "retire_age": 65},
        ],
        "income": [
            {"gross": 0, "frequency": "annual", "raise_pct": 0, "k401_pct": 0,
             "match_pct": 0, "match_up_to_pct": 0, "savings_monthly": 0, "savings_account_id": None},
            {"gross": 0, "frequency": "annual", "raise_pct": 0, "k401_pct": 0,
             "match_pct": 0, "match_up_to_pct": 0, "savings_monthly": 0, "savings_account_id": None},
        ],
        "spending": 0,
        "return_pct": 6.0,
        "return_volatility_pct": 15.0,
        "inflation_pct": 3.0,
        "state_tax_pct": 5.75,
        "max_age": 95,
        "roth_conversion": 0,
        "ss": [{"pia": 0, "claim_age": 67}, {"pia": 0, "claim_age": 67}],
        "pension": {"monthly": 0, "start_age": 65},
        "taxable_basis_pct": 70,
        # None = same growth as everything else; a number overrides crypto accounts only.
        "crypto_return_pct": None,
        # Timed spending: [{"name", "annual", "start_year", "end_year", "inflates"}]
        "adjustments": [],
        # Per-account cost basis overrides, {"<account id>": dollars}
        "basis_by_account": {},
    }


def _merge(base: dict, saved: dict | None) -> dict:
    if not isinstance(saved, dict):
        return base
    out = {**base, **{k: v for k, v in saved.items() if k in base}}
    for key in ("spending", "return_pct", "inflation_pct", "state_tax_pct", "max_age",
                "roth_conversion", "taxable_basis_pct"):
        if key in saved:
            out[key] = saved[key]
    for key in ("people", "income", "ss"):
        if isinstance(saved.get(key), list):
            out[key] = [{**base[key][i], **row} for i, row in enumerate(saved[key][:2])] + \
                       base[key][len(saved[key][:2]):]
    if isinstance(saved.get("pension"), dict):
        out["pension"] = {**base["pension"], **saved["pension"]}
    return out


def _num(value, default: float = 0.0, lo: float | None = None,
         hi: float | None = None) -> float:
    """A stored number that can never poison the projection (NaN/inf/strings)."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(n):
        return default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _int(value, default: int | None = None, lo: int | None = None,
         hi: int | None = None) -> int | None:
    n = _num(value, float("nan"))
    if not math.isfinite(n):
        return default
    n = int(round(n))
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _sanitize(plan: dict) -> dict:
    """Coerce and bound everything `project` touches.

    The plan is user input that survives in settings forever, so a stray NaN or a
    birth year of 1800 must never reach the engine — the loop would either crash or
    run for centuries.
    """
    this_year = date.today().year
    plan["spending"] = _num(plan.get("spending"), 0.0, 0, 1e9)
    plan["return_pct"] = _num(plan.get("return_pct"), 6.0, -50, 30)
    plan["return_volatility_pct"] = _num(plan.get("return_volatility_pct"), 15.0, 0, 80)
    plan["inflation_pct"] = _num(plan.get("inflation_pct"), 3.0, -5, 15)
    plan["state_tax_pct"] = _num(plan.get("state_tax_pct"), 5.75, 0, 15)
    plan["max_age"] = _int(plan.get("max_age"), 95, 60, 110)
    plan["roth_conversion"] = _num(plan.get("roth_conversion"), 0.0, 0, 1e8)
    plan["taxable_basis_pct"] = _num(plan.get("taxable_basis_pct"), 70.0, 0, 100)
    crypto = plan.get("crypto_return_pct")
    plan["crypto_return_pct"] = None if crypto in (None, "") else _num(crypto, 6.0, -50, 50)

    for person in plan.get("people") or []:
        birth = _int(person.get("birth_year"), None, 1900, this_year)
        person["birth_year"] = birth
        person["retire_age"] = _int(person.get("retire_age"), 65, 35, 90)
        person["name"] = str(person.get("name") or "")[:40]

    for row in plan.get("income") or []:
        row["gross"] = _num(row.get("gross"), 0.0, 0, 1e8)
        row["raise_pct"] = _num(row.get("raise_pct"), 0.0, 0, 25)
        row["k401_pct"] = _num(row.get("k401_pct"), 0.0, 0, 100)
        row["match_pct"] = _num(row.get("match_pct"), 0.0, 0, 100)
        row["match_up_to_pct"] = _num(row.get("match_up_to_pct"), 0.0, 0, 100)
        row["savings_monthly"] = _num(row.get("savings_monthly"), 0.0, 0, 1e6)
        if row.get("frequency") not in PERIODS_PER_YEAR:
            row["frequency"] = "annual"
        target = row.get("savings_account_id")
        row["savings_account_id"] = _int(target, None, 1) if target not in (None, "") else None

    for row in plan.get("ss") or []:
        row["pia"] = _num(row.get("pia"), 0.0, 0, 1e5)
        row["claim_age"] = _int(row.get("claim_age"), 67, 62, 70)

    pension = plan.get("pension") or {}
    pension["monthly"] = _num(pension.get("monthly"), 0.0, 0, 1e5)
    pension["start_age"] = _int(pension.get("start_age"), 65, 55, 90)
    plan["pension"] = pension

    clean = []
    for adj in plan.get("adjustments") or []:
        if not isinstance(adj, dict):
            continue
        annual = _num(adj.get("annual"), 0.0, -1e8, 1e8)
        if annual == 0 and not str(adj.get("name") or "").strip():
            continue
        clean.append({
            "name": str(adj.get("name") or "Adjustment")[:40],
            "annual": annual,
            "start_year": _int(adj.get("start_year"), None, 1900, 2200),
            "end_year": _int(adj.get("end_year"), None, 1900, 2200),
            "inflates": bool(adj.get("inflates", True)),
        })
    plan["adjustments"] = clean

    basis = {}
    for key, value in (plan.get("basis_by_account") or {}).items():
        amount = _num(value, -1.0, 0, 1e9)
        if base_key := str(key).strip():
            if amount >= 0:
                basis[base_key] = amount
    plan["basis_by_account"] = basis
    return plan


def load_plan(household_id: int) -> dict[str, Any]:
    plan = _sanitize(_merge(blank_plan(), alerts.get_setting(household_id, PLAN_KEY)))
    for person in plan["people"]:
        if person.get("birth_year"):
            person["retire_year"] = int(person["birth_year"]) + int(person.get("retire_age") or 65)
    return plan


def save_plan(household_id: int, plan: dict[str, Any]) -> None:
    alerts.set_setting(household_id, PLAN_KEY, plan)


def default_spending(household_id: int) -> int:
    """Trailing twelve months of spending, annualised — the natural starting point."""
    trend = reports.trend(household_id, 12)
    total = sum(t["expense_cents"] for t in trend)
    months = sum(1 for t in trend if t["expense_cents"])
    if not months:
        return 0
    return int(round(total / months * 12))


EARNED_INCOME_WORDS = ("payroll", "salary", "wages", "direct dep", "employer")
NOT_EARNED_WORDS = ("social security", "interest", "dividend", "refund", "rebate", "transfer")


def default_gross(household_id: int) -> int:
    """Annual *earned* income from confirmed recurring streams.

    Social Security, interest and dividends are income to the ledger but not salary, and
    the 401(k) math must not treat them as a paycheck.
    """
    total = 0
    selection = db.query_all(
        "SELECT name, amount_cents, cadence FROM recurring WHERE household_id = ?"
        " AND status = 'confirmed' AND kind = 'income'",
        (household_id,),
    )
    per_year = {"weekly": 52, "biweekly": 26, "monthly": 12, "quarterly": 4,
                "semiannual": 2, "annual": 1}
    for row in selection:
        name = (row["name"] or "").lower()
        if any(word in name for word in NOT_EARNED_WORDS):
            continue
        if not any(word in name for word in EARNED_INCOME_WORDS):
            continue
        total += int(row["amount_cents"]) * per_year.get(row["cadence"], 12)
    return total


# ---------------------------------------------------------------- accounts
# What the household says an account is beats what its name suggests: a bitcoin IRA
# sitting in a brokerage wrapper must still be taxed as an IRA.
TAX_TREATMENTS = {
    "cash": "cash",
    "taxable": "brokerage",
    "traditional": "trad-ira",
    "roth": "roth-ira",
    "hsa": "hsa",
}


def _planner_type(account: dict, basis_pct: Decimal) -> str:
    explicit = (account.get("tax_treatment") or "").strip().lower()
    if explicit in TAX_TREATMENTS:
        return TAX_TREATMENTS[explicit]
    kind = (account.get("type") or "").lower()
    name = (account.get("name") or "").lower()
    if kind == "hsa" or "hsa" in name:
        return "hsa"
    if kind == "retirement":
        if "roth" in name:
            return "roth-ira"
        if "401" in name or "403" in name:
            return "401k"
        return "trad-ira"
    if kind in ("checking", "savings", "cash"):
        return "cash"
    if kind in ("brokerage", "crypto", "investment"):
        # An IRA held in a brokerage wrapper is still an IRA — "BTC IRA" is the common case.
        if "roth" in name:
            return "roth-ira"
        if "401" in name or "403" in name:
            return "401k"
        if "ira" in name:
            return "trad-ira"
        if "hsa" in name:
            return "hsa"
        return "brokerage"
    return ""


def plan_accounts(household_id: int, plan: dict | None = None) -> list[dict]:
    """Real accounts, bucketed the way the projection engine thinks about them."""
    plan = plan or load_plan(household_id)
    basis_pct = Decimal(str(plan.get("taxable_basis_pct") or 0)) / Decimal("100")
    held = portfolio.holdings(household_id)
    basis_by_account: dict[int, int] = {}
    for row in held:
        basis_by_account[row["account_id"]] = basis_by_account.get(row["account_id"], 0) + \
            int(row.get("cost_basis_cents") or 0)
    out = []
    for account in networth.list_accounts(household_id):
        ptype = _planner_type(account, basis_pct)
        value = int(account.get("current_balance_cents") or 0)
        if not ptype or value <= 0:
            continue
        if account.get("is_liability"):
            continue
        if ptype == "cash":
            basis = value
        elif account["id"] in basis_by_account:
            basis = min(value, basis_by_account[account["id"]])
        elif (plan.get("basis_by_account") or {}).get(str(account["id"])) not in (None, ""):
            try:
                basis = int(round(float(plan["basis_by_account"][str(account["id"])]) * 100))
            except (TypeError, ValueError):
                basis = value
            basis = min(max(basis, 0), value)
        else:
            basis = int(round(value * basis_pct))
        out.append({
            "id": account["id"],
            "name": account["name"],
            "account_type": account["type"],
            "type": ptype,
            "type_label": TYPE_LABELS[ptype],
            "value_cents": value,
            "basis_cents": max(0, min(basis, value)),
            "tax_class": TAX_CLASS[ptype],
        })
    out.sort(key=lambda a: (WITHDRAWAL_ORDER.index(a["type"]), -a["value_cents"]))
    return out


# ---------------------------------------------------------------- calculators
def ss_benefit(pia: Decimal, claim_age: int, year: int, pia_year: int = 2026) -> Decimal:
    if pia <= 0:
        return ZERO
    multiplier = _CLAIMING_MULTIPLIERS.get(int(claim_age), Decimal("1.0"))
    years = max(year - pia_year, 0)
    return pia * multiplier * (Decimal("1.025") ** Decimal(str(years)))


def taxable_ss(ss: Decimal, agi_without_ss: Decimal) -> Decimal:
    if ss <= 0:
        return ZERO
    half = ss / 2
    combined = agi_without_ss + half
    if combined <= Decimal("32000"):
        return ZERO
    if combined <= Decimal("44000"):
        return min(half, (combined - Decimal("32000")) / 2)
    base = min(half, Decimal("6000"))
    extra = (combined - Decimal("44000")) * Decimal("0.85")
    return max(base, min(ss * Decimal("0.85"), base + extra))


def rmd(age: int, balance: Decimal, birth_year: int) -> Decimal:
    start = 75 if birth_year >= 1960 else 73
    if age < start or balance <= 0:
        return ZERO
    factor = _UNIFORM_LIFETIME.get(age)
    if factor is None:
        factor = max(Decimal("1.0"), Decimal("4.6") - Decimal(str(age - 100)) * Decimal("0.7"))
    return balance / factor


def _tax(taxable: Decimal, brackets) -> Decimal:
    remaining, tax, prev = taxable, ZERO, ZERO
    for rate, ceiling in brackets:
        if remaining <= 0:
            break
        band = min(remaining, ceiling - prev)
        tax += band * rate
        remaining -= band
        prev = ceiling
    return tax


def household_tax(employment: Decimal, trad: Decimal, conversions: Decimal, pension: Decimal,
                  ss: Decimal, ltcg: Decimal, pretax_contrib: Decimal, over_65: int,
                  state_pct: Decimal) -> dict:
    agi_with_gains = max(ZERO, employment + trad + conversions + pension + ltcg - pretax_contrib)
    agi_no_gains = max(ZERO, employment + trad + conversions + pension - pretax_contrib)
    included_ss = taxable_ss(ss, agi_with_gains)
    deductions = STANDARD_DEDUCTION + ADDITIONAL_65 * Decimal(over_65)
    taxable_ordinary = max(ZERO, agi_no_gains + included_ss - deductions)
    ordinary_tax = _tax(taxable_ordinary, BRACKETS)
    ltcg_tax = ZERO
    if ltcg > 0:
        left, prev = ltcg, taxable_ordinary
        for rate, ceiling in LTCG_BRACKETS:
            if left <= 0:
                break
            band = min(left, max(ZERO, ceiling - prev))
            ltcg_tax += band * rate
            left -= band
            prev = ceiling
    federal = ordinary_tax + ltcg_tax
    state = (agi_with_gains + included_ss) * (state_pct / Decimal("100"))
    return {"federal": federal, "state": state, "total": federal + state,
            "taxable_ss": included_ss, "taxable_ordinary": taxable_ordinary}


# ---------------------------------------------------------------- projection
class SimAccount:
    __slots__ = ("id", "type", "person", "value", "basis", "birth_year", "is_crypto")

    def __init__(self, row: dict, person: int, birth_year: int):
        self.id = row["id"]
        self.type = row["type"]
        self.person = person
        self.value = Decimal(row["value_cents"]) / 100
        self.basis = Decimal(row["basis_cents"]) / 100
        self.birth_year = birth_year
        self.is_crypto = (row.get("account_type") or "").lower() == "crypto"

    @property
    def tax_class(self) -> str:
        return TAX_CLASS[self.type]

    def withdraw(self, amount: Decimal) -> tuple[Decimal, Decimal, Decimal]:
        actual = min(amount, self.value)
        if actual <= 0:
            return ZERO, ZERO, ZERO
        if self.tax_class == "pretax":
            gain, pretax = ZERO, actual
            self.basis = max(ZERO, self.basis - actual)
        else:
            ratio = (self.value - self.basis) / self.value if self.value > 0 else ZERO
            ratio = max(ratio, ZERO)
            gain, pretax = actual * ratio, ZERO
            self.basis = max(ZERO, self.basis - (actual - gain))
        self.value -= actual
        return actual, gain, pretax


def project(household_id: int, plan: dict | None = None, today: date | None = None,
            return_overrides: dict[int, Decimal] | None = None,
            rows: list[dict] | None = None) -> dict:
    """Run the plan once. `return_overrides` swaps the flat return for a per-year path,
    which is what the Monte Carlo harness uses; `rows` skips re-reading the accounts."""
    plan = plan or load_plan(household_id)
    today = today or date.today()
    # Keep each person welded to their own income and Social Security: filtering the
    # people alone would slide person 2's salary onto person 1 when only one has a birth year.
    pairs = [(person, plan["income"][i], plan["ss"][i])
             for i, person in enumerate(plan["people"]) if person.get("birth_year")]
    people = [person for person, _, _ in pairs]
    if not people:
        return {"ready": False, "reason": "Add birth years to run the plan.",
                "accounts": plan_accounts(household_id, plan)}

    rows = plan_accounts(household_id, plan) if rows is None else rows
    if not rows:
        return {"ready": False, "reason": "No investable accounts found yet.", "accounts": []}

    births = [int(p["birth_year"]) for p in people]
    retire_years = [int(p["birth_year"]) + int(p.get("retire_age") or 65) for p in people]
    sim = [SimAccount(row, 0, births[0]) for row in rows]   # household accounts, primary owner

    incomes = [income for _, income, _ in pairs]
    ss = [benefit for _, _, benefit in pairs]
    infl = Decimal(str(plan["inflation_pct"])) / 100
    ret = Decimal(str(plan["return_pct"])) / 100
    _crypto = plan.get("crypto_return_pct")
    crypto_ret = None if _crypto in (None, "") else Decimal(str(_crypto)) / 100
    state_pct = Decimal(str(plan["state_tax_pct"]))
    base_spending = Decimal(str(plan["spending"]))
    conversion = Decimal(str(plan.get("roth_conversion") or 0))
    pension_monthly = Decimal(str(plan["pension"]["monthly"]))
    pension_age = int(plan["pension"]["start_age"])
    horizon = int(plan["max_age"])
    adjustments = [a for a in (plan.get("adjustments") or []) if isinstance(a, dict)]
    savings_ids = [inc.get("savings_account_id") for inc in incomes]

    def spending_in(year: int) -> Decimal:
        """Base spending inflated from today, plus each timed adjustment."""
        total = base_spending * ((Decimal("1") + infl) ** Decimal(str(year - start_year)))
        for adj in adjustments:
            try:
                starts = int(adj.get("start_year") or start_year)
            except (TypeError, ValueError):
                continue
            ends = adj.get("end_year")
            try:
                ends = int(ends) if ends not in (None, "") else None
            except (TypeError, ValueError):
                ends = None
            if year < starts or (ends is not None and year > ends):
                continue
            try:
                amount = Decimal(str(adj.get("annual") or 0))
            except (TypeError, ValueError, ArithmeticError):
                continue
            if not amount.is_finite():
                continue
            if adj.get("inflates", True) and year > starts:
                amount *= (Decimal("1") + infl) ** Decimal(str(year - starts))
            total += amount
        return max(ZERO, total)

    start_year = today.year
    end_year = max(b + horizon for b in births)
    if end_year < start_year:
        return {"ready": False,
                "reason": "The plan ends before it starts — check the birth years and plan-to age.",
                "accounts": rows}
    years = []
    failed = False
    fail_year = None
    retirement_year = min(retire_years)
    balance_at_retirement = None

    for year in range(start_year, end_year + 1):
        ages = [year - b for b in births]
        retired = [year >= ry for ry in retire_years]
        begin = {a.id: a.value for a in sim}
        flows: dict[int, Decimal] = {a.id: ZERO for a in sim}
        row = {"year": year, "age1": ages[0], "age2": ages[-1] if len(ages) > 1 else None,
               "employment": ZERO, "ss": ZERO, "pension": ZERO, "spending": ZERO,
               "withdrawn": ZERO, "tax": ZERO, "retired": all(retired),
               "rmd": ZERO, "conversion": ZERO, "early": False}

        # 1. Salary while working (401(k) employee + match flows into the same account)
        deferrals = ZERO
        for i, person in enumerate(people):
            if retired[i]:
                continue
            gross = Decimal(str(incomes[i].get("gross") or 0))
            periods = PERIODS_PER_YEAR.get(str(incomes[i].get("frequency") or "annual"), 1)
            gross *= Decimal(periods)
            raise_pct = Decimal(str(incomes[i].get("raise_pct") or 0)) / 100
            if raise_pct > 0 and year > start_year:
                gross *= (Decimal("1") + raise_pct) ** Decimal(str(year - start_year))
            row["employment"] += gross
            pct = Decimal(str(incomes[i].get("k401_pct") or 0)) / 100
            contribution = gross * pct
            deferrals += contribution
            match_rate = Decimal(str(incomes[i].get("match_pct") or 0)) / 100
            match_cap = Decimal(str(incomes[i].get("match_up_to_pct") or 0)) / 100
            match = gross * min(pct, match_cap) * match_rate
            target = next((a for a in sim if a.type == "401k"), None)
            if target and contribution + match > 0:
                target.value += contribution + match
                flows[target.id] += contribution + match
            # Post-tax savings land in the account you name, else the first brokerage.
            savings = Decimal(str(incomes[i].get("savings_monthly") or 0)) * 12
            if savings > 0:
                dest = next((a for a in sim if a.id == savings_ids[i]), None) or \
                       next((a for a in sim if a.type == "brokerage"), None)
                if dest:
                    dest.value += savings
                    flows[dest.id] += savings

        # 2. Social Security + pension
        for i, person in enumerate(people):
            claim = int(ss[i].get("claim_age") or 67)
            if ages[i] >= claim:
                row["ss"] += ss_benefit(Decimal(str(ss[i].get("pia") or 0)), claim, year)
        if pension_monthly > 0 and any(age >= pension_age for age in ages):
            row["pension"] = pension_monthly * 12

        # 3. Roth conversions during gap years
        everyone_retired, ss_started = all(retired), row["ss"] > 0
        if conversion > 0 and everyone_retired and not ss_started:
            remaining = conversion
            for source in sorted(sim, key=lambda a: WITHDRAWAL_ORDER.index(a.type)):
                if remaining <= 0:
                    break
                if source.tax_class != "pretax" or source.value <= 0:
                    continue
                amount = min(remaining, source.value)
                source.withdraw(amount)
                flows[source.id] -= amount
                target = next((a for a in sim if a.type == "roth-ira"), None)
                if target:
                    target.value += amount
                    flows[target.id] += amount
                row["conversion"] += amount
                remaining -= amount

        # 4. RMDs (parked back into cash so they stay inside the portfolio, taxed as income)
        taxable_rmd = ZERO
        for account in sim:
            if account.tax_class != "pretax" or account.value <= 0:
                continue
            age = year - account.birth_year
            amount = rmd(age, account.value, account.birth_year)
            if amount > 0:
                actual, _, rmd_pretax = account.withdraw(amount)
                row["rmd"] += actual
                taxable_rmd += rmd_pretax
                flows[account.id] -= actual
                parking = next((a for a in sim if a.type == "cash"), None)
                if parking:
                    parking.value += actual
                    flows[parking.id] += actual

        # 5. Spending
        spending = spending_in(year)
        row["spending"] = spending

        # 6. Shortfall + taxes
        def drain(amount_needed: Decimal):
            gains = pretax = ZERO
            for account in sorted(sim, key=lambda a: WITHDRAWAL_ORDER.index(a.type)):
                if amount_needed <= 0:
                    break
                actual, gain, px = account.withdraw(amount_needed)
                if px > 0 and (year - account.birth_year) < 60:
                    row["early"] = True
                if actual > 0:
                    flows[account.id] -= actual
                    amount_needed -= actual
                    gains += gain
                    pretax += px
            return gains, pretax

        external = row["employment"] + row["ss"] + row["pension"]
        gains = pretax = ZERO
        if spending - external > 0:
            gains, pretax = drain(spending - external)
        tax = household_tax(row["employment"], pretax + taxable_rmd, row["conversion"],
                            row["pension"], row["ss"], gains, deferrals,
                            sum(1 for a in ages if a >= 65), state_pct)
        surplus = max(ZERO, external - spending)
        shortfall = tax["total"] - surplus
        if shortfall > 0:
            g2, p2 = drain(shortfall)
            gains += g2
            pretax += p2
            tax = household_tax(row["employment"], pretax + taxable_rmd, row["conversion"],
                                row["pension"], row["ss"], gains, deferrals,
                                sum(1 for a in ages if a >= 65), state_pct)
        row["withdrawn"] = gains + pretax
        row["tax"] = tax["total"]

        # Money you earn but don't spend has to go somewhere — it used to evaporate,
        # which understated every plan where income outruns spending.
        # A 401(k) deferral is already in the account, not in the checking account.
        leftover = external - spending - row["tax"] - deferrals
        if leftover > 0:
            dest = next((a for a in sim if a.id == savings_ids[0]), None) or \
                   next((a for a in sim if a.type == "brokerage"), None) or \
                   next((a for a in sim if a.type == "cash"), None)
            if dest:
                dest.value += leftover
                flows[dest.id] += leftover

        portfolio = sum(a.value for a in sim)
        if portfolio <= 0 and spending > external:
            failed, fail_year = True, year
            row["portfolio_cents"] = 0
            years.append(row)
            break

        for account in sim:
            year_ret = return_overrides.get(year) if return_overrides else None
            base_rate = ret if year_ret is None else Decimal(year_ret)
            # Crypto keeps the same year-to-year shock but its own mean.
            if crypto_ret is not None and account.is_crypto:
                rate = max(Decimal("-0.95"), base_rate - ret + crypto_ret)
            else:
                rate = base_rate
            grown = begin[account.id] * (1 + rate) + flows[account.id] * (1 + rate * HALF)
            account.value = max(grown, ZERO)
        row["portfolio_cents"] = int(round(sum(a.value for a in sim) * 100))
        if balance_at_retirement is None and year >= retirement_year:
            balance_at_retirement = row["portfolio_cents"]
        years.append(row)

    for row in years:
        for key in ("employment", "ss", "pension", "rmd", "conversion", "spending", "withdrawn", "tax"):
            row[f"{key}_cents"] = int(round(row.pop(key) * 100))

    values = [row["portfolio_cents"] for row in years] or [0]
    peak = max(values + [1])
    width, height = 760, 220
    points = networth.line_points(values, width, height, lo=0, hi=peak * 1.05)
    totals = {
        "assets_cents": sum(r["value_cents"] for r in rows),
        "pretax_cents": sum(r["value_cents"] for r in rows if r["tax_class"] == "pretax"),
        "taxfree_cents": sum(r["value_cents"] for r in rows if r["tax_class"] == "taxfree"),
        "taxable_cents": sum(r["value_cents"] for r in rows if r["tax_class"] == "taxable"),
    }
    spend_at_retirement = next((row["spending_cents"] for row in years
                                if row["year"] >= retirement_year), 0)
    return {
        "ready": True,
        "years": years,
        "started": start_year,
        "ends": years[-1]["year"] if years else start_year,
        "succeeded": not failed,
        "fail_year": fail_year,
        "retirement_year": retirement_year,
        "balance_at_retirement_cents": balance_at_retirement or values[0],
        "spend_at_retirement_cents": spend_at_retirement,
        "final_cents": values[-1] if values else 0,
        "accounts": rows,
        "totals": totals,
        "chart": {"points": points, "area": f"6,{height - 6} {points} {width - 6},{height - 6}",
                  "width": width, "height": height, "peak_cents": peak,
                  # dollars, to match `series` (the scrub maps values, not cents)
                  "hi": round(peak * 1.05 / 100, 2),
                  "labels": [str(row["year"]) for row in years],
                  "series": [{"key": "portfolio", "label": "Portfolio",
                              "values": [round(v / 100, 2) for v in values]}],
                  "ticks": [{"pct": round(i / 3 * 100, 1),
                             "label": money.axis_label(int(peak * 1.05 * i / 3))}
                            for i in range(4)],
                  "grid": [round(height - 6 - (i / 3) * (height - 12), 1) for i in range(4)]},
        "years_to_retirement": max(0, retirement_year - start_year),
    }

# ---------------------------------------------------------------- monte carlo
_MC_CACHE: dict[str, tuple[float, dict]] = {}


def cached_monte_carlo(household_id: int, plan: dict | None = None, runs: int = 1000,
                       ttl: float = 600) -> dict | None:
    """The same simulation, remembered until the inputs change.

    1,000 paths costs ~3s; a page reload must not pay it again. The key is the whole
    input set, so saving the plan or a balance sync invalidates it automatically.
    """
    plan = plan or load_plan(household_id)
    rows = plan_accounts(household_id, plan)
    payload = json.dumps({"hid": household_id, "plan": plan, "rows": rows, "runs": runs,
                          "day": date.today().isoformat()}, sort_keys=True, default=str)
    key = hashlib.sha256(payload.encode()).hexdigest()
    now = time.time()
    hit = _MC_CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    result = monte_carlo(household_id, plan, runs=runs, rows=rows)
    if result is not None:
        _MC_CACHE[key] = (now, result)
        for stale in sorted(_MC_CACHE, key=lambda k: _MC_CACHE[k][0])[:-6]:
            _MC_CACHE.pop(stale, None)
    return result


def earliest_retirement(household_id: int, plan: dict | None = None, floor_age: int = 40) -> dict:
    """How much earlier both could stop and still reach the horizon.

    Shifts every person's retirement age down together, keeping the stagger between
    them, and takes the biggest shift that still funds the whole plan. Deterministic —
    a Monte Carlo success rate is a different question.
    """
    plan = plan or load_plan(household_id)
    rows = plan_accounts(household_id, plan)
    people = [p for p in plan["people"] if p.get("birth_year")]
    if not people or not rows:
        return {"ready": False}
    planned = [int(p.get("retire_age") or 65) for p in people]
    base = project(household_id, plan, rows=rows)
    if not base.get("ready"):
        return {"ready": False}

    def works(ages: list[int]) -> bool:
        trial = copy.deepcopy(plan)
        for person, age in zip([p for p in trial["people"] if p.get("birth_year")], ages):
            person["retire_age"] = age
        return bool(project(household_id, trial, rows=rows).get("succeeded"))

    if not base.get("succeeded"):
        return {"ready": True, "feasible": False, "planned": planned,
                "fail_year": base.get("fail_year"),
                "note": "The plan as written runs out, so no earlier date works."}

    best = 0
    limit = min(planned) - floor_age
    for delta in range(1, max(limit, 0) + 1):
        if works([max(floor_age, a - delta) for a in planned]):
            best = delta
        else:
            break    # retiring earlier only ever costs more, so stop at the first failure

    return {
        "ready": True,
        "feasible": True,
        "planned": planned,
        "ages": [max(floor_age, a - best) for a in planned],
        "years_earlier": best,
        "at_floor": best >= max(limit, 0) and limit > 0,
        "floor": floor_age,
    }


def monte_carlo(household_id: int, plan: dict | None = None, runs: int = 1000,
                seed: int | None = 20261122, rows: list[dict] | None = None) -> dict | None:
    """Randomized return paths through the same engine — the planner's Monte Carlo.

    Each run draws a lognormal annual return matched to the plan's arithmetic mean and
    volatility, so a path can compound down hard but never below -100%. Every tax,
    withdrawal and RMD rule runs identically; only the return changes.
    """
    plan = plan or load_plan(household_id)
    runs = max(1, min(int(runs), 20000))
    base = project(household_id, plan)
    if not base.get("ready"):
        return None
    rows = rows if rows is not None else plan_accounts(household_id, plan)
    years = [row["year"] for row in base["years"]]
    mean = max(float(plan["return_pct"]) / 100, -0.99)
    vol = float(plan.get("return_volatility_pct") or 15) / 100
    if vol > 0:
        sigma = math.sqrt(math.log(1 + (vol / (1 + mean)) ** 2))
        mu = math.log(1 + mean) - sigma * sigma / 2
    else:
        sigma, mu = 0.0, math.log(1 + mean)
    rng = random.Random(seed)

    finals: list[int] = []
    per_year: dict[int, list[int]] = {year: [] for year in years}
    runouts: list[int] = []          # the year each path ran dry; horizon + 1 if it never did
    failures = 0
    for _ in range(runs):
        overrides = {}
        for year in years:
            factor = math.exp(rng.normalvariate(mu, sigma)) if sigma else 1 + mean
            overrides[year] = Decimal(repr(factor - 1))   # the engine works in fractions
        result = project(household_id, plan, return_overrides=overrides, rows=rows)
        finals.append(result["final_cents"])
        # A failed path stops early; its missing years are zero, not absent.
        so_far = {row["year"]: row["portfolio_cents"] for row in result["years"]}
        for year in years:
            per_year[year].append(so_far.get(year, 0))
        runouts.append(result.get("fail_year") or (years[-1] + 1))
        if not result["succeeded"]:
            failures += 1

    ordered = sorted(finals)
    def pick(pct: float) -> int:
        return ordered[max(0, min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1)))))]

    def pct(column: list[int], share: float) -> int:
        return column[max(0, min(len(column) - 1, int(round(share * (len(column) - 1)))))]

    bands = []
    for year in years:
        column = sorted(per_year[year])
        bands.append({
            "year": year,
            "p10": pct(column, 0.10),
            "p25": pct(column, 0.25),
            "p50": pct(column, 0.50),
            "p75": pct(column, 0.75),
            "p90": pct(column, 0.90),
            "mean": int(round(sum(column) / len(column))),
        })

    # Share one y-scale with the deterministic line, widened if the 75th percentile
    # climbs above it — otherwise the band would clip at the top of the plot.
    chart = base["chart"]
    w, h = chart["width"], chart["height"]
    base_top = int(round(chart["peak_cents"] * 1.05))
    top = max(base_top, max((b["p75"] for b in bands), default=0))
    if top > base_top:
        values = [r["portfolio_cents"] for r in base["years"]]
        points = networth.line_points(values, w, h, lo=0, hi=top)
        chart = dict(chart)
        chart["points"] = points
        chart["area"] = f"6,{h - 6} {points} {w - 6},{h - 6}"
        chart["hi"] = round(top / 100, 2)
        chart["ticks"] = [{"pct": round(i / 3 * 100, 1),
                           "label": money.axis_label(int(top * i / 3))} for i in range(4)]

    def path(key: str) -> str:
        return networth.line_points([b[key] for b in bands], w, h, lo=0, hi=top)

    p25, p75 = path("p25"), path("p75")
    band_poly = " ".join([p75, " ".join(reversed(p25.split()))]) if p25 and p75 else ""
    chart_bands = {
        "band": band_poly,
        "median": path("p50"),
        "mean": path("mean"),
        "p25_end": bands[-1]["p25"] if bands else 0,
        "p50_end": bands[-1]["p50"] if bands else 0,
        "p75_end": bands[-1]["p75"] if bands else 0,
        "mean_end": bands[-1]["mean"] if bands else 0,
    }

    # "1 in 10 paths are out of money by ..." — the bottom decile of the *endings* is
    # $0 whenever more than 10% fail, so a year is the only honest way to show it.
    runouts.sort()
    p10_runout = runouts[max(0, int(round(0.1 * (len(runouts) - 1))))]
    return {
        "runs": runs,
        "success_pct": round(100 * (runs - failures) / runs, 1),
        # 95% binomial interval — the number is a sample, not a truth.
        "success_ci_pct": round(1.96 * math.sqrt(
            max((runs - failures) / runs * (failures / runs), 0) / runs) * 100, 1),
        "p10_runout_year": p10_runout if p10_runout <= years[-1] else None,
        "volatility_pct": round(vol * 100, 1),
        "median_cents": pick(50),
        "p10_cents": pick(10),
        "p90_cents": pick(90),
        "baseline_cents": base["final_cents"],
        "years": years,
        "bands": bands,
        "chart": chart,
        "chart_bands": chart_bands,
        # Same keys the legend uses, so dragging the chart rewrites those numbers.
        "chart_series": [
            {"key": key, "label": label,
             "values": [round(b[key] / 100, 2) for b in bands]}
            for key, label in (("p25", "25th percentile"), ("p50", "Median"),
                               ("mean", "Average"), ("p75", "75th percentile"))
        ],
    }

