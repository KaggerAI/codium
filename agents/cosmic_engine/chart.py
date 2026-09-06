"""
chart.py — deterministic chart kernel for the Cosmic engine.

Computes everything the knowledge base's precision and confirmation layers need but that
generate_cosmic_data_report() never supplied: navamsa (D9), dashamsa (D10), decanate and its
value-chain stage, pada sub-sector, dignity in each divisional chart, vargottama, aspects, and
graded distance to every pada/decanate boundary.

Design constraints (docs/COSMIC_ENGINE_MIGRATION.md section 3.6):
  * cast() takes an explicit timestamp. Nothing here calls now(); tests pin the date.
  * Output is JSON-serializable — ISO strings, no datetime objects, no tuples as dict keys.
  * No non-ASCII on any log path. Devanagari lives in kb/ data and prompt payloads only.

Sidereal throughout, Lahiri ayanamsa, matching agents/utils/ephemeris.py.
"""

import datetime

import swisseph as swe

from agents.cosmic_engine.kb.decanates import DECANATE_STAGE
from agents.cosmic_engine.kb.dignity import (
    DIGNITY, DUAL, ENEMIES, FIXED, FRIENDS, MOVABLE, SIGN_LORD, SLOWNESS_ORDER,
)
from agents.cosmic_engine.kb.padas import PADA_SUBSECTOR

SIGNS = [
    "Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo",
    "Libra", "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces",
]
NAKSHATRAS = [
    "Ashwini", "Bharani", "Krittika", "Rohini", "Mrigashira", "Ardra",
    "Punarvasu", "Pushya", "Ashlesha", "Magha", "Purva Phalguni", "Uttara Phalguni",
    "Hasta", "Chitra", "Swati", "Vishakha", "Anuradha", "Jyeshtha",
    "Mula", "Purva Ashadha", "Uttara Ashadha", "Shravana", "Dhanishta", "Shatabhisha",
    "Purva Bhadrapada", "Uttara Bhadrapada", "Revati",
]

NAK_SPAN = 360.0 / 27.0          # 13 deg 20'
PADA_SPAN = 360.0 / 108.0        # 3 deg 20' — also the navamsa span
D10_SPAN = 3.0
DECANATE_SPAN = 10.0

# Bodies the engine tracks. Rahu is the true node; Ketu is derived as Rahu + 180.
BODIES = (
    "Sun", "Moon", "Mercury", "Venus", "Mars", "Jupiter", "Saturn",
    "Rahu", "Ketu", "Uranus", "Neptune", "Pluto",
)
_SWE_ID = {
    "Sun": swe.SUN, "Moon": swe.MOON, "Mercury": swe.MERCURY, "Venus": swe.VENUS,
    "Mars": swe.MARS, "Jupiter": swe.JUPITER, "Saturn": swe.SATURN,
    "Rahu": swe.TRUE_NODE, "Ketu": swe.TRUE_NODE,
    "Uranus": swe.URANUS, "Neptune": swe.NEPTUNE, "Pluto": swe.PLUTO,
}
# Nodes are always retrograde in mean motion; the KB never treats that as a signal, so it is
# not reported as a retrograde event. Sun and Moon never retrograde.
_NEVER_RETROGRADE = ("Sun", "Moon", "Rahu", "Ketu")

# Combustion orbs, carried over from agents/utils/ephemeris.py so both agree.
COMBUSTION_ORBS = {
    "Moon": 12.0, "Mercury": 14.0, "Venus": 10.0,
    "Mars": 17.0, "Jupiter": 11.0, "Saturn": 15.0,
}

# Aspect definitions for conjunction/opposition/square/trine detection, with orbs. The KB's
# section 30 Stage 1 references "major conjunctions (within 3 deg orb)"; the wider orbs here are
# for context and each aspect carries its exact orb so the caller can filter.
ASPECTS = (
    ("conjunction", 0.0, 8.0),
    ("opposition", 180.0, 8.0),
    ("square", 90.0, 6.0),
    ("trine", 120.0, 6.0),
)

MALEFICS = ("Mars", "Saturn", "Rahu", "Ketu", "Sun")
BENEFICS = ("Jupiter", "Venus", "Moon", "Mercury")


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

def julian_day(dt):
    """UTC datetime -> Julian day. Accepts naive (assumed UTC) or tz-aware."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    hour = dt.hour + dt.minute / 60.0 + dt.second / 3600.0 + dt.microsecond / 3.6e9
    return swe.julday(dt.year, dt.month, dt.day, hour)


def sign_of(lon):
    """Sidereal longitude -> (sign_name, degree_within_sign)."""
    lon %= 360.0
    return SIGNS[int(lon / 30.0)], lon % 30.0


def nakshatra_of(lon):
    """Sidereal longitude -> (nakshatra_index, nakshatra_name, pada 1-4)."""
    lon %= 360.0
    idx = int(lon / NAK_SPAN)
    pada = int((lon % NAK_SPAN) / PADA_SPAN) + 1
    return idx, NAKSHATRAS[idx], pada


def d9_sign(lon):
    """
    Navamsa sign. Each sign splits into 9 parts of 3 deg 20', and the 108 navamsas cycle the
    zodiac 9 times from 0 Aries — which reproduces the classical movable/fixed/dual start rule
    (section 25) without special-casing it:
        movable -> starts from itself, fixed -> from the 9th, dual -> from the 5th.
    Verified against all 108 hand-written navamsa columns in KB section 25 (see tests).
    """
    return SIGNS[int((lon % 360.0) / PADA_SPAN) % 12]


def d10_sign(lon):
    """
    Dashamsa sign, per KB section 27: each sign splits into 10 parts of 3 deg; odd signs
    (1,3,5,7,9,11) start from the same sign, even signs from the 9th sign.
    """
    lon %= 360.0
    sign_idx = int(lon / 30.0)
    part = int((lon % 30.0) / D10_SPAN)          # 0..9
    is_odd = (sign_idx % 2 == 0)                 # sign_idx 0 == Aries == 1st == odd
    start = sign_idx if is_odd else (sign_idx + 8) % 12
    return SIGNS[(start + part) % 12]


def decanate_of(lon):
    """
    Drekkana, per KB section 26: 1st decanate lord is the sign's own lord, 2nd is the lord of
    the 5th sign from it, 3rd the lord of the 9th. Returns (1|2|3, lord, stage, character).
    """
    lon %= 360.0
    sign_idx = int(lon / 30.0)
    part = int((lon % 30.0) / DECANATE_SPAN)      # 0..2
    offset = (0, 4, 8)[part]                     # same sign, 5th, 9th
    lord = SIGN_LORD[SIGNS[(sign_idx + offset) % 12]]
    meta = DECANATE_STAGE[part + 1]
    return part + 1, lord, meta["stage"], meta["character"]


def sign_nature(sign):
    """movable | fixed | dual — used by the navamsa rule and the section 6 fixed-axis check."""
    if sign in MOVABLE:
        return "movable"
    if sign in FIXED:
        return "fixed"
    if sign in DUAL:
        return "dual"
    raise ValueError("unknown sign %r" % sign)


def dignity(body, sign, deg=None):
    """
    Dignity of a body in a sign: exalted | mooltrikona | own | friend | neutral | enemy |
    debilitated. `deg` is optional and only refines mooltrikona (which is degree-bounded).

    Outer planets have no classical dignity; they return 'neutral' rather than raising, so
    section 16's outer-planet rules can still run.
    """
    table = DIGNITY.get(body)
    if table is None:
        return "neutral"
    if table["exalted"] and sign == table["exalted"][0]:
        return "exalted"
    if sign == table["debilitated"]:
        return "debilitated"
    mt = table.get("mooltrikona")
    if mt and sign == mt[0] and deg is not None and mt[1] <= deg < mt[2]:
        return "mooltrikona"
    if sign in table["own"]:
        return "own"
    return friendship(body, SIGN_LORD[sign])


def friendship(body, other):
    """Natural friendship of `body` toward `other`: friend | neutral | enemy. Asymmetric."""
    if body == other:
        return "friend"
    if other in FRIENDS.get(body, ()):
        return "friend"
    if other in ENEMIES.get(body, ()):
        return "enemy"
    return "neutral"


def is_strong(verdict):
    """
    Collapse a dignity verdict to the strong/weak binary that KB section 27's Confirmation
    Output Matrix is expressed in. Deliberately a single chokepoint: the matrix has only two
    input states per chart, so the mapping from seven dignities to two states must be explicit
    and in one place rather than scattered across callers.
    """
    return verdict in ("exalted", "mooltrikona", "own", "friend")


def boundary_proximity(lon):
    """
    Graded distance to the next/previous pada and decanate boundary
    (docs/COSMIC_ENGINE_MIGRATION.md section 3.7 amendment 3).

    Returns degrees to the nearest edge in each division plus the ROTATION IMMINENT flag from
    KB sections 25/26 (within 1 deg). The magnitudes are returned alongside the flag so
    narration can hedge proportionally instead of flipping character over a 0.02 deg move.
    """
    lon %= 360.0
    pada_off = lon % PADA_SPAN
    dec_off = lon % DECANATE_SPAN
    pada_edge = min(pada_off, PADA_SPAN - pada_off)
    dec_edge = min(dec_off, DECANATE_SPAN - dec_off)
    return {
        "pada_deg_to_edge": round(pada_edge, 4),
        "decanate_deg_to_edge": round(dec_edge, 4),
        "pada_rotation_imminent": pada_edge <= 1.0,
        "decanate_rotation_imminent": dec_edge <= 1.0,
        "rotation_imminent": pada_edge <= 1.0 or dec_edge <= 1.0,
    }


def angular_separation(a, b):
    """Shortest angular distance between two longitudes, 0..180."""
    d = abs((a - b) % 360.0)
    return 360.0 - d if d > 180.0 else d


# ---------------------------------------------------------------------------
# Cast
# ---------------------------------------------------------------------------

def positions(dt_utc):
    """Raw sidereal longitudes and speeds for every tracked body at `dt_utc`."""
    swe.set_sid_mode(swe.SIDM_LAHIRI)
    jd = julian_day(dt_utc)
    flags = swe.FLG_SWIEPH | swe.FLG_SIDEREAL | swe.FLG_SPEED

    out = {}
    for body in BODIES:
        res = swe.calc_ut(jd, _SWE_ID[body], flags)[0]
        lon, speed = res[0], res[3]
        if body == "Ketu":
            lon = (lon + 180.0) % 360.0
        out[body] = {"lon": lon % 360.0, "speed": speed}
    return out


def cast(dt_utc):
    """
    Full chart. Returns a JSON-serializable dict:
        {"as_of", "ayanamsa", "julian_day", "bodies": {...}, "aspects": [...],
         "retrogrades": [...], "combust": [...]}

    Every body carries its D1/D9/D10 placement, dignity in each, decanate stage, pada
    sub-sector, and graded boundary distances — i.e. everything KB sections 25-27 need and
    which the model previously had to invent.
    """
    swe.set_sid_mode(swe.SIDM_LAHIRI)
    jd = julian_day(dt_utc)
    raw = positions(dt_utc)
    sun_lon = raw["Sun"]["lon"]

    bodies, retrogrades, combust = {}, [], []
    for body, p in raw.items():
        lon, speed = p["lon"], p["speed"]
        sign, deg = sign_of(lon)
        nak_idx, nak_name, pada = nakshatra_of(lon)
        d9, d10 = d9_sign(lon), d10_sign(lon)
        dec_num, dec_lord, dec_stage, dec_character = decanate_of(lon)

        retro = speed < 0 and body not in _NEVER_RETROGRADE
        if retro:
            retrogrades.append(body)

        orb = COMBUSTION_ORBS.get(body)
        sep_from_sun = angular_separation(lon, sun_lon)
        is_combust = bool(orb) and body != "Sun" and sep_from_sun <= orb
        if is_combust:
            combust.append(body)

        pada_row = PADA_SUBSECTOR.get((nak_idx, pada), {})
        d1_dignity = dignity(body, sign, deg)
        d9_dignity = dignity(body, d9)
        d10_dignity = dignity(body, d10)

        bodies[body] = {
            "lon": round(lon, 6),
            "speed": round(speed, 6),
            "sign": sign,
            "deg": round(deg, 4),
            "sign_nature": sign_nature(sign),
            "sign_lord": SIGN_LORD[sign],
            "nakshatra": nak_name,
            "nakshatra_index": nak_idx,
            "pada": pada,
            "d9_sign": d9,
            "d10_sign": d10,
            "vargottama": sign == d9,
            "decanate": dec_num,
            "decanate_lord": dec_lord,
            "value_chain_stage": dec_stage,
            "value_chain_character": dec_character,
            "dignity_d1": d1_dignity,
            "dignity_d9": d9_dignity,
            "dignity_d10": d10_dignity,
            "strong_d1": is_strong(d1_dignity),
            "strong_d9": is_strong(d9_dignity),
            "strong_d10": is_strong(d10_dignity),
            "retrograde": retro,
            "combust": is_combust,
            "deg_from_sun": round(sep_from_sun, 4),
            "sub_sector": pada_row.get("sub_sector", ""),
            "sub_sector_names": list(pada_row.get("names", [])),
            "navamsa_lord": pada_row.get("navamsa_lord", ""),
            "boundary": boundary_proximity(lon),
            "slowness_rank": (SLOWNESS_ORDER.index(body)
                              if body in SLOWNESS_ORDER else len(SLOWNESS_ORDER)),
            "malefic": body in MALEFICS,
            "benefic": body in BENEFICS,
        }

    # Cross-check the KB's hand-written navamsa column against the computed one. A mismatch means
    # either the pada table was mis-transcribed or the formula is wrong; either way the engine
    # must not silently produce a divisional verdict from disagreeing sources.
    for body, b in bodies.items():
        kb_nav = PADA_SUBSECTOR.get((b["nakshatra_index"], b["pada"]), {}).get("navamsa_sign")
        if kb_nav and kb_nav != b["d9_sign"]:
            raise AssertionError(
                "navamsa disagreement for %s: computed %s, KB section 25 says %s "
                "(nakshatra_index=%d pada=%d)"
                % (body, b["d9_sign"], kb_nav, b["nakshatra_index"], b["pada"]))

    return {
        "as_of": dt_utc.replace(microsecond=0).isoformat() + ("" if dt_utc.tzinfo else "Z"),
        "ayanamsa": "Lahiri",
        "ayanamsa_deg": round(swe.get_ayanamsa_ut(jd), 6),
        "julian_day": jd,
        "bodies": bodies,
        "aspects": find_aspects(raw),
        "retrogrades": sorted(retrogrades),
        "combust": sorted(combust),
    }


def find_aspects(raw):
    """
    Detect aspects between the seven traditional planets plus the nodes. Outer planets are
    included as aspect *sources* only when they touch a traditional planet, per section 16's
    rule that outer-planet conjunctions with malefics amplify effects.
    """
    names = [b for b in BODIES if b not in ("Uranus", "Neptune", "Pluto")]
    outer = ("Uranus", "Neptune", "Pluto")
    found = []
    pool = names + list(outer)

    for i, a in enumerate(pool):
        for b in pool[i + 1:]:
            if a in outer and b in outer:
                continue  # outer-to-outer aspects carry no mundane weight in the KB
            sep = angular_separation(raw[a]["lon"], raw[b]["lon"])
            for kind, exact, allow in ASPECTS:
                orb = abs(sep - exact)
                if orb <= allow:
                    found.append({
                        "a": a, "b": b, "type": kind,
                        "orb": round(orb, 4),
                        "separation": round(sep, 4),
                        "tight": orb <= 3.0,   # section 30 Stage 1 "within 3 deg orb"
                    })
                    break
    found.sort(key=lambda x: (x["orb"], x["a"], x["b"]))
    return found
