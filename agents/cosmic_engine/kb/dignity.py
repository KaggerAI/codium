"""
dignity.py — planetary dignity and natural friendship tables.

PROVENANCE WARNING — this module is NOT generated from COSMIC_PDF_AUGMENTATION_TEXT.

Sections 27 and 30 of the knowledge base require a strong/weak verdict for a planet in D1, D9
and D10 ("A planet strong in D1 but weak in D9...", "if D10 placement is in a sign whose lord is
friendly to the trigger"), but the KB never supplies the dignity or friendship tables those rules
depend on. Until now the model supplied them from training knowledge, unverifiably and
inconsistently between runs.

The values below are the standard classical Jyotish tables (Parashari). They are an explicit
addition to the rulebook, not a transcription of it. Two consequences worth knowing:

  * If these differ from what the model was previously assuming, divisional verdicts will change.
    That is the intended correctness fix, not a regression.
  * RAHU_KETU dignity is genuinely disputed in the tradition; the convention adopted here is
    stated explicitly below and is the single most defensible thing to change if the desk
    disagrees. Nothing else in the engine depends on which convention is chosen.
"""

SIGN_LORD = {
    "Aries": "Mars",
    "Taurus": "Venus",
    "Gemini": "Mercury",
    "Cancer": "Moon",
    "Leo": "Sun",
    "Virgo": "Mercury",
    "Libra": "Venus",
    "Scorpio": "Mars",
    "Sagittarius": "Jupiter",
    "Capricorn": "Saturn",
    "Aquarius": "Saturn",
    "Pisces": "Jupiter",
}

# Sign classification, used by the navamsa start rule (movable/fixed/dual) in section 25 and by
# the earthquake fixed-sign axis rule in section 6.
MOVABLE = ("Aries", "Cancer", "Libra", "Capricorn")
FIXED = ("Taurus", "Leo", "Scorpio", "Aquarius")
DUAL = ("Gemini", "Virgo", "Sagittarius", "Pisces")

# exalted: (sign, exact_degree) — the degree matters for "deeply exalted" grading, not for the
# binary verdict. own: signs ruled. mooltrikona: (sign, from_deg, to_deg).
DIGNITY = {
    "Sun": {"exalted": ("Aries", 10.0), "debilitated": "Libra",
            "own": ("Leo",), "mooltrikona": ("Leo", 0.0, 20.0)},
    "Moon": {"exalted": ("Taurus", 3.0), "debilitated": "Scorpio",
             "own": ("Cancer",), "mooltrikona": ("Taurus", 3.0, 30.0)},
    "Mars": {"exalted": ("Capricorn", 28.0), "debilitated": "Cancer",
             "own": ("Aries", "Scorpio"), "mooltrikona": ("Aries", 0.0, 12.0)},
    "Mercury": {"exalted": ("Virgo", 15.0), "debilitated": "Pisces",
                "own": ("Gemini", "Virgo"), "mooltrikona": ("Virgo", 16.0, 20.0)},
    "Jupiter": {"exalted": ("Cancer", 5.0), "debilitated": "Capricorn",
                "own": ("Sagittarius", "Pisces"), "mooltrikona": ("Sagittarius", 0.0, 10.0)},
    "Venus": {"exalted": ("Pisces", 27.0), "debilitated": "Virgo",
              "own": ("Taurus", "Libra"), "mooltrikona": ("Libra", 0.0, 15.0)},
    "Saturn": {"exalted": ("Libra", 20.0), "debilitated": "Aries",
               "own": ("Capricorn", "Aquarius"), "mooltrikona": ("Aquarius", 0.0, 20.0)},
    # Adopted convention for the nodes: Rahu exalted Taurus / debilitated Scorpio, Ketu the
    # reverse. Other schools give Rahu exaltation in Gemini and Ketu in Sagittarius, or hold that
    # the nodes have no exaltation at all since they own no sign. Change here if the desk prefers
    # a different school; no other module hardcodes node dignity.
    "Rahu": {"exalted": ("Taurus", 20.0), "debilitated": "Scorpio",
             "own": (), "mooltrikona": None},
    "Ketu": {"exalted": ("Scorpio", 20.0), "debilitated": "Taurus",
             "own": (), "mooltrikona": None},
}

# Naisargika maitri (natural friendship). Any pair absent from both lists is neutral.
# Note this relation is NOT symmetric in the classical tables: Venus counts Saturn a friend while
# Saturn counts Venus a friend, but Mercury counts Moon an enemy while Moon counts Mercury a
# friend. friendship() below preserves the asymmetry rather than averaging it away.
FRIENDS = {
    "Sun": ("Moon", "Mars", "Jupiter"),
    "Moon": ("Sun", "Mercury"),
    "Mars": ("Sun", "Moon", "Jupiter"),
    "Mercury": ("Sun", "Venus"),
    "Jupiter": ("Sun", "Moon", "Mars"),
    "Venus": ("Mercury", "Saturn"),
    "Saturn": ("Mercury", "Venus"),
    "Rahu": ("Venus", "Saturn"),
    "Ketu": ("Mars", "Venus", "Saturn"),
}
ENEMIES = {
    "Sun": ("Venus", "Saturn"),
    "Moon": (),
    "Mars": ("Mercury",),
    "Mercury": ("Moon",),
    "Jupiter": ("Mercury", "Venus"),
    "Venus": ("Sun", "Moon"),
    "Saturn": ("Sun", "Moon", "Mars"),
    "Rahu": ("Sun", "Moon", "Mars"),
    "Ketu": ("Sun", "Moon"),
}

# Section 30 Stage 1 tie-break: "among same-tier conflicts, slower planet wins".
# Index 0 is slowest. Outer planets are slower than Saturn but the KB's hierarchy (section 16)
# ranks them below Saturn for mundane weight, so they are appended rather than prepended.
SLOWNESS_ORDER = (
    "Saturn", "Jupiter", "Rahu", "Ketu", "Mars", "Sun", "Venus", "Mercury", "Moon",
    "Uranus", "Neptune", "Pluto",
)
