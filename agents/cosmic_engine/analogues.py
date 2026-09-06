"""
analogues.py — historical analogue finder.

KB section 30 Stage 7 demands an analogue for every HIGH CONFIDENCE call. Until now the model
supplied both halves from memory: the date and the outcome, neither checkable. This module splits
them so each comes from a source that can be verified.

    THE DATE is computed. The ephemeris is scanned backwards for the last time the configuration
    actually held — Saturn in Pisces, a Saturn-Mars conjunction in Capricorn, the previous Mars
    mahadasha. Every returned date can be checked against any ephemeris.

    THE OUTCOME comes from kb/history.py, a short table of documented macro events. If no event
    overlaps the computed range, the analogue reports its dates with no outcome rather than
    inventing one.

No causal claim is expressed anywhere. An analogue says "this configuration last held during
1994-1996" and, where the table has something, "which was the period of X". Whether that matters is
the reader's judgment, and the narration layer's to phrase.

Search cost is bounded deliberately. Slow bodies only, coarse stepping matched to each body's
period, and a hard year limit — a full daily back-scan over a century for every body would cost
seconds per report for no added precision, since the KB never keys anything on the exact day of a
historical recurrence.
"""

import swisseph as swe

from agents.cosmic_engine.chart import SIGNS, angular_separation
from agents.cosmic_engine.kb.history import coverage, events_overlapping

# Bodies whose return to a sign is rare enough to be an analogue. Mars is excluded here despite
# appearing in the KB's example: it re-enters a sign every ~2 years, so "Mars in Gemini last occurred
# in 2024" is a calendar fact, not a historical parallel. It still participates in ASPECT analogues,
# which is the form the KB's own example actually takes ("Saturn-Mars conjunction in Capricorn").
ANALOGUE_BODIES = ("Saturn", "Jupiter", "Rahu", "Ketu")

# Bodies slow enough to appear on either side of an aspect analogue. An aspect recurs on the period
# of the FASTER body, so a Moon-Ketu conjunction repeats monthly and carries no analogue value even
# though Ketu is slow.
ASPECT_BODIES = ("Saturn", "Jupiter", "Rahu", "Ketu", "Mars", "Uranus", "Neptune", "Pluto")

# Pairs that are permanently in aspect, so their "recurrence" is meaningless. Rahu and Ketu are
# always exactly opposed by definition.
DEGENERATE_PAIRS = (frozenset(("Rahu", "Ketu")),)

# An analogue must be genuinely historical. Anything inside this window is the current occurrence
# leaking through, or a recurrence too recent to be a parallel.
MIN_YEARS_AGO = 1

# Sidereal period in years, used to size the backward step. Half a sign's transit time is fine:
# large enough to keep the scan cheap, small enough never to step over an occupancy interval.
_PERIOD_YEARS = {
    "Saturn": 29.46, "Jupiter": 11.86, "Rahu": 18.6, "Ketu": 18.6, "Mars": 1.88,
}
YEAR_DAYS = 365.2425
MAX_BACK_YEARS = 120           # one Vimshottari cycle; also roughly the table's coverage
MAX_OCCURRENCES = 2            # the KB asks for "at least ONE"; two gives a sense of periodicity

_SWE_ID = {
    "Sun": swe.SUN, "Moon": swe.MOON, "Mercury": swe.MERCURY, "Venus": swe.VENUS,
    "Mars": swe.MARS, "Jupiter": swe.JUPITER, "Saturn": swe.SATURN,
    "Rahu": swe.TRUE_NODE, "Ketu": swe.TRUE_NODE,
    "Uranus": swe.URANUS, "Neptune": swe.NEPTUNE, "Pluto": swe.PLUTO,
}
_FLAGS = swe.FLG_SWIEPH | swe.FLG_SIDEREAL


def _lon(jd, body):
    swe.set_sid_mode(swe.SIDM_LAHIRI)
    value = swe.calc_ut(jd, _SWE_ID[body], _FLAGS)[0][0]
    if body == "Ketu":
        value = (value + 180.0) % 360.0
    return value % 360.0


def _sign_of(jd, body):
    return SIGNS[int(_lon(jd, body) / 30.0)]


def _year_of(jd):
    return swe.revjul(jd)[0]


def _iso(jd):
    y, m, d, _h = swe.revjul(jd)
    return "%04d-%02d-%02d" % (y, m, d)


def _refine_boundary(predicate, jd_true, jd_false, iterations=24):
    """Bisect between a jd where `predicate` holds and one where it does not."""
    for _ in range(iterations):
        mid = (jd_true + jd_false) / 2.0
        if predicate(mid):
            jd_true = mid
        else:
            jd_false = mid
    return jd_true


def _scan_back(predicate, from_jd, step_days, max_back_years=MAX_BACK_YEARS,
               max_occurrences=MAX_OCCURRENCES):
    """
    Walk backwards from `from_jd` collecting intervals where `predicate(jd)` holds.

    Skips the interval containing `from_jd` itself — the caller wants the PREVIOUS occurrence, not
    the current one. Returns a list of (start_jd, end_jd), most recent first.
    """
    limit_jd = from_jd - max_back_years * YEAR_DAYS
    jd = from_jd
    intervals = []
    in_current = predicate(jd)

    while jd > limit_jd and len(intervals) < max_occurrences:
        nxt = jd - step_days
        holds = predicate(nxt)
        if in_current and not holds:
            in_current = False          # walked out of the interval we started inside
        elif not in_current and holds:
            # Found the end of an earlier interval; refine both edges.
            end = _refine_boundary(predicate, nxt, jd)
            probe = nxt
            while probe > limit_jd and predicate(probe - step_days):
                probe -= step_days
            start = _refine_boundary(predicate, probe, probe - step_days)
            intervals.append((start, end))
            jd = start - step_days
            continue
        jd = nxt
    return intervals


def _historical_only(entries):
    """
    Drop anything too recent to be a historical parallel.

    Needed because a backward scan that starts inside an occurrence can re-detect the tail of that
    same occurrence — which produced a "Ketu-Moon conjunction in Leo, 0 years ago" entry, i.e. the
    present dressed up as a precedent.
    """
    return [e for e in entries if e["years_ago"] >= MIN_YEARS_AGO]


def _describe(start_jd, end_jd, from_jd, label):
    start_year, end_year = _year_of(start_jd), _year_of(end_jd)
    return {
        "configuration": label,
        "start": _iso(start_jd),
        "end": _iso(end_jd),
        "years": ("%d" % start_year) if start_year == end_year
                 else ("%d-%d" % (start_year, end_year)),
        "years_ago": int(round((from_jd - end_jd) / YEAR_DAYS)),
        "date_source": "computed from the ephemeris",
        "events": events_overlapping(start_year, end_year),
    }


# ---------------------------------------------------------------------------
# Finders
# ---------------------------------------------------------------------------

def body_in_sign(body, sign, from_dt, max_occurrences=MAX_OCCURRENCES):
    """Previous periods when `body` occupied `sign`."""
    if body not in ANALOGUE_BODIES or body not in _PERIOD_YEARS:
        return []
    from_jd = swe.julday(from_dt.year, from_dt.month, from_dt.day, 0.0)
    # Step at a fraction of the time the body spends in one sign, so an occupancy is never skipped.
    step = max(2.0, _PERIOD_YEARS[body] * YEAR_DAYS / 12.0 / 4.0)
    label = "%s in %s" % (body, sign)
    intervals = _scan_back(lambda jd: _sign_of(jd, body) == sign, from_jd, step,
                           max_occurrences=max_occurrences)
    return _historical_only([_describe(s, e, from_jd, label) for s, e in intervals])


def aspect_in_sign(body_a, body_b, aspect_degrees, sign_a, from_dt, orb=8.0,
                   max_occurrences=1):
    """
    Previous occurrences of an aspect between two bodies while the first occupied a given sign —
    the shape of the KB's own example ("Saturn-Mars conjunction in Capricorn").
    """
    if body_a not in ASPECT_BODIES or body_b not in ASPECT_BODIES:
        return []
    if frozenset((body_a, body_b)) in DEGENERATE_PAIRS:
        return []
    from_jd = swe.julday(from_dt.year, from_dt.month, from_dt.day, 0.0)

    def holds(jd):
        if _sign_of(jd, body_a) != sign_a:
            return False
        sep = angular_separation(_lon(jd, body_a), _lon(jd, body_b))
        return abs(sep - aspect_degrees) <= orb

    name = {0.0: "conjunction", 180.0: "opposition",
            90.0: "square", 120.0: "trine"}.get(aspect_degrees, "%.0f deg aspect" % aspect_degrees)
    label = "%s-%s %s in %s" % (body_a, body_b, name, sign_a)
    # Step small enough for the faster of the pair to stay inside orb between samples.
    step = 3.0
    intervals = _scan_back(holds, from_jd, step, max_occurrences=max_occurrences)
    return _historical_only([_describe(s, e, from_jd, label) for s, e in intervals])


def eclipse_in_sign(kind, sign, from_dt, max_occurrences=1):
    """
    The previous eclipse of the same kind in the same zodiac sign.

    Needed because eclipse-driven signals take the eclipsed luminary as their driver, and the Sun and
    Moon are far too fast for a sign-occupancy analogue. "When did a lunar eclipse last fall in
    Aquarius" is the question that actually has analogue value, and swisseph answers it directly by
    searching backwards. Eclipses recur in a given sign roughly every 19 years, so this reaches two
    or three genuine precedents inside the 120-year limit.
    """
    fn = swe.sol_eclipse_when_glob if kind == "solar" else swe.lun_eclipse_when
    luminary = "Sun" if kind == "solar" else "Moon"
    from_jd = swe.julday(from_dt.year, from_dt.month, from_dt.day, 0.0)
    limit_jd = from_jd - MAX_BACK_YEARS * YEAR_DAYS

    out = []
    jd = from_jd
    for _ in range(400):        # generous: ~2 eclipses of each kind per year over 120 years
        if jd <= limit_jd or len(out) >= max_occurrences:
            break
        try:
            _retflag, tret = fn(jd, swe.FLG_SWIEPH, 0, True)   # backwards=True
        except Exception:
            break
        peak = tret[0]
        if not peak or peak <= limit_jd:
            break
        jd = peak - 1.0
        if _sign_of(peak, luminary) != sign:
            continue
        entry = _describe(peak, peak, from_jd, "%s eclipse in %s" % (kind, sign))
        if entry["years_ago"] >= MIN_YEARS_AGO:
            out.append(entry)
    return out


def dasha_analogue(md_lord, from_dt, natal=None):
    """
    The previous mahadasha under the same lord. Cheap — no ephemeris scan, just the Vimshottari
    chain walked backwards, so this works even for lords whose last period predates the event table.
    """
    from agents.cosmic_engine import dasha as dasha_mod

    chain = dasha_mod.mahadashas(natal)
    from_jd = swe.julday(from_dt.year, from_dt.month, from_dt.day, 0.0)
    earlier = [p for p in chain if p["lord"] == md_lord and p["end_jd"] < from_jd]
    if not earlier:
        return None
    period = earlier[-1]
    out = _describe(period["start_jd"], period["end_jd"], from_jd,
                    "%s mahadasha" % md_lord)
    out["date_source"] = "computed from the Vimshottari chain"
    return out


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

def build(state, from_dt):
    """
    Analogues for the current chart. Returns a dict keyed by configuration label.

    Covers the slow bodies in their current signs, tight aspects between slow bodies, and the
    previous mahadasha. Bounded to a handful of lookups so a report never waits on this.
    """
    out = {}
    bodies = state["chart"]["bodies"]

    for body in ANALOGUE_BODIES:
        b = bodies.get(body)
        if not b:
            continue
        for entry in body_in_sign(body, b["sign"], from_dt):
            out[entry["configuration"]] = entry

    # Tight aspects only, and only where at least one side is a slow body — a Mercury-Venus square
    # recurs constantly and tells a reader nothing.
    for asp in state["chart"].get("aspects", []):
        if not asp.get("tight"):
            continue
        a, b_ = asp["a"], asp["b"]
        if a not in ASPECT_BODIES or b_ not in ASPECT_BODIES:
            continue
        if frozenset((a, b_)) in DEGENERATE_PAIRS:
            continue
        degrees = {"conjunction": 0.0, "opposition": 180.0,
                   "square": 90.0, "trine": 120.0}.get(asp["type"])
        if degrees is None:
            continue
        slow, other = (a, b_) if _PERIOD_YEARS.get(a, 0) >= _PERIOD_YEARS.get(b_, 0) else (b_, a)
        found = aspect_in_sign(slow, other, degrees, bodies[slow]["sign"], from_dt)
        for entry in found:
            out[entry["configuration"]] = entry

    # Eclipse recurrences, one per distinct (kind, sign) in the event window. Eclipse-driven signals
    # have a luminary as their driver, which no sign-occupancy scan can serve.
    seen_eclipses = set()
    for e in state.get("events", []):
        if e["type"] != "eclipse":
            continue
        key = (e["kind"], e["sign"])
        if key in seen_eclipses:
            continue
        seen_eclipses.add(key)
        for entry in eclipse_in_sign(e["kind"], e["sign"], from_dt):
            out[entry["configuration"]] = entry

    dasha = state.get("dasha")
    if dasha:
        prior = dasha_analogue(dasha["md"]["lord"], from_dt)
        if prior:
            out[prior["configuration"]] = prior

    return {
        "configurations": out,
        "search_limit_years": MAX_BACK_YEARS,
        "event_table_coverage": "%d-%d" % coverage(),
        "note": ("Dates are computed from the ephemeris. Outcomes come from a curated macro-event "
                 "table and are stated without any causal claim. A configuration with an empty "
                 "events list recurred at a time the table does not cover."),
    }


def for_signal(signal, analogues):
    """
    The most relevant analogue for a signal, or None.

    Preference order: an aspect involving the driver, then the driver in its current sign, then the
    mahadasha. Returns None rather than a loose match — section 30 Stage 7 wants a real analogue or
    an admission that there is not one.
    """
    if not analogues:
        return None
    configs = analogues.get("configurations", {})
    driver = signal.get("driver_body")

    # An eclipse signal's analogue is the previous eclipse in the same sign, not anything about its
    # luminary driver.
    if signal.get("eclipse_kind") and signal.get("eclipse_sign"):
        key = "%s eclipse in %s" % (signal["eclipse_kind"], signal["eclipse_sign"])
        if key in configs:
            return configs[key]

    aspect_hits = [v for k, v in sorted(configs.items())
                   if driver and driver in k and ("conjunction" in k or "opposition" in k
                                                  or "square" in k or "trine" in k)]
    if aspect_hits:
        return aspect_hits[0]
    sign_hits = [v for k, v in sorted(configs.items())
                 if driver and k.startswith("%s in " % driver)]
    if sign_hits:
        return sign_hits[0]
    dasha_hits = [v for k, v in sorted(configs.items()) if k.endswith("mahadasha")]
    return dasha_hits[0] if dasha_hits else None
