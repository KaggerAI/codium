"""
dasha.py — Vimshottari constants and the India natal reference.

Sections 28 and 30 of the knowledge base make the running Mahadasha/Antardasha the GATEKEEPER
above every transit signal, but §28 supplies the answer as a hardcoded table of approximate dates
rather than a calculation. The table is not sloppy — it is exact Vimshottari arithmetic — but it
is arithmetic from a natal Moon position that does not match what Lahiri gives at the standard
birth instant. See docs/COSMIC_ENGINE_MIGRATION.md section 5 for the full analysis.

THE CALIBRATION CHOICE (this is the load-bearing decision in this module):

  mode = "kb_calibrated"  (default)
      Pins the natal Moon to 94.3857 deg, the value that reproduces every date in KB section 28
      exactly: Saturn balance 17.500y, Mars MD from 2025-02-12, Mars-Rahu AD Jul 2025 -> Jul 2026,
      Mars-Jupiter AD from 2026-07-29. Behaviour-neutral: the engine agrees with the rulebook and
      with every report produced to date.

  mode = "computed"
      Derives the Moon from Lahiri at 15 Aug 1947 00:00 IST, giving 93.9835 deg, a Saturn balance
      of 18.073y, and Mars MD from 2025-09-09. Astronomically correct for that instant, but it
      shifts every juncture ~7 months earlier and changes the CURRENT antardasha: as of Aug 2026
      it says Mars-Rahu where the KB says Mars-Jupiter. Since §28 Rule G1 gives those two very
      different theme lists, this flips the Stage 0 gate for most predictions.

The 0.4022 deg gap between the two corresponds to a birth time of ~00:38 IST (bisection-solved)
or an ayanamsa ~0.40 deg smaller than true Lahiri. Which is "right" is an astrological judgment,
not an engineering one, so it is a config switch and every computed result carries a `source`
field recording which mode produced it.
"""

# (lord, mahadasha years). Sequence order is fixed; the starting lord is the ruler of the natal
# Moon's nakshatra, hence index = nakshatra_index % 9.
VIMSHOTTARI_SEQUENCE = (
    ("Ketu", 7),
    ("Venus", 20),
    ("Sun", 6),
    ("Moon", 10),
    ("Mars", 7),
    ("Rahu", 18),
    ("Jupiter", 16),
    ("Saturn", 19),
    ("Mercury", 17),
)
TOTAL_CYCLE_YEARS = 120
assert sum(y for _, y in VIMSHOTTARI_SEQUENCE) == TOTAL_CYCLE_YEARS

# Vimshottari years are conventionally solar years. 365.2425 reproduces §28's month boundaries;
# some traditions use a 360-day savana year, which would drift the table by ~1.5%.
YEAR_DAYS = 365.2425

INDIA_NATAL = {
    "label": "India independence chart",
    # 15 August 1947, 00:00:00 IST = 14 August 1947 18:30:00 UTC.
    "date_utc": "1947-08-14T18:30:00Z",
    "year": 1947, "month": 8, "day": 14, "ut_hour": 18.5,
    "place": "New Delhi (28d36'N, 77d12'E)",
    "lagna": "Taurus",          # used for mundane house placement (KB sections 15, 19, 29)

    # See the calibration note above. Change this one string to switch schools.
    "mode": "kb_calibrated",

    # Reproduces KB section 28 exactly. Cancer 4d23' -> 1.0524 deg into Pushya -> balance 17.500y.
    "moon_lon_kb_calibrated": 94.3857,
}

# KB section 28 Rule G5: multi-year sectoral bias by Mahadasha lord. Verbatim from the table.
MD_SECTOR_BIAS = {
    "Sun": "Govt PSU, gold, sovereign bonds",
    "Moon": "FMCG, consumer staples, water/utilities, mass consumption",
    "Mars": ("Defense, infrastructure, capex cycle, real estate, energy, metals - "
             "STRUCTURAL BULL in capital goods, weakness in pure consumer plays"),
    "Rahu": "Disruption - AI, biotech, crypto/digital assets, foreign capital, speculative themes",
    "Jupiter": "Banking/financials, education, expansion phase, large-cap leadership",
    # The KB's Rule G5 table stops at Jupiter. The remaining lords are given their section 20
    # sector rulership as the bias, which is the same source the KB draws G5 from.
    "Saturn": "Infrastructure, coal, iron ore, real estate, construction, PSU banks, value stocks",
    "Mercury": "IT services, telecom, media, logistics, fintech, e-commerce",
    "Venus": "Luxury goods, entertainment, tourism, textiles, cosmetics, premium FMCG",
    "Ketu": "Pharma, wellness, contrarian value plays, volatility products",
}

# KB section 28 Rule G1: theme lists for the current Mars mahadasha and its antardashas. Keyed by
# (md_lord, ad_lord); the md-only entry is the fallback when an AD has no explicit list.
DASHA_THEMES = {
    ("Mars", None): (
        "War risk, militarization, defense capex, infrastructure, real estate, energy, "
        "metals/mining, surgical pharma, fire/accident risk, aggressive monetary stance, "
        "geopolitical assertiveness, border tensions, industrial accidents, rapid policy execution"),
    ("Mars", "Rahu"): (
        "Foreign/external Mars themes - defense exports, military diplomacy, tech-defense fusion "
        "(AI in warfare), unpredictable geopolitical shocks, speculative defense plays, "
        "cryptocurrency intersecting with military, sanctions, sudden inflammatory events"),
    ("Mars", "Jupiter"): (
        "Expansive Mars - large defense procurement deals, military-banking nexus, defense IPOs, "
        "judicial activism on military matters, religious-military tensions, education-defense "
        "partnerships, optimistic infra capex cycle"),
}

# KB section 24, dasha special case: dashas are gates, not transit signals, so they take juncture
# proximity weights rather than transit recency multipliers.
JUNCTURE_WEIGHTS = ((30, 2.0), (90, 1.5))   # (days_within, weight); beyond the last -> 1.0
JUNCTURE_DEFAULT_WEIGHT = 1.0
