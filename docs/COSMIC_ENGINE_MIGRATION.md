# Cosmic Engine Migration — Implementation Blueprint

**Scope:** Convert the Cosmic Macro Agent's AI-derived predictive pipeline into a deterministic
Python engine, leaving the LLM responsible only for narration and genuinely generative work.
The Cosmic Micro Agent shares the same knowledge base and inherits most of the benefit.

**Status:** **The build block is complete.** Deploy 1 (Phase 0) and Phases 1, 2, 3, 4 and 4.5 are
implemented as of 2026-08-13, and the engine is live in **shadow mode** — computing, scoring and
logging, with no prompt or report touched. All open decisions resolved: dasha calibration (§5),
eclipse duration and brief budget (§4.2). Remaining: the **Deploy 2 cutover** (§4.3), then optional
Phase 6. Pre-migration prompts archived in [reference/](reference/).

**Current engine output at the pinned date:** 60 signals — 19 crystal_ball, 9 trigger_calendar,
32 suppressed — 4 conflicts resolved, 8 historical analogues, 0 Stage 7 gaps on HIGH-confidence
calls, brief ~4,290 tok of a 4,500 budget at full detail, **234 tests passing**, engine state 191 KB,
build time ~0.5 s.

**Before Deploy 2:** the shadow scorecard has never run against a live report, because that needs a
real macro run. The `COSMIC_ENGINE: engine vs model` lines from a few days of 08:00 IST warmups are
the entire evidence base for trusting the engine over the model, and they cannot be manufactured
locally. Collect them first.
**Written:** 2026-08-13 · branch `branch-with-AI-chat_functioning`
**Primary driver:** per-run context size and token cost; secondary: correctness and reproducibility.

**Measurement convention:** token counts are `len(chars) / 4` unless stated otherwise. They are
comparative, not billing-accurate. Values marked *measured* were computed against the working tree;
values marked *estimated* depend on live API responses and were not captured.

---

## 0. Purpose and non-goals

### Purpose

1. Cut the fixed prompt payload from ~21.4k tokens of rulebook prose to ~3k tokens of
   pre-resolved facts, at three call sites.
2. Move the entire "V2 Depth Pipeline" (Stages 0–8) out of the prompt and into Python, so
   conviction numbers become reproducible and auditable instead of model-asserted.
3. Bound the currently unbounded user-message feeds.

### Non-goals

- Changing the astrological method. The rulebook's logic is preserved verbatim; only its
  *execution substrate* moves from prompt to code. The one exception requiring an explicit
  decision is documented in §5.
- Changing the output JSON schema consumed by [agents.html](../agents.html). The schema is
  frozen; the engine fills fields the model used to fill.
- Touching the 16-hour cache or the 08:00 IST warmup. Both stay; they simply get cheaper.

---

## 1. Current state — the AI context exactly as assembled

### 1.1 Call graph

Macro entry points ([cosmic_agent.py](../agents/cosmic_agent.py)):

| Route | Handler | Notes |
|---|---|---|
| `POST /agent/cosmic/analyze` | `agent_cosmic_analyze` → thread → `_run_cosmic_analysis` | 16h cache, keyed `GLOBAL`, gated on matching `region_focus` |
| `GET /agent/cosmic/<job_id>/status` | `agent_cosmic_status` | poll |
| `POST /agent/cosmic/chat` | `agent_cosmic_chat` | one GPT-5.5 call per message |
| `GET /agent/cosmic/latest` | `agent_cosmic_latest` | cache read |

`_run_cosmic_analysis` is also called directly by `_cosmic_morning_warmup`
([handler.py:13193](../handler.py:13193)), so a full synthesis runs at least once per day
independent of user traffic.

Four pipeline steps ([cosmic_agent.py:184](../agents/cosmic_agent.py:184)):

| Step | Source | Produces |
|---|---|---|
| 1 | Perplexity `sonar-pro`, `enable_pro_search=True`, 180s | `geopolitical_context` |
| 2 | `generate_cosmic_data_report()` — **local Swiss Ephemeris, no API** | `astro_context` |
| 3 | Perplexity `/search` (12 domains, `recency=week`, `max_results=15`, `max_tokens_per_page=1024`) prefixed with `get_live_market_data()` | `economic_context` |
| 4 | GPT-5.5, `temperature=1`, `timeout=420`, `use_streaming=True` | the report JSON |

No `reasoning_effort` is passed at any cosmic call site, so all four GPT-5.5 calls
(macro synthesis, macro chat, micro synthesis, micro chat) run at the API default. The parameter
is supported and forwarded by `call_openai_api` when supplied
([handler.py:5249](../handler.py:5249)).

### 1.2 The prompt payloads

| Call site | System prompt composition | User message | Measured system size |
|---|---|---|---|
| Macro synthesis | `COSMIC_SYNTHESIS_PROMPT` (17,628 ch) with `{astro_framework}` = `""`, `{pdf_augmentation}` = KB (85,414 ch), `{region_focus}` | date + region + 3 raw feeds | **~103,071 ch ≈ 25.8k tok** |
| Macro chat | `COSMIC_CHAT_PROMPT` (2,520 ch) + `analysis[:40000]` + 3 feeds `[:15000]` each | the question | **~87,500 ch ≈ 21.9k tok/msg** |
| Micro synthesis | `COSMIC_MICRO_SYNTHESIS_PROMPT` (9,700 ch) + KB | ticker fundamentals + astro | **~95,100 ch ≈ 23.8k tok** |
| Micro chat | `COSMIC_MICRO_CHAT_PROMPT` (1,795 ch) + `analysis[:40000]` + `fundamental[:15000]` + `astro[:15000]` + KB | the question | **~155,500 ch ≈ 38.9k tok/msg** |

Measured constant sizes ([cosmic_prompts.py](../agents/prompts/cosmic_prompts.py)):

```
COSMIC_PDF_AUGMENTATION_TEXT     85,414 ch   ~21,353 tok   <- injected at 3 sites
COSMIC_SYNTHESIS_PROMPT          17,628 ch    ~4,407 tok
COSMIC_MICRO_SYNTHESIS_PROMPT     9,700 ch    ~2,425 tok
COSMIC_ASTRO_QUERY                3,625 ch      ~906 tok   <- DEAD, see 1.6
COSMIC_ECONOMIC_QUERY             2,946 ch      ~736 tok
COSMIC_CHAT_PROMPT                2,520 ch      ~630 tok
COSMIC_MICRO_CHAT_PROMPT          1,795 ch      ~448 tok
COSMIC_GEOPOLITICAL_QUERY           905 ch      ~226 tok
COSMIC_ASTRO_FRAMEWORK                0 ch                 <- empty placeholder, still formatted in
```

**The pre-migration prompt stack is archived verbatim in [reference/](reference/)** — the pristine
module from commit `102261ae` plus the macro and micro system prompts *as actually assembled and
sent to the model*. Those assembled files, not this table, are the authoritative record of the v1
AI context. See [reference/README.md](reference/README.md).

### 1.3 Two structural defects in the current context

**(a) Unbounded user message.** `geopolitical_context` and `economic_context` are interpolated
into `user_message` **raw** ([cosmic_agent.py:254-277](../agents/cosmic_agent.py:254)). The
`[:50000]` caps at lines 327–329 apply only when *storing* the result, not when building the
prompt. With `max_tokens_per_page=1024` across `max_results=15`, the economic feed alone can
reach ~60k chars (~15k tok). This is the most likely mechanical cause of run-to-run token
variance.

**(b) Cache-hostile ordering in micro chat.** `COSMIC_MICRO_CHAT_PROMPT`
([cosmic_prompts.py:1818](../agents/prompts/cosmic_prompts.py:1818)) places the dynamic
`{analysis}` *before* the static 85KB `{pdf_augmentation}`. Any prefix cache is invalidated by
the first token of the analysis, so all ~38.9k tokens are billed uncached on every message.
The macro synthesis prompt has the correct ordering (KB near the top, `{region_focus}` only
appearing later inside the schema).

### 1.4 FEED 2 claims more than it delivers

`COSMIC_SYNTHESIS_PROMPT` line 1271 tells the model FEED 2 contains:

> current planetary positions (sidereal Lahiri), transits, retrogrades, eclipses, pada positions,
> D9/D10 placements, India dasha status, active yogas

What `generate_cosmic_data_report()` ([ephemeris.py:76](../agents/utils/ephemeris.py:76)) actually
emits — **1,623 chars, ~405 tokens, measured**:

1. Sidereal + tropical position for 12 bodies, with nakshatra and pada
2. Active retrogrades
3. Combustion status
4. Tithi / paksha

Absent: **eclipses, D9, D10, dasha, yogas, ingress dates, retrograde station dates, aspects,
dignity**. The model is asked to derive all of these from raw longitudes plus 21.4k tokens of
prose, every run. This is simultaneously the token sink and a silent correctness hole — nothing
in the pipeline can detect a wrong navamsa or an invented eclipse date.

Note the ratio: **405 tokens of computed fact, 21,353 tokens of prose to interpret it.**

### 1.5 Output contract vs. what the UI renders

The schema demands per-prediction pipeline traceability. `renderCosmicReport`
([agents.html:7631](../agents.html)) renders, from `crystal_ball`: `prediction`, `confidence`,
`probability`, `timeframe`, `planetary_trigger`, `economic_anchor`, `historical_analogue`,
`invalidation_condition`.

These schema fields have **no reader anywhere in the macro renderer** (verified by grep across
`agents.html`; the `pipeline_trace` / `dasha_gate` hits in that file belong to the *micro*
renderer's `d.pipeline_trace` and `d.astro_identity` objects at lines 8314–8476):

`pada_precision`, `decanate_stage`, `dasha_gate`, `divisional_check`, `yoga_modulation`,
`counter_dasha`, `counter_trend`, `dasha_status`, `active_yogas`, `conflict_resolution_log`,
`recent_trend`, `pada_signal`, `dashamsa_strength`, `dasha_alignment`, `navamsa_position`,
`dashamsa_position`, `strength_assessment`, `sub_sector_activated`, `value_chain_stage`,
`pipeline_trace`.

They are enforced chain-of-thought, paid for in output tokens on every run. They do reach the
chat prompt via `analysis_context`, so they are not worthless — but after migration the engine
produces them for free and consistently, instead of the model producing them expensively and
unverifiably.

Fields the renderer *does* consume, and which therefore constrain the schema:

```
report_title report_subtitle
crystal_ball[]      prediction confidence probability timeframe planetary_trigger
                    economic_anchor historical_analogue invalidation_condition
dashboard[]         label value sentiment note
key_events[]        date title description severity
planetary_compass[] icon text badge sentiment
trigger_calendar[]  date trigger prediction markets_affected action conviction
planets[]           symbol name position effect prediction trigger_date badge sentiment
vedic_insights[]    title description
timeline[]          quarter subtitle events[]{date title description severity}
sectors[]           name score signal sentiment
sector_insights[]   title description
geopolitics[]       country flag badge sentiment analysis
macro_metrics[]     label value sentiment note
central_banks[]     name title analysis
commodities[]       icon text badge sentiment price_direction target_range
scenarios[]         type label probability description
time_horizons[]     title description
actionable          allocation rotation[] risk_signals india_strategy disclaimer
```

### 1.6 Dead code and dead constants

- `_fetch_astrological_data` ([cosmic_agent.py:104](../agents/cosmic_agent.py:104)) is defined and
  **never called**. Step 2 uses `generate_cosmic_data_report()` instead. The function is the only
  consumer of `COSMIC_ASTRO_QUERY` (906 tok).
- `COSMIC_ASTRO_FRAMEWORK = ""` — an empty placeholder still passed through `.format()` at three
  sites, kept "for backward compatibility."
- KB §23 "SIGNAL CONFLICT RESOLUTION (LEGACY)" — its own heading says *superseded by Section 30*,
  and §30 is fully present. 125 tok of contradictory instructions.
- `call_perplexity_api_fn` remains a required parameter of `register_cosmic_routes` and
  `_run_cosmic_analysis` but, once `_fetch_astrological_data` is removed, is used only by step 1.
  Keep the parameter; note it is single-use.

### 1.7 Token budget summary (per interaction, current)

| Interaction | Input | Output | Frequency |
|---|---|---|---|
| Macro synthesis | ~26k fixed + ~8–18k variable = **34–44k** | ~10–12k visible + reasoning | ≥1/day (warmup) + refreshes |
| Macro chat | **~22k** | ~1–2k | per message |
| Micro synthesis | **~24k** + fundamentals | ~5–8k | per ticker |
| Micro chat | **~39k**, uncached | ~1–2k | per message |

---

## 2. Convertibility audit

All 33 KB sections, measured, classified by what can replace them.

### Class A — pure lookup tables: keep the data, resolve in Python, inject only matched rows

The table stays authoritative; it moves from prose the model must scan into a dict the engine
indexes. Only rows the current sky actually hits enter the prompt.

| § | Section | Tok | Key | Resolves to |
|---|---|---|---|---|
| 25 | Nakshatra-pada → sub-sector (108 padas) | **4,251** | `(nakshatra, pada)` | 9–12 occupied padas ≈ 350 tok |
| 3 + 3A | Sun ingress + equity transmission | 1,402 | current Sun sign + affliction branch | 1 row + computed Teji/Manda |
| 4 | Samvatsar cabinet | 1,155 | weekday lord of 10 ingress moments | 10 computed portfolio holders |
| 16 | Planet mundane significations | 936 | trigger planets | 2–4 rows |
| 17/18 | Eclipse effects by sign | 935 | sign of upcoming eclipse | 1–2 rows |
| 20 | Modern financial sector correlations | 690 | trigger planets + retrogrades | 3–5 rows |
| 26 | Decanate value-chain stage (36) | 642 | `int(deg/10)` | 1 computed line per planet |
| 15 | Twelve mundane house significations | 589 | occupied houses of the cast chart | 3–5 rows |
| 12 | Sign → country/city | 540 | occupied/afflicted signs | 4–7 rows |
| 13 | Nakshatra → Indian zone | 385 | occupied nakshatras | 4–6 rows |
| 1 | Planet → commodity | 349 | active planets | 3–5 rows |
| 2 | Sign → commodity | 346 | occupied signs | 5–8 rows |
| 21 | Classical → modern instrument bridge | 342 | commodities named by §1/§2 | 3–6 rows |
| 14 | Planet → Indian region | 308 | afflicted planets | 2–4 rows |
| 9 | Planetary pair → weather/market | 306 | detected conjunctions within orb | 0–3 rows |
| 7 | Sapta Nadi Chakra | 273 | nakshatra of Moon/Venus/Mercury | 1–3 rows |
| 10 | Poornima/Amavasya signals | 206 | tithi + lunar month | 0–1 rows |

**Class A total: ~13,655 tok → ~1,400 tok injected.**

### Class B — deterministic computation: section text leaves the prompt entirely

| § | Section | Tok | Python replacement |
|---|---|---|---|
| 30 | Conflict Resolution Engine V2 | 1,359 | the arbitration *is* arithmetic: tier weight → gate delta → divisional matrix → Σ yoga modulation, cap 85 / floor 30 |
| 28 | India Vimshottari dasha | 1,309 | Vimshottari calculator; see §5 for the calibration decision |
| 29 | Mundane yogas | 850 | geometric detection from longitudes (12 yogas) |
| 27 | D9/D10 + confirmation matrix | 761 | two closed-form divisional formulas + an 8-row truth table |
| 11 | Stambhas / Megh / conception | 542 | calendar + nakshatra percentage math; `(Saka − 1508) mod 4` |
| 24 | Time decay model | 529 | recency multiplier from days-to-activation; juncture proximity for dashas |
| 19 | Mundane chart methodology | 428 | chart casting order — becomes the engine's control flow |
| 22 | Tier system | 406 | event-type → tier lookup |
| 6 | Earthquake triggers | 372 | configuration detection (clusters, kendra/2-12/6-8, fixed-sign axis, Sanghata triangles) |
| 5 | Eclipse impact rules | 235 | ±15/±30/±90-day windows, node polarity, duration-to-effect scaling |
| 8 | Weekday repetition | 196 | count weekdays in the lunar month |
| 23 | Legacy conflict resolution | 125 | **delete** — superseded by §30 |

**Class B total: ~7,112 tok → ~600 tok of computed results.**

### Class C — stays with the LLM

- Guiding Principle (from the Sages) — 118 tok — the "every position change is a trigger" framing
  that motivates the event scan. Keep, it is cheap and orienting.
- Guiding Principle V2 — 348 tok — output-class definitions. Rewrite to ~120 tok once the engine
  owns classification.
- Prose generation: `report_subtitle`, every `description` / `analysis` / `effect` field,
  `historical_analogue` narrative, `scenarios`, `time_horizons`, `geopolitics`, `central_banks`.
- Geopolitical interpretation of the Perplexity feed.
- Economic-snippet → `macro_metrics` extraction.
- Both chat surfaces.

### Net

| | Now | After | Δ |
|---|---|---|---|
| KB in prompt | 21,353 | ~2,100 resolved + ~900 retained prose | **−18,350** |
| `COSMIC_SYNTHESIS_PROMPT` | 4,407 | ~1,500 | −2,900 |

Full per-interaction and per-day token modelling is in **§9**. The headline: macro synthesis
input **~36.8k → ~13.8k (−63%)**, micro chat **~39.3k → ~17.2k (−56%)**, and on an illustrative
daily volume mix, total spend **−56%**.

---

## 3. Target architecture

### 3.1 The inversion

**Now:** the model derives signals from prose tables and asserts a conviction number; the prompt
demands pipeline fields as proof of work; nothing verifies them.

**After:** Python derives the signals and the conviction; the model narrates them; `report.py`
overwrites the pipeline fields with engine truth after the call, so narration cannot drift from
arithmetic.

### 3.2 Package layout

```
agents/cosmic_engine/
  __init__.py          # public API: build_engine_state(), render_brief(), merge_report()
  kb/
    __init__.py
    padas.py           # §25  PADA_SUBSECTOR[(nak, pada)] -> {sub_sector, navamsa_sign, navamsa_lord, names[]}
    decanates.py       # §26  stage table + decanate-lord formula
    signs.py           # §2 §3 §12 §17 §18  sign-keyed tables
    planets.py         # §1 §14 §16 §20  planet-keyed tables + retrograde behaviour
    houses.py          # §15  mundane house significations
    bridge.py          # §21  classical -> modern instrument
    yogas.py           # §29  definitions + modulation weights
    dignity.py         # exaltation / debilitation / own / friendly / mooltrikona
    tiers.py           # §22 tier weights + §24 decay multipliers
    matrix.py          # §27 confirmation matrix + §30 stage arithmetic constants
    dasha.py           # Vimshottari constants + India natal reference (see §5)
    nadi.py            # §7 Sapta Nadi + §11 stambha nakshatras
  chart.py             # positions, D1/D9/D10, pada, decanate, dignity, aspects,
                       # combustion, boundary proximity, house placement
  events.py            # ingresses, retrograde stations, eclipses, lunations over a window
  dasha.py             # exact MD/AD/PD + juncture proximity + Rule G4 volatility windows
  yogas.py             # detection
  samvatsar.py         # §4 cabinet from the 10 ingress moments
  signals.py           # raw signal ledger: driver, tier, decay, target sector, direction
  pipeline.py          # Stages 0-8 -> conviction %, gate, divisional verdict, yoga modulation
  tape.py              # Stage 7.5 counter_trend against market_data metrics
  brief.py             # -> the compact prompt payload (resolved rows only)
  report.py            # merge engine truth into the LLM's JSON
tests/cosmic_engine/   # golden-file tests, see §7
```

`agents/utils/ephemeris.py` keeps its current public function (other callers exist) and gains a
structured sibling; `chart.py` consumes the structured form.

### 3.3 The two-artifact contract

Everything downstream depends on exactly two objects.

**`engine_state`** — the full structured dict. Cached inside `result_data`, reused by chat,
merged into the report. Never sent to the model in full.

```python
{
  "as_of": "2026-08-13T09:14:00Z",
  "ayanamsa": "Lahiri",
  "bodies": {
    "Saturn": {
      "sid_lon": 331.42, "sign": "Aquarius", "deg": 1.42,
      "nakshatra": "Dhanishtha", "pada": 3,
      "d9_sign": "Libra", "d10_sign": "Aquarius", "vargottama": False,
      "decanate": 1, "decanate_lord": "Saturn", "value_chain_stage": "RAW/UPSTREAM",
      "retrograde": False, "combust": False,
      "dignity_d1": "own", "dignity_d9": "friend", "dignity_d10": "own",
      "boundary": {"pada_deg_to_edge": 1.91, "decanate_deg_to_edge": 8.58,
                   "rotation_imminent": False},
      "sub_sector": "Renewable energy, solar/wind tech",
      "names": ["Adani Green", "Tata Power Renewable", "Inox Wind"]
    }
  },
  "aspects": [{"a": "Saturn", "b": "Venus", "type": "opposition", "orb": 1.2}],
  "dasha": {"md": {"lord": "Mars", "start": "...", "end": "..."},
            "ad": {"lord": "Rahu", "start": "...", "end": "..."},
            "pd": {"lord": "...", "start": "...", "end": "..."},
            "next_juncture": {"date": "...", "days": 195, "context_weight": 1.0},
            "md_sector_bias": "Defense, infrastructure, capex, real estate, energy, metals",
            "source": "computed|kb_calibrated"},
  "yogas": [{"name": "Kala Sarpa", "status": "ACTIVE",
             "modulation": "DAMPEN_BENEFIC|AMPLIFY_MALEFIC", "factor": 0.85}],
  "samvatsar": {"king": "Mars", "finance_minister": "Saturn", ...},
  "events": [{"date": "2026-09-02", "type": "sign_change", "body": "Mars",
              "from": "Leo", "to": "Virgo", "tier": 2, "decay": 1.5}],
  "eclipses": [{"date": "...", "kind": "solar", "sign": "Pisces",
                "node": "Rahu", "duration_h": 2.1, "window": "within_30d"}],
  "signals": [ ... ranked signal ledger, see 3.4 ... ],
  "model_proposed": [ ... LLM-proposed theses scored by the same pipeline, see 3.7 ... ],
  "tape": {"Nifty IT": {"chg_10d": -3.1, "down_n": 7, "sess_n": 10,
                        "vs_sma_pct": -2.4, "flag": "SUSTAINED_DECLINE"}}
}
```

**`brief`** — the *only* engine output entering the prompt. Budget ≤3,000 tokens. Resolved rows
only, no tables, no methodology. Per §3.7 the `## BODIES` block covers **every** body, not just
flagged triggers. Shape:

```
## COSMIC STATE (computed — authoritative, do not recompute)
Ayanamsa Lahiri · as of 2026-08-13 09:14 UTC
DASHA: Mars MD (to 2032-09-09) / Rahu AD (to 2027-02-24) / <PD>
       next juncture 2027-02-24 (195d, context weight 1.0)
       MD sector bias: defense, infra, capex, real estate, energy, metals
YOGAS: Kala Sarpa ACTIVE (dampen benefic / amplify malefic, x0.85)

## BODIES
Saturn  Aquarius 1.42  Dhanishtha P3  D9 Libra(friend)  D10 Aquarius(own)  dec 1 RAW/UPSTREAM
        sub-sector: renewable energy, solar/wind  ->  Adani Green, Tata Power Renewable, Inox Wind
        commodity (S1): blue sapphire, iron, wool ...   sector (S20): infrastructure, coal, PSU banks
        region (S14): Rajasthan, Punjab, Haryana, Delhi   country (S12): India, Punjab, Afghanistan
[... one block per body — ALL bodies, trigger or not; see 3.7 amendment 1 ...]

## EVENT CALENDAR (next 180d, tier + decay applied)
2026-09-02  Mars -> Virgo            tier 2  decay 1.5
2026-09-17  Solar eclipse, Pisces    tier 1  decay 2.0  [override window ±15d]

## RESOLVED SIGNAL LEDGER (pipeline complete)
S01 sector=Defence dir=UP conviction=72% tier=1 gate=AMPLIFY(Mars=MD lord)
    D9=strong D10=strong -> HIGH  yoga x0.85 -> 72%  tape: 10d +4.2%, 8/10 up, AGREES
    driver=Mars ingress Virgo 2026-09-02  invalidation=<computed price/date>
S07 sector=Nifty IT dir=UP conviction=41% tier=2 gate=FAIL(counter-dasha)
    D9=weak D10=strong -> LOW-WATCH  tape: 10d -3.1%, 7/10 down, CONTRADICTS
    -> counter_dasha=true counter_trend=true  route=trigger_calendar WATCH
```

### 3.4 Module contracts

```python
# chart.py
def cast(dt_utc: datetime) -> dict            # bodies + aspects + houses (India natal lagna)
def d9_sign(sid_lon: float) -> str            # navamsa; identical division to pada
def d10_sign(sid_lon: float) -> str           # odd signs from self, even from 9th, 3 deg steps
def decanate(sid_lon: float) -> tuple         # (1|2|3, lord, stage)
def dignity(body: str, sign: str) -> str      # exalted|own|mooltrikona|friend|neutral|enemy|debilitated
def boundary_proximity(sid_lon: float) -> dict # GRADED: returns deg-to-edge, never a bare bool

# events.py
def scan(start: datetime, days: int = 180) -> list   # ingress | station | eclipse | lunation
def eclipses(start: datetime, days: int) -> list     # swe.sol_eclipse_when_glob / lun_eclipse_when

# dasha.py
def vimshottari(natal_moon_lon: float, natal_jd: float, at: datetime) -> dict   # md/ad/pd + bounds
def juncture_weight(at: datetime, state: dict) -> float                        # Rule G4 / S24

# yogas.py
def detect(chart: dict) -> list

# signals.py
def build(chart, events, state) -> list    # raw signals: driver, tier, target, direction, decay

# pipeline.py
def arbitrate(signal: dict, state: dict) -> dict
# Stage 0 gate -> 1 tier -> 2 eclipse -> 3 precision -> 4 divisional -> 5 yoga
# -> 6 decay -> 7.5 tape -> 8 conviction band. Returns conviction %, all trace fields,
# and the output class (crystal_ball | trigger_calendar | counter_dasha_watch | suppressed).

# tape.py
def reconcile(signal: dict, metrics: dict) -> dict   # counter_trend + price invalidation

# brief.py
def render(state: dict, max_tokens: int = 3000) -> str

# report.py
def merge(llm_json: dict, state: dict) -> dict
# Engine fields overwrite model fields ONLY for keys in ENGINE_OWNED (3.7 amendment 4).
# Every key not in ENGINE_OWNED is model-owned and must pass through untouched.
```

### 3.5 Stage arithmetic to encode verbatim

Straight from §27/§28/§30 — these are the constants `pipeline.py` must reproduce:

- **Tier weights (§22):** T1 40%, T2 30%, T3 20%, T4 10%. Same-tier tie-break by slowness:
  Saturn > Jupiter > Rahu/Ketu > Mars > Sun > Venus > Mercury > Moon.
- **Gate deltas (§28 G2):** PASS 0, FAIL −1 tier, AMPLIFY +1, DOUBLE AMPLIFY +2.
  DOUBLE AMPLIFY requires trigger body == AD lord **and** a same-body event.
- **No-drop rule (§30 Stage 0):** GATE FAIL never deletes a signal. Below LOW it routes to
  `trigger_calendar` as a COUNTER-DASHA WATCH. The gate may never be the sole reason a signal
  disappears. **This is a hard invariant — assert it in tests.**
- **Eclipse windows (§30 Stage 2):** ±15d overrides Stage 1; ±30d upgrades one tier;
  31–90d equal-weighted with Tier 1.
- **Precision (§30 Stage 3):** pada wins sector identity, decanate wins value-chain stage;
  within ±1° of either boundary → ROTATION IMMINENT, half position. Does **not** change tier.
  Emit the actual degrees-to-boundary alongside the flag so narration can grade the hedge
  (§3.7 amendment 3) — a body at 1.01° should not read identically to one at 8°.
- **Divisional matrix (§27):** 8 rows over (D1, D9, D10) → HIGH >75 / MEDIUM 50–65 /
  LOW 35–50 / LOW-WATCH 30–45 / REJECT. D10 priority for equity+sector, D9 for
  commodity+outcome.
- **Yoga modulation (§30 Stage 5):** final = Stage-4 conviction × Π(active yoga factors);
  stacking multiplicative; cap 85%; floor 30% except protected COUNTER-DASHA WATCH.
- **Decay (§24):** ≤14d 2.0× · 15–30d 1.5× · 31–60d 1.0× · 61–90d 0.75× · >90d 0.5×.
  Dashas are exempt — weight by juncture proximity: ≤30d 2.0× · 31–90d 1.5× · else 1.0×.
- **Bands (§30):** HIGH >70 · MEDIUM 50–70 · LOW 30–50 · suppress <30.

### 3.6 Implementation constraints

- **Console encoding.** The KB data modules will carry Devanagari (`सूर्य`), `°`, `'`, and `→`.
  Anything reaching `print()`/stderr on the Windows console must be ASCII — non-ASCII raises
  `UnicodeEncodeError`, and the existing broad `except` blocks will swallow it as a silent
  feature failure. Keep non-ASCII in data and prompt payloads only; strip or `ascii(errors=...)`
  on every log path. Existing `print()` calls in `cosmic_agent.py` already use `✓`/`⚠`/emoji and
  survive only because output is redirected in production — do not copy that pattern.
- **Environment.** `venv/` is the live environment: **pyswisseph 2.10.03**, verified to expose
  `calc_ut`, `set_sid_mode`, `julday`, `revjul`, `deltat`, `houses_ex`, `get_ayanamsa_ut`,
  `sol_eclipse_when_glob`, `lun_eclipse_when`, `sol_eclipse_when_loc`. `codium_env/` does **not**
  have swisseph and its `python.exe` shim points at a nonexistent interpreter
  (`C:\Users\harsh\miniconda3`) — do not use it. `pyswisseph` is pinned in
  [requirements.txt:85](../requirements.txt).
- **Cold-start.** Per the known TradingView behaviour, the first `tvDatafeed` call in a fresh
  process fails; `tape.py` must tolerate a missing tape (degrade to `counter_trend: null`,
  never crash the engine) rather than assume `market_data` succeeded.
- **Determinism.** `build_engine_state()` must accept an explicit timestamp so tests can pin a
  date. No bare `datetime.now()` inside the engine.
- **`utcnow()`.** `ephemeris.py` uses the deprecated `datetime.datetime.utcnow()`. Use
  `datetime.now(timezone.utc)` in new modules.

### 3.7 Capability preservation (mandatory)

Determinism buys fidelity and reproducibility. It can also *narrow* the system, because a
resolver only emits combinations someone programmed, whereas a model reading all 30 sections at
once can notice ones nobody did. The four requirements below exist to keep that capability. They
are not optional polish — a migration that skips them trades breadth for cost.

**Amendment 1 — the brief is generous, not minimal.**
`## BODIES` covers **all 12 bodies** with their full resolved rows (§1 commodity, §2 sign
commodity, §12 country, §13 zone, §14 region, §20 sector, §25 sub-sector, §26 stage), not only
bodies the ledger flagged as triggers. Cost is ~500 tokens against a ~18k saving. Rationale: the
model cannot reason about a row it never sees, so anything `brief.py` withholds becomes
permanently unreachable — including counter-evidence. When the budget binds, drop *narrative
detail* first, then *event lines*, and *bodies* last; whatever is dropped is reported in the
returned metadata and logged. Never truncate silently.

Budget: **4,000 tokens** (raised from 3,000 during Phase 3 — see the Phase 3 notes in §4.2).

**Amendment 2 — a `model_proposed_signals[]` channel.**
The engine ledger is authoritative for *scoring*, not for *imagination*. The synthesis prompt
invites the model to propose theses the engine did not generate:

```
"model_proposed_signals": [
  {"thesis": "<directional call>", "target": "<sector|commodity|currency|index>",
   "direction": "UP|DOWN", "basis": "<which resolved rows in the brief support this>",
   "trigger_body": "<body>", "trigger_event": "<event or transit>"}
]
```

The model supplies **no probability and no conviction**. `pipeline.arbitrate()` then runs each
proposal through Stages 0–8 exactly as it does an engine-generated signal, and the result lands
in `engine_state["model_proposed"]` with a `source: "model"` tag. Proposals that clear the floor
merge into `crystal_ball`; the rest route per the normal output classes.

This is the single most important amendment. It preserves emergent cross-section synthesis — the
one genuine capability the migration would otherwise cost — while keeping every number the
product of arithmetic rather than assertion. It also creates a signal for engine gaps: proposals
that repeatedly clear the pipeline indicate a combination `signals.py` should generate natively.

**Amendment 3 — graded flags, not binary cliffs.**
Deterministic thresholds create discontinuities the model previously blurred. Every threshold
crossing must carry its underlying magnitude so narration can hedge proportionally:

| Flag | Also emit |
|---|---|
| `rotation_imminent` | `pada_deg_to_edge`, `decanate_deg_to_edge` |
| `counter_trend` | `chg_10d`, `down_n`/`sess_n`, `vs_sma_pct`, and the margin past the ≥7/10 line |
| eclipse window | exact days-to-eclipse, not just the bucket |
| dasha juncture | exact days-to-juncture alongside the context weight |

The flag drives routing; the magnitude drives language. Without this, output flips character
between consecutive days over a 0.02° move.

**Amendment 4 — never engine-overwrite prose.**
`report.merge()` operates on an explicit allowlist. `ENGINE_OWNED` contains only computed
scalars and trace fields:

```
probability confidence dasha_gate counter_dasha counter_trend divisional_check
yoga_modulation pada_precision decanate_stage pada_signal dashamsa_strength
dasha_alignment recent_trend navamsa_position dashamsa_position strength_assessment
sub_sector_activated value_chain_stage position trigger_date score signal
dasha_status active_yogas conflict_resolution_log pipeline_trace
```

Everything else is **model-owned in perpetuity** and passes through untouched — every
`description`, `analysis`, `effect`, `note`, `text`, `prediction`, `title`, plus
`report_subtitle`, `historical_analogue`, `invalidation_condition` narrative, `scenarios`,
`geopolitics`, `central_banks`, `time_horizons`, and `actionable`. A key absent from both the
allowlist and the model's output is a bug, not a default.

Corollary: `historical_analogue` has no engine source until the Phase 6 finder exists, so
**Phase 6's analogue finder is promoted ahead of Phase 5** (see §4). Shipping Phase 5 first
would strip the field's only producer and regress the report.

---

## 4. Execution plan

### 4.0 Sequencing: three deployments, not six

**Do not deploy phase by phase, and do not big-bang.** Ship Phase 0 alone, build Phases 1–4.5 as
purely additive code that never touches the prompt, then cut over once.

Two facts drive this. There is **no test suite** — no `pytest` in [requirements.txt](../requirements.txt),
no `tests/`, only a stray `test_gemini.py`. And verifying a macro change costs a real ~7-minute,
four-API-call run plus a manual JSON diff. Six production checkpoints against that is expensive;
one big-bang landing ~15 new astronomy modules with no baseline to bisect against is reckless.

| | Contents | Prompt changes? | Token change | Risk |
|---|---|---|---|---|
| **Deploy 1** | Phase 0 | yes, trivial | immediate | ~none |
| **Build block** | Phases 1, 2, 3, 4, 4.5 | **none** | **none** | ~none (shadow) |
| **Deploy 2** | Phase 5 (cutover) | yes, total | all of it | all of it |
| **Deploy 3** | Phase 6 | yes | small | low |

**Dependency graph — not linear:**

```
0 -> 1 -> (2 || 3) -> (4, 4.5) -> 5        [6 optional, any time after 5]
```

Phases 2 and 3 are independent of each other. Phase 4 is a hard barrier: it consumes chart,
dasha, yogas, events and tape. Build 2 before 3 — it is the subtlest code and the §5 decision
hangs on it.

**Rough sizing** (relative, not a commitment):

| Phase | Size | Character |
|---|---|---|
| 1 | 2–3 days | High volume, low difficulty. Mostly §25's 108 rows — the only phase that is transcription |
| 2 | 1–2 days | Low volume, **highest subtlety per line** |
| 3 | ~1 day | Self-contained: bisection + two swisseph wrappers |
| 4 | 2–3 days | Design-heavy — the signal taxonomy is a real decision |
| 4.5 | ~1 day + curation | Curation of the outcomes table is the long pole |
| 5 | 1–2 days | The only phase touching production surfaces |

### 4.0.1 Shadow mode

During the build block the engine computes `engine_state`, stores it alongside the model's output,
and logs a field-by-field comparison. **The prompt is not modified and `COSMIC_PDF_AUGMENTATION_TEXT`
is not touched.** Gate it with an env flag, matching the convention already used at
[live_concall.py:60](../agents/live_concall.py:60):

```bash
COSMIC_ENGINE_MODE=shadow    # off | shadow | live
```

Shadow mode is what makes the whole migration safe, and it produces something no other sequencing
can:

- **Evidence on the model's current error rate.** Diff `engine_state` against the model's
  `structured` on every daily warmup. After a week you know how often the model's D9/D10, pada
  sub-sector, yoga list and eclipse dates were actually right — *before* betting anything on the
  engine.
- **An empirical answer to §5.** Run `dasha.py` in both `kb_calibrated` and `computed` modes and
  see which the reports have implicitly been assuming. Today that decision is a coin flip made
  from a doc comment.
- **Cheap failure.** If the engine is wrong you learn from a log line, not a broken report.

Constraint: `store_latest_result` writes through to Redis
([base.py:141](../agents/base.py:141)), so `engine_state` must be JSON-serializable — ISO date
strings, no `datetime` objects.

### 4.0.2 Two defects this sequencing avoids

Both are real problems with deploying the phases individually, and both are the reason the build
block exists:

1. **Phase 1 deployed alone produces a self-contradicting prompt.** It removes the KB while
   keeping the Stage 0–8 instruction block, which says things like *"Look up exact pada of the
   trigger planet → identify SUB-SECTOR (Section 25)"* — pointing at a section that no longer
   exists. The brief tags rows with their source section (`sector (S20):`), but those imperatives
   still need rewriting from "look up X in Section N" to "use the pre-resolved (SN) row in the
   brief." **That prompt edit belongs to Phase 1, not Phase 4.** In shadow mode the question
   never arises, because Phase 1 ships no prompt change at all.
2. **Phases 1–3 mutate `COSMIC_PDF_AUGMENTATION_TEXT`, which the micro agent imports at three
   points.** Sequential deploys force either a lockstep migration of both agents or a frozen
   `_LEGACY` copy of the constant. Deferring all KB edits to the cutover lets both agents flip
   together, and removes the risk row about micro-agent coupling from §6 entirely.

### 4.1 Deploy 1 — Phase 0: bounded context and free wins

**No behaviour change intended. Ship first, on its own.**

Companion work, done in the same deploy: add `pytest` to `requirements.txt`, create
`tests/cosmic_engine/`, and capture the golden baseline per §7 **before** the edits below land.

1. Delete `_fetch_astrological_data` and the `COSMIC_ASTRO_QUERY` import + constant.
2. Delete KB §23; delete `COSMIC_ASTRO_FRAMEWORK` and its three `.format()` placeholders.
3. Cap the prompt-path feeds: `geopolitical_context[:12000]`, `economic_context[:24000]` in
   `user_message`. This bounds the variance described in §1.3(a).
4. Reorder `COSMIC_MICRO_CHAT_PROMPT` so `{pdf_augmentation}` precedes `{analysis}`.
5. Make the synthesis model and reasoning effort configurable, so verification runs are pinned
   and reproducible (§7):

   ```python
   COSMIC_SYNTHESIS_MODEL   = os.getenv("COSMIC_SYNTHESIS_MODEL", "gpt-5.5")
   COSMIC_REASONING_EFFORT  = os.getenv("COSMIC_REASONING_EFFORT") or None
   ```

   `call_openai_api` already accepts `reasoning_effort` and forwards it only when non-None
   ([handler.py:5249](../handler.py:5249)), so the default path is byte-identical to today.

**Files:** `agents/cosmic_agent.py`, `agents/cosmic_micro_agent.py`,
`agents/prompts/cosmic_prompts.py`, `requirements.txt`
**Exit criteria:** a macro run and a micro chat both succeed; report JSON parses; no schema change;
`COSMIC_SYNTHESIS_MODEL`/`COSMIC_REASONING_EFFORT` unset reproduces current behaviour exactly.

**Implemented 2026-08-13. Measured result:**

| | Before | After |
|---|---|---|
| KB (`COSMIC_PDF_AUGMENTATION_TEXT`) | 85,414 ch / ~21,353 tok | 84,884 ch / ~21,221 tok |
| Macro synthesis system prompt | 103,071 ch / ~25,768 tok | 102,493 ch / ~25,623 tok |
| Micro synthesis system prompt | 95,115 ch / ~23,779 tok | 94,587 ch / ~23,646 tok |
| **Micro chat cacheable prefix** | **~1,731 ch / ~430 tok** | **85,173 ch / ~21,293 tok** |
| Macro user-message ceiling | unbounded (~72k ch observed ceiling) | 36,000 ch hard cap |

The static-prompt reduction is deliberately small — §23 and the empty framework placeholder were
only ~1.1k tokens between them. The two real wins are the **micro-chat prefix going from ~430 to
~21,293 cacheable tokens** (the KB now precedes all dynamic content, so it is eligible for prefix
caching on every message instead of none), and the **macro user message becoming bounded**, which
is what removes the run-to-run variance in §1.3(a). Also removed: one dead Perplexity call path
(`_fetch_astrological_data`, 906 tok of dead constant) and the contradictory legacy §23.

### 4.2 Build block — Phases 1 to 4.5, shadow mode

Every phase below is additive code behind `COSMIC_ENGINE_MODE`. None modifies a prompt or the KB
constant. Merge freely; nothing here reaches a user until Deploy 2.

#### Phase 1 — chart kernel + Class A resolvers

1. `chart.py`: structured cast — D9, D10, decanate, dignity, aspects, boundary proximity, houses.
2. `kb/` modules for all Class A sections, transcribed from the KB **verbatim** (no editorialising).
3. `brief.py` emits the `## COSMIC STATE` + `## BODIES` blocks, every row tagged with its source
   section (`sector (S20):`, `sub-sector (S25):`).
4. Draft — **do not deploy** — the replacement of `{pdf_augmentation}` with the brief, *including*
   rewriting the Stage 0–8 imperatives from "look up X in Section N" to "use the pre-resolved
   (SN) row in the brief" (§4.0.2 defect 1). This draft lands with Deploy 2, not here.

**Saving:** ~13.7k → ~1.4k tok at cutover. Largest single win; lowest risk.
**Exit criteria:** every `sub_sector` / `pada_precision` / `decanate_stage` the model emits now
matches the engine's computed value for the same body (they frequently will not today — that is
the bug being fixed).

**Implemented 2026-08-13. Steps 1–3 landed; step 4 (the prompt draft) deferred to Deploy 2 by
design.**

| Delivered | |
|---|---|
| `tools/extract_cosmic_kb.py` | Generates the Class A modules by **parsing the KB** rather than hand-transcribing. Asserts every table's row count and that §25 is in strict zodiacal order; `--check` mode fails if the generated files drift from the KB, so prompt text and engine data can never silently disagree. |
| `kb/padas.py` | §25 — 108/108 padas, 27 nakshatras × 4, no gaps, no duplicates |
| `kb/signs.py` | §2, §12, §17, §18 — 12 signs each |
| `kb/planets.py` | §1, §14 (9 planets), §16, §20 (12 bodies) |
| `kb/zones.py` | §13 — all 27 nakshatras mapped to a zone |
| `kb/decanates.py`, `kb/bridge.py` | §26 (3 stages), §21 (15 rows) |
| `kb/dignity.py` | **Hand-authored, not from the KB** — see below |
| `chart.py` | D1/D9/D10, decanate + value-chain stage, pada sub-sector, dignity in all three charts, vargottama, aspects with orbs, graded boundary distances, combustion, retrograde |
| `brief.py` | The prompt payload. **2,124 tok at full detail on all 12 bodies** — inside the 3,000 budget, replacing 21,221 tok of KB. Degrades detail before dropping bodies and reports whatever it drops. |
| `compare.py` | The shadow scorecard: diffs the engine's computed sign/pada/D9/D10/sub-sector against the model's `planets[]` claims |
| `__init__.py` | `COSMIC_ENGINE_MODE` (off / shadow / live), `build_engine_state`, `render_brief`, `run_shadow`, `log_shadow_comparison` |
| `cosmic_agent.py` wiring | One `run_shadow()` call after step 2 and one `log_shadow_comparison()` after JSON parse. **No prompt change.** Both swallow every exception — shadow mode cannot break a report. |
| `tests/cosmic_engine/` | 71 tests, all passing |

`COSMIC_ENGINE_MODE` defaults to **`shadow`**, so the corpus starts accruing from the next daily
warmup without anyone remembering to switch it on. Cost is ~50 ms of local CPU; the only
production surface change is three additional keys (`engine_mode`, `engine_state`,
`engine_comparison`, ~20 KB) on the cached result, which no UI code reads. `COSMIC_ENGINE_MODE=off`
disables computation entirely.

Sample scorecard line (stderr, one per run):

```
COSMIC_ENGINE: engine vs model over 9 bodies: sign 9/9; pada 8/9; d9 4/7; d10 3/6; d9 absent x2
COSMIC_ENGINE:   Saturn   d9          model=Virgo engine=Capricorn
```

Two findings worth recording:

1. **The D9 formula is now cross-validated against the KB itself.** KB §25 carries a
   hand-written navamsa sign for each of the 108 padas; `chart.d9_sign()` derives it from
   `int(lon / 3°20′) % 12`. Those are independent sources for the same fact and they agree on
   **all 108 rows** — which validates the formula and the transcription simultaneously. `cast()`
   also asserts the agreement at runtime and raises rather than emitting a divisional verdict
   from disagreeing sources.
2. **The dignity and friendship tables were missing from the rulebook entirely.** §27 and §30
   require a strong/weak verdict per divisional chart, but the KB never supplies the exaltation,
   debilitation, own-sign, mooltrikona or naisargika-maitri tables those rules consume — the
   model was filling them from training knowledge on every run. `kb/dignity.py` adds them
   explicitly, with a provenance warning, and flags the one genuinely disputed choice (node
   dignity) as isolated and safe to change. Expect divisional verdicts to move once this is live;
   that is the §1.4 correctness hole closing, not a regression.

#### Phase 2 — deterministic layers

1. `dasha.py` — Vimshottari. **§5 resolved: configurable, defaulting to `kb_calibrated`.**
2. `yogas.py` — detection for all 12 configurations.
3. Divisional confirmation matrix, tier assignment, decay multipliers in `kb/matrix.py`,
   `kb/tiers.py`.
4. `samvatsar.py` — §4 cabinet. **Moved to Phase 3** — see below.
5. Prompt removal of §4, §22, §24, §27, §28, §29 lands at Deploy 2, not here.

**Saving:** ~5.0k tok input at cutover, plus a sharp drop in reasoning tokens — this is where the
model stops doing arithmetic.
**Exit criteria:** `dasha_status` in the report matches `engine_state["dasha"]` exactly; yoga list
is reproducible across two runs on the same timestamp.

**Implemented 2026-08-13. Steps 1–3 landed; step 4 re-sequenced.**

| Delivered | |
|---|---|
| `dasha.py` | Full Vimshottari chain, MD/AD/PD with exact boundaries, nearest-juncture detection with the §24 context weight, §28 Rule G5 sectoral bias, Rule G1 themes, and the mechanical half of the Stage 0 gate |
| `kb/dasha.py` | Sequence constants, India natal reference, and the **calibration switch** |
| `yogas.py` | All 12 §29 yogas detected geometrically, with evidence strings and `net_factor()` for Stage 5 stacking |
| `kb/yogas.py` | Definitions verbatim + the numeric modulation factors (**not from the KB** — see below) |
| `kb/matrix.py` | §27's 8-row confirmation matrix, §27 divisional priority, §30 Stage 5 bounds, Stage 8 bands, Stage 2 eclipse windows |
| `kb/tiers.py` | §22 tier weights and signal lists, §24 durations and recency multipliers, event→tier mapping |
| brief integration | Gate and yoga blocks now render, with dasha provenance |
| tests | **126 passing** total |

**§5 resolved.** `INDIA_NATAL["mode"]` defaults to `kb_calibrated`, which pins the natal Moon to
94.3857° and reproduces §28's table exactly — verified against all nine mahadasha boundaries
(Feb 1965 → Feb 2032) *and* the Mars antardasha schedule (Mars-Mars 149d, Mars-Rahu Jul 2025→Jul
2026, Mars-Jupiter from Jul 2026). Saturn balance 17.500y. The migration is therefore
behaviour-neutral on the gate. `mode = "computed"` is implemented and tested alongside it: Moon
93.9835°, balance 18.073y, Mars MD from 2025-09-09, and current AD Mars-**Rahu** rather than
Mars-**Jupiter**. A test asserts the two modes disagree on today's antardasha, so the tradeoff
cannot silently disappear. Every result carries `source`.

Three things worth recording:

1. **A second table was missing from the rulebook, in the same shape as the dignity gap.** §29
   states yoga modulations qualitatively ("DAMPENS benefic transit effects") and §30 Stage 5 then
   demands arithmetic ("conviction × Σ active yoga modulations", "combine multiplicatively"). The
   KB never bridges the two, so the model has been inventing the multipliers every run.
   `kb/yogas.py` supplies them explicitly — mild and symmetric (0.80/0.85/1.15/1.20) so a single
   yoga nudges rather than decides — with a provenance warning. `applies_to` targeting is part of
   the fix: a yoga that amplifies benefics must not touch a Saturn-driven signal, which prose
   alone could not enforce.
2. **Stage 0's GATE FAIL is deliberately *not* implemented in `dasha.gate()`.** The mechanical
   half (is the trigger the MD/AD lord → AMPLIFY / DOUBLE AMPLIFY) is decidable from the body
   alone. "Theme contradicts the dasha" is a judgment about a *prediction's subject*, needs the
   signal's target sector, and belongs to Phase 4. `gate()` returning PASS means "not amplified",
   never "verified consistent", and its `reason` string says so. This keeps §6's warning about
   the least-mechanizable step honest rather than papering over it.
3. **§4 Samvatsar moved to Phase 3.** The cabinet is determined by the weekday lord at ten
   specific solar ingress moments, so it depends on the ingress scanner rather than on anything in
   Phase 2. Landing it beside `events.py` is the correct dependency order, not a scope cut.

#### Phase 3 — event calendar

0. `samvatsar.py` — §4 cabinet, re-sequenced here from Phase 2: the ten portfolio holders are the
   weekday lords at ten solar ingress moments, so it depends on the ingress scanner below.
1. `events.py` — ingress and station scan by daily stepping with bisection to the exact crossing;
   eclipses via `sol_eclipse_when_glob` / `lun_eclipse_when`; lunations from Sun–Moon elongation.
2. Feed `trigger_calendar` dates from the scan instead of from Perplexity prose.
3. Remove §5, §6, §8, §11 text (1,345 tok). §7 and §10 stay as Class A resolvers — the Sapta
   Nadi channel and the Poornima/Amavasya row are looked up from the computed Moon nakshatra and
   tithi, not deleted.

**Saving:** ~1.3k tok, and a real correctness fix — the model currently invents eclipse and
station dates.
**Exit criteria:** every `trigger_calendar[].date` traces to an entry in
`engine_state["events"]`; spot-check two eclipse dates against an external ephemeris.

**Implemented 2026-08-13.**

| Delivered | |
|---|---|
| `events.py` | Sign ingresses and retrograde stations by daily stepping with 44-iteration bisection to the exact crossing; eclipses via `sol_eclipse_when_glob` / `lun_eclipse_when` with type, sign, nakshatra, node polarity and duration; lunations from Sun–Moon elongation. 56 events in a 180-day window, ~0.2 s. |
| `samvatsar.py` | §4 cabinet — all ten portfolios, with the Vedic sunrise weekday rule and Chaitra Shukla Pratipada |
| brief integration | `## EVENT CALENDAR` block (tier-filtered, capped at 20 per the KB's own "15-20 entries") and a cabinet block in the header |
| tests | **167 passing** total |

The scanner is validated against physics rather than against expectations, which caught more than
a fixture would have. Three internal-consistency tests: every eclipse must coincide with a lunation
found by an unrelated code path (elongation scan vs. swisseph eclipse search); Rahu and Ketu must
change sign simultaneously and stay 180° apart; and any retrograde sign re-entry must be preceded by
a retrograde station. All three hold — the Dec 13 Jupiter station at Leo 2.79° is what produces the
Jan 24 backward entry into Cancer, and the Aug 28 partial lunar eclipse lands on the same full moon.

Three findings:

1. **Two bugs in the cabinet, both silent.** (a) The samvatsar year runs Pratipada→Pratipada, so a
   calendar-year ingress scan took the Capricorn and Pisces portfolios from the *previous* cabinet.
   `sun_ingress_moments()` is now range-based, and the window is explicit in the output. (b) Vedic
   weekdays run sunrise-to-sunrise, and anchoring sunrise on "midnight UT of the civil day" is wrong
   for Delhi — local sunrise (~05:30 IST) falls near 00:00 UT, so it often lands on the previous UT
   date. That made a 12:26 IST moment read as pre-sunrise. Now solved by searching backward for the
   last sunrise at or before the moment. All ten weekday attributions were then cross-checked
   against Python's own calendar.
2. **Eclipse duration: resolved — local, observer at New Delhi.** §5 makes effect duration
   proportional to eclipse duration (solar 1 h → 1 year, lunar 1 h → 1 month), but "duration" has
   two readings. Global duration (first contact anywhere on Earth to last contact anywhere) drove
   the ratio to absurd outputs: the Feb 2027 annular eclipse spans 6.1 h globally → ~73 months of
   claimed market effect. Localised to New Delhi, the Aug 2027 total solar eclipse goes from 5.2 h
   (≈62 months) to 1.2 h (≈14 months). The rule comes from a naked-eye tradition that could only
   ever time what an observer saw, so local is the defensible reading.

   Corroboration: locally-visible lunar eclipses run ~1–3.5 h, which the 1-hour-per-month ratio
   maps directly onto §24's own stated 1–3 month bracket. The global figures overshoot it.

   Two consequences, both surfaced in the data rather than hidden:

   - **An eclipse can be invisible from Delhi** — the Feb 2027 annular is, and so is the Aug 2026
     partial lunar. Those get `effect_months: null` rather than an invented number. The eclipse
     stays in the calendar, because §17/§18 key their effects on the eclipse's zodiac *sign* and
     §30 Stage 2's override window is purely a date test — neither depends on visibility. Only the
     duration-derived effect period is withheld.
   - **The solar side of the rule is internally inconsistent in the source.** §24 brackets solar
     effects at 6–12 months while its own ratio yields 12 months for a 1-hour eclipse and more
     above that; §5 hedges with "months/years" rather than committing. No choice of duration
     reconciles it. Every eclipse therefore reports `effect_months`, `effect_months_kb_bracket` and
     `effect_months_within_kb_bracket`, and the brief prints an explicit NOTE when they disagree.
     Nothing is clamped.

   Residual limitation worth knowing: a Delhi observer is right for this agent's India tilt (§13,
   §14 and the §28 dasha are all India-specific), but an eclipse invisible from India may still
   matter to the report's global regions. That is why invisible eclipses stay in the calendar with
   their sign mapping intact.

3. **Brief budget: resolved — raised to 4,000 tokens.** At full detail the brief reached ~2,875 tok
   against the old 3,000 budget, leaving no room for Phase 4's signal ledger. The alternative was
   operating permanently at `core` detail, which drops the commodity, sign-commodity and geography
   rows for every body — and a row the model never receives is unreachable, since it can no longer
   fall back on reading the rulebook. ~1,000 extra tokens to keep several hundred facts reachable,
   against an ~18,000-token saving. Current headroom: 1,125 tok.

   The degradation order is now header prose → event lines → bodies, since a body block carries
   eight resolved KB lookups while an event line carries one date. An invariant test asserts across
   the whole ladder that no body is ever dropped while event lines remain.

#### Phase 4 — pipeline arbitration in Python

1. `signals.py` — build the raw ledger.
2. `pipeline.py` — Stages 0–8 per §3.5.
3. `tape.py` — Stage 7.5 `counter_trend` from `_trend_metrics`
   ([market_data.py:80](../agents/utils/market_data.py:80)), the single most fudgeable
   instruction in the current prompt.
4. `model_proposed_signals[]` per §3.7 amendment 2 — accept proposals, score them through the
   same `arbitrate()`, tag `source: "model"`, merge the survivors.
5. Remove §30 and the whole "MANDATORY PREDICTION METHODOLOGY" block from
   `COSMIC_SYNTHESIS_PROMPT`. Replace with: *the ledger is authoritative for scoring; narrate it;
   do not re-derive conviction; propose additional theses via `model_proposed_signals` without
   probabilities.*

**Saving:** ~1.4k tok input; the large win is output and reasoning — the model no longer emits
per-prediction pipeline reasoning. Amendment 2 adds back ~400 tok of output; accept it.
**Exit criteria:** the no-drop invariant holds (§3.5); conviction distribution over a fixed date
is byte-identical across two runs; every ledger entry appears in exactly one output class; a
hand-written `model_proposed_signals` fixture scores identically to an engine-native signal with
the same inputs.

**Implemented 2026-08-13.**

| Delivered | |
|---|---|
| `signals.py` | Seven generators, each implementing an explicit KB directional rule (§1 affliction, §20 retrograde and dignity, §25 pada, §20 cycles, §17/§18 eclipse, §13/§14 region). 60 signals at the pinned date. |
| `pipeline.py` | Stages 0–8 plus conflict resolution. Every signal carries a full trace: gate, tier, eclipse, precision, divisional, yoga, tape. |
| `tape.py` | Stage 7.5. Missing tape yields `counter_trend: None` — "not checked" — never `False`. |
| brief + wiring | `## RESOLVED SIGNAL LEDGER` and `## CONFLICT RESOLUTION` blocks; `COSMIC_ENGINE_TAPE` opt-in for live fetching |
| tests | **208 passing** total |

Direction is never invented. Every generator implements a quoted KB rule and cites it in `basis`;
a test asserts that no engine signal exists without a section citation. §1's own wording supplies
the core rule — afflicted planet → its commodities rise, strong planet → they ease — and §20
supplies the retrograde and cycle rules.

Four findings, two of them bugs the tests caught:

1. **§30's actual purpose was missing.** I built per-signal scoring and no conflict resolution
   between signals — so the first run produced "insurance UP" and "insurance DOWN" simultaneously,
   both at 85%. That is precisely the mixed-signals failure mode the V2 engine exists to eliminate.
   `resolve_conflicts()` now groups by target and applies §30 Stage 1's hierarchy: tier, then planet
   speed, then a dated transit over a standing condition, then conviction. An invariant test asserts
   no target ever carries contradictory live signals. The losers are suppressed with a stated reason
   and both sides get a record, which is what `conflict_resolution_log` needs.

2. **The counter-trend penalty silently did nothing.** It was applied before the final clamp, so for
   a saturated signal 85 × 1.32 = 112, × 0.80 = 90, clamped back to **85** — no reduction at all,
   for exactly the high-conviction calls Stage 7.5 exists to restrain. Now clamped at each step.
   This is the sort of ordering bug that survives review and fails silently in production.

3. **Two generators disagreed about the same planet.** One read `dignity_d1` directly while another
   used the affliction test, and Jupiter is currently *both* exalted in Cancer and combust. §1's
   wording resolves the precedence — affliction wins, since a combust planet cannot deliver the
   abundance dignity promises — and all generators now route through a single `condition()` verdict.

4. **§12's country names are archaic and will look odd in output.** The table is Ptolemaic, so
   Aquarius maps to "Abyssinia", "Piedmont" and "Transvaal". The engine reproduces it faithfully —
   that is what the rulebook says — but eclipse-driven geopolitics signals currently name
   19th-century polities at high conviction. Worth modernising §12 and regenerating, which is a
   one-file KB edit plus `python tools/extract_cosmic_kb.py`.

One design note: `probability` never includes decay. §30 Stage 6 uses decay for "sorting order in
trigger_calendar", and an imminent trigger is more *actionable*, not more likely to be true — so
decay feeds `rank_score` only. A test asserts the separation.

#### Phase 4.5 — historical analogue finder

**Promoted ahead of Phase 5 per §3.7 amendment 4.** Phase 5 tells the model to stop re-deriving;
if `historical_analogue` has no engine source by then, the field's only producer disappears.

1. Back-scan the ephemeris for prior occurrences of the current configuration (same body, same
   sign, same aspect within orb) and return dated matches.
2. Pair with a small curated outcomes table for the matches that have market significance.
3. Emit `analogues[]` into `engine_state`; the model still writes the narrative sentence from it.

**Saving:** none — this phase spends tokens to protect quality.
**Exit criteria:** every HIGH-confidence call carries an analogue whose date is verifiable in the
ephemeris; no analogue is asserted without a computed date behind it.

**Implemented 2026-08-13. Exit criteria met: 11 HIGH-confidence live signals, 11 analogues, 0 gaps.**

| Delivered | |
|---|---|
| `analogues.py` | Backward ephemeris scan for body-in-sign, aspect-in-sign, eclipse-in-sign and prior-mahadasha recurrences. 8 configurations found in ~0.15 s. |
| `kb/history.py` | 19 documented macro events, hand-authored with a provenance warning |
| pipeline + brief | Stage 7 attaches an analogue to every signal; a HIGH call without one is flagged `analogue_gap` rather than left blank |
| tests | **234 passing** total |

The dates and the outcomes come from different sources on purpose. Dates are computed — "Saturn in
Pisces last held 1968-1969" is checkable against any ephemeris. Outcomes come from the curated table
and carry **no causal claim**; where a computed range overlaps nothing in the table, the analogue
reports its dates with an empty `events` list rather than inventing a narrative. That split is what
turns Stage 7 from the report's last unverifiable field into a checkable one.

Four filters were needed before the output was usable, each removing a class of false analogue:

- **"0 years ago" entries.** A backward scan starting inside an occurrence re-detects that same
  occurrence — producing "Ketu-Moon conjunction in Leo, 0 years ago", the present dressed as a
  precedent. Now filtered by `MIN_YEARS_AGO`.
- **Degenerate pairs.** Rahu and Ketu are permanently opposed, so their "recurrence" is meaningless.
- **Fast bodies on either side of an aspect.** An aspect recurs on the period of the *faster* body,
  so a Moon-Ketu conjunction repeats monthly. Both sides must now be slow.
- **Mars in a sign.** Mars re-enters a sign every ~2 years, which is a calendar fact rather than a
  historical parallel. Mars still participates in aspect analogues, which is the form the KB's own
  example takes.

Two further findings:

1. **Eclipse signals needed their own analogue type.** They take the eclipsed luminary as driver, and
   no sign-occupancy scan can serve the Sun or Moon. "When did a lunar eclipse last fall in Aquarius"
   is the question with analogue value, and swisseph answers it directly with a backward eclipse
   search. This took coverage from 5/15 to 11/15.
2. **The remaining gaps exposed a tier bug.** Moon-driven commodity and sector signals were reaching
   85% conviction because every condition-based generator hardcoded Tier 2. §22 never lists Moon
   transits as Tier 1 or 2 — the Moon appears only in Tier 4, via "Poornima/Amavasya signals". So the
   rulebook was being made to demand Stage 7 evidence for signals it never intended as primary
   drivers, and that evidence cannot exist for a body that returns to any position monthly. Tiers now
   derive from a `BODY_TIER` table grounded in §22's own lists, which both fixed the conviction
   inflation and closed the analogue gap to zero. Signal classes moved from 23/7/30 to 19/9/32
   (crystal_ball / trigger_calendar / suppressed).

Brief is now **~4,290 of the 4,500 budget** at full detail.

### 4.3 Deploy 2 — Phase 5: report merge and prompt rewrite

1. `report.py` — merge on the `ENGINE_OWNED` allowlist in §3.7 amendment 4. Model-owned prose
   passes through untouched; a key in neither set is a bug.
2. Trim `COSMIC_SYNTHESIS_PROMPT` to output schema + prose rules + narration constraints
   (~4,407 → ~1,500 tok).
3. Chat: replace the three raw feeds with `render_brief(engine_state)` plus the analysis.
   `COSMIC_CHAT_PROMPT` argument list changes — update `agent_cosmic_chat` and the
   `agents.html` fetch body together.
4. Store `engine_state` in `result_data` so `/latest` and chat reuse it without recomputation.

**Saving:** macro chat ~21.9k → ~13.6k per message; micro chat ~39.3k → ~17.2k; macro synthesis
system prompt −2.9k. Per §9, both chat surfaces stay dominated by the analysis and fundamentals
they must carry — substituting the brief for the raw feeds is the entire win here.
**Exit criteria:** rendered report is visually equivalent; every model-owned prose field survives
the merge byte-identical; chat answers still cite specific padas, dasha, and yogas correctly.

### 4.4 Deploy 3 — Phase 6: optional, separate decisions

- **Deterministic FEED 3.** Replace the Perplexity `/search` economic feed with a structured
  Trading Economics pull. This is the largest *remaining* lever after Phase 5: it takes macro
  synthesis input from ~13.8k to ~9.3k tokens (§9) and removes the last source of run-to-run
  variance.
- **`reasoning_effort`.** Once the model only narrates, an explicit lower effort may be correct.
  Measure before setting it.

---

## 5. Blocking decision: dasha calibration

**This must be settled before Phase 2 is written. It changes report content, not just cost.**

KB §28 hardcodes India's Mahadasha table and states the natal Moon is at "Pushya pada 1
(Cancer ~4°25')" with a Saturn balance of "~17.5 years."

Computed from the standard chart — **15 Aug 1947, 00:00 IST, Lahiri ayanamsa 23.1255°**,
using pyswisseph 2.10.03:

```
Moon sidereal longitude = 93.9835  ->  Cancer 3.9835   Pushya pada 1
elapsed fraction of nakshatra = 0.04877  ->  Saturn balance = 18.073 years
```

The two chains diverge:

| | Mars MD | Mars-Mars AD | Mars-Rahu AD | Mars-Jupiter AD |
|---|---|---|---|---|
| **Computed** (Lahiri, 00:00 IST) | 2025-09-09 → 2032-09-09 | 2025-09-09 → 2026-02-05 | **2026-02-05 → 2027-02-24** | 2027-02-24 → 2028-01-31 |
| **KB §28 as written** | ~Feb 2025 → ~Feb 2032 | Feb → Jul 2025 | Jul 2025 → Jul 2026 | **Jul 2026 → Jun 2027** |

**As of 2026-08-13 the two disagree on the running antardasha: computed says Mars-Rahu,
the KB says Mars-Jupiter.** That is the Stage 0 gatekeeper input for every prediction, and
§28 Rule G1 supplies materially different theme lists for the two (Mars-Rahu: defense exports,
military diplomacy, sanctions, speculative shocks; Mars-Jupiter: large procurement deals,
military-banking nexus, defense IPOs, infra capex optimism).

The KB table is not sloppy — it is exact Vimshottari arithmetic from a Moon at Cancer 4°23′
(balance 17.500y), which reproduces every date in §28 including the 1965/1982/1989/2009/2015
boundaries. It corresponds to a birth time of **≈00:38 IST** (bisection-solved), i.e. ~38 minutes
later than 00:00, or equivalently an ayanamsa ~0.40° smaller than true Lahiri.

**Recommendation:** make it configurable, default to reproducing the KB.

```python
# kb/dasha.py
INDIA_NATAL = {
    "date_utc": "1947-08-14T18:30:00Z",   # 15 Aug 1947 00:00 IST
    # "kb_calibrated" pins the Moon longitude that reproduces the §28 table exactly;
    # "computed" derives it from Lahiri at the stated instant (balance 18.073y,
    # Mars MD from 2025-09-09). See docs/COSMIC_ENGINE_MIGRATION.md section 5.
    "mode": "kb_calibrated",
    "moon_lon_kb": 94.3857,
}
```

This keeps the migration token-focused and behaviour-neutral, and turns the astrological
recalibration into a separate, deliberate, one-line change. `engine_state["dasha"]["source"]`
records which mode produced the dates, so any report can be traced.

If instead `computed` is chosen: expect the current AD to flip to Mars-Rahu, every juncture date
in `trigger_calendar` to shift ~7 months earlier, and §28's prose theme lists to need rewriting
against the new boundaries.

---

## 6. Risks and expected behaviour changes

| Risk | Assessment |
|---|---|
| **Yoga detection will contradict the model.** | Python is right. Kala Sarpa, Kala Amrita, Gajakesari, Kemadruma, Chandra-Mangal, Shakata, Graha Malika, Sunafa/Anafa/Durdhura are pure geometry. Lakshmi, Daridra, Vipreet Raja need house lords — use the India natal Taurus lagna and record that assumption in `kb/yogas.py`. |
| **Conviction becomes auditable, therefore falsifiable.** | Some current HIGH-confidence calls will land MEDIUM once the matrix and yoga factors are applied honestly. This is the objective, but it will read as a regression to anyone not expecting it. Say so in the release note. |
| **Fewer predictions may survive.** | The floor-30 suppression is currently self-reported. Real arithmetic may cut the count below the prompt's "10-15 crystal_ball" requirement. Fix by relaxing the count requirement, not by loosening the floor. |
| **Loss of emergent cross-section synthesis.** | The genuine capability cost. A resolver emits only programmed combinations; a model reading all 30 sections can find unprogrammed ones. Mitigated by §3.7 amendments 1 and 2 — all bodies in the brief, and a scored proposal channel. Track how often model proposals clear the pipeline; a high rate means `signals.py` has gaps worth closing. |
| **Stage 0 PASS/FAIL is the least mechanizable step.** | AMPLIFY / DOUBLE AMPLIFY are mechanical (is the trigger body the MD/AD lord?). "Theme *contradicts* the dasha" is semantic. The formalization — sector → ruling planet (§20) → planetary friendship with the MD/AD lord — is defensible but a proxy, and Python may be *less* nuanced than the model here. Write the mapping down explicitly in `kb/matrix.py`, and log gate decisions so disagreements are reviewable. |
| **`brief.py` becomes the new bottleneck.** | Budget is 3,000 tok (§3.7 amendment 1). `render()` takes `max_tokens`, drops narrative detail before bodies, and logs exactly what it dropped. Never truncate silently. |
| **Two sources of truth during the build block.** | *Resolved by §4.0.1.* The engine runs in shadow while the prompt keeps its instructions; there is exactly one source of truth for output (the model) until the Deploy 2 cutover flips it. The overlap is deliberate and is what generates the comparison corpus. |
| **Micro agent shares the KB.** | *Resolved by §4.0.* All KB edits are deferred to Deploy 2, so both agents flip together and no frozen `_LEGACY` copy of `COSMIC_PDF_AUGMENTATION_TEXT` is ever needed. |
| **`swe.houses_ex` needs a lagna.** | §15/§19 house work requires a cast chart. Use the India natal Taurus lagna for mundane house placement, and the ingress/eclipse moment for chart-specific work. Document which chart each signal used. |

---

## 7. Verification protocol

### 7.1 Pinned verification configuration

All verification runs — the golden baseline and every subsequent comparison — use a **fixed
model and reasoning effort**, set via the Phase 0 env vars:

```bash
COSMIC_SYNTHESIS_MODEL=gpt-5.4
COSMIC_REASONING_EFFORT=xhigh
```

Both values are already established in this codebase: `gpt-5.4` is used at two other call sites,
and `xhigh` is documented as an accepted `reasoning_effort` in
[handler.py:5260](../handler.py:5260).

Two rules about this, because getting it wrong silently invalidates every diff:

- **The baseline and all later runs must use the same pinned config.** A baseline captured at
  `gpt-5.4`/`xhigh` is not comparable to a production run at `gpt-5.5`/default. Diffs are only
  meaningful within one configuration.
- **The pin is for verification, not a production change.** Unset, the defaults reproduce today's
  behaviour byte-for-byte. Whether production also moves to `gpt-5.4`/`xhigh` is a separate
  decision, and one worth taking *after* the shadow corpus shows what higher effort actually buys
  on this task — running the current 9-stage prompt at `xhigh` will raise reasoning tokens
  substantially, which is measurable before committing.

### 7.2 Golden baseline

Captured **before** the Phase 0 edits land, with §7.1 pinned:

```bash
curl -s -X POST localhost:<port>/agent/cosmic/analyze -H 'Content-Type: application/json' -d '{"force_refresh":true}'
# then poll status and save result.structured to tests/cosmic_engine/golden/baseline_2026-08-13.json
```

Also save the fully-formatted system and user messages for that run, and the pinned config
alongside them. They are the "before" context and the only accurate record of what the model was
actually told.

### 7.3 Per phase

1. **Schema diff.** Assert the key set of `result.structured` is unchanged. Any missing key the
   renderer reads is a release blocker.
2. **Field-level diff** against the baseline. Engine-computed fields should agree with the model
   where the model was right; where they differ, hand-verify the engine for two bodies before
   accepting.
3. **Determinism.** Two `build_engine_state(pinned_timestamp)` calls must produce byte-identical
   output. Pin the timestamp in tests; never call `now()` inside the engine.
4. **Invariant tests.** The §30 no-drop rule; conviction cap 85 / floor 30; pada-wins-sector;
   eclipse ±15d override; D10 priority for equity and D9 for commodity.
5. **Token accounting.** Log `len(system) + len(user)` per run to stderr. Every phase must move
   the number in the intended direction; record the actual delta in the phase's commit message.
6. **Renderer smoke test.** Load the report in the UI and confirm all nine tabs populate.

Cross-checks worth doing once, by hand, against an external ephemeris: two eclipse dates, one
D10 placement, one D9 placement, the current MD/AD boundary, and one Saturn ingress date.

---

## 8. Reference values (measured 2026-08-13)

```
KB total                                      85,414 ch   ~21,353 tok   33 sections
ephemeris report (generate_cosmic_data_report) 1,623 ch      ~405 tok
macro synthesis system prompt (formatted)    103,071 ch   ~25,768 tok
micro chat system prompt (formatted, max)    155,500 ch   ~38,900 tok
macro instrument universe                     57 instruments across 8 categories
India natal Moon (Lahiri, 00:00 IST)          93.9835  Cancer 3.9835  Pushya P1
India Saturn MD balance, computed             18.073 y  ->  Mars MD 2025-09-09
India Saturn MD balance, KB-implied           17.500 y  ->  Mars MD 2025-02-12
Lahiri ayanamsa, 1947-08-14                   23.1255 deg
pyswisseph                                    2.10.03  (venv/, not codium_env/)
```

Largest Class A sections, for transcription order: §25 (4,251) · §3+3A (1,402) · §4 (1,155) ·
§16 (936) · §17+18 (935) · §20 (690) · §26 (642) · §15 (589) · §12 (540).

Largest Class B sections, for implementation order: §30 (1,359) · §28 (1,309) · §29 (850) ·
§27 (761) · §11 (542) · §24 (529) · §19 (428) · §22 (406).

---

## 9. Token model — before and after

**Method.** System-prompt sizes are *measured* against the working tree. Feed sizes and output
sizes are *estimated* from the code's own caps and the schema's stated element counts
(`10-15 crystal_ball`, `15-20 trigger_calendar`, `10-12 sectors`, `6-9 planets`, `4+ quarters`,
etc.). Reasoning tokens are *inferred* and given as a range — they are not visible in the
response and were not instrumented. All figures use `chars / 4`. "After" means Phases 0–5 plus
4.5, with the §3.7 amendments included and Phase 6 **not** applied.

### 9.1 Per interaction

| Interaction | | Before | After | Δ |
|---|---|---|---|---|
| **Macro synthesis** | system prompt | 25,768 *(m)* | ~1,750 | |
| | brief | — | ~3,000 | |
| | FEED 1 geopolitical | ~1,750 *(e)* | ~3,000 (cap) | |
| | FEED 2 astro | 405 *(m)* | 0 (in brief) | |
| | FEED 3 economic | ~8,900 *(e)*, up to ~16,000 | ~6,000 (cap) | |
| | **input total** | **~36,800** (up to ~43,800) | **~13,750** | **−63%** |
| | visible output | ~15,600 *(e)* | ~10,900 | −30% |
| | reasoning | ~10,000–20,000 *(i)* | ~4,000–6,000 *(i)* | ~−65% |
| **Macro chat** | input | **~21,900** *(m)* | **~13,600** | **−38%** |
| **Micro synthesis** | input | **~25,300** *(m/e)* | **~5,750** | **−77%** |
| **Micro chat** | input | **~39,300** *(m)* | **~17,200** | **−56%** |

Notes on what does *not* shrink, so the numbers aren't read as better than they are:

- **Macro synthesis output only falls ~30%, not ~50%.** The trace fields the engine takes over
  live in `crystal_ball`, `trigger_calendar`, `sectors`, `planets`, `commodities` and
  `conflict_resolution_log` — together roughly half the payload. The other half is prose
  (`geopolitics`, `central_banks`, `timeline`, `scenarios`, `time_horizons`, `actionable`,
  every `description`) which is model-owned in perpetuity per amendment 4 and does not move.
- **Both chat surfaces are dominated by content that must stay.** Macro chat carries
  `analysis[:40000]` (~10,000 tok) and micro chat carries analysis + fundamentals (~13,750 tok).
  Substituting the brief for the raw feeds is the whole win; the report itself has to be there.
- **The Phase 0 caps improve the *worst* case more than the typical case.** Macro synthesis
  input is currently unbounded (§1.3a); the ~43,800 figure is what a wide economic feed produces
  today, and capping is what makes the number predictable rather than merely smaller.

### 9.2 Illustrative daily volume

Actual traffic is unknown; this mix is an illustration, not a measurement. Adjust the multipliers
to your real numbers. Input + visible output, reasoning excluded.

| Interaction | /day | Before | After |
|---|---|---|---|
| Macro synthesis | 3 (1 warmup + 2 refresh) | 157,200 | 73,950 |
| Macro chat | 10 | 224,800 | 142,000 |
| Micro synthesis | 15 | 476,700 | 161,250 |
| Micro chat | 30 | 1,197,000 | 533,250 |
| **Total** | | **~2.06M** | **~0.91M** |

**Net ≈ −56%.**

Two things this table makes obvious:

1. **Micro chat is the single largest line item** — 58% of spend before, 59% after. If only one
   thing ships, it should be the Phase 0 reorder (which makes its 21.4k-token rulebook a
   cacheable prefix) followed by Phase 5's brief substitution.
2. **Macro synthesis is not the problem it appears to be.** It is the most expensive *single*
   call and the most complex, but at ~3 runs/day it is under 8% of total spend. It is worth
   migrating for correctness and reproducibility (§1.4, §5), not primarily for cost.

### 9.3 With Phase 6

Replacing the Perplexity `/search` economic feed with a structured pull takes FEED 3 from ~6,000
to ~1,500 tokens: **macro synthesis input ~13,750 → ~9,250 (−75% vs. baseline)**. It has little
effect on the daily total, because macro synthesis is a small share of it. Sequence it for the
determinism, not the tokens.
