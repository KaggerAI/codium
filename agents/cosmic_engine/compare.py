"""
compare.py — diff the engine's computed chart against what the model asserted.

This is the payload of shadow mode (docs/COSMIC_ENGINE_MIGRATION.md section 4.0.1). The macro
report's `planets[]` entries carry the model's own D9/D10/sub-sector/value-chain claims, and the
engine now computes the same facts from the ephemeris. Comparing them measures the section 1.4
error rate directly — before anything is bet on the engine.

Design notes:
  * The model writes free prose into these fields ("Aquarius 1 deg 25' Dhanishtha Pada 3"), so
    matching is deliberately lenient: find a sign name anywhere in the string. A lenient match
    that occasionally scores a near-miss as agreement biases the measured error rate DOWNWARD,
    which is the safe direction — it will never manufacture a disagreement that is not there.
  * Fields the model omitted are counted as 'absent', never as agreement. Absence is itself a
    finding: the schema demands these fields.
  * Nothing here raises on malformed model output. Shadow mode must never be able to break a
    real report.
"""

import re

from agents.cosmic_engine.chart import NAKSHATRAS, SIGNS

# Model planet names vs engine keys. The schema asks for "planet name"; the model has been seen
# to write "Rahu (North Node)" and "Ketu (South Node)".
_NAME_ALIASES = {
    "rahu": "Rahu", "north node": "Rahu", "rahu (north node)": "Rahu",
    "ketu": "Ketu", "south node": "Ketu", "ketu (south node)": "Ketu",
}

# Fields compared, mapped from the model's schema key to the engine's chart key.
_FIELD_MAP = (
    ("position", "sign", "sign"),                       # D1 sign, parsed out of the prose
    ("navamsa_position", "d9_sign", "d9"),
    ("dashamsa_position", "d10_sign", "d10"),
)


def _canonical_body(raw):
    if not raw:
        return None
    s = str(raw).strip().lower()
    if s in _NAME_ALIASES:
        return _NAME_ALIASES[s]
    for sign_free in (s, s.split("(")[0].strip()):
        for body in ("Sun", "Moon", "Mercury", "Venus", "Mars", "Jupiter", "Saturn",
                     "Rahu", "Ketu", "Uranus", "Neptune", "Pluto"):
            if sign_free == body.lower():
                return body
    return None


def _find_sign(text):
    """First zodiac sign named anywhere in `text`, else None."""
    if not text:
        return None
    low = str(text).lower()
    hits = [(low.index(s.lower()), s) for s in SIGNS if s.lower() in low]
    return min(hits)[1] if hits else None


def _find_nakshatra_pada(text):
    """Extract (nakshatra, pada) from prose like 'Dhanishtha Pada 3' / 'Revati P2'."""
    if not text:
        return None, None
    low = str(text).lower()
    nak = None
    for n in NAKSHATRAS:
        # The model uses KB spellings, which differ from ours for two nakshatras; compare on a
        # prefix long enough to disambiguate but short enough to survive Dhanishta/Dhanishtha.
        if n.lower()[:7] in low:
            nak = n
            break
    m = re.search(r"\b(?:pada|p)\s*([1-4])\b", low)
    return nak, (int(m.group(1)) if m else None)


def compare(engine_state, structured):
    """
    Compare engine chart against the model's `planets[]`.

    Returns a JSON-serializable dict:
        {"comparable": int, "fields": {field: {"agree", "disagree", "absent"}},
         "disagreements": [...], "bodies_missing_from_model": [...], "summary": str}

    Returns a dict with "error" instead of raising if `structured` is unusable.
    """
    result = {
        "comparable": 0,
        "fields": {tag: {"agree": 0, "disagree": 0, "absent": 0} for _, _, tag in _FIELD_MAP},
        "disagreements": [],
        "bodies_missing_from_model": [],
        "summary": "",
    }
    result["fields"]["pada"] = {"agree": 0, "disagree": 0, "absent": 0}
    result["fields"]["sub_sector"] = {"agree": 0, "disagree": 0, "absent": 0}

    if not isinstance(structured, dict):
        result["error"] = "model output is not a dict (JSON parse likely failed)"
        return result
    planets = structured.get("planets")
    if not isinstance(planets, list) or not planets:
        result["error"] = "model output has no usable planets[] array"
        return result

    chart_bodies = engine_state["chart"]["bodies"]
    seen = set()

    for entry in planets:
        if not isinstance(entry, dict):
            continue
        body = _canonical_body(entry.get("name") or entry.get("symbol"))
        if body is None or body not in chart_bodies:
            continue
        seen.add(body)
        truth = chart_bodies[body]
        result["comparable"] += 1

        for model_key, chart_key, tag in _FIELD_MAP:
            claimed = _find_sign(entry.get(model_key))
            if claimed is None:
                result["fields"][tag]["absent"] += 1
                continue
            if claimed == truth[chart_key]:
                result["fields"][tag]["agree"] += 1
            else:
                result["fields"][tag]["disagree"] += 1
                result["disagreements"].append({
                    "body": body, "field": tag,
                    "model": claimed, "engine": truth[chart_key],
                    "model_raw": str(entry.get(model_key))[:160],
                })

        nak, pada = _find_nakshatra_pada(entry.get("position"))
        if pada is None:
            result["fields"]["pada"]["absent"] += 1
        elif pada == truth["pada"]:
            result["fields"]["pada"]["agree"] += 1
        else:
            result["fields"]["pada"]["disagree"] += 1
            result["disagreements"].append({
                "body": body, "field": "pada",
                "model": pada, "engine": truth["pada"],
                "model_raw": str(entry.get("position"))[:160],
            })

        # Sub-sector is prose on both sides, so score a distinctive-token match. This is a weak
        # signal by design; it flags wholesale disagreement, not wording differences.
        #
        # Tokens must be >5 chars AND match on a word boundary. Both guards are load-bearing:
        # with a 5-char minimum and bare containment, the model's "Infrastructure and
        # construction" scored as agreement with the engine's "Marine infra, fishing fleet,
        # aquaculture" because 'infra' appears inside 'infrastructure' — a materially wrong
        # sub-sector counted as correct.
        claimed_sub = str(entry.get("sub_sector_activated") or "").strip().lower()
        if not claimed_sub:
            result["fields"]["sub_sector"]["absent"] += 1
        else:
            truth_sub = truth["sub_sector"].lower()
            tokens = [t for t in re.split(r"[^a-z]+", truth_sub) if len(t) > 5]
            hit = any(re.search(r"\b%s\b" % re.escape(t), claimed_sub) for t in tokens[:6])
            key = "agree" if hit else "disagree"
            result["fields"]["sub_sector"][key] += 1
            if not hit:
                result["disagreements"].append({
                    "body": body, "field": "sub_sector",
                    "model": claimed_sub[:80], "engine": truth["sub_sector"][:80],
                    "model_raw": "",
                })

    result["bodies_missing_from_model"] = sorted(
        b for b in ("Sun", "Moon", "Mercury", "Venus", "Mars", "Jupiter", "Saturn", "Rahu", "Ketu")
        if b not in seen)
    result["summary"] = summarize(result)
    return result


def summarize(result):
    """One-line ASCII summary for stderr. Non-ASCII would raise on the Windows console."""
    if result.get("error"):
        return "engine/model comparison unavailable: %s" % result["error"]
    parts = []
    for tag in ("sign", "pada", "d9", "d10", "sub_sector"):
        f = result["fields"].get(tag)
        if not f:
            continue
        total = f["agree"] + f["disagree"]
        if total:
            parts.append("%s %d/%d" % (tag, f["agree"], total))
        if f["absent"]:
            parts.append("%s absent x%d" % (tag, f["absent"]))
    return "engine vs model over %d bodies: %s" % (result["comparable"], "; ".join(parts) or "no comparable fields")
