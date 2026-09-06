"""
dasha.py — Vimshottari mahadasha / antardasha / pratyantardasha calculator.

Replaces KB section 28's hardcoded table (1,309 tokens of approximate dates with "~" hedges) with
the arithmetic that produced it. Also supplies the Stage 0 gate inputs that section 30 requires:
the running lords, their exact boundaries, the next juncture with its section 24 context weight,
and the section 28 Rule G5 sectoral bias.

Which natal Moon longitude seeds the chain is a configuration choice with real consequences —
see kb/dasha.py for the analysis. Every result carries `source` so any report can be traced back
to the mode that produced it.

All dates are ISO strings; nothing here returns datetime objects (Redis serialization, see
docs/COSMIC_ENGINE_MIGRATION.md section 4.0.1).
"""

import datetime

import swisseph as swe

from agents.cosmic_engine.kb.dasha import (
    DASHA_THEMES, INDIA_NATAL, JUNCTURE_DEFAULT_WEIGHT, JUNCTURE_WEIGHTS,
    MD_SECTOR_BIAS, TOTAL_CYCLE_YEARS, VIMSHOTTARI_SEQUENCE, YEAR_DAYS,
)

NAK_SPAN = 360.0 / 27.0


def natal_moon_longitude(natal=None):
    """
    Sidereal longitude of the natal Moon, per the configured calibration mode.

    Returns (longitude, source) where source is 'kb_calibrated' or 'computed'.
    """
    natal = natal or INDIA_NATAL
    mode = natal.get("mode", "kb_calibrated")
    if mode == "kb_calibrated":
        return natal["moon_lon_kb_calibrated"], "kb_calibrated"
    if mode == "computed":
        swe.set_sid_mode(swe.SIDM_LAHIRI)
        jd = swe.julday(natal["year"], natal["month"], natal["day"], natal["ut_hour"])
        lon = swe.calc_ut(jd, swe.MOON, swe.FLG_SWIEPH | swe.FLG_SIDEREAL)[0][0]
        return lon % 360.0, "computed"
    raise ValueError("unknown dasha calibration mode %r (expected kb_calibrated|computed)" % mode)


def natal_julian_day(natal=None):
    natal = natal or INDIA_NATAL
    return swe.julday(natal["year"], natal["month"], natal["day"], natal["ut_hour"])


def _jd_to_iso(jd):
    """Julian day -> 'YYYY-MM-DD'. swe.revjul returns a fractional hour we do not need."""
    y, m, d, _h = swe.revjul(jd)
    return "%04d-%02d-%02d" % (y, m, d)


def _seq_index(lord):
    for i, (name, _) in enumerate(VIMSHOTTARI_SEQUENCE):
        if name == lord:
            return i
    raise ValueError("unknown dasha lord %r" % lord)


def _periods_from(start_jd, parent_lord, parent_years, level_years_total):
    """
    Subdivide a period into its sub-periods, starting from the parent's own lord and cycling the
    Vimshottari sequence. Sub-period length = parent_years * sub_lord_years / 120.

    Used for both antardasha (inside a mahadasha) and pratyantardasha (inside an antardasha), which
    follow identical arithmetic one level down.
    """
    out = []
    at = start_jd
    base = _seq_index(parent_lord)
    for k in range(len(VIMSHOTTARI_SEQUENCE)):
        lord, years = VIMSHOTTARI_SEQUENCE[(base + k) % len(VIMSHOTTARI_SEQUENCE)]
        span = parent_years * years / float(level_years_total) * YEAR_DAYS
        out.append({"lord": lord, "start_jd": at, "end_jd": at + span})
        at += span
    return out


def mahadashas(natal=None):
    """
    Full mahadasha chain from birth. Returns a list of
    {"lord", "years", "start", "end", "start_jd", "end_jd"} covering one full 120-year cycle
    plus the balance, which is more than enough for any forecasting window.
    """
    natal = natal or INDIA_NATAL
    moon_lon, _source = natal_moon_longitude(natal)
    birth_jd = natal_julian_day(natal)

    nak_index = int(moon_lon / NAK_SPAN)
    elapsed_fraction = (moon_lon % NAK_SPAN) / NAK_SPAN
    start_idx = nak_index % len(VIMSHOTTARI_SEQUENCE)

    chain = []
    at = birth_jd
    for k in range(len(VIMSHOTTARI_SEQUENCE) + 1):
        lord, years = VIMSHOTTARI_SEQUENCE[(start_idx + k) % len(VIMSHOTTARI_SEQUENCE)]
        # The first mahadasha is only the unelapsed balance of the birth nakshatra's lord.
        effective = years * (1.0 - elapsed_fraction) if k == 0 else float(years)
        end = at + effective * YEAR_DAYS
        chain.append({
            "lord": lord,
            "years": round(effective, 4),
            "full_years": years,
            "start": _jd_to_iso(at),
            "end": _jd_to_iso(end),
            "start_jd": at,
            "end_jd": end,
            "is_balance": k == 0,
        })
        at = end
    return chain


def _active(periods, jd):
    for p in periods:
        if p["start_jd"] <= jd < p["end_jd"]:
            return p
    return None


def _juncture_weight(days):
    for within, weight in JUNCTURE_WEIGHTS:
        if days <= within:
            return weight
    return JUNCTURE_DEFAULT_WEIGHT


def state(at_dt, natal=None):
    """
    Running dasha state at `at_dt` (UTC datetime).

    Returns the Stage 0 gate inputs section 30 needs:
        {"md", "ad", "pd", "next_juncture", "md_sector_bias", "themes", "source",
         "natal_moon_lon", "calibration_mode"}

    Each of md/ad/pd is {"lord", "start", "end", "days_remaining"}.
    """
    natal = natal or INDIA_NATAL
    moon_lon, source = natal_moon_longitude(natal)
    if at_dt.tzinfo is not None:
        at_dt = at_dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    jd = swe.julday(at_dt.year, at_dt.month, at_dt.day,
                    at_dt.hour + at_dt.minute / 60.0 + at_dt.second / 3600.0)

    chain = mahadashas(natal)
    md = _active(chain, jd)
    if md is None:
        raise ValueError("no mahadasha covers %s; chain spans %s..%s"
                         % (at_dt.isoformat(), chain[0]["start"], chain[-1]["end"]))

    ads = _periods_from(md["start_jd"], md["lord"], md["years"], TOTAL_CYCLE_YEARS)
    ad = _active(ads, jd)
    pds = _periods_from(ad["start_jd"], ad["lord"],
                        (ad["end_jd"] - ad["start_jd"]) / YEAR_DAYS, TOTAL_CYCLE_YEARS)
    pd = _active(pds, jd)

    def pack(p):
        return {
            "lord": p["lord"],
            "start": _jd_to_iso(p["start_jd"]),
            "end": _jd_to_iso(p["end_jd"]),
            "days_remaining": int(round(p["end_jd"] - jd)),
        }

    # Next juncture is whichever boundary lands first — an AD changeover usually, an MD changeover
    # when they coincide. KB section 28 Rule G4 marks these as volatility windows regardless of
    # transits, so the caller needs the nearest one, not just the MD one.
    nearest_jd = min(md["end_jd"], ad["end_jd"])
    days = int(round(nearest_jd - jd))
    level = "mahadasha" if nearest_jd == md["end_jd"] else "antardasha"

    themes = DASHA_THEMES.get((md["lord"], ad["lord"])) or DASHA_THEMES.get((md["lord"], None), "")

    return {
        "md": pack(md),
        "ad": pack(ad),
        "pd": pack(pd),
        "next_juncture": {
            "date": _jd_to_iso(nearest_jd),
            "days": days,
            "level": level,
            "context_weight": _juncture_weight(days),
            "volatility_window": days <= 30,
        },
        "md_sector_bias": MD_SECTOR_BIAS.get(md["lord"], ""),
        "themes": themes,
        "source": source,
        "calibration_mode": natal.get("mode", "kb_calibrated"),
        "natal_moon_lon": round(moon_lon, 6),
        "natal_label": natal.get("label", ""),
    }


def gate(trigger_body, at_dt=None, dasha_state=None, same_body_event=False, natal=None):
    """
    Stage 0 dasha gate for a single trigger body, per KB section 28 Rule G2.

    Only the mechanical half is decided here — whether the trigger IS a dasha lord:
        DOUBLE AMPLIFY  trigger is the AD lord AND the transit is a same-body event
        AMPLIFY         trigger is the MD or AD lord
        PASS            otherwise

    GATE FAIL is NOT decided here. Rule G2's fail condition is "transit theme contradicts current
    dasha themes", which is a semantic judgment about a prediction's subject, not a property of
    the trigger body. It needs the signal's target sector, so it belongs to pipeline.py in Phase 4
    (and is flagged in the migration doc section 6 as the least mechanizable step in the pipeline).
    Returning PASS here means "not amplified", never "verified as consistent".

    Returns {"status", "tier_delta", "reason", "md_lord", "ad_lord"}.
    """
    st = dasha_state or state(at_dt, natal)
    md_lord, ad_lord = st["md"]["lord"], st["ad"]["lord"]

    if trigger_body == ad_lord and same_body_event:
        return {"status": "DOUBLE AMPLIFY", "tier_delta": 2,
                "reason": "%s is the antardasha lord and the trigger is a same-body event"
                          % trigger_body,
                "md_lord": md_lord, "ad_lord": ad_lord}
    if trigger_body in (md_lord, ad_lord):
        which = "mahadasha" if trigger_body == md_lord else "antardasha"
        return {"status": "AMPLIFY", "tier_delta": 1,
                "reason": "%s is the %s lord" % (trigger_body, which),
                "md_lord": md_lord, "ad_lord": ad_lord}
    return {"status": "PASS", "tier_delta": 0,
            "reason": "%s is neither the mahadasha (%s) nor antardasha (%s) lord; theme "
                      "consistency not yet evaluated" % (trigger_body, md_lord, ad_lord),
            "md_lord": md_lord, "ad_lord": ad_lord}
