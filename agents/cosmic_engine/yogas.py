"""
yogas.py — geometric detection of the mundane yogas in KB section 29.

Section 29 is the MODULATION layer: yogas are not transits but chart-wide states that bias every
other signal. Detection is pure geometry from longitudes, so it is fully determinable — yet the
model has been asserting the active-yoga list from prose definitions on every run, with no way for
anything downstream to check it.

Three of the twelve yogas (Lakshmi, Daridra, Vipreet Raja) are defined by house lords rather than
by planetary geometry, so they need a lagna. This module uses the India natal lagna (Taurus, per
KB section 28's independence chart reference) and records that in every result's `basis` field.
That is an assumption, not a derivation: a different chart — Aries ingress, eclipse, lunation —
would give different house lords and therefore a different verdict for those three.
"""

from agents.cosmic_engine.chart import BENEFICS, MALEFICS, SIGNS, angular_separation
from agents.cosmic_engine.kb.dasha import INDIA_NATAL
from agents.cosmic_engine.kb.dignity import SIGN_LORD, SLOWNESS_ORDER
from agents.cosmic_engine.kb.yogas import (
    MUTUALLY_EXCLUSIVE, YOGA_ANCHOR, YOGA_DEFINITIONS,
)

# The seven traditional planets. Kala Sarpa and Graha Malika are defined over these only — the
# nodes bound the arc rather than sitting inside it, and outer planets are not classical.
TRADITIONAL = ("Sun", "Moon", "Mercury", "Venus", "Mars", "Jupiter", "Saturn")
KENDRA = (1, 4, 7, 10)
DUSTANA = (6, 8, 12)
CONJUNCTION_ORB = 8.0


def _sign_index(bodies, body):
    return SIGNS.index(bodies[body]["sign"])


def house_from(bodies, body, lagna_sign):
    """House number (1-12) of `body` counted from `lagna_sign`."""
    return (_sign_index(bodies, body) - SIGNS.index(lagna_sign)) % 12 + 1


def house_distance(bodies, body, from_body):
    """House number of `body` counted from `from_body`'s sign (1 = same sign)."""
    return (_sign_index(bodies, body) - _sign_index(bodies, from_body)) % 12 + 1


def lord_of_house(house, lagna_sign):
    """Sign lord of the Nth house from a given lagna."""
    return SIGN_LORD[SIGNS[(SIGNS.index(lagna_sign) + house - 1) % 12]]


def _in_arc(lon, start, end):
    """Is `lon` within the forward arc from `start` to `end` (wrapping at 360)?"""
    span = (end - start) % 360.0
    return (lon - start) % 360.0 <= span


# ---------------------------------------------------------------------------
# Individual detectors. Each returns None or a dict of evidence.
# ---------------------------------------------------------------------------

def _kala_sarpa(bodies):
    rahu, ketu = bodies["Rahu"]["lon"], bodies["Ketu"]["lon"]
    forward = [b for b in TRADITIONAL if _in_arc(bodies[b]["lon"], rahu, ketu)]
    if len(forward) == len(TRADITIONAL):
        return {"evidence": "all 7 traditional planets in the Rahu->Ketu arc"}
    return None


def _kala_amrita(bodies):
    rahu, ketu = bodies["Rahu"]["lon"], bodies["Ketu"]["lon"]
    backward = [b for b in TRADITIONAL if _in_arc(bodies[b]["lon"], ketu, rahu)]
    if len(backward) == len(TRADITIONAL):
        return {"evidence": "all 7 traditional planets in the Ketu->Rahu arc"}
    return None


def _gajakesari(bodies):
    h = house_distance(bodies, "Jupiter", "Moon")
    if h in KENDRA:
        return {"evidence": "Jupiter in house %d from the Moon (kendra)" % h}
    return None


def _kemadruma(bodies):
    """Moon with nothing in its 2nd/12th and nothing in kendra to it."""
    others = [b for b in TRADITIONAL if b != "Moon"]
    occupied = {house_distance(bodies, b, "Moon") for b in others}
    if occupied & {2, 12}:
        return None
    if occupied & set(KENDRA):
        return None
    return {"evidence": "no planet in the 2nd, 12th or any kendra from the Moon"}


def _chandra_mangal(bodies):
    sep = angular_separation(bodies["Moon"]["lon"], bodies["Mars"]["lon"])
    if sep <= CONJUNCTION_ORB:
        return {"evidence": "Moon-Mars conjunction, orb %.2f deg" % sep}
    # Mutual exchange (parivartana): each in the other's sign.
    moon_sign, mars_sign = bodies["Moon"]["sign"], bodies["Mars"]["sign"]
    if SIGN_LORD[moon_sign] == "Mars" and SIGN_LORD[mars_sign] == "Moon":
        return {"evidence": "Moon-Mars mutual sign exchange (%s / %s)" % (moon_sign, mars_sign)}
    return None


def _lakshmi(bodies, lagna):
    """Venus and the 9th lord both strong. Uses D1 dignity from chart.cast()."""
    ninth = lord_of_house(9, lagna)
    if ninth not in bodies:
        return None
    if bodies["Venus"]["strong_d1"] and bodies[ninth]["strong_d1"]:
        return {"evidence": "Venus (%s) and 9th lord %s (%s) both strong"
                            % (bodies["Venus"]["dignity_d1"], ninth, bodies[ninth]["dignity_d1"])}
    return None


def _daridra(bodies, lagna):
    """11th lord weak, or conjunct a malefic, or in a dustana from the lagna."""
    eleventh = lord_of_house(11, lagna)
    if eleventh not in bodies:
        return None
    reasons = []
    if not bodies[eleventh]["strong_d1"]:
        reasons.append("11th lord %s is weak (%s)" % (eleventh, bodies[eleventh]["dignity_d1"]))
    house = house_from(bodies, eleventh, lagna)
    if house in DUSTANA:
        reasons.append("11th lord %s is in the %dth (dustana)" % (eleventh, house))
    for mal in MALEFICS:
        if mal == eleventh:
            continue
        if angular_separation(bodies[eleventh]["lon"], bodies[mal]["lon"]) <= CONJUNCTION_ORB:
            reasons.append("11th lord %s conjunct %s" % (eleventh, mal))
            break
    if reasons:
        return {"evidence": "; ".join(reasons)}
    return None


def _vipreet_raja(bodies, lagna):
    """The 6th/8th/12th lords placed in 6/8/12 from each other."""
    lords = {h: lord_of_house(h, lagna) for h in DUSTANA}
    hits = []
    for h_a, lord_a in lords.items():
        for h_b, lord_b in lords.items():
            if h_a >= h_b or lord_a == lord_b:
                continue
            if lord_a not in bodies or lord_b not in bodies:
                continue
            if house_distance(bodies, lord_a, lord_b) in DUSTANA:
                hits.append("%dth lord %s is in a dustana from the %dth lord %s"
                            % (h_a, lord_a, h_b, lord_b))
    if hits:
        return {"evidence": "; ".join(hits)}
    return None


def _adhi(bodies):
    """Benefics in the 6th, 7th and 8th from the Moon — all three houses must be occupied."""
    occupied = {}
    for b in BENEFICS:
        if b == "Moon":
            continue
        h = house_distance(bodies, b, "Moon")
        if h in (6, 7, 8):
            occupied.setdefault(h, []).append(b)
    if set(occupied) == {6, 7, 8}:
        return {"evidence": "benefics in the 6th/7th/8th from the Moon: %s"
                            % "; ".join("%dth %s" % (h, ",".join(v))
                                        for h, v in sorted(occupied.items()))}
    return None


def _shakata(bodies):
    h = house_distance(bodies, "Moon", "Jupiter")
    if h in (6, 8):
        return {"evidence": "Moon in house %d from Jupiter" % h}
    return None


def _graha_malika(bodies):
    """Four or more planets in successive signs."""
    occupied = sorted({_sign_index(bodies, b) for b in TRADITIONAL})
    best, run = [], []
    for i in range(12):
        idx = (occupied[0] + i) % 12 if occupied else 0
        if idx in occupied:
            run.append(idx)
            if len(run) > len(best):
                best = list(run)
        else:
            run = []
    if len(best) >= 4:
        return {"evidence": "%d planets in successive signs: %s"
                            % (len(best), ", ".join(SIGNS[i] for i in best))}
    return None


def _sunafa_anafa_durdhura(bodies):
    """Planets (excluding the Sun) in the 2nd, the 12th, or both from the Moon."""
    second, twelfth = [], []
    for b in TRADITIONAL:
        if b in ("Moon", "Sun"):
            continue
        h = house_distance(bodies, b, "Moon")
        if h == 2:
            second.append(b)
        elif h == 12:
            twelfth.append(b)
    if second and twelfth:
        return {"evidence": "Durdhura: 2nd %s and 12th %s from the Moon"
                            % (",".join(second), ",".join(twelfth)), "variant": "Durdhura"}
    if second:
        return {"evidence": "Sunafa: %s in the 2nd from the Moon" % ",".join(second),
                "variant": "Sunafa"}
    if twelfth:
        return {"evidence": "Anafa: %s in the 12th from the Moon" % ",".join(twelfth),
                "variant": "Anafa"}
    return None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect(chart, lagna_sign=None):
    """
    Detect all active yogas in a cast chart.

    Returns a list of JSON-serializable dicts, each:
        {"name", "status", "definition", "effect", "modulation", "direction",
         "factors": {target: multiplier}, "anchor", "evidence", "basis"}

    `basis` is 'geometry' for the nine geometric yogas and 'house lords from <lagna> lagna' for
    the three that need a chart, so a reader can tell which verdicts rest on the lagna assumption.
    """
    lagna_sign = lagna_sign or INDIA_NATAL["lagna"]
    bodies = chart["bodies"]

    geometric = {
        "Kala Sarpa": _kala_sarpa,
        "Kala Amrita": _kala_amrita,
        "Gajakesari": _gajakesari,
        "Kemadruma": _kemadruma,
        "Chandra-Mangal": _chandra_mangal,
        "Adhi": _adhi,
        "Shakata": _shakata,
        "Graha Malika": _graha_malika,
        "Sunafa / Anafa / Durdhura": _sunafa_anafa_durdhura,
    }
    house_based = {
        "Lakshmi": _lakshmi,
        "Daridra": _daridra,
        "Vipreet Raja": _vipreet_raja,
    }

    found = []
    for name, fn in geometric.items():
        hit = fn(bodies)
        if hit:
            found.append((name, hit, "geometry"))
    for name, fn in house_based.items():
        hit = fn(bodies, lagna_sign)
        if hit:
            found.append((name, hit, "house lords from %s lagna" % lagna_sign))

    for a, b in MUTUALLY_EXCLUSIVE:
        names = {n for n, _, _ in found}
        if a in names and b in names:
            raise AssertionError(
                "yoga detection bug: %s and %s are mutually exclusive states of the same "
                "condition but both were detected" % (a, b))

    out = []
    for name, hit, basis in found:
        meta = YOGA_DEFINITIONS[name]
        anchor = YOGA_ANCHOR[name]
        entry = {
            "name": name,
            "status": "ACTIVE",
            "definition": meta["definition"],
            "effect": meta["effect"],
            "modulation": meta["modulation"],
            "direction": meta["direction"],
            "factors": dict(meta["factors"]),
            "anchor": anchor,
            "anchor_slowness": (SLOWNESS_ORDER.index(anchor)
                                if anchor in SLOWNESS_ORDER else len(SLOWNESS_ORDER)),
            "basis": basis,
            "evidence": hit["evidence"],
        }
        if "variant" in hit:
            entry["variant"] = hit["variant"]
        out.append(entry)

    # Slowest anchor first: section 29's conflict rule is "the yoga of the slower-moving planet
    # wins", so presenting them in that order makes the precedence visible without extra logic.
    out.sort(key=lambda y: (y["anchor_slowness"], y["name"]))
    return out


def net_factor(yogas, signal_class):
    """
    Multiplicative net modulation for a signal of class 'benefic' or 'malefic', per KB section 30
    Stage 5 ("stacking allowed: multiple yogas combine multiplicatively").

    Only factors that actually govern this signal class apply; a yoga that amplifies benefics
    leaves a malefic-driven signal untouched. Returns (factor, [contributing yoga names]).
    """
    factor, contributors = 1.0, []
    for y in yogas:
        f = y["factors"].get(signal_class, y["factors"].get("all"))
        if f is None or f == 1.0:
            continue
        factor *= f
        contributors.append(y["name"])
    return factor, contributors
