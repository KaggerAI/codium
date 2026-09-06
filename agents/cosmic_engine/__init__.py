"""
cosmic_engine — deterministic computation layer for the Cosmic agents.

See docs/COSMIC_ENGINE_MIGRATION.md.

Currently in SHADOW mode: the engine computes alongside the existing prompt, logs how its
computed facts compare against what the model asserted, and stores its state — but no prompt is
modified and no engine value reaches the report. The Deploy 2 cutover flips that.

Mode is controlled by COSMIC_ENGINE_MODE:
    off     - do not compute at all (zero cost)
    shadow  - compute, persist, log the comparison; the model's output is still authoritative
    live    - engine values feed the prompt and the report (not yet implemented)

Import cost is data-only, so importing this package is safe from anywhere.
"""

import os
import sys
import traceback

ENGINE_MODE = (os.getenv("COSMIC_ENGINE_MODE", "shadow").strip().lower() or "shadow")
if ENGINE_MODE not in ("off", "shadow", "live"):
    print("COSMIC_ENGINE: unknown COSMIC_ENGINE_MODE=%r, falling back to 'off'" % ENGINE_MODE,
          file=sys.stderr)
    ENGINE_MODE = "off"

# Phase 1 delivers the chart layer only. dasha/yogas/events/signals arrive in Phases 2-4; the
# brief omits those blocks entirely until then rather than stubbing them, so the model is never
# told a fact the engine did not compute.
ENGINE_PHASE = "4.5 (chart + dasha + yogas + events + samvatsar + analogues + signals)"

# Forecast horizon for the event calendar. Matches the KB's six-month framing.
EVENT_WINDOW_DAYS = 180

# Whether to fetch the live price tape for Stage 7.5. Off by default: market_data makes live network
# calls, and the shadow-mode build runs inside the report thread where a slow fetch would delay the
# report. When off, counter_trend is None ("not checked") rather than False.
FETCH_TAPE = (os.getenv("COSMIC_ENGINE_TAPE", "").strip().lower()
              in ("1", "true", "yes", "on"))


def build_engine_state(dt_utc):
    """
    Compute the engine state for an explicit UTC timestamp.

    Deterministic: pass the same timestamp, get byte-identical output. Nothing in the engine
    calls now() (docs section 3.6). JSON-serializable throughout, because callers persist this
    through Redis.

    Layers absent from the returned dict are not-yet-implemented rather than computed-empty, so
    brief.py can tell the difference and omit the block rather than assert a fact the engine never
    derived.
    """
    from agents.cosmic_engine import (
        analogues, chart, dasha, events, pipeline, samvatsar, tape, yogas,
    )

    cast = chart.cast(dt_utc)
    naive = dt_utc.replace(tzinfo=None) if dt_utc.tzinfo else dt_utc
    state = {
        "engine_phase": ENGINE_PHASE,
        "chart": cast,
        "dasha": dasha.state(dt_utc),
        "yogas": yogas.detect(cast),
        "events": events.scan(dt_utc, days=EVENT_WINDOW_DAYS),
        "samvatsar": samvatsar.cabinet(samvatsar.samvatsar_year_for(naive)),
    }

    # Analogues must exist before arbitration, because Stage 7 attaches one to every signal.
    state["analogues"] = analogues.build(state, naive)

    # Stage 7.5 needs the live tape keyed by target. Only fetched when explicitly enabled; otherwise
    # every call comes back "not checked", which is honest, whereas False would read as "cleared".
    tape_by_target = {}
    if FETCH_TAPE:
        raw = signals_targets(state)
        tape_by_target = tape.fetch_tape(sorted(raw))

    state["signals"], state["conflict_log"] = pipeline.run(state, tape_by_target)
    state["tape_checked"] = bool(tape_by_target)
    return state


def signals_targets(state):
    """Targets the ledger will claim, so the tape can be fetched for just those."""
    from agents.cosmic_engine import signals

    return {s["target"] for s in signals.build(state["chart"], state.get("events", []))}


def score_model_proposals(state, proposals, tape_by_target=None):
    """
    Score model-proposed theses through the same pipeline (section 3.7 amendment 2).

    `proposals` is the `model_proposed_signals[]` array from the synthesis response. The model
    supplies no probability; every number here is computed. Returns the scored list.
    """
    from agents.cosmic_engine import pipeline

    tape_by_target = tape_by_target or {}
    out = []
    for i, proposal in enumerate(proposals or [], start=1):
        if not isinstance(proposal, dict):
            continue
        try:
            out.append(pipeline.score_proposal(
                proposal, state, tape_by_target.get(proposal.get("target")), index=i))
        except Exception as e:
            print("COSMIC_ENGINE: could not score proposal %d (%s: %s)"
                  % (i, type(e).__name__, e), file=sys.stderr)
    return out


def render_brief(state, max_tokens=None):
    """Render the compact prompt payload. Returns (text, meta)."""
    from agents.cosmic_engine import brief

    if max_tokens is None:
        max_tokens = brief.DEFAULT_MAX_TOKENS
    return brief.render(state, max_tokens=max_tokens)


def compare_with_model(state, structured):
    """Diff the engine's computed chart against the model's asserted planets[]."""
    from agents.cosmic_engine import compare

    return compare.compare(state, structured)


def run_shadow(dt_utc):
    """
    Build engine state for a run, logging failures instead of raising.

    Returns the state dict, or None when the mode is 'off' or the engine errored. Shadow mode
    must never be able to break a production report, so every failure path here is swallowed
    after logging.
    """
    if ENGINE_MODE == "off":
        return None
    try:
        state = build_engine_state(dt_utc)
        text, meta = render_brief(state)
        msg = ("COSMIC_ENGINE: shadow state built - phase %s, brief %d tok (%d ch), detail=%s"
               % (ENGINE_PHASE, meta["tokens"], meta["chars"], meta["detail_level"]))
        if meta["bodies_dropped"]:
            msg += " DROPPED=%s" % ",".join(meta["bodies_dropped"])
        if not meta["within_budget"]:
            msg += " OVER BUDGET"
        print(msg, file=sys.stderr)
        state["brief"] = text
        state["brief_meta"] = meta
        return state
    except Exception as e:
        print("COSMIC_ENGINE: shadow build failed (%s: %s) - continuing without it"
              % (type(e).__name__, e), file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return None


def log_shadow_comparison(state, structured):
    """
    Compare and log, swallowing all errors. Returns the comparison dict or None.

    The one-line summary is the corpus: after a week of daily warmups it shows how often the
    model's D9/D10/pada/sub-sector claims were actually right.
    """
    if state is None or ENGINE_MODE == "off":
        return None
    try:
        result = compare_with_model(state, structured)
        print("COSMIC_ENGINE: " + result["summary"], file=sys.stderr)
        for d in result["disagreements"][:12]:
            print("COSMIC_ENGINE:   %-8s %-11s model=%s engine=%s"
                  % (d["body"], d["field"], d["model"], d["engine"]), file=sys.stderr)
        if len(result["disagreements"]) > 12:
            print("COSMIC_ENGINE:   ... %d more disagreements"
                  % (len(result["disagreements"]) - 12), file=sys.stderr)
        if result["bodies_missing_from_model"]:
            print("COSMIC_ENGINE:   model omitted: %s"
                  % ", ".join(result["bodies_missing_from_model"]), file=sys.stderr)
        return result
    except Exception as e:
        print("COSMIC_ENGINE: comparison failed (%s: %s)" % (type(e).__name__, e), file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return None
