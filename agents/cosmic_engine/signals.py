"""
signals.py — builds the raw signal ledger from a cast chart and its event calendar.

A signal is a directional claim about a target, attributable to a transit. Nothing here decides
conviction; that is pipeline.py's job. This module answers only: what is being claimed, about what,
driven by what, and on the authority of which rulebook line.

WHERE DIRECTION COMES FROM. This is the part that could have been invented and was not. Every
generator below implements an explicit directional rule from the knowledge base, and each carries
the section it came from in `basis`:

  section 1   "When a planet is afflicted (combust, retrograde, conjunct malefic), commodities it
              governs face supply disruption and price increases. When strong (exalted, own sign,
              aspected by benefics), those commodities become abundant and prices stabilize or fall."
  section 20  Retrograde -> market behaviour: Mercury retrograde "avoid new positions in
              Mercury-ruled sectors"; Venus retrograde "luxury stocks dip"; Mars "real estate
              stalls"; Jupiter "banking stock pullback"; Saturn "PSU underperformance".
  section 20  Major cycles: "Rahu-Ketu axis shift -> Rahu's sign INFLATES, Ketu's sign DEFLATES";
              "Jupiter's new sign = leading sectors"; "Saturn's new sign = headwind sectors".
  section 12  "When multiple malefics afflict a sign, the countries ruled by that sign face
              political upheaval... When benefics strengthen a sign, its regions prosper."
  section 13  "When malefics transit nakshatras of a zone -> that Indian region faces instability...
              When benefics transit -> that zone prospers."
  section 17/18  Every listed eclipse effect is adverse, so an eclipse signal is always DOWN for
              the sign's domain.
  section 25  A slow planet in a pada gives its sub-sectors "a SUSTAINED multi-month/year
              influence"; a fast planet gives "a SHORT tactical influence".

A generator that cannot cite a rule does not exist. That constraint is what keeps the ledger a
transcription of the rulebook rather than a model of my own.
"""

from agents.cosmic_engine.chart import BENEFICS, MALEFICS, SIGNS, angular_separation
from agents.cosmic_engine.kb.aliases import modern, modern_list
from agents.cosmic_engine.kb.bridge import CLASSICAL_TO_MODERN
from agents.cosmic_engine.kb.planets import PLANET_COMMODITY, PLANET_REGION, PLANET_SECTOR
from agents.cosmic_engine.kb.signs import (
    LUNAR_ECLIPSE_EFFECT, SIGN_COMMODITY, SIGN_COUNTRY, SOLAR_ECLIPSE_EFFECT,
)
from agents.cosmic_engine.kb.zones import NAKSHATRA_ZONE

CONJUNCTION_ORB = 8.0
SLOW_BODIES = ("Saturn", "Jupiter", "Rahu", "Ketu")

# Section 22 tier by driving body, for the condition-based generators.
#
# Tier 1 lists "Saturn/Jupiter sign change or retrograde, Rahu-Ketu axis shift". Tier 2 lists "Sun
# ingress, Mercury/Venus/Mars retrogrades, Mars sign change, combustion of benefics". The Moon
# appears only in Tier 4, via "Poornima/Amavasya signals" - so a Moon-driven claim is a confirming
# micro-signal in the rulebook's own scheme, never a primary driver.
#
# This was hardcoded to Tier 2 for every body at first, which let Moon-driven commodity calls reach
# 85% conviction. They also cannot carry a historical analogue, because the Moon returns to any
# position monthly - so the rulebook was demanding Stage 7 evidence that cannot exist for a signal
# the rulebook never intended to be primary.
BODY_TIER = {
    "Saturn": 1, "Jupiter": 1, "Rahu": 1, "Ketu": 1,
    "Sun": 2, "Mars": 2, "Mercury": 2, "Venus": 2,
    "Moon": 4,
    "Uranus": 3, "Neptune": 3, "Pluto": 3,
}
DEFAULT_BODY_TIER = 3


def tier_for_body(body):
    """Section 22 tier for a signal driven by `body`'s standing condition."""
    return BODY_TIER.get(body, DEFAULT_BODY_TIER)

# Reverse index: which planet rules a given modern sector (section 20). Used by the Stage 0 gate to
# find a signal's "theme planet". Built rather than hand-written so it cannot drift from the table.
SECTOR_RULER = {}
for _planet, _row in PLANET_SECTOR.items():
    for _sector in _row.get("sectors_list", ()):
        SECTOR_RULER.setdefault(_sector.lower(), _planet)

COMMODITY_RULER = {}
for _planet, _row in PLANET_COMMODITY.items():
    for _item in _row.get("governs_list", ()):
        COMMODITY_RULER.setdefault(_item.lower(), _planet)


def _afflicted(body, chart):
    """
    Section 1's affliction test: combust, retrograde, or conjunct a malefic. Debilitation is
    included because section 27 treats a debilitated placement as weak and section 1's contrast is
    explicitly affliction versus strength.

    Returns (bool, [reasons]).
    """
    b = chart["bodies"][body]
    reasons = []
    if b["combust"]:
        reasons.append("combust (%.1f deg from Sun)" % b["deg_from_sun"])
    if b["retrograde"]:
        reasons.append("retrograde")
    if b["dignity_d1"] == "debilitated":
        reasons.append("debilitated in %s" % b["sign"])
    for mal in MALEFICS:
        if mal == body:
            continue
        sep = angular_separation(b["lon"], chart["bodies"][mal]["lon"])
        if sep <= CONJUNCTION_ORB:
            reasons.append("conjunct %s (%.1f deg)" % (mal, sep))
            break
    return bool(reasons), reasons


def _strong(body, chart):
    """Section 1's strength test: exalted, own sign, or aspected by a benefic — and not afflicted."""
    b = chart["bodies"][body]
    reasons = []
    if b["dignity_d1"] in ("exalted", "own", "mooltrikona"):
        reasons.append("%s in %s" % (b["dignity_d1"], b["sign"]))
    for ben in BENEFICS:
        if ben == body:
            continue
        sep = angular_separation(b["lon"], chart["bodies"][ben]["lon"])
        if sep <= CONJUNCTION_ORB:
            reasons.append("aspected by %s (%.1f deg)" % (ben, sep))
            break
    afflicted, _ = _afflicted(body, chart)
    return (bool(reasons) and not afflicted), reasons


def condition(body, chart):
    """
    THE single verdict on a body's condition: "afflicted", "strong" or "neutral", plus reasons.

    Every generator must route through this. Before it existed, one generator read
    `dignity_d1` directly while another used the affliction test, and they disagreed about the same
    body: Jupiter exalted in Cancer *and* combust produced simultaneous "insurance UP" and
    "insurance DOWN" signals from the same planet, each at maximum conviction. Section 1's own
    wording resolves the precedence — affliction is the operative condition, since a combust or
    retrograde planet cannot deliver the abundance that dignity alone would promise.
    """
    afflicted, why_bad = _afflicted(body, chart)
    if afflicted:
        return "afflicted", why_bad
    strong, why_good = _strong(body, chart)
    if strong:
        return "strong", why_good
    return "neutral", []


def _signal(sid, target, target_class, direction, driver, basis, tier, decay,
            event=None, horizon=None, extra=None):
    out = {
        "id": sid,
        "source": "engine",
        "target": target,
        "target_class": target_class,
        "direction": direction,
        "driver_body": driver,
        "basis": basis,
        "tier": tier,
        "decay": decay,
        "event_date": (event or {}).get("date"),
        "event_type": (event or {}).get("type"),
        "horizon": horizon or "unspecified",
    }
    if extra:
        out.update(extra)
    return out


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

def _commodity_signals(chart, next_id):
    """Section 1: afflicted planet -> its commodities rise; strong planet -> they ease."""
    out = []
    for body in ("Sun", "Moon", "Mercury", "Venus", "Mars", "Jupiter", "Saturn"):
        row = PLANET_COMMODITY.get(body)
        if not row:
            continue
        cond, why = condition(body, chart)
        if cond == "afflicted":
            direction = "UP"
        elif cond == "strong":
            direction = "DOWN"
        else:
            continue
        afflicted = cond == "afflicted"
        # Only surface commodities the classical-to-modern bridge can actually trade (section 21).
        tradeable = [c for c in row["governs_list"] if c in CLASSICAL_TO_MODERN]
        for item in tradeable[:3]:
            out.append(_signal(
                next_id(), CLASSICAL_TO_MODERN[item]["instrument"], "commodity", direction, body,
                ["S1 planet-commodity: %s %s" % (body, "afflicted" if afflicted else "strong"),
                 "S21 bridge: %s -> %s" % (item, CLASSICAL_TO_MODERN[item]["instrument"]),
                 "S22 tier %d for a %s-driven signal" % (tier_for_body(body), body)],
                tier=tier_for_body(body), decay=1.0,
                horizon="while %s remains %s" % (body, "afflicted" if afflicted else "strong"),
                extra={"driver_detail": "; ".join(why), "classical_item": item,
                       "exchanges": CLASSICAL_TO_MODERN[item]["exchanges"]}))
    return out


def _retrograde_sector_signals(chart, events, next_id):
    """Section 20: a retrograde planet's ruled sectors underperform."""
    out = []
    stations = {e["body"]: e for e in events
                if e["type"] == "station" and e["direction"] == "retrograde"}
    for body in chart["retrogrades"]:
        row = PLANET_SECTOR.get(body)
        if not row:
            continue
        event = stations.get(body)
        # A retrograde station is an event, so Tier 1 for slow bodies per S22; otherwise the body's
        # own tier. S22 names Mercury/Venus/Mars retrogrades explicitly as Tier 2.
        tier = 1 if body in SLOW_BODIES else tier_for_body(body)
        for sector in row["sectors_list"][:3]:
            out.append(_signal(
                next_id(), sector, "sector", "DOWN", body,
                ["S20 retrograde: %s retrograde weighs on its ruled sectors" % body],
                tier=tier, decay=(event or {}).get("decay", 1.0), event=event,
                horizon="until %s stations direct" % body,
                extra={"driver_detail": "%s retrograde in %s" % (body, chart["bodies"][body]["sign"])}))
    return out


def _dignity_sector_signals(chart, next_id):
    """Section 20 sectors, directed by the ruling planet's dignity (sections 1/27 strength logic)."""
    out = []
    for body, b in chart["bodies"].items():
        row = PLANET_SECTOR.get(body)
        if not row or b["retrograde"]:
            continue  # retrograde is handled by its own generator; do not double-count
        # Route through condition() rather than reading dignity directly, so this generator can
        # never contradict the commodity or pada generators about the same body.
        cond, reasons = condition(body, chart)
        if cond == "strong":
            direction, why = "UP", "%s strong: %s" % (body, "; ".join(reasons))
        elif cond == "afflicted":
            direction, why = "DOWN", "%s afflicted: %s" % (body, "; ".join(reasons))
        else:
            continue
        tier = tier_for_body(body)
        for sector in row["sectors_list"][:2]:
            out.append(_signal(
                next_id(), sector, "sector", direction, body,
                ["S20 planet-sector rulership", "S1/S27 strength: %s" % why],
                tier=tier, decay=1.0,
                horizon="while %s holds %s" % (body, b["sign"]),
                extra={"driver_detail": why}))
    return out


def _pada_subsector_signals(chart, next_id):
    """
    Section 25: the pada a planet occupies activates a specific sub-sector. Slow planets give a
    sustained influence, fast planets a short tactical one. Direction follows the planet's condition.
    """
    out = []
    for body, b in chart["bodies"].items():
        if not b["sub_sector"]:
            continue
        cond, why = condition(body, chart)
        if cond == "afflicted":
            direction = "DOWN"
        elif cond == "strong":
            direction = "UP"
        else:
            continue
        slow = body in SLOW_BODIES
        out.append(_signal(
            next_id(), b["sub_sector"], "sub_sector", direction, body,
            ["S25 pada %s P%d -> %s" % (b["nakshatra"], b["pada"], b["sub_sector"]),
             "S26 decanate %d -> %s" % (b["decanate"], b["value_chain_stage"])],
            tier=tier_for_body(body), decay=1.0,
            horizon="sustained (multi-month)" if slow else "tactical (days to weeks)",
            extra={"driver_detail": "; ".join(why),
                   "value_chain_stage": b["value_chain_stage"],
                   "instruments": b["sub_sector_names"],
                   "rotation_imminent": b["boundary"]["rotation_imminent"],
                   "deg_to_pada_edge": b["boundary"]["pada_deg_to_edge"]}))
    return out


def _cycle_signals(chart, events, next_id):
    """
    Section 20 major transit cycles:
      Rahu's sign INFLATES, Ketu's sign DEFLATES
      Jupiter's new sign = leading sectors; Saturn's new sign = headwind sectors
    """
    out = []
    node_rules = (("Rahu", "UP", "inflates"), ("Ketu", "DOWN", "deflates"))
    for body, direction, verb in node_rules:
        sign = chart["bodies"][body]["sign"]
        for item in (SIGN_COMMODITY.get(sign, {}).get("commodities_list") or [])[:3]:
            out.append(_signal(
                next_id(), item, "commodity", direction, body,
                ["S20 axis rule: %s's sign %s" % (body, verb),
                 "S2 sign-commodity: %s -> %s" % (sign, item)],
                tier=1, decay=1.0, horizon="~18 months per sign pair (S24)",
                extra={"driver_detail": "%s in %s" % (body, sign)}))

    ingress = {e["body"]: e for e in events if e["type"] == "sign_change"}
    for body, direction, verb in (("Jupiter", "UP", "leading sectors"),
                                  ("Saturn", "DOWN", "headwind sectors")):
        event = ingress.get(body)
        if not event:
            continue
        for sector in (PLANET_SECTOR.get(body, {}).get("sectors_list") or [])[:2]:
            out.append(_signal(
                next_id(), sector, "sector", direction, body,
                ["S20 cycle: %s's new sign = %s" % (body, verb),
                 "S22 tier 1: %s sign change" % body],
                tier=1, decay=event["decay"], event=event,
                horizon="12 months (Jupiter) / ~2.5 years (Saturn) per S24",
                extra={"driver_detail": "%s enters %s on %s"
                                       % (body, event["to_sign"], event["date"])}))
    return out


def _eclipse_signals(events, next_id):
    """
    Sections 17/18 mapped through section 12: every listed eclipse effect is adverse, so the sign's
    countries and commodities take a DOWN signal. Visibility is irrelevant here — sections 17/18
    key on the eclipse's zodiac sign, not on whether it can be seen.
    """
    out = []
    for e in [x for x in events if x["type"] == "eclipse"]:
        table = SOLAR_ECLIPSE_EFFECT if e["kind"] == "solar" else LUNAR_ECLIPSE_EFFECT
        effects = table.get(e["sign"], {}).get("effects", "")
        countries = (SIGN_COUNTRY.get(e["sign"], {}).get("countries_list") or [])[:3]
        for country in countries:
            # Modernise the target name; the basis below still quotes the KB's own wording, so
            # "Ethiopia" stays traceable to "Aquarius -> Abyssinia (Ethiopia)".
            out.append(_signal(
                next_id(), modern(country), "geopolitics", "DOWN", e["body"],
                ["S%d eclipse-by-sign: %s" % (17 if e["kind"] == "solar" else 18, effects[:90]),
                 "S12 sign-country: %s -> %s" % (e["sign"], country)],
                tier=1, decay=e["decay"], event=e,
                horizon=("%s months (S5)" % e["effect_months"]) if e.get("effect_months")
                        else "duration not claimed (not locally visible)",
                extra={"driver_detail": e["detail"], "eclipse_window": e["window"],
                       "eclipse_node": e["node"], "eclipse_kind": e["kind"],
                       "eclipse_sign": e["sign"]}))
    return out


def _region_signals(chart, next_id):
    """
    Sections 13 and 14: a malefic transiting a nakshatra zone stresses that Indian region; a benefic
    brings prosperity. Section 14 adds the planet's own regional dominion.
    """
    out = []
    for body, b in chart["bodies"].items():
        if body not in MALEFICS and body not in BENEFICS:
            continue
        zone = NAKSHATRA_ZONE.get(b["nakshatra_index"])
        if not zone:
            continue
        malefic = body in MALEFICS
        direction = "DOWN" if malefic else "UP"
        regions = zone["regions"][:2]
        own = modern_list((PLANET_REGION.get(body, {}).get("regions_list") or [])[:2])
        for region in regions:
            out.append(_signal(
                next_id(), modern(region), "region", direction, body,
                ["S13 nakshatra-zone: %s (%s) -> %s zone" % (b["nakshatra"], body, zone["zone"]),
                 "S13 rule: %s transit -> %s" % ("malefic" if malefic else "benefic",
                                                 "instability" if malefic else "prosperity")],
                tier=3, decay=1.0, horizon="while %s holds %s" % (body, b["nakshatra"]),
                extra={"driver_detail": "%s in %s" % (body, b["nakshatra"]),
                       "planet_own_regions": own}))
    return out


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

def build(chart, events):
    """
    Build the raw signal ledger. Returns a list of signal dicts with no conviction assigned —
    pipeline.arbitrate() does that.

    Ids are assigned in generation order and are stable for a given chart, so a signal can be
    referenced between the ledger, the brief and the final report.
    """
    counter = [0]

    def next_id():
        counter[0] += 1
        return "S%02d" % counter[0]

    ledger = []
    ledger.extend(_commodity_signals(chart, next_id))
    ledger.extend(_retrograde_sector_signals(chart, events, next_id))
    ledger.extend(_dignity_sector_signals(chart, next_id))
    ledger.extend(_pada_subsector_signals(chart, next_id))
    ledger.extend(_cycle_signals(chart, events, next_id))
    ledger.extend(_eclipse_signals(events, next_id))
    ledger.extend(_region_signals(chart, next_id))
    return ledger


def theme_planet(signal):
    """
    Which planet governs a signal's subject matter, for the Stage 0 gate.

    Resolution order: the sector/commodity reverse index from sections 20 and 1 first, because the
    gate asks whether the *theme* aligns with the dasha lord, not whether the trigger does. Falls
    back to the driver body when the target is not in either table (regions, countries, sub-sectors).
    """
    target = (signal.get("target") or "").lower()
    if signal["target_class"] in ("sector", "sub_sector"):
        for sector, planet in SECTOR_RULER.items():
            if sector in target or target in sector:
                return planet, "S20 sector rulership"
    if signal["target_class"] == "commodity":
        for item, planet in COMMODITY_RULER.items():
            if item in target or target in item:
                return planet, "S1 planet-commodity"
    return signal["driver_body"], "driver body (target not in S1/S20 tables)"
