"""
tape.py — Stage 7.5 price-tape reconciliation.

The current prompt asks the model to cross-check every directional call against the multi-session
trend in the live price feed and to set `counter_trend` when a call opposes a sustained move. That
is the single most fudgeable instruction in the whole prompt: nothing verifies it, and the model
has every incentive to state a clean thesis rather than a hedged one. Here it becomes a comparison.

The rule, verbatim from the synthesis prompt's Stage 7.5: a call contradicts the tape when the
10-day move opposes it AND at least 7 of the last 10 sessions opposed it, or when the feed carries a
[SUSTAINED DECLINE] / [SUSTAINED RALLY] flag against it.

Failure handling is deliberate. `market_data` makes live network calls, and per the known
cold-start behaviour the first tvDatafeed call in a fresh process fails. A missing tape must
therefore produce `counter_trend: None` — "not checked" — never `False`, which would read as
"checked and cleared" and quietly re-open the exact hole this module closes.
"""

import sys

SUSTAINED_SESSIONS = 7      # "at least 7 of the last 10 sessions opposing"
SUSTAINED_WINDOW = 10


def fetch_tape(names):
    """
    Fetch trend metrics for the given instrument display names via agents.utils.market_data.

    Returns {name: metrics_or_None}. Never raises: every instrument that cannot be resolved or
    fetched maps to None, and the caller reports those as unchecked.
    """
    out = {}
    try:
        from agents.utils import market_data
    except Exception as e:
        print("COSMIC_ENGINE: market_data unavailable (%s: %s) - tape unchecked"
              % (type(e).__name__, e), file=sys.stderr)
        return {name: None for name in names}

    for name in names:
        try:
            entry = market_data._INDEX_BY_NAME.get(name)
            if entry is None:
                out[name] = None
                continue
            yf_symbol, tv_tuple = entry
            _price, _chg, metrics = market_data._fetch_quote(name, yf_symbol, tv_tuple, [None])
            out[name] = metrics or None
        except Exception as e:
            print("COSMIC_ENGINE: tape fetch failed for %s (%s: %s)"
                  % (name, type(e).__name__, e), file=sys.stderr)
            out[name] = None
    return out


def flag_from_metrics(metrics):
    """
    Reproduce market_data._format_trend's [SUSTAINED ...] tag from raw metrics.

    Duplicated deliberately rather than parsed back out of the formatted string: the string is for
    the prompt, the flag is for arithmetic, and re-deriving it keeps this module independent of that
    formatting.
    """
    if not metrics:
        return None
    c10 = metrics.get("chg_10d")
    down = metrics.get("down_n", 0)
    up = (metrics.get("sess_n") or 0) - down
    if c10 is None:
        return None
    if c10 < 0 and down >= SUSTAINED_SESSIONS:
        return "SUSTAINED_DECLINE"
    if c10 > 0 and up >= SUSTAINED_SESSIONS:
        return "SUSTAINED_RALLY"
    return None


def reconcile(direction, metrics):
    """
    Compare a directional call against the tape.

    `direction` is "UP" or "DOWN"; `metrics` is a market_data._trend_metrics dict, or None.

    Returns:
        {"counter_trend": True|False|None, "checked": bool, "flag", "chg_10d", "down_n",
         "sess_n", "vs_sma_pct", "agreement", "margin", "summary"}

    counter_trend is None when the tape could not be checked. `margin` is how far past the 7/10
    threshold the opposing streak ran, so narration can hedge proportionally rather than treating a
    7/10 streak the same as a 10/10 one (section 3.7 amendment 3).
    """
    if not metrics:
        return {
            "counter_trend": None, "checked": False, "flag": None,
            "chg_10d": None, "down_n": None, "sess_n": None, "vs_sma_pct": None,
            "agreement": "unchecked", "margin": None,
            "summary": "tape unavailable - not checked",
        }

    c10 = metrics.get("chg_10d")
    down = metrics.get("down_n")
    sess = metrics.get("sess_n") or 0
    up = sess - (down or 0)
    flag = flag_from_metrics(metrics)
    opposing = down if direction == "UP" else up

    contradicts = False
    if c10 is not None:
        move_opposes = (c10 < 0) if direction == "UP" else (c10 > 0)
        streak_opposes = opposing is not None and opposing >= SUSTAINED_SESSIONS
        if move_opposes and streak_opposes:
            contradicts = True
    if flag == "SUSTAINED_DECLINE" and direction == "UP":
        contradicts = True
    if flag == "SUSTAINED_RALLY" and direction == "DOWN":
        contradicts = True

    if contradicts:
        agreement = "contradicts"
    elif c10 is None:
        agreement = "indeterminate"
    else:
        aligned = (c10 > 0) if direction == "UP" else (c10 < 0)
        agreement = "agrees" if aligned else "mixed"

    parts = []
    if c10 is not None:
        parts.append("10d %+.1f%%" % c10)
    if sess:
        parts.append("%d/%d sessions down" % (down or 0, sess))
    if metrics.get("vs_sma_pct") is not None:
        parts.append("%.1f%% %s %dd-MA" % (abs(metrics["vs_sma_pct"]),
                                           "above" if metrics["vs_sma_pct"] >= 0 else "below",
                                           metrics.get("sma_w", 20)))
    if flag:
        parts.append("[%s]" % flag)

    return {
        "counter_trend": contradicts,
        "checked": True,
        "flag": flag,
        "chg_10d": c10,
        "down_n": down,
        "sess_n": sess,
        "vs_sma_pct": metrics.get("vs_sma_pct"),
        "agreement": agreement,
        "margin": (opposing - SUSTAINED_SESSIONS) if opposing is not None else None,
        "summary": "%s vs tape: %s" % (direction, ", ".join(parts) or "no metrics"),
    }


def price_invalidation(direction, metrics):
    """
    A price-based invalidation condition for a counter-trend call.

    Stage 7.5 requires a contrarian call to carry an explicit price trigger rather than a vague
    reversal claim ("gold has fallen 10 sessions; the transit sets up a reversal ONLY IF gold
    reclaims X by [date]"). The level is expressed relative to the moving average, because that is
    the only reference level the metrics actually carry.
    """
    if not metrics or metrics.get("vs_sma_pct") is None:
        return None
    window = metrics.get("sma_w", 20)
    if direction == "UP":
        return ("invalid unless the instrument reclaims its %dd moving average "
                "(currently %.1f%% below it)" % (window, abs(metrics["vs_sma_pct"])))
    return ("invalid unless the instrument loses its %dd moving average "
            "(currently %.1f%% above it)" % (window, abs(metrics["vs_sma_pct"])))
