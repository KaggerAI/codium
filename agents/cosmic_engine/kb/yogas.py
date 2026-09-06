"""
yogas.py — mundane yoga definitions and their Stage 5 modulation factors.

Definitions and effects are verbatim from KB section 29. The NUMERIC FACTORS ARE NOT.

PROVENANCE WARNING — section 29 states modulations qualitatively ("DAMPENS benefic transit
effects; AMPLIFIES malefic transit effects") and section 30 Stage 5 then demands arithmetic
("Final conviction = Stage 4 conviction x sum of active yoga modulations", "stacking allowed:
multiple yogas combine multiplicatively"). The rulebook never bridges the two, so the model has
been inventing the numbers on every run.

The factors below are that bridge, chosen conservatively and symmetrically:

    strong amplify  1.20      strong dampen  0.80
    amplify         1.15      dampen         0.85
    oscillate       1.00      (Shakata: alternating, no net bias)

They are deliberately mild. Section 30 caps conviction at 85% and floors it at 30%, so a single
yoga should nudge a verdict, not decide it; two stacked dampeners (0.85 x 0.85 = 0.72) is a
meaningful haircut without collapsing a HIGH call on ambient context alone. Tune here if
backtesting ever gives a reason to — nothing else hardcodes these.

Target selectivity matters as much as magnitude: a yoga that "amplifies benefics" must not touch a
Saturn signal. `applies_to` carries that, so pipeline.py can apply each factor only to the signals
it actually governs.
"""

AMPLIFY_STRONG = 1.20
AMPLIFY = 1.15
NEUTRAL = 1.00
DAMPEN = 0.85
DAMPEN_STRONG = 0.80

# applies_to: which signal classes the factor touches.
#   'benefic'  - signals driven by Jupiter/Venus/Moon/Mercury
#   'malefic'  - signals driven by Mars/Saturn/Rahu/Ketu/Sun
#   'all'      - any signal
# direction: 'amplify' | 'dampen' | 'oscillate', for narration.
YOGA_DEFINITIONS = {
    "Kala Sarpa": {
        "definition": "All 7 traditional planets between the Rahu-Ketu axis",
        "effect": ("Systemic tension, suppressed energy, sudden ruptures, hidden agendas, "
                   "mass psychology distortion"),
        "modulation": "DAMPEN benefics; AMPLIFY malefics; increases hidden/black-swan risk",
        "factors": {"benefic": DAMPEN_STRONG, "malefic": AMPLIFY_STRONG},
        "direction": "dampen",
    },
    "Kala Amrita": {
        "definition": "All planets between Ketu and Rahu (opposite of Kala Sarpa)",
        "effect": "Karmic resolution, transformative cycles, slow steady gains",
        "modulation": "AMPLIFIES long-term benefic transits; DAMPENS impulsive malefic effects",
        "factors": {"benefic": AMPLIFY, "malefic": DAMPEN},
        "direction": "amplify",
    },
    "Gajakesari": {
        "definition": "Jupiter in kendra (1/4/7/10) from the Moon",
        "effect": "Wisdom-driven policy, prosperity, institutional strength",
        "modulation": "AMPLIFIES benefic transits, particularly Jupiter/Moon triggers",
        "factors": {"benefic": AMPLIFY},
        "direction": "amplify",
    },
    "Kemadruma": {
        "definition": "Moon with no planets in the 2nd/12th from it and no planets in kendra to it",
        "effect": "Public despair, mass psychological weakness, sentiment-driven selloffs",
        "modulation": "DAMPENS benefic effects on consumption/sentiment-driven sectors",
        "factors": {"benefic": DAMPEN},
        "direction": "dampen",
    },
    "Chandra-Mangal": {
        "definition": "Moon-Mars conjunction or mutual exchange",
        "effect": "Trade boom, commercial activity surge, business activity",
        "modulation": "AMPLIFIES trade/commerce/retail transit effects",
        "factors": {"all": AMPLIFY},
        "direction": "amplify",
    },
    "Lakshmi": {
        "definition": "Venus and the 9th lord both strong in the mundane chart",
        "effect": "Wealth circulation, luxury sector boom, prosperity flows",
        "modulation": "AMPLIFIES Venus-ruled sector transits (luxury, jewelry, premium FMCG)",
        "factors": {"benefic": AMPLIFY},
        "direction": "amplify",
    },
    "Daridra": {
        "definition": "11th lord weak, conjunct malefics, or in a dustana (6/8/12)",
        "effect": "Income contraction, fiscal austerity, deflation risk",
        "modulation": "DAMPENS income-sensitive transits; AMPLIFIES contraction signals",
        "factors": {"benefic": DAMPEN, "malefic": AMPLIFY},
        "direction": "dampen",
    },
    "Vipreet Raja": {
        "definition": "6th/8th/12th lords placed in 6/8/12 from each other",
        "effect": "Recovery from crisis, contrarian leadership, comeback stories",
        "modulation": "AMPLIFIES recovery/turnaround transit themes; favors distressed assets",
        "factors": {"all": AMPLIFY},
        "direction": "amplify",
    },
    "Adhi": {
        "definition": "Benefics in the 6th, 7th and 8th from the Moon",
        "effect": "Stability, leadership, public confidence",
        "modulation": "AMPLIFIES stability-themed transits; DAMPENS volatility",
        "factors": {"benefic": AMPLIFY, "malefic": DAMPEN},
        "direction": "amplify",
    },
    "Shakata": {
        "definition": "Moon in the 6th or 8th from Jupiter",
        "effect": "Cyclic instability, periodic boom-bust",
        "modulation": "INTRODUCES oscillation: alternating amplify/dampen on benefic transits",
        # Oscillation has no net directional bias, so the factor is 1.0 and the yoga's value is
        # entirely in being *disclosed* to the narration layer. Section 29 asks for the
        # disclosure; inventing a number here would be worse than admitting there isn't one.
        "factors": {"all": NEUTRAL},
        "direction": "oscillate",
    },
    "Graha Malika": {
        "definition": "Four or more planets in successive signs",
        "effect": "Concentrated thematic intensity",
        "modulation": "AMPLIFIES all transit themes touching those houses",
        "factors": {"all": AMPLIFY},
        "direction": "amplify",
    },
    "Sunafa / Anafa / Durdhura": {
        "definition": "Planets in the 2nd, 12th, or both from the Moon (excluding the Sun)",
        "effect": "Income flow patterns",
        "modulation": "AMPLIFIES income-positive transits",
        "factors": {"benefic": AMPLIFY},
        "direction": "amplify",
    },
}

# Section 29: "When yogas conflict (e.g. Gajakesari + Kemadruma both active) the yoga of the
# slower-moving planet wins." These are the planets each yoga is anchored on, slowest first, so
# the resolver can rank them.
YOGA_ANCHOR = {
    "Kala Sarpa": "Rahu",
    "Kala Amrita": "Rahu",
    "Gajakesari": "Jupiter",
    "Kemadruma": "Moon",
    "Chandra-Mangal": "Mars",
    "Lakshmi": "Venus",
    "Daridra": "Saturn",
    "Vipreet Raja": "Saturn",
    "Adhi": "Jupiter",
    "Shakata": "Jupiter",
    "Graha Malika": "Saturn",
    "Sunafa / Anafa / Durdhura": "Moon",
}

# Pairs that section 29 treats as mutually exclusive states of the same chart-wide condition.
# Detecting both would be a bug, so yogas.detect() asserts they never co-occur.
MUTUALLY_EXCLUSIVE = (("Kala Sarpa", "Kala Amrita"), ("Gajakesari", "Kemadruma"))
