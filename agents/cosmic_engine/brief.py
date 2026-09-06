"""
brief.py — renders the compact prompt payload that replaces the 21k-token rulebook.

This is the ONLY engine output that ever enters a prompt (docs/COSMIC_ENGINE_MIGRATION.md
section 3.3). Everything here is already-resolved fact: no tables, no methodology, no lookup
instructions.

Budget is 4,000 tokens and enforced in code, per section 3.7 amendment 1:

  * The BODIES block covers ALL bodies, not only flagged triggers. The model cannot reason about
    a row it never sees, so anything withheld here becomes permanently unreachable — including
    counter-evidence. ~500 tokens against an ~18k saving is the right trade.
  * When the budget binds, narrative DETAIL is dropped before BODIES are, and if bodies must go
    they go fastest-mover-first. Whatever is dropped is reported in the returned metadata and
    logged. Never truncate silently.

Every resolved row is tagged with the KB section it came from (S1, S12, S13, S14, S20, S25, S26)
so the synthesis prompt's imperatives can be rewritten from "look up X in Section N" to "use the
pre-resolved (SN) row" without dangling references (section 4.0.2 defect 1).
"""

from agents.cosmic_engine.kb.aliases import modern_list
from agents.cosmic_engine.kb.bridge import CLASSICAL_TO_MODERN
from agents.cosmic_engine.kb.planets import (
    PLANET_COMMODITY, PLANET_REGION, PLANET_SECTOR,
)
from agents.cosmic_engine.kb.signs import SIGN_COMMODITY, SIGN_COUNTRY
from agents.cosmic_engine.kb.zones import NAKSHATRA_ZONE

# Token budget and the chars/token convention used throughout the migration doc.
#
# Raised 3,000 -> 4,000 (Phase 3, once chart/dasha/yoga/event/cabinet were all in) -> 4,500
# (Phase 4.5, for the historical analogues). The alternative each time was operating permanently at
# 'core' detail, which drops the commodity, sign-commodity and geography rows for every body. Those
# rows are unreachable to the model if not sent - it cannot fall back on reading the rulebook,
# because the rulebook is no longer in the prompt.
#
# 4,500 is still a 4.7x reduction against the 21,221 tokens of rulebook it replaces, so the marginal
# cost of headroom is a rounding error against the saving while the cost of losing reachable facts
# is permanent.
DEFAULT_MAX_TOKENS = 4500
CHARS_PER_TOKEN = 4

# Detail levels, richest first. Degradation walks this list before touching bodies.
DETAIL_LEVELS = ("full", "core", "minimal")

# Drop order when even 'minimal' does not fit: outer planets first, then fastest movers.
# Saturn, Jupiter and the nodes are last to go — they are the KB's Tier 1 drivers.
BODY_DROP_ORDER = (
    "Pluto", "Neptune", "Uranus",
    "Moon", "Mercury", "Venus", "Sun", "Mars",
    "Ketu", "Rahu", "Jupiter", "Saturn",
)

MAX_LIST_ITEMS = 5   # how many entries of a KB list cell to surface


def estimate_tokens(text):
    return len(text) // CHARS_PER_TOKEN


def _short(items, limit=MAX_LIST_ITEMS):
    """Render the head of a KB list cell, marking that it was cut."""
    if not items:
        return ""
    head = [str(x) for x in items[:limit]]
    return ", ".join(head) + (", ..." if len(items) > limit else "")


def _headline(name, b):
    """One-line placement summary: the part that must survive every degradation level."""
    flags = []
    if b.get("retrograde"):
        flags.append("R")
    if b.get("combust"):
        flags.append("combust")
    if b.get("vargottama"):
        flags.append("vargottama")
    if b.get("boundary", {}).get("rotation_imminent"):
        flags.append("ROTATION IMMINENT %.2f deg to edge"
                     % b["boundary"]["pada_deg_to_edge"])
    tail = ("  [" + "; ".join(flags) + "]") if flags else ""
    return (
        "%-8s %-11s %5.2f  %s P%d  D9 %s(%s)  D10 %s(%s)  dec %d %s%s"
        % (name, b["sign"], b["deg"], b["nakshatra"], b["pada"],
           b["d9_sign"], b["dignity_d9"], b["d10_sign"], b["dignity_d10"],
           b["decanate"], b["value_chain_stage"], tail)
    )


def _body_block(name, b, level):
    """Render one body at the requested detail level."""
    lines = [_headline(name, b)]
    if level == "minimal":
        return lines

    pad = " " * 9
    if b.get("sub_sector"):
        names = b.get("sub_sector_names") or []
        suffix = ("  -> " + _short(names, 4)) if names else ""
        lines.append("%ssub-sector (S25): %s%s" % (pad, b["sub_sector"], suffix))

    sector = PLANET_SECTOR.get(name, {}).get("sectors_list") or []
    if sector:
        lines.append("%ssector (S20): %s" % (pad, _short(sector)))

    if level == "core":
        return lines

    commodity = PLANET_COMMODITY.get(name, {}).get("governs_list") or []
    if commodity:
        bridged = [CLASSICAL_TO_MODERN[c]["instrument"]
                   for c in commodity[:MAX_LIST_ITEMS] if c in CLASSICAL_TO_MODERN]
        line = "%scommodity (S1): %s" % (pad, _short(commodity))
        if bridged:
            line += "  | tradeable (S21): %s" % _short(bridged, 3)
        lines.append(line)

    sign_comm = SIGN_COMMODITY.get(b["sign"], {}).get("commodities_list") or []
    if sign_comm:
        lines.append("%ssign commodity (S2): %s" % (pad, _short(sign_comm)))

    # Modernised for display: the model reads these rows and will name places from them, so it must
    # not be handed "Transvaal" or "Abyssinia". See kb/aliases.py.
    region = modern_list(PLANET_REGION.get(name, {}).get("regions_list") or [])
    country = modern_list(SIGN_COUNTRY.get(b["sign"], {}).get("countries_list") or [])
    zone = NAKSHATRA_ZONE.get(b["nakshatra_index"], {})
    geo = []
    # Three entries each rather than four: modernised names run longer than the KB's archaic ones
    # ("South Africa (Gauteng)" for "Transvaal"), which took full detail 30 tokens over budget.
    # Trimming the geography lists is cheaper than losing a whole detail level.
    if region:
        geo.append("region (S14): %s" % _short(region, 3))
    if country:
        geo.append("country (S12): %s" % _short(country, 3))
    if zone:
        geo.append("zone (S13): %s / %s"
                   % (zone["zone"], _short(modern_list(zone["regions"]), 3)))
    if geo:
        lines.append(pad + "  ".join(geo))

    return lines


def _state_header(state, level="full"):
    """
    COSMIC STATE block. A layer absent from `state` is omitted rather than stubbed, so the model
    is never told a fact the engine did not actually compute.

    Degrades with `level` for the same reason the body blocks do: amendment 1 requires narrative
    detail to be sacrificed before bodies are. Long prose (dasha themes, sector bias, yoga
    evidence) goes at 'core'; the aspect list shortens at 'minimal'. The gate lords, boundaries
    and modulation factors are load-bearing and survive every level.
    """
    chart = state["chart"]
    verbose = level == "full"
    lines = [
        "## COSMIC STATE (computed - authoritative, do not recompute)",
        "Ayanamsa %s (%.4f deg) | as of %s"
        % (chart["ayanamsa"], chart["ayanamsa_deg"], chart["as_of"]),
    ]

    dasha = state.get("dasha")
    if dasha:
        lines.append(
            "DASHA (S28 gate): %s MD (to %s) / %s AD (to %s) / %s PD (to %s)"
            % (dasha["md"]["lord"], dasha["md"]["end"],
               dasha["ad"]["lord"], dasha["ad"]["end"],
               dasha["pd"]["lord"], dasha["pd"]["end"]))
        juncture = dasha.get("next_juncture")
        if juncture:
            lines.append(
                "       next juncture %s (%s changeover, %dd, context weight %.1f)%s"
                % (juncture["date"], juncture["level"], juncture["days"],
                   juncture["context_weight"],
                   "  [VOLATILITY WINDOW]" if juncture.get("volatility_window") else ""))
        if verbose and dasha.get("themes"):
            lines.append("       running themes: %s" % dasha["themes"])
        if verbose and dasha.get("md_sector_bias"):
            lines.append("       MD sector bias: %s" % dasha["md_sector_bias"])
        # Provenance: which calibration produced these dates (see migration doc section 5).
        # Never dropped — an untraceable dasha date is worse than no dasha date.
        lines.append("       [dasha source: %s, natal Moon %.4f]"
                     % (dasha.get("source", "?"), dasha.get("natal_moon_lon", 0.0)))

    yogas = state.get("yogas")
    if yogas:
        lines.append("YOGAS (S29 ambient modulation):")
        for y in yogas:
            factors = ", ".join("%s x%.2f" % (target, mult)
                                for target, mult in sorted(y["factors"].items()))
            line = "       %s %s - %s | %s" % (y["name"], y["status"], y["direction"], factors)
            if verbose:
                line += " | %s" % y["evidence"]
            lines.append(line)

    if chart["retrogrades"]:
        lines.append("RETROGRADE: " + ", ".join(chart["retrogrades"]))
    if chart["combust"]:
        lines.append("COMBUST: " + ", ".join(chart["combust"]))

    cab = state.get("samvatsar")
    if cab and cab.get("portfolios"):
        p = cab["portfolios"]
        headline = [(k, p[k]) for k in ("king", "finance", "defence", "industry", "agriculture")
                    if k in p and p[k].get("lord")]
        if headline:
            lines.append("SAMVATSAR CABINET (S4, %s -> %s):"
                         % (cab.get("pratipada", "?"), cab.get("window_end", "?")))
            for _key, entry in headline:
                line = "       %s: %s" % (entry["portfolio"], entry["lord"])
                if verbose and entry.get("effect"):
                    line += " - %s" % entry["effect"]
                lines.append(line)

    tight = [a for a in chart["aspects"] if a["tight"]]
    if tight:
        limit = 10 if level != "minimal" else 4
        shown = tight[:limit]
        line = "TIGHT ASPECTS (orb <= 3 deg): " + "; ".join(
            "%s %s %s %.1f" % (a["a"], a["type"], a["b"], a["orb"]) for a in shown)
        if len(tight) > len(shown):
            line += "; +%d more" % (len(tight) - len(shown))
        lines.append(line)

    return lines


# Event calendar caps per detail level: (max tier included, max entries).
# The KB asks for "15-20 trigger_calendar entries spanning 6 months", so the full cap is set to 20
# rather than to whatever fits: a 180-day scan yields ~55 events, which is deliberately more than
# the report needs. Ranking is (tier, date), so Tier 1 never loses a slot to an earlier Tier 2.
EVENT_CAPS = {"full": (2, 20), "core": (2, 14), "minimal": (1, 10)}


def _event_calendar(state, level, cap_override=None):
    """
    EVENT CALENDAR block, tier-filtered and capped.

    Whatever is excluded is stated in the output — a silent cap reads to the model as "this is
    everything", which is exactly the failure the migration doc warns about under "no silent caps".

    `cap_override` lets the budget squeeze the calendar before it starts dropping bodies: an event
    line carries one date, while a body block carries eight resolved KB lookups, so events are the
    cheaper thing to lose.
    """
    events = state.get("events")
    if not events:
        return []

    max_tier, max_entries = EVENT_CAPS[level]
    if cap_override is not None:
        max_entries = cap_override
    if max_entries <= 0:
        return ["", "## EVENT CALENDAR omitted entirely to fit the token budget (%d events computed)"
                % len(events)]
    eligible = [e for e in events if e["tier"] <= max_tier]
    # Rank by tier then chronology so Tier 1 never loses its slot to an earlier Tier 2.
    ranked = sorted(eligible, key=lambda e: (e["tier"], e["jd"]))[:max_entries]
    shown = sorted(ranked, key=lambda e: e["jd"])
    omitted = len(events) - len(shown)

    lines = ["", "## EVENT CALENDAR (S22 tier, S24 decay applied; computed from the ephemeris)"]
    for e in shown:
        extra = ""
        if e["type"] == "eclipse":
            if e.get("effect_months") is None:
                extra = "  [%s window; not visible from %s, no effect period claimed]" % (
                    e["window"], e.get("observer", "the reference location"))
            else:
                extra = "  [%s window; %.1fh local -> ~%.0f months of effect" % (
                    e["window"], e["duration_hours"], e["effect_months"])
                # Surface the rulebook's own disagreement rather than resolving it silently.
                if e.get("effect_months_within_kb_bracket") is False:
                    lo, hi = e["effect_months_kb_bracket"]
                    extra += "; NOTE S24 brackets %s effects at %.0f-%.0f months, so the S5 " \
                             "ratio and the S24 range disagree here" % (e["kind"], lo, hi)
                extra += "]"
        elif e["type"] == "sign_change" and e.get("retrograde_entry"):
            extra = "  [retrograde entry]"
        lines.append("%s  T%d x%.2f  %s%s"
                     % (e["date"], e["tier"], e["decay"], e["detail"], extra))
    if omitted > 0:
        lines.append("(+%d further events omitted: tier > %d or beyond the %d-entry cap)"
                     % (omitted, max_tier, max_entries))
    return lines


# Ledger caps per detail level. The KB asks for "10-15 crystal_ball predictions", so the full cap
# sits inside that range rather than dumping every scored signal. 12 rather than 15 because full
# detail measured 4,097 tok against the 4,000 budget and the ledger was the cheapest place to find
# the difference — trimming three of the lowest-ranked live signals rather than dropping a whole
# body block or moving the budget again.
LEDGER_CAPS = {"full": 12, "core": 10, "minimal": 6}


def _signal_ledger(state, level, cap_override=None):
    """
    RESOLVED SIGNAL LEDGER block — the pipeline's output, which the model narrates rather than
    re-derives.

    Suppressed signals are counted but not listed: they are the ones the pipeline rejected, and
    re-surfacing them would invite the model to reinstate calls the arbitration already killed. The
    count is stated so the omission is visible.
    """
    signals = state.get("signals")
    if not signals:
        return []

    cap = LEDGER_CAPS[level] if cap_override is None else cap_override
    live = [s for s in signals if s["output_class"] != "suppressed"]
    if cap <= 0:
        return ["", "## RESOLVED SIGNAL LEDGER omitted entirely to fit the token budget "
                "(%d live signals computed)" % len(live)]
    shown = live[:cap]
    suppressed = len(signals) - len(live)

    lines = ["", "## RESOLVED SIGNAL LEDGER (pipeline complete - authoritative, do not re-score)"]
    for s in shown:
        flags = []
        if s.get("counter_dasha"):
            flags.append("COUNTER-DASHA")
        if s.get("counter_trend") is True:
            flags.append("COUNTER-TREND")
        elif s.get("counter_trend") is None:
            flags.append("tape unchecked")
        if s["trace"]["precision"].get("rotation_imminent"):
            flags.append("ROTATION IMMINENT")
        flag_str = ("  [" + "; ".join(flags) + "]") if flags else ""

        lines.append("%s %s %s -> %d%% %s (%s)%s"
                     % (s["id"], s["target"][:46], s["direction"], s["probability"],
                        s["confidence"], s["output_class"], flag_str))
        lines.append("     driver %s | tier %d | gate %s | %s"
                     % (s["driver_body"], s["tier"], s["trace"]["gate"]["status"],
                        s["trace"]["divisional"]["summary"]))
        if level == "full":
            # Primary basis only. The full citation chain lives in engine_state; repeating all of it
            # here cost ~1,100 tokens and pushed the brief past its budget for no added reach.
            lines.append("     basis: %s" % (s["basis"][0] if s["basis"] else "unstated"))
            analogue = s.get("historical_analogue")
            if analogue:
                events = "; ".join(e["label"] for e in analogue["events"])
                lines.append("     analogue (S30 Stage 7): %s last held %s (%dy ago)%s"
                             % (analogue["configuration"], analogue["years"],
                                analogue["years_ago"],
                                (" - the period of %s" % events) if events else
                                " - no curated macro event in that range"))
            elif s.get("analogue_gap"):
                lines.append("     analogue (S30 Stage 7): %s" % s["analogue_gap"])
            if s.get("price_invalidation"):
                lines.append("     invalidation: %s" % s["price_invalidation"])

    if len(live) > len(shown):
        lines.append("(+%d further live signals beyond the %d-entry cap)"
                     % (len(live) - len(shown), cap))
    if suppressed:
        lines.append("(%d signals suppressed by the pipeline - rejected, not omitted for space)"
                     % suppressed)

    conflicts = state.get("conflict_log") or []
    if conflicts and level == "full":
        lines.append("")
        lines.append("## CONFLICT RESOLUTION (S30 Stage 1)")
        for c in conflicts[:5]:
            lines.append("%s: %s beat %s - %s"
                         % (c["target"], c["dominant"], ", ".join(c["opposing"]),
                            c["resolution_reasoning"]))
    return lines


def render(state, max_tokens=DEFAULT_MAX_TOKENS):
    """
    Render the brief. Returns (text, meta) where meta records the budget outcome:

        {"tokens", "chars", "detail_level", "bodies_included", "bodies_dropped", "within_budget"}

    `bodies_dropped` being non-empty is a signal the budget is too tight for the body set — it
    is surfaced rather than swallowed so the caller can log it (amendment 1).
    """
    chart = state["chart"]
    all_bodies = list(chart["bodies"])
    # Present in a stable, meaningful order: slowest (highest KB weight) first.
    ordered = sorted(all_bodies, key=lambda n: chart["bodies"][n]["slowness_rank"])

    dropped = []
    included = list(ordered)
    event_cap = None
    ledger_cap = None

    def _meta(text, level, ok):
        return {
            "tokens": estimate_tokens(text),
            "chars": len(text),
            "detail_level": level,
            "bodies_included": included,
            "bodies_dropped": dropped,
            "event_cap": EVENT_CAPS[level][1] if event_cap is None else event_cap,
            "ledger_cap": LEDGER_CAPS[level] if ledger_cap is None else ledger_cap,
            "within_budget": ok,
        }

    for level in DETAIL_LEVELS:
        # The header degrades alongside the body blocks, so narrative prose is always sacrificed
        # before any body is dropped (amendment 1).
        header = _state_header(state, level)
        while True:
            lines = list(header)
            lines.append("")
            lines.append("## BODIES (all tracked bodies; resolved rows tagged with KB section)")
            for name in included:
                lines.extend(_body_block(name, chart["bodies"][name], level))
            lines.extend(_event_calendar(state, level, event_cap))
            lines.extend(_signal_ledger(state, level, ledger_cap))
            text = "\n".join(lines) + "\n"

            if estimate_tokens(text) <= max_tokens:
                return text, _meta(text, level, True)

            # Only start shedding content once we are already at the leanest detail level.
            if level != DETAIL_LEVELS[-1]:
                break

            # Degradation order at 'minimal': event lines, then ledger entries, then bodies.
            # Bodies go last because each one carries eight resolved KB lookups that become
            # unreachable if withheld - and amendment 2's proposal channel depends on the model
            # having the raw rows to reason over, not just the engine's conclusions.
            current_events = EVENT_CAPS[level][1] if event_cap is None else event_cap
            if current_events > 0:
                event_cap = max(0, current_events - 4)
                continue
            current_ledger = LEDGER_CAPS[level] if ledger_cap is None else ledger_cap
            if current_ledger > 0:
                ledger_cap = max(0, current_ledger - 3)
                continue

            nxt = next((b for b in BODY_DROP_ORDER if b in included), None)
            if nxt is None or len(included) <= 1:
                return text, _meta(text, level, False)
            included.remove(nxt)
            dropped.append(nxt)

    raise AssertionError("unreachable: DETAIL_LEVELS exhausted without returning")
