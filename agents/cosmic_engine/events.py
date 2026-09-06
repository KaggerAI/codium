"""
events.py — the deterministic trigger calendar.

Replaces the model's guesswork about *when* things happen. Today `trigger_calendar[].date` values
come from the model reading Perplexity prose or its own training memory; nothing verifies them.
Sign ingresses, retrograde stations, eclipses and lunations are all exactly computable, so this
module computes them and KB sections 5, 6, 8, 10 and 11 stop being prompt text.

Each event carries its KB section 22 tier and its section 24 recency multiplier, so the calendar
arrives pre-weighted for Stage 1 and Stage 6.

Scope choices worth knowing:
  * Moon sign changes are NOT scanned. The Moon changes sign every ~2.3 days, which would put ~78
    Tier 4 entries in a 180-day calendar and drown the Tier 1/2 signals. The Moon's contribution
    reaches the calendar as lunations (section 10) instead.
  * Nakshatra changes are scanned only for the slow bodies. For fast movers they are noise at the
    same scale as the Moon problem.
"""

import datetime

import swisseph as swe

from agents.cosmic_engine.chart import (
    NAKSHATRAS, NAK_SPAN, SIGNS, angular_separation, julian_day,
)
from agents.cosmic_engine.kb.matrix import (
    ECLIPSE_EQUAL_WEIGHT_DAYS, ECLIPSE_OVERRIDE_DAYS, ECLIPSE_UPGRADE_DAYS,
)
from agents.cosmic_engine.kb.tiers import SLOW_BODIES, recency_multiplier, tier_for_event

DEFAULT_WINDOW_DAYS = 180  # the KB's six-month forecasting horizon

# Bodies scanned for sign changes. The Moon is excluded (see module docstring).
INGRESS_BODIES = ("Sun", "Mercury", "Venus", "Mars", "Jupiter", "Saturn",
                  "Rahu", "Ketu", "Uranus", "Neptune", "Pluto")
# Bodies that can station. The Sun, Moon and nodes never do (the nodes are always retrograde in
# mean motion, which the KB does not treat as an event).
STATION_BODIES = ("Mercury", "Venus", "Mars", "Jupiter", "Saturn", "Uranus", "Neptune", "Pluto")
NAKSHATRA_SCAN_BODIES = SLOW_BODIES

_SWE_ID = {
    "Sun": swe.SUN, "Moon": swe.MOON, "Mercury": swe.MERCURY, "Venus": swe.VENUS,
    "Mars": swe.MARS, "Jupiter": swe.JUPITER, "Saturn": swe.SATURN,
    "Rahu": swe.TRUE_NODE, "Ketu": swe.TRUE_NODE,
    "Uranus": swe.URANUS, "Neptune": swe.NEPTUNE, "Pluto": swe.PLUTO,
}
_FLAGS = swe.FLG_SWIEPH | swe.FLG_SIDEREAL | swe.FLG_SPEED

# Eclipse type bits, richest first so the most specific label wins.
_ECLIPSE_TYPES = (
    (swe.ECL_ANNULAR_TOTAL, "annular-total"),
    (swe.ECL_TOTAL, "total"),
    (swe.ECL_ANNULAR, "annular"),
    (swe.ECL_PARTIAL, "partial"),
    (swe.ECL_PENUMBRAL, "penumbral"),
)

# KB section 5: "Solar eclipse duration (hours) | Effects last equal number of months/years";
# "Lunar eclipse duration (hours) | Effects last equal number of months".
# KB section 24 restates the ratios as "1 hr eclipse ~ 1 yr effect" (solar) and "1 hr ~ 1 month"
# (lunar), and additionally brackets total effect duration at 6-12 months (solar) / 1-3 months
# (lunar).
_EFFECT_MONTHS_PER_HOUR = {"solar": 12.0, "lunar": 1.0}
_KB_EFFECT_BRACKET_MONTHS = {"solar": (6.0, 12.0), "lunar": (1.0, 3.0)}

# Duration is measured for an OBSERVER AT NEW DELHI, not globally.
#
# The rule comes from a naked-eye tradition: an observer could only ever time the eclipse they
# could actually see, never the moment a shadow touched the far side of the planet. Global duration
# (what sol_eclipse_when_glob reports) runs several times longer and drives the section 5 rule to
# absurd outputs - the Feb 2027 annular eclipse spans 6.1 h globally, which the ratio turns into
# ~73 months of market effect.
#
# The lunar numbers corroborate the choice: locally-visible lunar eclipses run ~1-3.5 h, which the
# 1-hour-per-month ratio maps onto section 24's own stated 1-3 month bracket. The global figures
# would overshoot that bracket badly.
#
# Two honest caveats, both surfaced in the data rather than hidden:
#   * The SOLAR side of the rule is internally inconsistent in the source. Even local durations
#     (~1-3 h) exceed section 24's 6-12 month bracket, and section 5 hedges with "months/years"
#     rather than committing. No choice of duration reconciles it, so every eclipse reports both
#     the computed figure and the KB bracket plus whether they agree.
#   * An eclipse can be invisible from New Delhi (the Feb 2027 annular is). Those have no local
#     duration, so effect_months is None rather than invented. The eclipse itself is still reported
#     because sections 17 and 18 key their effects on the eclipse's zodiac SIGN, which does not
#     depend on visibility.
DELHI_GEOPOS = (77.2, 28.6, 216.0)
OBSERVER_LABEL = "New Delhi"


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

def _lon_speed(jd, body):
    swe.set_sid_mode(swe.SIDM_LAHIRI)
    res = swe.calc_ut(jd, _SWE_ID[body], _FLAGS)[0]
    lon, speed = res[0], res[3]
    if body == "Ketu":
        lon = (lon + 180.0) % 360.0
    return lon % 360.0, speed


def _jd_iso(jd):
    """Julian day -> 'YYYY-MM-DD'. Dates only; the KB never keys anything on time of day."""
    y, m, d, _h = swe.revjul(jd)
    return "%04d-%02d-%02d" % (y, m, d)


def _jd_iso_minutes(jd):
    """Julian day -> 'YYYY-MM-DDTHH:MMZ', for events whose exact moment matters."""
    y, m, d, h = swe.revjul(jd)
    hh = int(h)
    mm = int(round((h - hh) * 60))
    if mm == 60:
        hh, mm = hh + 1, 0
    return "%04d-%02d-%02dT%02d:%02dZ" % (y, m, d, hh % 24, mm)


def _bisect(predicate, jd_lo, jd_hi, iterations=44):
    """
    Find the jd where `predicate` flips value between jd_lo and jd_hi.

    44 halvings of a one-day bracket resolve to well under a second, which is far finer than any
    KB rule needs — but exactness here is free and makes the boundary reproducible.
    """
    base = predicate(jd_lo)
    for _ in range(iterations):
        mid = (jd_lo + jd_hi) / 2.0
        if predicate(mid) == base:
            jd_lo = mid
        else:
            jd_hi = mid
    return jd_hi


def _sign_index(jd, body):
    return int(_lon_speed(jd, body)[0] / 30.0)


def _nak_index(jd, body):
    return int(_lon_speed(jd, body)[0] / NAK_SPAN)


# ---------------------------------------------------------------------------
# Scanners
# ---------------------------------------------------------------------------

def _scan_sign_changes(jd_start, jd_end):
    out = []
    for body in INGRESS_BODIES:
        jd = jd_start
        prev_idx = _sign_index(jd, body)
        while jd < jd_end:
            nxt = min(jd + 1.0, jd_end)
            idx = _sign_index(nxt, body)
            if idx != prev_idx:
                exact = _bisect(lambda t, b=body: _sign_index(t, b) == prev_idx, jd, nxt)
                out.append({
                    "type": "sign_change",
                    "body": body,
                    "jd": exact,
                    "from_sign": SIGNS[prev_idx],
                    "to_sign": SIGNS[idx],
                    "retrograde_entry": _lon_speed(exact, body)[1] < 0,
                    "detail": "%s enters %s" % (body, SIGNS[idx]),
                })
                prev_idx = idx
            jd = nxt
    return out


def _scan_nakshatra_changes(jd_start, jd_end):
    out = []
    for body in NAKSHATRA_SCAN_BODIES:
        jd = jd_start
        prev_idx = _nak_index(jd, body)
        while jd < jd_end:
            nxt = min(jd + 1.0, jd_end)
            idx = _nak_index(nxt, body)
            if idx != prev_idx:
                exact = _bisect(lambda t, b=body: _nak_index(t, b) == prev_idx, jd, nxt)
                out.append({
                    "type": "nakshatra_change",
                    "body": body,
                    "jd": exact,
                    "from_nakshatra": NAKSHATRAS[prev_idx],
                    "to_nakshatra": NAKSHATRAS[idx],
                    "detail": "%s enters %s" % (body, NAKSHATRAS[idx]),
                })
                prev_idx = idx
            jd = nxt
    return out


def _scan_stations(jd_start, jd_end):
    out = []
    for body in STATION_BODIES:
        jd = jd_start
        prev_dir = _lon_speed(jd, body)[1] >= 0
        while jd < jd_end:
            nxt = min(jd + 1.0, jd_end)
            direct = _lon_speed(nxt, body)[1] >= 0
            if direct != prev_dir:
                exact = _bisect(lambda t, b=body: (_lon_speed(t, b)[1] >= 0) == prev_dir, jd, nxt)
                lon = _lon_speed(exact, body)[0]
                sign = SIGNS[int(lon / 30.0)]
                going = "direct" if direct else "retrograde"
                out.append({
                    "type": "station",
                    "body": body,
                    "jd": exact,
                    "direction": going,
                    "sign": sign,
                    "deg": round(lon % 30.0, 4),
                    "detail": "%s stations %s at %s %.2f deg" % (body, going, sign, lon % 30.0),
                })
                prev_dir = direct
            jd = nxt
    return out


def _scan_lunations(jd_start, jd_end):
    """New and full moons from the Sun-Moon elongation crossing 0 and 180 degrees."""
    def elong(jd):
        return (_lon_speed(jd, "Moon")[0] - _lon_speed(jd, "Sun")[0]) % 360.0

    out = []
    jd = jd_start
    prev = elong(jd)
    while jd < jd_end:
        nxt = min(jd + 1.0, jd_end)
        cur = elong(nxt)
        kind = None
        if cur < prev:                      # wrapped through 360 -> 0
            kind, target = "new_moon", 0.0
        elif prev < 180.0 <= cur:
            kind, target = "full_moon", 180.0
        if kind:
            if target == 0.0:
                exact = _bisect(lambda t: elong(t) > 180.0, jd, nxt)
            else:
                exact = _bisect(lambda t: elong(t) < 180.0, jd, nxt)
            lon = _lon_speed(exact, "Moon")[0]
            nak_idx = int(lon / NAK_SPAN)
            out.append({
                "type": "lunation",
                "body": "Moon",
                "jd": exact,
                "kind": kind,
                "sign": SIGNS[int(lon / 30.0)],
                "nakshatra": NAKSHATRAS[nak_idx],
                "detail": "%s in %s (%s)" % (
                    "Amavasya / new moon" if kind == "new_moon" else "Poornima / full moon",
                    SIGNS[int(lon / 30.0)], NAKSHATRAS[nak_idx]),
            })
        jd, prev = nxt, cur
    return out


def _eclipse_type(retflag):
    for bit, label in _ECLIPSE_TYPES:
        if retflag & bit:
            return label
    return "unknown"


def _node_polarity(jd, eclipse_lon):
    """
    KB section 5 distinguishes north-nodal (Rahu) from south-nodal (Ketu) eclipses, the former
    "usually favourable when well-aspected" and the latter "usually unfavourable". Decided by
    which node the eclipse point sits nearer.
    """
    rahu, _ = _lon_speed(jd, "Rahu")
    ketu = (rahu + 180.0) % 360.0
    d_rahu = angular_separation(eclipse_lon, rahu)
    d_ketu = angular_separation(eclipse_lon, ketu)
    return ("Rahu", round(d_rahu, 3)) if d_rahu <= d_ketu else ("Ketu", round(d_ketu, 3))


def _local_circumstances(kind, peak_jd, geopos=DELHI_GEOPOS):
    """
    Local eclipse circumstances for an observer, for the globally-detected eclipse peaking at
    `peak_jd`.

    Returns {"visible", "duration_hours", "magnitude", "phase"} — duration_hours is None when the
    eclipse is not visible from the location.

    Detection stays global so the calendar lists every eclipse; only the DURATION is localised.
    swe.*_eclipse_when_loc returns the next eclipse *visible at the location*, so if what it finds
    is more than a day away from our peak, this eclipse simply is not visible here.
    """
    fn = swe.sol_eclipse_when_loc if kind == "solar" else swe.lun_eclipse_when_loc
    try:
        result = fn(peak_jd - 2.0, geopos, swe.FLG_SWIEPH, False)
    except Exception:
        return {"visible": False, "duration_hours": None, "magnitude": None, "phase": None}

    retflag, tret = result[0], result[1]
    attr = result[2] if len(result) > 2 else None
    if not tret or not tret[0] or abs(tret[0] - peak_jd) > 1.0:
        return {"visible": False, "duration_hours": None, "magnitude": None, "phase": None}

    if kind == "solar":
        # tret[1] = first contact at the location, tret[4] = last contact.
        begin, end, phase = tret[1], tret[4], "first-to-last contact"
    else:
        # Prefer the partial phase (what an observer actually sees the Moon bitten during);
        # fall back to penumbral for a penumbral-only eclipse, which is otherwise zero-length.
        if tret[2] and tret[3]:
            begin, end, phase = tret[2], tret[3], "partial phase"
        else:
            begin, end, phase = tret[6], tret[7], "penumbral phase"

    duration = (end - begin) * 24.0 if (begin and end and end > begin) else None
    magnitude = round(attr[0], 4) if attr else None
    return {
        "visible": True,
        "duration_hours": round(duration, 3) if duration else None,
        "magnitude": magnitude,
        "phase": phase,
    }


def eclipses(jd_start, jd_end):
    """
    Solar and lunar eclipses in the window, via swe.sol_eclipse_when_glob / lun_eclipse_when.

    tret[0] is the moment of maximum; tret[2]/tret[3] bracket the partial phase globally. For a
    penumbral-only lunar eclipse the partial entries are zero, so the penumbral bracket
    (tret[6]/tret[7]) is used instead — otherwise duration would come out as zero and the section
    5 effect-duration rule would silently produce nothing.
    """
    out = []
    for kind, fn in (("solar", swe.sol_eclipse_when_glob), ("lunar", swe.lun_eclipse_when)):
        jd = jd_start
        for _ in range(60):  # generous bound; ~4-7 eclipses per year of each kind at most
            retflag, tret = fn(jd, swe.FLG_SWIEPH, 0, False)
            peak = tret[0]
            if not peak or peak > jd_end:
                break
            begin, end = tret[2], tret[3]
            if not begin or not end:
                begin, end = tret[6], tret[7]
            global_h = max(0.0, (end - begin) * 24.0) if (begin and end) else 0.0

            # Section 17 keys solar effects by the eclipse sign (the Sun's); section 18 keys lunar
            # effects by the Moon's sign. Use the luminary that is being eclipsed.
            luminary = "Sun" if kind == "solar" else "Moon"
            lon, _ = _lon_speed(peak, luminary)
            nak_idx = int(lon / NAK_SPAN)
            node, node_orb = _node_polarity(peak, lon)
            local = _local_circumstances(kind, peak)

            # The section 5 duration rule runs on the LOCAL figure; see the module notes.
            local_h = local["duration_hours"]
            if local_h:
                effect_months = round(local_h * _EFFECT_MONTHS_PER_HOUR[kind], 1)
                basis = "local (%s), %s" % (OBSERVER_LABEL, local["phase"])
            else:
                effect_months = None
                basis = ("not visible from %s - no local duration, so no effect period is claimed"
                         % OBSERVER_LABEL)

            low, high = _KB_EFFECT_BRACKET_MONTHS[kind]
            within = None if effect_months is None else bool(low <= effect_months <= high)

            detail = "%s %s eclipse in %s (%s), %s-nodal" % (
                _eclipse_type(retflag), kind, SIGNS[int(lon / 30.0)], NAKSHATRAS[nak_idx], node)
            detail += (", %.1fh local" % local_h) if local_h else (", not visible from %s"
                                                                   % OBSERVER_LABEL)

            out.append({
                "type": "eclipse",
                "body": luminary,
                "jd": peak,
                "kind": kind,
                "eclipse_type": _eclipse_type(retflag),
                "sign": SIGNS[int(lon / 30.0)],
                "nakshatra": NAKSHATRAS[nak_idx],
                "node": node,
                "node_orb": node_orb,
                "visible_locally": local["visible"],
                "observer": OBSERVER_LABEL,
                "magnitude_local": local["magnitude"],
                "duration_hours": local_h,
                "duration_hours_global": round(global_h, 3),
                "duration_basis": basis,
                "effect_months": effect_months,
                # Section 24 states a total-duration bracket alongside the ratio, and the two
                # disagree for solar eclipses at any duration. Both are reported so the
                # inconsistency is visible in the output instead of being silently resolved.
                "effect_months_kb_bracket": [low, high],
                "effect_months_within_kb_bracket": within,
                "detail": detail,
            })
            jd = peak + 1.0
    return out


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

def scan(start_dt, days=DEFAULT_WINDOW_DAYS):
    """
    Full trigger calendar for [start_dt, start_dt + days].

    Returns a chronologically sorted list of JSON-serializable events, each carrying:
        type, body, date, exact (UTC minutes), days_from_start, tier, decay, detail
    plus type-specific fields.

    `tier` is KB section 22; `decay` is the section 24 recency multiplier from `start_dt`.
    """
    jd_start = julian_day(start_dt)
    jd_end = jd_start + float(days)

    events = []
    events.extend(_scan_sign_changes(jd_start, jd_end))
    events.extend(_scan_nakshatra_changes(jd_start, jd_end))
    events.extend(_scan_stations(jd_start, jd_end))
    events.extend(_scan_lunations(jd_start, jd_end))
    events.extend(eclipses(jd_start, jd_end))

    for e in events:
        days_out = e["jd"] - jd_start
        e["date"] = _jd_iso(e["jd"])
        e["exact"] = _jd_iso_minutes(e["jd"])
        e["days_from_start"] = int(days_out)
        e["tier"] = tier_for_event(e["type"], e.get("body"))
        e["decay"] = recency_multiplier(int(days_out))
        if e["type"] == "eclipse":
            e["window"] = eclipse_window(int(days_out))
        e["jd"] = round(e["jd"], 8)

    events.sort(key=lambda e: (e["jd"], e["type"], e.get("body") or ""))
    return events


def eclipse_window(days_away):
    """
    KB section 30 Stage 2 classification for an eclipse `days_away` days out:
        override      within +/-15 days - eclipse rules override all Stage 1 signals
        upgrade       within +/-30 days - eclipse signals upgrade by one tier
        equal_weight  within +/-90 days - equal-weighted with other Tier 1
        distant       beyond
    """
    d = abs(days_away)
    if d <= ECLIPSE_OVERRIDE_DAYS:
        return "override"
    if d <= ECLIPSE_UPGRADE_DAYS:
        return "upgrade"
    if d <= ECLIPSE_EQUAL_WEIGHT_DAYS:
        return "equal_weight"
    return "distant"


def sun_ingress_moments(jd_start, jd_end, signs=None):
    """
    Exact moments the Sun enters each sign within [jd_start, jd_end). Used by samvatsar.py, which
    keys the section 4 cabinet on the weekday of ten specific solar moments.

    Deliberately range-based rather than year-based: the samvatsar year runs Chaitra Shukla
    Pratipada to Pratipada, so a calendar year mixes ingresses from two different cabinets. Scanning
    a calendar year would attribute the January Capricorn and March Pisces ingresses to the wrong
    samvatsar entirely.

    Returns {sign_name: jd}, first occurrence wins.
    """
    wanted = set(signs or SIGNS)
    jd = jd_start
    out = {}
    prev_idx = _sign_index(jd, "Sun")
    while jd < jd_end:
        nxt = min(jd + 1.0, jd_end)
        idx = _sign_index(nxt, "Sun")
        if idx != prev_idx:
            exact = _bisect(lambda t: _sign_index(t, "Sun") == prev_idx, jd, nxt)
            if SIGNS[idx] in wanted and SIGNS[idx] not in out:
                out[SIGNS[idx]] = exact
            prev_idx = idx
        jd = nxt
    return out


def sun_nakshatra_moment(jd_start, jd_end, nakshatra):
    """Exact moment the Sun enters `nakshatra` within [jd_start, jd_end), or None."""
    target = NAKSHATRAS.index(nakshatra)
    jd = jd_start
    prev_idx = _nak_index(jd, "Sun")
    while jd < jd_end:
        nxt = min(jd + 1.0, jd_end)
        idx = _nak_index(nxt, "Sun")
        if idx != prev_idx:
            if idx == target:
                return _bisect(lambda t: _nak_index(t, "Sun") == prev_idx, jd, nxt)
            prev_idx = idx
        jd = nxt
    return None


def new_moon_before(jd):
    """
    Most recent new moon at or before `jd`. Needed for the Chaitra Shukla Pratipada rule in
    samvatsar.py. Searches backward a maximum of 32 days, which comfortably exceeds one synodic
    month (29.53 days).
    """
    def elong(t):
        return (_lon_speed(t, "Moon")[0] - _lon_speed(t, "Sun")[0]) % 360.0

    t = jd
    for _ in range(32):
        prev = t - 1.0
        if elong(prev) > elong(t):          # a new moon lies between prev and t
            return _bisect(lambda x: elong(x) > 180.0, prev, t)
        t = prev
    return None
