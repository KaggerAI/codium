"""
samvatsar.py — KB section 4's "Cabinet of the Universe" for a given year.

Ten portfolios, each held by the lord of the weekday on which a specific solar moment falls. The
KB gives the table and the per-planet effects but no way to determine the holders, so the model has
been asserting the cabinet from nothing at all.

Two conventions are decided here rather than left implicit, because both change the answer:

1. VEDIC WEEKDAY BOUNDARY. A Jyotish day runs sunrise to sunrise, not midnight to midnight, so a
   moment at 02:00 IST belongs to the *previous* weekday. Sunrise is computed for New Delhi
   (the reference location of the India chart in section 28) with swe.rise_trans. Ignoring this
   would misassign roughly a quarter of the portfolios — any ingress landing between midnight and
   ~06:20 IST.

2. CHAITRA SHUKLA PRATIPADA. The King's portfolio keys on the Vedic new year, which is the tithi
   following the new moon that precedes the Sun's entry into Aries. This is the amanta (new-moon
   ending month) reckoning used across most of India (Gudi Padwa / Ugadi). Purnimanta regions
   would place it differently. Recorded in the result as `pratipada_convention`.

Everything returns ISO strings and plain floats; nothing here is a datetime object.
"""

import swisseph as swe

from agents.cosmic_engine import events

# New Delhi, matching the section 28 India chart reference (28d36'N, 77d12'E).
DELHI_LON, DELHI_LAT, DELHI_ALT = 77.2, 28.6, 216.0
IST_OFFSET_HOURS = 5.5

# swe.revjul weekday: Julian day 0.0 was a Monday. jd+1.5 mod 7 gives 0=Sunday.
WEEKDAY_LORDS = ("Sun", "Moon", "Mars", "Mercury", "Jupiter", "Venus", "Saturn")
WEEKDAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")

# Section 4's portfolio table: (key, display name, how it is determined).
# 'aries'/'cancer'/... mean the Sun's ingress into that sign; 'ardra' is the nakshatra entry;
# 'pratipada' is Chaitra Shukla Pratipada.
PORTFOLIOS = (
    ("king", "King (Raja)", "pratipada",
     "Overall national fortune"),
    ("minister", "Minister (Mantri)", "Aries",
     "Policy direction, governance quality"),
    ("lord_of_clouds", "Lord of Clouds (Meghesh)", "ardra",
     "Monsoon quality, rainfall patterns"),
    ("summer_crops", "Lord of Summer Crops (Shashyesh)", "Cancer",
     "Summer crop output, agricultural production"),
    ("defence", "Defence Minister (Durgesh)", "Leo",
     "National security, military affairs"),
    ("finance", "Finance Minister (Dhanesh)", "Virgo",
     "Economic performance, fiscal health"),
    ("juicy_products", "Lord of Juicy Products (Rasesh)", "Libra",
     "Sugar, sugarcane, juice-related commodities"),
    ("agriculture", "Agriculture Minister (Dhanyesh)", "Sagittarius",
     "Winter (Kharif) crop output"),
    ("industry", "Industry Minister (Nirasesh)", "Capricorn",
     "Metals, commerce, industrial output"),
    ("horticulture", "Horticulture Minister (Phalesh)", "Pisces",
     "Fruits, flowers, vegetables"),
)

# Section 4's per-planet effect tables, verbatim, for the four portfolios the KB elaborates.
KING_EFFECTS = {
    "Sun": "Less rain, public trouble, theft/fire risk, communal clashes, grain/iron dealers profit",
    "Moon": "Abundant milk, grain, happiness, peace, good harvests",
    "Mars": "Dearth of rain, fire/riots/unrest, gold/silver/rice prices rise",
    "Mercury": "Good/timely rains, good grain production",
    "Jupiter": "Good year for business, progressive, trees laden with fruit",
    "Venus": "Sufficient rains, abundance of wheat/rice/sugarcane/fruits",
    "Saturn": "Less rain, cattle loss, consumable prices rise, diseases spread",
}
FINANCE_EFFECTS = {
    "Sun": "Government revenue strong but high taxes, public resentment, fiscal tightness",
    "Moon": "Strong consumer spending, banking deposits grow, liquidity abundant",
    "Mars": "Aggressive taxation, defense spending crowds out social spending, deficits widen",
    "Mercury": "Trade surplus, strong forex reserves, IT/services sector drives revenue",
    "Jupiter": "Fiscal expansion, ambitious spending, credit growth strong, GDP accelerates",
    "Venus": "Luxury consumption drives revenue, tourism booms, entertainment sector thrives",
    "Saturn": "Fiscal austerity, spending cuts, national debt concerns, banking NPAs rise",
}
DEFENCE_EFFECTS = {
    "Sun": "Strong military posture, aggressive foreign policy, border tensions manageable",
    "Moon": "Focus on internal security, police modernization, low external threat",
    "Mars": "HIGH war risk, military conflicts, defense stocks outperform, border skirmishes",
    "Mercury": "Cyber warfare focus, intelligence operations, diplomatic solutions preferred",
    "Jupiter": "Military expansion, new defense deals, strong alliances, peaceful year",
    "Venus": "Peace treaties, de-escalation, defense cuts possible, diplomatic harmony",
    "Saturn": "Prolonged low-intensity conflicts, military fatigue, defense budget strain",
}
AGRICULTURE_EFFECTS = {
    "Sun": "Poor harvest due to heat/drought, grain prices rise, food inflation",
    "Moon": "Excellent harvest, abundant water, grain prices stable/fall, dairy thrives",
    "Mars": "Crop damage from fire/heat/pests, food inflation, farmer distress",
    "Mercury": "Average harvest, technology-driven farming gains, mixed output",
    "Jupiter": "Bumper harvest, abundant grain, prices fall, agricultural exports strong",
    "Venus": "Good fruit/vegetable/sugarcane output, diverse agricultural prosperity",
    "Saturn": "Drought/flood damage, poor harvest, grain prices spike, rural distress",
}
INDUSTRY_EFFECTS = {
    "Sun": "Government-driven industrial push, PSU stocks rally, gold mining active",
    "Moon": "Consumer goods industry thrives, FMCG strong, water/beverage industries grow",
    "Mars": "Metals/mining boom, infrastructure push, steel/iron prices rise, industrial accidents",
    "Mercury": "IT/telecom/services boom, startup activity high, trade surplus",
    "Jupiter": "Industrial expansion across sectors, large capex projects, manufacturing growth",
    "Venus": "Luxury manufacturing, textiles, auto sector strong, consumer electronics boom",
    "Saturn": "Industrial slowdown, factory closures, labor unrest, coal/iron stressed",
}
EFFECT_TABLES = {
    "king": KING_EFFECTS,
    "finance": FINANCE_EFFECTS,
    "defence": DEFENCE_EFFECTS,
    "agriculture": AGRICULTURE_EFFECTS,
    "industry": INDUSTRY_EFFECTS,
}


def last_sunrise_at_or_before(jd, lon=DELHI_LON, lat=DELHI_LAT, alt=DELHI_ALT):
    """
    The most recent sunrise at or before `jd` at the given location.

    Searching backward is the only correct framing. Anchoring on "midnight UT of the civil day
    containing jd" is wrong for Delhi: local sunrise (~05:30 IST) falls at ~00:00 UT, so the
    sunrise belonging to a Delhi day frequently lands on the *previous* UT date. Anchoring that way
    made a 12:26 IST moment look pre-sunrise.
    """
    t = jd - 1.5
    last = None
    for _ in range(4):
        flag, tret = swe.rise_trans(t, swe.SUN, swe.CALC_RISE, geopos=(lon, lat, alt))
        if flag != 0 or not tret or not tret[0]:
            return None
        rise = tret[0]
        if rise > jd:
            break
        last = rise
        t = rise + 0.01
    return last


def _weekday_index_local(jd):
    """Weekday index (0=Sunday) of the IST calendar date containing `jd`."""
    y, m, d, _h = swe.revjul(jd + IST_OFFSET_HOURS / 24.0)
    return int((swe.julday(y, m, d, 0.0) + 1.5) % 7)


def vedic_weekday(jd):
    """
    Weekday of `jd` under the Jyotish sunrise-to-sunrise convention, evaluated at New Delhi.

    The Vedic day begins at sunrise and carries the weekday of that sunrise's local date, so a
    moment at 02:00 IST belongs to the previous weekday.

    Returns (weekday_index 0=Sunday, weekday_name, lord, used_previous_day). `used_previous_day`
    is surfaced so the attribution is auditable: it is True exactly when the moment's own IST date
    differs from the Vedic day's date.
    """
    rise = last_sunrise_at_or_before(jd)
    if rise is None:
        # No sunrise resolvable (should not occur at Delhi's latitude). Fall back to the local
        # civil date rather than silently shifting a day.
        idx = _weekday_index_local(jd)
        return idx, WEEKDAY_NAMES[idx], WEEKDAY_LORDS[idx], False

    idx = _weekday_index_local(rise)
    used_previous = idx != _weekday_index_local(jd)
    return idx, WEEKDAY_NAMES[idx], WEEKDAY_LORDS[idx], used_previous


def chaitra_shukla_pratipada(year):
    """
    Chaitra Shukla Pratipada for `year`: the tithi beginning at the new moon immediately preceding
    the Sun's entry into Aries (amanta reckoning). Returns the jd of that new moon, or None.
    """
    jd_start = swe.julday(year, 1, 1, 0.0)
    jd_end = swe.julday(year + 1, 1, 1, 0.0)
    ingress = events.sun_ingress_moments(jd_start, jd_end, signs=["Aries"]).get("Aries")
    if ingress is None:
        return None
    return events.new_moon_before(ingress)


def cabinet(year):
    """
    The section 4 cabinet for the samvatsar year beginning at Chaitra Shukla Pratipada of `year`.

    The scan window is Pratipada(year) -> Pratipada(year+1), NOT the calendar year. This matters:
    the Capricorn and Pisces ingresses of a given samvatsar fall in the following January and March,
    so a calendar-year scan would take those two portfolios from the previous cabinet.

    Returns a JSON-serializable dict:
        {"year", "pratipada", "window_end", "pratipada_convention", "weekday_convention",
         "portfolios": {key: {...}}, "summary"}
    """
    pratipada = chaitra_shukla_pratipada(year)
    next_pratipada = chaitra_shukla_pratipada(year + 1)
    if pratipada is None or next_pratipada is None:
        return {"year": year, "pratipada": None, "portfolios": {},
                "summary": "samvatsar boundaries not resolvable for %d" % year}

    ingresses = events.sun_ingress_moments(pratipada, next_pratipada)
    ardra = events.sun_nakshatra_moment(pratipada, next_pratipada, "Ardra")

    out = {}
    for key, name, determinant, governs in PORTFOLIOS:
        if determinant == "pratipada":
            jd = pratipada
        elif determinant == "ardra":
            jd = ardra
        else:
            jd = ingresses.get(determinant)

        if jd is None:
            out[key] = {"portfolio": name, "lord": None, "governs": governs,
                        "note": "determining moment for %r not found in %d" % (determinant, year)}
            continue

        _idx, day_name, lord, shifted = vedic_weekday(jd)
        entry = {
            "portfolio": name,
            "determined_by": ("Chaitra Shukla Pratipada" if determinant == "pratipada"
                              else "Sun enters %s" % determinant),
            "moment": _iso_ist(jd),
            "weekday": day_name,
            "weekday_from_previous_civil_day": shifted,
            "lord": lord,
            "governs": governs,
        }
        table = EFFECT_TABLES.get(key)
        if table and lord in table:
            entry["effect"] = table[lord]
        out[key] = entry

    king = out.get("king", {}).get("lord")
    finance = out.get("finance", {}).get("lord")
    return {
        "year": year,
        "pratipada": _iso_ist(pratipada),
        "window_end": _iso_ist(next_pratipada),
        "pratipada_convention": "amanta - new moon preceding Mesha Sankranti (Gudi Padwa / Ugadi)",
        "weekday_convention": "Jyotish sunrise-to-sunrise, New Delhi",
        "portfolios": out,
        "summary": ("King %s, Finance Minister %s" % (king, finance)) if king and finance else "",
    }


def _iso_ist(jd):
    """Julian day -> 'YYYY-MM-DD HH:MM IST'. The cabinet is an Indian system; report it in IST."""
    y, m, d, h = swe.revjul(jd + IST_OFFSET_HOURS / 24.0)
    hh = int(h)
    mm = int(round((h - hh) * 60))
    if mm == 60:
        hh, mm = hh + 1, 0
    return "%04d-%02d-%02d %02d:%02d IST" % (y, m, d, hh % 24, mm)


def samvatsar_year_for(dt_utc):
    """
    Which samvatsar year governs `dt_utc`. The Vedic year begins at Chaitra Shukla Pratipada, so a
    date in January or February belongs to the previous samvatsar year, not the current one.
    """
    year = dt_utc.year
    pratipada = chaitra_shukla_pratipada(year)
    if pratipada is None:
        return year
    jd = swe.julday(dt_utc.year, dt_utc.month, dt_utc.day,
                    dt_utc.hour + dt_utc.minute / 60.0)
    return year if jd >= pratipada else year - 1
