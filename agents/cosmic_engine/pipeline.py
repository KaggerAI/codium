"""
pipeline.py — the V2 Depth Pipeline as arithmetic.

Replaces the 1,359-token Stage 0-8 instruction block in the synthesis prompt, plus the per-prediction
reasoning the model was emitting to prove it had followed it. Same stages, same constants, executed
rather than described.

    Stage 0   dasha gate            section 28 rules G1/G2 -> tier delta, with the no-drop rule
    Stage 1   tier primacy          section 22 tier, slowest-planet tie-break
    Stage 2   eclipse override      section 30, +/-15d overrides, +/-30d upgrades one tier
    Stage 3   precision overlay     sections 25/26 - resolves WHICH sub-sector, never conviction
    Stage 4   divisional confirm    section 27's 8-row matrix -> the conviction band
    Stage 5   yoga modulation       section 29 factors, multiplicative, cap 85 / floor 30
    Stage 6   time decay            section 24 - affects RANKING only, see note below
    Stage 7.5 price-tape reconcile  the live tape, via tape.py
    Stage 8   band assignment       section 30's output classes

Two readings worth stating because they are easy to get wrong:

DECAY IS FOR RANKING, NOT PROBABILITY. Section 30 Stage 6 says decay multipliers "determine sorting
order in trigger_calendar" — it is a relevance weight, not evidence about whether a claim is true. An
imminent trigger is not more likely to be correct than a distant one, it is more actionable. So decay
feeds `rank_score` and never `probability`.

THE NO-DROP RULE IS AN INVARIANT, NOT A PREFERENCE. Section 30 Stage 0: "GATE FAIL NEVER deletes a
prediction... The dasha gate may never be the sole reason a signal disappears from output." A signal
pushed below the floor purely by the gate penalty is retained as a COUNTER-DASHA WATCH. Tests assert
this directly, because it is the one rule in the rulebook that exists to stop the system hiding
counter-evidence from itself.
"""

from agents.cosmic_engine import signals as signals_mod
from agents.cosmic_engine import tape as tape_mod
from agents.cosmic_engine.kb.dignity import ENEMIES, SLOWNESS_ORDER
from agents.cosmic_engine.kb.matrix import (
    CONFIRMATION_MATRIX, CONVICTION_CAP, CONVICTION_FLOOR, DIVISIONAL_PRIORITY,
    DIVISIONAL_PRIORITY_DEFAULT, band_for,
)
from agents.cosmic_engine.kb.tiers import TIER_WEIGHTS
from agents.cosmic_engine.yogas import net_factor

# Section 27's bands ordered weakest to strongest. The Stage 0 tier delta moves a signal along this
# ladder, which is what "reduce conviction by one tier" means once the matrix has spoken.
BAND_LADDER = ("REJECT", "LOW-WATCH", "LOW", "MEDIUM", "HIGH")
BAND_MIDPOINTS = {"REJECT": 0, "LOW-WATCH": 37, "LOW": 42, "MEDIUM": 57, "HIGH": 80}

# Section 22's tier weights are 0.40/0.30/0.20/0.10. Expressed as a nudge in probability points so a
# Tier 1 driver outranks a Tier 4 one without letting tier alone cross a band boundary.
TIER_NUDGE = {1: 6, 2: 2, 3: -2, 4: -6}

# Output classes, section 30.
CLASS_CRYSTAL_BALL = "crystal_ball"
CLASS_TRIGGER_CALENDAR = "trigger_calendar"
CLASS_COUNTER_DASHA_WATCH = "counter_dasha_watch"
CLASS_SUPPRESSED = "suppressed"


def _clamp(value, low, high):
    return max(low, min(high, value))


# ---------------------------------------------------------------------------
# Stage 0
# ---------------------------------------------------------------------------

def stage0_gate(signal, dasha_state):
    """
    Section 28 rules G1 and G2.

    The mechanical half is exact: is the trigger the mahadasha or antardasha lord?

    The semantic half — G2's "transit theme contradicts current dasha themes" — cannot be decided
    exactly, because "contradicts" is a judgment about subject matter. The formalization used here is
    the most defensible one available from the rulebook's own tables, and it is a PROXY:

        theme planet (section 20 sector rulership / section 1 commodity rulership)
          is an ENEMY of the mahadasha lord (naisargika maitri)  ->  GATE FAIL
        the target sits in the mahadasha lord's own sector list and the call opposes the
          section 28 rule G5 structural bias                     ->  GATE FAIL

    Both are recorded in `reason` so a reader can see the inference rather than trust it. The
    migration doc flags this as the least mechanizable step in the pipeline; nothing here pretends
    otherwise.
    """
    md, ad = dasha_state["md"]["lord"], dasha_state["ad"]["lord"]
    driver = signal["driver_body"]
    theme, theme_basis = signals_mod.theme_planet(signal)

    same_body_event = signal.get("event_type") in ("station", "sign_change") and driver == ad
    if driver == ad and same_body_event:
        return {"status": "DOUBLE AMPLIFY", "tier_delta": 2, "theme_planet": theme,
                "theme_basis": theme_basis,
                "reason": "%s is the antardasha lord and the trigger is a same-body %s"
                          % (driver, signal.get("event_type"))}
    if driver in (md, ad):
        which = "mahadasha" if driver == md else "antardasha"
        return {"status": "AMPLIFY", "tier_delta": 1, "theme_planet": theme,
                "theme_basis": theme_basis,
                "reason": "%s is the %s lord" % (driver, which)}

    if theme in ENEMIES.get(md, ()):
        return {"status": "FAIL", "tier_delta": -1, "theme_planet": theme,
                "theme_basis": theme_basis,
                "reason": "theme planet %s (%s) is a natural enemy of the %s mahadasha lord, so "
                          "the theme runs against the running dasha" % (theme, theme_basis, md)}

    md_sectors = [s.lower() for s in
                  signals_mod.PLANET_SECTOR.get(md, {}).get("sectors_list", ())]
    target = (signal.get("target") or "").lower()
    in_md_sectors = any(s in target or target in s for s in md_sectors if s)
    if in_md_sectors and signal["direction"] == "DOWN":
        return {"status": "FAIL", "tier_delta": -1, "theme_planet": theme,
                "theme_basis": theme_basis,
                "reason": "target sits in the %s mahadasha's own sector list (S28 G5 structural "
                          "bias) but the call is bearish" % md}

    return {"status": "PASS", "tier_delta": 0, "theme_planet": theme, "theme_basis": theme_basis,
            "reason": "theme planet %s is neutral to the %s/%s dasha lords" % (theme, md, ad)}


# ---------------------------------------------------------------------------
# Stages 1-2
# ---------------------------------------------------------------------------

def stage1_tier(signal):
    """Section 22 tier plus the slowest-planet tie-break used when two signals conflict."""
    body = signal["driver_body"]
    rank = SLOWNESS_ORDER.index(body) if body in SLOWNESS_ORDER else len(SLOWNESS_ORDER)
    return {"tier": signal["tier"], "weight": TIER_WEIGHTS.get(signal["tier"], 0.1),
            "slowness_rank": rank}


def stage2_eclipse(signal, events):
    """
    Section 30 Stage 2. Returns the tier delta an eclipse in the window confers.

    Note the asymmetry the rulebook specifies: within 15 days an eclipse OVERRIDES other Stage 1
    signals, which for a non-eclipse signal means it is displaced rather than boosted. Within 30 days
    everything upgrades one tier.
    """
    eclipses = [e for e in events if e["type"] == "eclipse"]
    if not eclipses:
        return {"tier_delta": 0, "status": "none", "reason": "no eclipse in the window"}

    nearest = min(eclipses, key=lambda e: abs(e["days_from_start"]))
    window = nearest["window"]
    is_eclipse_signal = signal.get("event_type") == "eclipse"

    if window == "override":
        if is_eclipse_signal:
            return {"tier_delta": 1, "status": "override-holder",
                    "reason": "this signal IS the overriding eclipse (%s)" % nearest["date"]}
        return {"tier_delta": -1, "status": "overridden",
                "reason": "eclipse on %s is within 15 days and overrides other Stage 1 signals"
                          % nearest["date"]}
    if window == "upgrade":
        return {"tier_delta": 1, "status": "upgraded",
                "reason": "eclipse on %s is within 30 days, upgrading one tier" % nearest["date"]}
    return {"tier_delta": 0, "status": window,
            "reason": "nearest eclipse %s is %s" % (nearest["date"], window)}


# ---------------------------------------------------------------------------
# Stage 3-4
# ---------------------------------------------------------------------------

def stage3_precision(signal, chart):
    """
    Sections 25/26. Resolves WHICH sub-sector and value-chain stage a signal addresses. Per section
    30 Stage 3 this "does NOT change tier or conviction" — it only sharpens the target, so no delta
    is returned.
    """
    b = chart["bodies"].get(signal["driver_body"], {})
    boundary = b.get("boundary", {})
    return {
        "pada": "%s P%s" % (b.get("nakshatra"), b.get("pada")),
        "sub_sector": b.get("sub_sector", ""),
        "decanate": b.get("decanate"),
        "value_chain_stage": b.get("value_chain_stage", ""),
        "rotation_imminent": boundary.get("rotation_imminent", False),
        "deg_to_pada_edge": boundary.get("pada_deg_to_edge"),
        "deg_to_decanate_edge": boundary.get("decanate_deg_to_edge"),
        "tier_delta": 0,
    }


def stage4_divisional(signal, chart):
    """
    Section 27's Confirmation Output Matrix, keyed on the driver's strength in D1, D9 and D10.
    Section 27 also sets priority: D10 decides sector and equity calls, D9 decides commodity and
    outcome calls — recorded so a reader knows which chart carried the verdict.
    """
    b = chart["bodies"].get(signal["driver_body"])
    if not b:
        return {"band": "LOW", "reject": False, "verdict": "driver not in chart",
                "d1": None, "d9": None, "d10": None, "priority": None}

    key = (b["strong_d1"], b["strong_d9"], b["strong_d10"])
    row = CONFIRMATION_MATRIX[key]
    priority = DIVISIONAL_PRIORITY.get(signal["target_class"], DIVISIONAL_PRIORITY_DEFAULT)
    return {
        "band": row["band"],
        "reject": row["reject"],
        "verdict": row["verdict"],
        "d1": b["dignity_d1"], "d9": b["dignity_d9"], "d10": b["dignity_d10"],
        "strong": {"d1": b["strong_d1"], "d9": b["strong_d9"], "d10": b["strong_d10"]},
        "priority": priority,
        "summary": "D1=%s D9=%s D10=%s -> %s (%s priority)"
                   % ("strong" if b["strong_d1"] else "weak",
                      "strong" if b["strong_d9"] else "weak",
                      "strong" if b["strong_d10"] else "weak", row["band"], priority),
    }


# ---------------------------------------------------------------------------
# Stage 5
# ---------------------------------------------------------------------------

def stage5_yoga(signal, chart, yogas):
    """
    Section 29/30 Stage 5. A signal is classed benefic or malefic by its DRIVER, because that is what
    the yoga modulations target — "DAMPENS benefic transit effects" refers to the transit, not to
    whether the call happens to be bullish.
    """
    b = chart["bodies"].get(signal["driver_body"], {})
    signal_class = "malefic" if b.get("malefic") else "benefic"
    factor, contributors = net_factor(yogas, signal_class)
    return {"signal_class": signal_class, "factor": round(factor, 4),
            "contributors": contributors,
            "summary": ("%s driver, x%.2f from %s" % (signal_class, factor,
                                                      ", ".join(contributors))
                        if contributors else "%s driver, no active modulation" % signal_class)}


# ---------------------------------------------------------------------------
# Arbitration
# ---------------------------------------------------------------------------

def arbitrate(signal, state, tape_metrics=None):
    """
    Run one signal through Stages 0-8 and return it enriched with a full trace.

    `tape_metrics` is a market_data._trend_metrics dict for the signal's target, or None. None means
    "not checked" and produces `counter_trend: None`, never False.

    The returned dict is the signal plus:
        probability, confidence, output_class, rank_score, counter_dasha, counter_trend,
        suppressed_by, trace{gate, tier, eclipse, precision, divisional, yoga, tape}
    """
    gate = stage0_gate(signal, state["dasha"])
    tier = stage1_tier(signal)
    eclipse = stage2_eclipse(signal, state.get("events", []))
    precision = stage3_precision(signal, state["chart"])
    divisional = stage4_divisional(signal, state["chart"])
    yoga = stage5_yoga(signal, state["chart"], state.get("yogas", []))
    tape = tape_mod.reconcile(signal["direction"], tape_metrics)

    # Stage 4 sets the band; Stage 0 and Stage 2 move it along the ladder.
    band_index = BAND_LADDER.index(divisional["band"]) if divisional["band"] in BAND_LADDER else 2
    band_index = _clamp(band_index + gate["tier_delta"] + eclipse["tier_delta"],
                        0, len(BAND_LADDER) - 1)
    band = BAND_LADDER[band_index]

    # Clamp at each step so every later multiplier acts on a valid probability rather than an
    # out-of-range intermediate. Section 30 Stage 5 is explicit that the yoga factor multiplies the
    # Stage 4 conviction, so saturation at the cap when several amplifying yogas stack is the
    # rulebook's own arithmetic, not an artefact here — `rank_score` preserves ordering among ties.
    probability = _clamp(BAND_MIDPOINTS[band] + TIER_NUDGE.get(signal["tier"], 0),
                         0, CONVICTION_CAP)
    probability = _clamp(probability * yoga["factor"], 0, CONVICTION_CAP)

    # Stage 7.5: a counter-trend call is reduced and must carry a price invalidation.
    #
    # The clamp above is load-bearing. Applying this penalty to an unclamped intermediate made it
    # invisible for any saturated signal: 85 x 1.32 = 112, x 0.80 = 90, which still clamps back to
    # 85 — so the single most important guard in the pipeline silently did nothing for exactly the
    # high-conviction calls it exists to restrain.
    price_invalidation = None
    if tape["counter_trend"]:
        probability *= 0.80
        price_invalidation = tape_mod.price_invalidation(signal["direction"], tape_metrics)

    probability = int(round(_clamp(probability, 0, CONVICTION_CAP)))

    counter_dasha = gate["status"] == "FAIL"
    suppressed_by = None

    if divisional["reject"] and not counter_dasha:
        output_class = CLASS_SUPPRESSED
        suppressed_by = "S27 matrix REJECT (all three charts weak)"
    elif probability < CONVICTION_FLOOR:
        # Section 30 Stage 0's no-drop rule. Would this signal have cleared the floor without the
        # gate penalty? If so, the gate is the sole reason it fell, and it must be retained.
        without_gate = BAND_LADDER[_clamp(BAND_LADDER.index(divisional["band"])
                                          + eclipse["tier_delta"], 0, len(BAND_LADDER) - 1)]
        floor_without_gate = (BAND_MIDPOINTS[without_gate]
                              + TIER_NUDGE.get(signal["tier"], 0)) * yoga["factor"]
        if counter_dasha and floor_without_gate >= CONVICTION_FLOOR:
            output_class = CLASS_COUNTER_DASHA_WATCH
        else:
            output_class = CLASS_SUPPRESSED
            suppressed_by = "conviction %d%% below the S30 floor of %d%%" % (
                probability, CONVICTION_FLOOR)
    elif counter_dasha and probability < 50:
        output_class = CLASS_COUNTER_DASHA_WATCH
    elif probability >= 50:
        output_class = CLASS_CRYSTAL_BALL
    else:
        output_class = CLASS_TRIGGER_CALENDAR

    out = dict(signal)
    out.update({
        "probability": probability,
        "confidence": band_for(probability),
        "band": band,
        "output_class": output_class,
        "suppressed_by": suppressed_by,
        "counter_dasha": counter_dasha,
        "counter_trend": tape["counter_trend"],
        "price_invalidation": price_invalidation,
        # Stage 6: decay ranks, it does not judge. See the module docstring.
        "rank_score": round(probability * signal.get("decay", 1.0) * tier["weight"], 4),
        "trace": {
            "gate": gate, "tier": tier, "eclipse": eclipse, "precision": precision,
            "divisional": divisional, "yoga": yoga, "tape": tape,
        },
    })

    # Stage 7 (section 30): a HIGH CONFIDENCE call must reference a historical analogue. The engine
    # supplies the dated configuration; the narration layer writes the sentence. When no analogue is
    # found the field says so explicitly rather than being left absent, because an unfilled Stage 7
    # requirement on a HIGH call is itself a finding.
    from agents.cosmic_engine import analogues as analogues_mod
    analogue = analogues_mod.for_signal(out, state.get("analogues"))
    out["historical_analogue"] = analogue
    if analogue is None and out["confidence"].startswith("HIGH"):
        out["analogue_gap"] = ("no historical analogue found within %d years for this driver"
                               % analogues_mod.MAX_BACK_YEARS)
    return out


def resolve_conflicts(scored):
    """
    Section 30's actual purpose: arbitrate signals that contradict each other.

    Per-signal scoring is not enough. Two generators can produce opposite directional calls on the
    same target, and publishing both is the "mixed signals" output failure the V2 engine was written
    to eliminate. This step finds those pairs and resolves them by the rulebook's stated hierarchy
    (section 30 Stage 1, restating the legacy section 23 rules):

        1. higher tier wins
        2. among the same tier, the slower planet wins
        3. a dated transit event outranks a standing condition
        4. failing all three, the higher conviction wins

    Rule 3 is an inference, not a quotation: section 30 Stage 1 says to "identify the primary transit
    driver", and a signal anchored to a dated ingress or station has one while a signal resting on a
    planet's current dignity does not. It is needed because rules 1 and 2 tie whenever the same
    planet drives both sides — which happens for real: an afflicted Jupiter weighs on its sectors
    (sections 1/20) while Jupiter's ingress makes its new sign's sectors lead (section 20's cycle
    rule). Those two rulebook lines genuinely disagree, and something has to break the tie.

    If all four rules tie, the resolution is recorded as arbitrary rather than dressed up as
    reasoned. The loser is suppressed with an explicit reason rather than dropped silently, and both
    sides receive a `conflict` record — which is what the report's `conflict_resolution_log` needs.

    Mutates and returns `scored`.
    """
    groups = {}
    for s in scored:
        key = (s["target"] or "").strip().lower()
        groups.setdefault(key, []).append(s)

    log = []
    for key, group in groups.items():
        directions = {s["direction"] for s in group
                      if s["output_class"] != CLASS_SUPPRESSED}
        if len(directions) < 2:
            continue  # no contradiction on this target

        live = [s for s in group if s["output_class"] != CLASS_SUPPRESSED]
        winner = sorted(live, key=lambda s: (
            s["tier"],                                  # lower tier number wins
            s["trace"]["tier"]["slowness_rank"],        # then the slower planet
            0 if s.get("event_date") else 1,            # then a dated transit over a standing state
            -s["probability"],                          # then higher conviction
            s["id"],                                    # deterministic last resort
        ))[0]

        losers = [s for s in live if s is not winner]
        record = {
            "target": winner["target"],
            "dominant": "%s %s %s (Tier %d, %s, gate %s)" % (
                winner["id"], winner["driver_body"], winner["direction"],
                winner["tier"], winner["confidence"], winner["trace"]["gate"]["status"]),
            "opposing": ["%s %s %s (Tier %d)" % (l["id"], l["driver_body"], l["direction"],
                                                 l["tier"]) for l in losers],
            "divisional_check": winner["trace"]["divisional"]["summary"],
            "active_yogas_effect": winner["trace"]["yoga"]["summary"],
            "precision_overlay": "%s / %s" % (winner["trace"]["precision"]["pada"],
                                              winner["trace"]["precision"]["value_chain_stage"]),
            "net_bias": "%s at %d%%" % (winner["direction"], winner["probability"]),
            "resolution_reasoning": _conflict_reason(winner, losers),
        }
        log.append(record)

        winner["conflict"] = {"role": "dominant", "beat": [l["id"] for l in losers],
                              "reasoning": record["resolution_reasoning"]}
        for loser in losers:
            loser["output_class"] = CLASS_SUPPRESSED
            loser["suppressed_by"] = (
                "S30 Stage 1 conflict on %r: lost to %s (%s)"
                % (winner["target"], winner["id"], record["resolution_reasoning"]))
            loser["conflict"] = {"role": "superseded", "lost_to": winner["id"],
                                 "reasoning": record["resolution_reasoning"]}
    return scored, log


def _conflict_reason(winner, losers):
    """One sentence naming which hierarchy rule actually decided the conflict."""
    loser = losers[0]
    if winner["tier"] < loser["tier"]:
        return "Tier %d outranks Tier %d" % (winner["tier"], loser["tier"])
    if winner["trace"]["tier"]["slowness_rank"] < loser["trace"]["tier"]["slowness_rank"]:
        return "same tier, so the slower planet %s outranks %s" % (
            winner["driver_body"], loser["driver_body"])
    if bool(winner.get("event_date")) != bool(loser.get("event_date")):
        return ("same tier and driver, so the dated transit (%s) outranks the standing condition"
                % winner.get("event_date"))
    if winner["probability"] != loser["probability"]:
        return "same tier, driver and anchor, so the higher conviction (%d%% vs %d%%) wins" % (
            winner["probability"], loser["probability"])
    return ("tie on every rulebook criterion (tier, planet speed, transit anchor, conviction) - "
            "resolved arbitrarily by signal id, and flagged as such")


def run(state, tape_by_target=None):
    """
    Arbitrate every signal, resolve contradictions, and return (signals, conflict_log) ranked
    strongest first.

    `tape_by_target` maps a signal target to its trend metrics. Targets absent from it are reported
    as unchecked rather than as cleared.
    """
    tape_by_target = tape_by_target or {}
    ledger = signals_mod.build(state["chart"], state.get("events", []))
    out = [arbitrate(s, state, tape_by_target.get(s["target"])) for s in ledger]
    out, conflict_log = resolve_conflicts(out)
    out.sort(key=lambda s: -s["rank_score"])
    return out, conflict_log


def score_proposal(proposal, state, tape_metrics=None, index=1):
    """
    Score a model-proposed thesis through the identical pipeline (section 3.7 amendment 2).

    The model supplies no probability and no conviction — only the thesis, its target, direction and
    which resolved rows it believes support it. Everything numeric is computed here, so an emergent
    idea the engine's generators missed can still reach the report without the model being able to
    assert confidence in it.
    """
    driver = proposal.get("trigger_body") or ""
    if driver not in state["chart"]["bodies"]:
        driver = max(state["chart"]["bodies"],
                     key=lambda b: -state["chart"]["bodies"][b]["slowness_rank"])
    signal = {
        "id": "M%02d" % index,
        "source": "model",
        "target": proposal.get("target", ""),
        "target_class": proposal.get("target_class", "sector"),
        "direction": (proposal.get("direction") or "UP").upper(),
        "driver_body": driver,
        "basis": ["model proposal: %s" % (proposal.get("basis") or "unstated")],
        "tier": int(proposal.get("tier") or 2),
        "decay": 1.0,
        "event_date": None,
        "event_type": proposal.get("trigger_event"),
        "horizon": proposal.get("horizon", "unspecified"),
        "thesis": proposal.get("thesis", ""),
    }
    return arbitrate(signal, state, tape_metrics)
