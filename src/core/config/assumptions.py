"""Tunable thresholds and SYNTHETIC assumptions for the analysis core.

Agency thresholds here are transcribed from docs/domain-rules.md (program law).
Anything labeled SYNTHETIC or HOUSE POLICY is a modeling choice, NOT regulation —
see domain-rules.md §3 and §5.
"""

from __future__ import annotations

from datetime import date

# Fixed evaluation date ("refi closing" / as-of) so seasoning math is deterministic
# regardless of when the code runs. Matches the latest data pull era.
AS_OF_DATE = date(2026, 6, 10)

# --- HOUSE POLICY (not agency law) — domain-rules §3 ---
BREAK_EVEN_THRESHOLD_MONTHS = 48

# --- SYNTHETIC cost assumptions (not market truth) — domain-rules §5 ---
RECOUPMENT_COST_PCT_BASE = 0.010  # recoupment-eligible costs, 1.0% of balance
RECOUPMENT_COST_PCT_LOW = 0.005   # sensitivity band low
RECOUPMENT_COST_PCT_HIGH = 0.015  # sensitivity band high
PREPAIDS_ESCROW_PCT = 0.004       # synthetic prepaids/escrow, excluded from VA recoupment

# --- VA agency constants — domain-rules §2.1 ---
VA_IRRRL_FUNDING_FEE_RATE = 0.005  # 0.5% (closing 2023-04-07 .. before 2031-11-14)
VA_NTB_FIXED_TO_FIXED_DROP = 0.50  # percentage points
VA_NTB_FIXED_TO_ARM_DROP = 2.00
VA_SEASONING_DAYS = 210
VA_SEASONING_PAYMENTS = 6
VA_RECOUPMENT_MAX_MONTHS = 36

# --- FHA agency constants — domain-rules §2.2 ---
FHA_NTB_COMBINED_DROP = 0.50                 # percentage points
FHA_NTB_TERM_REDUCTION_PIMIP_TOLERANCE = 50.0  # dollars, P+I+MIP
FHA_SEASONING_DAYS = 210
FHA_SEASONING_PAYMENTS = 6
FHA_SEASONING_MONTHS = 6
FHA_MAX_30DAY_LATES_6MO = 1
FHA_CASH_BACK_LIMIT = 500.0
# Layer 3 heuristic: payment jump that forces FHA credit-qualifying path.
FHA_CREDIT_QUALIFY_PAYMENT_INCREASE = 0.20

# --- Trigger ladder — brief §7 ---
LADDER_STEP = 0.125  # percentage points
LADDER_RANGE = 2.00  # step down this many points below current market
