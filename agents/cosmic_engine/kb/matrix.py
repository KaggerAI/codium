"""
matrix.py — KB section 27's Confirmation Output Matrix and section 30's stage constants.

Verbatim transcription of two tables the model has been applying from prose. Section 27's matrix
has exactly 8 rows over (D1 strong?, D9 strong?, D10 strong?), and section 30 Stage 5 bounds the
result. Neither involves judgment; both are lookups.
"""

# Section 27 Confirmation Output Matrix, keyed (d1_strong, d9_strong, d10_strong).
# `band` and the probability range are verbatim; `reject` marks the one row that section 30
# Stage 4 turns into "no directional call".
CONFIRMATION_MATRIX = {
    (True, True, True): {
        "band": "HIGH", "low": 75, "high": 85, "reject": False,
        "verdict": "theme will manifest with strong sector follow-through"},
    (True, True, False): {
        "band": "MEDIUM", "low": 50, "high": 65, "reject": False,
        "verdict": "theme manifests but sector underperforms expectations"},
    (True, False, True): {
        "band": "MEDIUM", "low": 50, "high": 65, "reject": False,
        "verdict": "sector moves but final outcome disappoints"},
    (True, False, False): {
        "band": "LOW", "low": 35, "high": 50, "reject": False,
        "verdict": "theme is mostly noise; AVOID major positioning"},
    (False, True, True): {
        "band": "MEDIUM", "low": 55, "high": 65, "reject": False,
        "verdict": "contrarian opportunity; theme delivers despite weak surface"},
    (False, True, False): {
        "band": "LOW-WATCH", "low": 30, "high": 45, "reject": False,
        "verdict": "outcome may surprise positively but sector lags"},
    (False, False, True): {
        "band": "LOW-WATCH", "low": 30, "high": 45, "reject": False,
        "verdict": "sector tactical play possible, not strategic"},
    (False, False, False): {
        "band": "REJECT", "low": 0, "high": 0, "reject": True,
        "verdict": "do not issue prediction"},
}
assert len(CONFIRMATION_MATRIX) == 8

# Section 27: "D10 has PRIORITY over D9 for sector and equity calls; D9 has PRIORITY over D10 for
# outcome/commodity-delivery calls." Maps a signal category to which chart breaks a tie.
DIVISIONAL_PRIORITY = {
    "equity": "d10", "sector": "d10", "policy": "d10", "geopolitics": "d10",
    "commodity": "d9", "currency": "d9", "natural_event": "d9",
}
DIVISIONAL_PRIORITY_DEFAULT = "d10"

# Section 30 Stage 5 bounds, and Stage 8's reporting bands.
CONVICTION_CAP = 85
CONVICTION_FLOOR = 30
# Half-open [low, high) ranges. Section 30 Stage 8 reads "HIGH CONFIDENCE (>70%)" and
# "MEDIUM CONFIDENCE (50-70%)" — so 70 itself is MEDIUM, and HIGH starts at 71. Getting this
# boundary wrong silently promotes every borderline call by one band.
BANDS = (
    ("HIGH CONFIDENCE", 71, 101),
    ("MEDIUM CONFIDENCE", 50, 71),
    ("LOW CONFIDENCE", 30, 50),
)

# Section 30 Stage 2 eclipse windows, in days from the exact eclipse.
ECLIPSE_OVERRIDE_DAYS = 15     # overrides all Stage 1 signals
ECLIPSE_UPGRADE_DAYS = 30      # upgrades one tier
ECLIPSE_EQUAL_WEIGHT_DAYS = 90  # equal-weighted with other Tier 1

# Section 30 Stage 3: within this many degrees of a pada or decanate boundary -> ROTATION IMMINENT.
ROTATION_BOUNDARY_DEG = 1.0

# Section 30 Stage 0 no-drop rule. Kept as a named constant because it is an invariant the
# pipeline tests assert: a GATE FAIL may never be the sole reason a signal disappears.
GATE_FAIL_NEVER_DROPS = True


def band_for(probability):
    """Section 30 Stage 8 confidence label for a probability."""
    for label, low, high in BANDS:
        if low <= probability < high:
            return label
    return "SUPPRESSED"
