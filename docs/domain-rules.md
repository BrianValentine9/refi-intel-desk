# Domain rules — VA IRRRL and FHA Streamline clearance

> This is the regulatory spec the analysis core implements. It is adapted from a
> research session (June 10, 2026) into the contract for `src/core/`. Rule
> modules cite the section numbers here in their docstrings. Where this document
> is silent on a program rule, the code must not invent one — see
> [§7 Open questions](#7-open-questions-and-limitations) and any "Resolved
> clarifications" recorded at the end.

## 1. Architecture — three independent layers

A loan clears only when it is **agency-clear** under the exact program rule,
**economically-clear** under house break-even math, and **call-clear** under
execution heuristics. These are related but **non-interchangeable** tests and are
never collapsed into one flag or one "savings" number.

| Layer | Question it answers | Nature |
|---|---|---|
| 1 — agency-clear | Does the loan qualify under VA/FHA program rules? | Pure rules |
| 2 — economically-clear | Is the refinance genuinely worth doing for the borrower under house math? | Pure math |
| 3 — call-clear | Is the file executable/saleable enough to justify outreach? | Heuristics + unknowns |

They must stay separate because VA's statutory recoupment test **excludes** some
costs that still matter economically, while FHA's streamline analysis is built on
**combined rate** and **P+I+MIP**, not note rate alone. Collapsing them would hide
the nuance that makes the model credible.

Two corrections anchor the design:

- **VA cost treatment.** Statutory recoupment is *not* an all-in borrower
  break-even test. VA excludes the funding fee, escrow amounts, and prepaid
  expenses (insurance, taxes, special assessments, HOA), and — after Change 1 —
  EEM funds; lender credits may offset allowable fees. The model therefore keeps
  **two** VA cost buckets: a statutory recoupment bucket (agency compliance) and a
  broader borrower-cost bucket (practical economics).
- **FHA MIP.** FHA streamline is **not** a universal "0.55 annual / 0.55 upfront."
  Standard UFMIP is 1.75% of base loan amount; annual MIP is **schedule-driven**.
  A special reduced treatment applies to prior FHA mortgages endorsed on or before
  **May 31, 2009** (UFMIP 0.01%, annual MIP 0.55%). MIP logic is **versioned by
  endorsement date and applicable schedule**, never a single hard-coded constant.

## 2. Layer 1 — Agency-clear

### 2.1 VA IRRRL

**2.1.1 Program basics.** Must be VA-to-VA; may not result in cash out; may not
pay off any loan other than the existing VA loan; any second lien must subordinate
so the new VA loan remains first lien.

**2.1.2 Occupancy.** The veteran need only certify that they **previously**
occupied the home — not that they currently occupy it.

**2.1.3 Seasoning (hard gate).** The existing loan is seasoned only if, as of refi
closing, the first monthly payment due date on the loan being refinanced is **at
least 210 days before closing** AND the borrower has made **six consecutive
monthly payments**. If the loan being refinanced is not properly seasoned, the
refinance cannot be guaranteed.

**2.1.4 Net tangible benefit.** Narrower than "payment improves":
- Fixed-rate → fixed-rate: the new rate must be **at least 0.50 percentage points
  lower** than the old rate.
- Fixed-rate → ARM: the new initial rate must be **at least 2.00 percentage points
  lower** than the old fixed rate, with **additional VA constraints on financed
  discount points and LTV** in fixed-to-ARM cases.

**2.1.5 Recoupment (statutory).** Applies to **all** IRRRLs, including where the
principal balance rises, the term decreases, or the old loan is an ARM.

```
va_statutory_recoupment_months
    = statutory_eligible_costs / (old_monthly_PI - new_monthly_PI)

statutory_eligible_costs EXCLUDE:
    - VA funding fee
    - escrow amounts
    - prepaid expenses (insurance, taxes, special assessments, HOA)
    - EEM funds

Pass condition: va_statutory_recoupment_months <= 36 months from closing.
```

If `new_monthly_PI >= old_monthly_PI`, then `statutory_eligible_costs` **must be 0**
for agency-clear. This is a program rule, not a house rule. The denominator is the
monthly P+I reduction **only**. The numerator includes fees paid in or out of
closing, minus the exclusions above.

**2.1.6 Funding fee.** The IRRRL funding fee is **0.5%** for loans closing on or
after April 7, 2023 and prior to November 14, 2031. Several exemption categories
exist (including many borrowers receiving or entitled to disability compensation).
`va_funding_fee_rate` and `va_funding_fee_exempt` are explicit first-class fields,
never buried in pricing logic.

**2.1.7 Comparison statement.** Lenders must present the veteran with a loan
comparison statement within 3 business days of application and again at closing —
modeled as a compliance/documentation field.

### 2.2 FHA Streamline

**2.2.1 Program basics.** The loan being refinanced must already be FHA-insured and
**current**; the refinance must provide a net tangible benefit; cash back is limited
to **no more than $500**. "Streamline" refers to **reduced documentation**, not a
no-cost transaction.

**2.2.2 Financed costs (structural).** FHA does **not** allow lenders to include
**ordinary closing costs** in the new mortgage amount on a streamline refinance.
Lenders may instead cover them through premium pricing at a higher rate, so the
model separates financed costs from rate-paid costs.

**2.2.3 Combined rate.** HUD defines the **combined rate** as the interest rate
**plus the annual mortgage insurance premium rate**. FHA NTB logic is built on
combined rate, never on note rate or P+I alone.

**2.2.4 Net tangible benefit.**
- Without term reduction: a fixed→fixed refi must reduce the new **combined rate**
  by **at least 0.50 percentage points**.
- With term reduction: the new combined rate must be **below** the prior combined
  rate, AND the new combined **P+I+MIP** payment may **not exceed** the old
  combined P+I+MIP payment by more than **$50**.

**2.2.5 Seasoning (hard gate).** On the date of FHA **case-number assignment**:
- at least **six payments** made on the FHA mortgage being refinanced, AND
- at least **six full months** since the first payment due date, AND
- at least **210 days** since the closing date of the mortgage being refinanced.

If the borrower assumed the loan, they must have made six payments since assumption.

**2.2.6 Payment history.** For files with more than six months of mortgage history,
payments must have been made **within the month due for the prior six months**, with
**no more than one 30-day late** in that six-month period; the borrower must also
have made the payment for all mortgages on the subject property within the month due
for the month before disbursement.

**2.2.7 Underwriting path.** FHA streamline refinances must be **manually
underwritten**. The mortgagee may run TOTAL Mortgage Scorecard, but the findings are
**invalid** for streamline purposes. Non-credit-qualifying streamlines are generally
available when all current borrowers stay on the new loan; **credit-qualifying**
streamlines (e.g., borrower removal, or a large payment increase) must meet
manual-underwriting requirements.

**2.2.8 Subordinate financing.** Existing subordinate financing must be
**re-subordinated** to the new streamline mortgage (certain new subordinate
financing is allowed for specific purposes).

**2.2.9 Occupancy.** Non-owner-occupied properties and HUD-approved secondary
residences are eligible for streamline refinancing **only into a fixed-rate
mortgage**.

**2.2.10 Insurance costs.** Standard UFMIP is **1.75%** of base loan amount; annual
MIP is **schedule-based**. Special reduced treatment for streamline/simple
refinances of a prior FHA mortgage **endorsed on or before May 31, 2009**: UFMIP
**0.01%**, annual MIP **0.55%**. If a borrower refinances a current FHA-insured
mortgage to another FHA-insured mortgage **within 3 years**, a **UFMIP refund
credit** reduces the new UFMIP. Fields `fha_endorsement_date`, `fha_ufmip_rate`,
`fha_annual_mip_rate`, and `fha_ufmip_refund_credit` are all first-class.

## 3. Layer 2 — Economically-clear

Separate from agency-clear by design. VA agency compliance depends on the statutory
recoupment bucket and the monthly P+I reduction only; but the borrower still
experiences cash-flow effects the statute deliberately excludes (funded fee
treatment, lender credits, points, escrow resets, term extension). So the model
computes **both** `va_statutory_recoupment_months` (can VA guaranty this?) and a
broader `economic_break_even_months_all_in` (should a human want this?).

For FHA the economic baseline is monthly **P+I+MIP**, because HUD's NTB test is built
on combined rate and, in the term-reduction path, on the change in combined P+I+MIP.
Required derived values: `old_monthly_PI`, `new_monthly_PI`, `old_monthly_MIP`,
`new_monthly_MIP`, `old_monthly_PIMIP`, `new_monthly_PIMIP`, `monthly_savings_PIMIP`.

**Three cost buckets, always separate:**
1. `agency_recoupment_cost` — used only where the agency test requires it.
2. `economic_total_cost` — every borrower-borne cost or refi-created balance increase
   in house break-even math.
3. `excluded_prepaids_escrow_cost` — amounts omitted from agency recoupment but still
   visible in the file.

**economically_clear** = positive monthly housing-payment savings under the correct
program lens (P&I for the VA framing, P+I+MIP for FHA) AND an all-in break-even under
a stated business threshold. **That threshold is house policy, not VA or FHA law**,
and is documented as such (default 48 months).

## 4. Layer 3 — Call-clear

Agency-clear and economically-clear are fully computed math. Call-clear is a **schema
of fields, blockers, unknowns, and heuristics** — too many decisive factors are not
available in synthetic data and are not agency-computable anyway. The docs state
plainly: Layer 3 models **execution risk**, it is **not** a deterministic rule engine.

Three categories:
- **Hard blockers** — known disqualifiers (e.g., confirmed seasoning failure,
  payment-history defect beyond tolerance).
- **Soft blockers** — quote-integrity / optics risks that weaken a borrower outcome.
- **Unknowns** — items requiring human review that synthetic data cannot resolve.

Representative flags: `subordination_unknown`, `term_reset_optics`,
`escrow_reset_risk`, `insurance_payment_shift`, `funding_fee_exempt_unknown`,
`manual_underwrite_path`, `credit_qualifying_required` (FHA borrower change or large
payment increase), plus seasoning and payment-history defects, and VA prior-occupancy
certification issues.

Beyond agency rules sit **lender overlays** the market does not standardize (minimum
FICO, income-verification preferences, allowable fee treatments, state title/fee
issues, servicer overlays, lock/pricing tolerance). These are real reasons not to
call but are **not honestly computable** from synthetic data, so they live as
unknowns.

## 5. Cost model and synthetic assumptions

- VA recoupment-eligible costs: synthetic base case **1.0% of balance**, sensitivity
  band **0.5%–1.5%**. **Labeled a synthetic assumption, not market truth.** The
  statutory-cost bucket (excludes funding fee, escrow, prepaids, EEM) is stored
  separately from broader borrower costs.
- FHA economics computed on **P+I+MIP**. Premium logic is **versioned by endorsement
  date**; the pre-May 31, 2009 streamlined treatment is handled separately; an
  explicit UFMIP refund credit field covers FHA-to-FHA refis within 3 years.
- The all-in break-even **house threshold** (default 48 months) is a **business
  policy**, never presented as agency law.

## 6. Synthetic pool design (scoping, resolved)

Main note-rate band 5.250%–7.875% in 0.125 steps, weighted to the middle, plus a
deliberate **legacy low-coupon tail** (below the main band) that **should fail
Layer 2** — in a low-to-mid 6% 2026 environment, a book made entirely of
5.25%–7.875% loans would overstate callability. Seasoning is varied so a meaningful
slice fails the 210-day / six-payment gates (a dataset where everything passes
demonstrates nothing). Seeded edge-case strata: VA ARM-to-fixed, term reductions,
pre-May 31, 2009 FHA endorsements, FHA-to-FHA within 3 years, second liens,
borrower-removal (FHA credit-qualifying), and VA funding-fee exemptions.

## 7. Open questions and limitations

- **Current FHA standard annual-MIP schedule.** FHA premium treatment is
  schedule-driven and historical-case dependent. Only the pre-May 31, 2009 reduced
  treatment (0.01% UFMIP / 0.55% annual) is fixed here. For a live calculator, the
  generic annual-MIP table must be stored in versioned config and checked against the
  current HUD handbook at release rather than frozen as a constant.
- **VA fixed→ARM** imposes additional discount-point and LTV constraints that are
  acknowledged but not quantified in the source.
- **VA comparison-statement** exhibit text was not retrievable; the 3-business-day /
  at-closing requirement is modeled as a documentation field without sample text.

## 7a. Resolved clarifications (architect rulings, 2026-06-10)

Where §7 was silent, these decisions were made by the architect and are now binding
on the code. They are explicitly **synthetic-model choices**, not new regulation.

- **FHA standard annual MIP.** Post-May 31, 2009 FHA loans use **1.75% UFMIP /
  0.55% annual MIP** as a single synthetic stratum, stored in versioned config and
  labeled a synthetic simplification. (0.55% is the current HUD value for the most
  common 30-year stratum — base ≤ $726,200, LTV > 95%, effective 2023-03-20.) The
  pre-May 31, 2009 special treatment (§2.2.10) remains separate. No property-value/
  LTV tiering is modeled; the pool schema is unchanged.
- **VA ARM→fixed NTB.** Satisfied when the new fixed rate is **lower than the old
  ARM note rate** (any reduction). The source specifies thresholds only for
  fixed→fixed and fixed→ARM (§2.1.4); ARM→fixed is treated as a tangible benefit by
  interpretation, consistent with VA's general treatment of ARM→fixed.
- **VA fixed→ARM extra constraints.** Only the **≥2.00% rate-drop** gate (§2.1.4) is
  enforced. The additional discount-point/LTV constraints are acknowledged but **not
  enforced**, because the source does not quantify them and the synthetic data
  carries no discount-point detail. Documented as a simplification.
- **FHA UFMIP refund credit.** Modeled with a **simplified declining schedule** in
  config (refund share steps down from closing to zero by 36 months) for FHA-to-FHA
  refis within 3 years — a synthetic simplification of HUD's actual refund table,
  clearly labeled as such.

## 8. The one-sentence core

A loan clears only when it is agency-clear under the exact program rule,
economically-clear under house break-even math, and call-clear under execution
heuristics; those are related but non-interchangeable tests.
