"""
tiers.py — KB section 22's tier system and section 24's time-decay model.

Both are verbatim. Section 22's own caveat is preserved because it matters for how much weight
anything downstream should place on these numbers:

    "Tier weights are HEURISTIC, not derived from calibrated statistics."

So the arithmetic in pipeline.py is reproducible, not validated. Moving it into code makes it
auditable and consistent; it does not make it correct.
"""

# Section 22, TRANSIT layer only. The dasha gate (section 28) sits above this; yogas (29) modulate
# it; divisional charts (27) confirm or reject its outputs.
TIER_WEIGHTS = {1: 0.40, 2: 0.30, 3: 0.20, 4: 0.10}

TIER_SIGNALS = {
    1: ("Saturn sign change", "Saturn retrograde", "Jupiter sign change", "Jupiter retrograde",
        "Rahu-Ketu axis shift", "eclipse within 30 days", "major conjunction within 3 deg orb",
        "Jupiter-Saturn conjunction or opposition"),
    2: ("Sun ingress", "Mercury retrograde", "Venus retrograde", "Mars retrograde",
        "stellium of 3+ planets in one sign", "Mars sign change", "combustion of a benefic"),
    3: ("nakshatra zone activation", "Sapta Nadi position", "Samvatsar cabinet",
        "planetary combination", "geographic rulership"),
    4: ("weekday repetition", "Poornima/Amavasya signal", "stambha/megh calculation",
        "Rohini Niwas", "tithi observation"),
}

# Event-type -> tier, for the events the engine detects mechanically (Phase 3). Slow-mover sign
# changes and stations are Tier 1; fast movers are Tier 2.
SLOW_BODIES = ("Saturn", "Jupiter", "Rahu", "Ketu")
EVENT_TIERS = {
    ("sign_change", "slow"): 1,
    ("sign_change", "fast"): 2,
    ("station", "slow"): 1,
    ("station", "fast"): 2,
    ("eclipse", "any"): 1,
    ("conjunction_tight", "any"): 1,
    ("conjunction_wide", "any"): 2,
    ("lunation", "any"): 4,
    ("nakshatra_change", "slow"): 3,
    ("nakshatra_change", "fast"): 4,
}


def tier_for_event(event_type, body=None):
    """Tier for a detected event. Unknown types fall to Tier 3 (contextual), never Tier 1."""
    speed = "any"
    if (event_type, "any") not in EVENT_TIERS:
        speed = "slow" if body in SLOW_BODIES else "fast"
    return EVENT_TIERS.get((event_type, speed), 3)


# Section 24 signal durations. Informational: used for narration and for choosing how far ahead a
# signal remains relevant, not for the decay multiplier itself.
SIGNAL_DURATION = {
    "eclipse_solar": {"duration": "6-12 months", "peak": "first 30 days"},
    "eclipse_lunar": {"duration": "1-3 months", "peak": "first 14 days"},
    "saturn_transit": {"duration": "2-3 years per sign", "peak": "entire period, peaks at station"},
    "jupiter_transit": {"duration": "12 months per sign", "peak": "first 2 months of ingress"},
    "rahu_ketu_axis": {"duration": "18 months per sign pair", "peak": "first 3 months of shift"},
    "mars_transit": {"duration": "6-8 weeks per sign", "peak": "first 2 weeks of ingress"},
    "mercury_retro": {"duration": "3-4 weeks", "peak": "shadow period +/-1 week each side"},
    "venus_retro": {"duration": "6 weeks", "peak": "central 2 weeks"},
    "sun_ingress": {"duration": "30 days", "peak": "first 7 days"},
    "conjunction_major": {"duration": "weeks to months", "peak": "exact date +/-7 days"},
    "conjunction_minor": {"duration": "days to weeks", "peak": "exact date +/-3 days"},
}

# Section 24 recency multipliers, applied to TRANSIT signals by days-until-activation.
# (max_days, multiplier), ascending; beyond the last entry -> DECAY_DISTANT.
RECENCY_MULTIPLIERS = ((14, 2.0), (30, 1.5), (60, 1.0), (90, 0.75))
DECAY_DISTANT = 0.5


def recency_multiplier(days_until):
    """
    Section 24 decay multiplier for a transit signal activating in `days_until` days.

    Dashas are explicitly exempt ("Dasha periods are GATES, not transit signals. Do NOT apply
    transit recency multipliers to dashas") — they use dasha.state()'s juncture context_weight
    instead. Past-dated signals are treated as imminent rather than negative.
    """
    if days_until < 0:
        return RECENCY_MULTIPLIERS[0][1]
    for max_days, mult in RECENCY_MULTIPLIERS:
        if days_until <= max_days:
            return mult
    return DECAY_DISTANT
