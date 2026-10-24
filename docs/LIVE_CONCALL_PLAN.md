# Live Concall Agent — Implementation Plan

**Status:** proposal · **Date:** 2026-10-24 · **Scope:** `agents/live_concall.py`, `agents/live_concall_stt.py`, Live Concall UI in `agents.html`, schedulers in `handler.py`

**Goal:** an AI concall agent that is the fastest and most accurate available for Indian listed companies. It should find every upcoming call, let users choose the calls they care about, join and listen automatically, transcribe and analyse while the call is running, and tell users when the report is ready. It should do this at the lowest cost per call that still gives top-quality listening.

---

## 0. TL;DR

1. **Listen once per call, then send the result to every user who wants it.** Cost should grow with the number of calls, not the number of users. Today each user starts their own phone call.
2. **Pick the cheapest audio source that works.** In order: public webcast or YouTube audio (no telephony cost, better audio), then a phone line with pre-registered credentials (passcode and PIN entered automatically), then a phone line through the operator (the bot plays a short spoken intro). If all of these fail, analyse the company's official recording when it's published.
3. **Use a two-lane transcript.** A streaming STT "live lane" shows captions in under 1.5 s. A "polish lane" uses a cheap LLM every ~60 s to fix names and numbers and label speakers. The final pass re-transcribes the full recording, so the stored transcript is the most accurate version.
4. **Get speaker labels without paying for diarization.** Indian concall moderators announce every question: *"The next question is from the line of X from Y Securities."* We parse those cues for Q&A turns and take the management line-up from the opening remarks.
5. **Check spoken numbers against the published results.** The results PDF and investor presentation usually come out before the call. Spoken figures are checked against them, and likely STT errors are corrected.
6. **Turn live streaming on only when someone is watching.** If followers only want the report, record the call and transcribe it in batch afterwards (about 45% cheaper than streaming). Streaming starts the moment someone opens the live room, and the earlier part is backfilled.
7. **Move listening into a separate worker process.** Calls that run for an hour must not depend on the Flask/gunicorn process. That process has 16 threads, uses polling-only Socket.IO, and restarts on every deploy and every edit to `handler.py`.
8. **Estimated cost:** about **₹90–145 per 60-minute call on the phone path** and **₹40–80 on the webcast path**. Telephony is roughly half the cost, so joining from webcasts and tightening when we join and hang up save the most.

---

## 1. What exists today (audit)

### 1.1 What works

| Area | Where | Notes |
|---|---|---|
| Schedule aggregation | `live_concall.py:201` `refresh_concall_schedule_cache` | IR Pulse, Screener and ScanX are merged and de-duplicated, then saved to `cached_concall_schedule.json`. Refreshes daily at 8 AM IST, plus a refresh at startup if the file is stale. |
| Dial-in extraction | `live_concall.py:379` `extract_dialin_from_pdf` | pdfplumber reads the text and the hyperlink annotations, Gemini extracts the fields as JSON, with a vision fallback and a regex fallback. Also detects DiamondPass links. |
| Exotel dialing | `live_concall.py:807` `place_exotel_call` | Calls/connect to an App Bazaar flow (Record + Passthru + optional Stream), with the passcode sent as DTMF via `number,,,passcode#`. |
| Live STT stub | `live_concall_stt.py` | Deepgram nova-2 over WebSocket, 8 kHz linear16. Clean pluggable interface. |
| Media stream WS | `live_concall.py:922` | flask-sock route, gated behind `LIVE_CONCALL_STREAMING`. |
| Post-call pipeline | `live_concall.py:1205` | Recording, then Gemini transcription, then `CONCALL_ANALYSIS_PROMPT`, then `store_latest_result('concall', …)`. |
| UI | `agents.html:4261-4360`, `:5828-6340` | Live-mode toggle, schedule list, dial-in modal, stream panel, toast for calls starting soon. |
| Toast watcher | `handler.py:13601` | Emits `concall_starting_soon` 9–11 minutes before a call. |

### 1.2 Bugs and gaps to fix first (Phase 0)

1. **Most Indian bridges are operator-assisted.** Universal access numbers (Chorus Call and others) send callers to a human operator who asks for your name and the event. Credentials only skip the operator if you pre-register through **DiamondPass**, which emails a dial-in number, passcode and PIN. Today the code dials the universal number, keys in a passcode the PDF usually doesn't have, and stays silent, so it will not reliably get in.
2. **The `number,,,passcode#` post-dial DTMF is unverified on Exotel.** The code comment says it works, but the spike needs to confirm it. Fallbacks: Exotel's bidirectional Voicebot applet sending in-band DTMF tones, or Plivo/Twilio `sendDigits` for the passcode calls only.
3. **Timezone.** `load_concall_schedule` (`live_concall.py:299-321`) and the toast watcher (`handler.py:13617`) compare IST call times with a naive `datetime.now()`. In a UTC container, statuses and toasts are off by 5 h 30 m. Use `Asia/Kolkata`-aware datetimes everywhere.
4. **Every event goes to every user.** `session._emit_fn = lambda evt, d: socketio.emit(evt, d)` (`live_concall.py:1375`) broadcasts every transcript to all connected browsers. Use per-call rooms.
5. **No auth on paid actions.** Anyone can call `/start`, `/end` and `/extract-dialin`, and `/start` places a paid phone call. `/status` is also unauthenticated, and it downloads whatever `RecordingUrl` it receives (`_transcribe_recording_with_gemini`). That's an SSRF and cost-abuse vector. Fixes: a webhook secret, an Exotel-domain allow-list, `login_required`, and per-user quotas.
6. **The transcript is sent to the analysis twice.** In `_run_post_call_pipeline` (`live_concall.py:1225-1226`), `full_transcript = transcript` is followed by `add_transcript(transcript)`, which appends the same text again. The summary prompt gets every line twice, doubling cost and hurting quality.
7. **Stale model IDs.** `gemini-2.5-flash-preview-05-20` (`:1195`) and `gemini-2.0-flash` (`:633`, `:1150`) differ from `ANALYSIS_MODEL` in `concall_agent.py:114`. Reuse the same constants.
8. **Non-ASCII in `print()`.** `update_state` prints `→` (`:1022`) outside any try, and route registration prints `✓` (`:1568`, `:1574`). On a Windows console this raises `UnicodeEncodeError` partway through a state change. Use ASCII only.
9. **Sessions live only in memory.** `_active_sessions` is lost on restart or hot reload and is never pruned. `/end` with no recording leaves the session in `ended` until a webhook arrives that may never come.
10. **Audio can be credited to the wrong call.** `_resolve_stream_session` falls back to "the only live call", which is wrong as soon as two calls run at once. Remove it, and correlate on CallSid or a custom parameter only.
11. **Data quality in the schedule.** In the current snapshot, all 90 calls have `ticker: ""`, and every one came from Screener: IR Pulse and ScanX returned nothing without any warning. Without a ticker there's no link to watchlists, portfolios or the Concall Agent cache. Add ticker resolution and per-source health alerts.
12. **The frontend can follow only one session** (`liveSessionId`). It uses `alert()` for errors, calls the unsupported `/inject-dtmf` endpoint, and the "Join Live" toast still sends the user through a manual dial modal.

---

## 2. Product design

### 2.1 User stories

- *As an investor*, I see every concall for today and this week, with my watchlist and portfolio companies highlighted. I tap **Follow** and do nothing else.
- *As a follower*, I get a notification when the call starts. I can open a live room with captions, speaker labels, key numbers and guidance changes as they happen, and ask questions about what has been said so far.
- *As a follower who didn't watch*, I get a push notification and an email minutes after the call ends with the 3-line verdict and a link to the full report and transcript.
- *As a power user*, I turn on **Auto-follow my watchlist and portfolio** so I never miss a call.
- *As an admin*, I see coverage, join success rate, latency and cost per call, and I set a daily budget and a concurrency cap.

### 2.2 Experience principles

- **No forms.** Users never see dial-in numbers or passcodes. The system handles joining, and only admins get an "edit dial-in" override.
- **Always show status.** A clear stepper shows: *Scheduled → Joining → Waiting for host → Live · Opening remarks → Live · Q&A → Wrapping up → Report ready*.
- **Fail without leaving the user stuck.** If joining fails, say so plainly and explain the fallback: *"We couldn't get into the bridge. We'll analyse the company's official recording as soon as it's published (SEBI requires it within 24 h) and notify you."*
- **The newest content is the most reliable.** Interim captions appear in muted grey and are replaced in place as the polish lane finishes each paragraph.

### 2.3 Screens

**A. Concall Agent → "Live" tab** (a segmented control *Analyze | Live*, replacing today's toggle button)

```
┌──────────────────────────────────────────────────────────────────────┐
│ Live Concalls      Today 14 · Live now 3 · Following 5               │
│ [All] [Watchlist] [Portfolio] [Following]   🔍 search   ⚙ Auto-follow │
├──────────────────────────────────────────────────────────────────────┤
│ LIVE NOW                                                             │
│ ● TCS   Q2 FY27  · 34:12 · Q&A · 212 listening   [Open live room →]  │
├──────────────────────────────────────────────────────────────────────┤
│ TODAY                                                                │
│ 16:00  INFY  Infosys        ⭐watchlist  ✓Auto-join   in 1h 12m  [🔔 Following ✓] │
│ 16:30  HDFCLIFE HDFC Life               ◐Operator    in 1h 42m  [🔔 Follow]       │
│ 17:00  DLF   DLF            🌐Webcast                in 2h 12m  [🔔 Follow]       │
│ TOMORROW …                                                           │
│ RECENTLY COMPLETED  ✔ YESBANK report ready · 18 min after call  [View] │
└──────────────────────────────────────────────────────────────────────┘
```

- Readiness badges come from the call's `join_plan` (§3.3): ✓ Auto-join (credentials ready), 🌐 Webcast, ◐ Operator (best effort), ⚠ Needs details.
- Each row has a single main button, Follow. A row's overflow menu has: View PDF, Add to calendar (.ics), and for admins only, Edit dial-in.

**B. Live room** (desktop split view; tabs on mobile)

```
┌ TCS · Q2 FY27 Earnings Call ──── ● LIVE · Q&A · 34:12 · 212 listening ─ [Unfollow] ┐
│ Transcript                         │ Insights                                       │
│ K. Krithivasan (CEO) 00:03:12      │ KEY NUMBERS          stated   vs Q1   vs pub   │
│  "Revenue grew 2.4% in constant    │ Revenue CC growth     2.4%    ▲0.6pp   ✓       │
│   currency, led by BFSI…"          │ EBIT margin          24.6%    ▼30bps   ✓       │
│ ── Q&A ──                          │ GUIDANCE TRACKER                                │
│ Q · Ravi M. (Kotak) 00:31:05       │ FY27 margin band 26–28%  ⚠ softened ("aspire") │
│  "On deal TCV…"                    │ ALERTS                                          │
│ A · Samir S. (CFO)                 │ 🔴 Evaded question on BSNL deal ramp-down        │
│  …interim grey text…               │ Q&A (7)  ▸ Deal TCV — specific  ▸ Attrition — vague │
│              [↓ Jump to live]      │ Tone ▁▂▃▃▂▅▆ (sparkline)                        │
├────────────────────────────────────┴───────────────────────────────────────────────┤
│ 💬 Ask the call: "What did they say about pricing pressure?"                [Ask] │
└───────────────────────────────────────────────────────────────────────────────────┘
```

- **Live dock:** when several followed calls run at once, a bottom dock shows mini-cards (company, last line, alert count) for one-tap switching.
- **Transcript:** find in transcript, ⭐ bookmark a moment, copy a quote with its timestamp, A−/A+ font size.
- **Catch-up:** joining mid-call loads the transcript so far over REST, then streams. Includes a *"Summary so far"* button.

**C. Report view.** Reuses the existing `concallAnalysis` card and chat, adding:
- a header with call duration, *"report delivered N min after call end"*, and the source (webcast or phone)
- downloads: transcript (TXT/PDF) with speaker labels and timestamps
- a "What changed vs last quarter" section, the guidance tracker, and a Q&A scorecard

**D. Notification preferences** (per user): channels (in-app, browser push, email) × events (starting in 10 min, live now, critical alert, report ready). Defaults: browser push for *live now* and *report ready*, email for *report ready*.

---

## 3. System architecture

### 3.1 Components

```
               ┌────────────────────── Web app (existing Flask, gunicorn gthread) ─────────────────────┐
 Browser ◄────►│ REST /api/live-concall/*  ·  Socket.IO rooms lc:{call_id}  ·  Web Push  ·  Email      │
               │ Schedule service · Follow/subscription API · Orchestrator (decides what to listen to)  │
               └───────────────▲───────────────────────────────┬──────────────────────────────────────┘
                               │ Redis pub/sub + Streams        │ Redis job queue (arm/dial/stop)
               ┌───────────────┴───────────────────────────────▼──────────────────────────────────────┐
               │ Listener Worker (new, asyncio, standalone container)                                │
               │  CallListener per call:                                                             │
               │   Source adapters ─► AudioBus ─┬─► VAD / hold-music gate ─► STT stream (live lane)   │
               │   (Exotel WS, webcast/HLS,     ├─► Recorder (FLAC chunks → Blob / /home)            │
               │    replay simulator)           └─► Operator handler (TTS intro, DTMF)                │
               │  Intelligence: cue parser · polish lane · insights engine · alerts · end-of-call     │
               │  Final pass: full-audio re-transcribe → reconcile → final report                    │
               └──────────────────────────────────────────────────────────────────────────────────────┘
 Exotel ──Calls API◄── Worker        Exotel ──Stream/Voicebot WS──► Worker (wss)     Exotel ──status──► Worker
```

**Why a separate worker.** Each live call holds one inbound media WebSocket plus one outbound STT WebSocket for about an hour. The web process has 16 gthread slots, uses polling-only Socket.IO (`handler.py:170`), and restarts on every deploy and every `handler.py` edit. A restart in the middle of a call loses audio we can't get back. The worker is pure asyncio, so dozens of concurrent calls cost almost no CPU.

- **Hosting:** Azure Container Apps (consumption), running at minimum 1 replica from 09:00–20:00 IST on weekdays and scaling to 0 otherwise. Estimated ~$10–20/month.
- **One codebase, two modes:** `LIVE_CONCALL_WORKER_MODE=embedded|standalone`. In embedded mode the same asyncio code runs in a daemon thread inside the web app, for local development and the Phase 2 MVP. Standalone is for production.
- **Getting events to browsers:** set Flask-SocketIO `message_queue=CACHE_REDIS_URL` so the worker can `emit(..., room=…)` straight to browsers. A REST catch-up endpoint covers reconnects.
- **Outbound STT WebSockets need keepalives.** Per the Azure SNAT finding, flows with no bytes for more than 4 minutes get dropped. While the gate is closed, send the provider's KeepAlive message every 5–10 s.

### 3.2 Code layout

Turn `agents/live_concall.py` into a package. The `__init__.py` re-exports today's public names (`register_live_concall_routes`, `load_concall_schedule`, `load_raw_concall_calls`, `_norm_company_name`, `_parse_call_dt`, `refresh_concall_schedule_cache`, `SCHEDULE_CACHE_FILE`) so `handler.py` and `calculations/watchlist_warmer.py` keep working unchanged.

```
agents/live_concall/
  __init__.py            # re-exports (backward compat)
  config.py              # env, IST tz helpers, budget/concurrency settings
  schedule.py            # sources, merge, ticker resolution, IST statuses (moved from today's module)
  dialin.py              # PDF extraction (moved) + join_plan classifier + DiamondPass creds ingestion
  models.py              # dataclasses + SQLite DAO (lc_calls, lc_follows, lc_usage, …)
  routes.py              # Flask REST + Socket.IO room join/leave
  orchestrator.py        # who/what/when to listen; budget + capacity; arms jobs
  notify.py              # in-app, Web Push (pywebpush/VAPID), email (auth/email_service.py)
  worker/
    main.py              # standalone entry: asyncio loop, job consumer, WS server
    listener.py          # CallListener state machine
    sources/exotel.py    # dial, status webhooks, media WS (Stream / Voicebot)
    sources/webcast.py   # HLS/YouTube audio via ffmpeg (Playwright to resolve player URL)
    sources/replay.py    # replays a recording as Exotel-format frames (testing/demos)
    audio.py             # AudioBus, resample, Silero/webrtc VAD, hold-music detector, recorder
    stt/base.py, stt/assemblyai.py, stt/deepgram.py, stt/batch_gemini.py
    intel/cues.py        # moderator-cue parser → speakers, Q&A turns, end-of-call
    intel/polish.py      # LLM correction lane
    intel/insights.py    # running CallState + alerts
    intel/final.py       # re-transcribe, reconcile, final report, store_latest_result
    events.py            # Redis Streams publish (seq-numbered) + Socket.IO emit
static/live_concall.js, static/live_concall.css   # pulled out of the 10.5k-line agents.html
tests/live_concall/…                               # unit + replay-based integration tests
tools/live_concall_eval.py                         # accuracy/latency benchmark harness
```

### 3.3 Join playbook (`join_plan`)

`join_plan` is computed for every call the day before (T-1d) and again at T-2h. It's stored on the call and drives both the UI badge and the listener.

| Priority | Method | When available | How | Join at | Cost |
|---|---|---|---|---|---|
| 1 | **Webcast / YouTube Live** | The PDF has a webcast link that resolves to an audio stream, or the company streams on YouTube | Playwright resolves the player's HLS/DASH URL, then ffmpeg reads the audio at 16 kHz mono | T-2 min | ₹0 telephony |
| 2 | **DiamondPass / pre-registered creds** | The PDF has a DiamondPass link | Register once from a dedicated Kagger research mailbox. An inbox watcher (IMAP or Gmail API) parses the emailed number, passcode and PIN with an LLM. Dial and send DTMF | T-3 min | Phone |
| 3 | **Zoom/Teams phone bridge** | The PDF gives a webinar ID and passcode | Dial the India Zoom number and send `ID#`, `#`, `passcode#` as DTMF | T-3 min | Phone |
| 4 | **Operator-assisted** | Only universal access numbers are available | Dial. The Voicebot (bidirectional) stream listens for the operator's prompt (*"may I have your name"*) and plays a pre-rendered TTS clip: *"Hello, this is Kagger Research joining the {Company} earnings call."* It then waits silently on hold | T-8 min | Phone + a few extra minutes |
| 5 | **Official recording fallback** | All of the above failed | Hand off to `concall_agent` sources (IR website audio, NSE/BSE, YouTube), which already exist in `concall_agent.py:1668`. The watchlist warmer retries until the audio appears | — | Batch STT only |

Failover: if the webcast stalls (no audio for 45 s) and phone credentials exist, dial the phone and use whichever source delivers audio first. For calls with more than N followers (default 50), optionally run both paths at once and deduplicate on the transcript.

**Compliance:** always identify honestly as Kagger Research, never ask questions in Q&A, and never impersonate an analyst. Registering for DiamondPass automatically means filling in Chorus Call's form, so check their terms first. Phase 1 starts with admin-assisted registration (one click per call from an admin queue), and automates it only if that's acceptable. Do not redistribute raw audio by default. Redistributing transcripts to paying users needs a quick legal review.

### 3.4 Listener state machine

```
SCHEDULED ─(T-15m, followers>0 or auto-cover, budget ok)─► ARMED
ARMED ─(join time)─► JOINING ─(answered)─► IN_LOBBY  (hold music / operator; STT gate CLOSED)
IN_LOBBY ─(VAD: sustained speech + moderator cue "welcome to the … conference call")─► LIVE_REMARKS
LIVE_REMARKS ─(cue "we will now begin the question-and-answer session")─► LIVE_QA
LIVE_QA ─(cue "this concludes…" / "you may now disconnect" OR non-speech > 60 s)─► ENDING ─► hang up
ENDING ─► POST_PROCESSING (final pass) ─► COMPLETE ─► notify
JOINING/IN_LOBBY ─(failed/busy/no audio > 90 s / not admitted by T+15m)─► RETRY (≤2, backoff) ─► FAILED_JOIN ─► FALLBACK_PENDING
any ─(hard cap: scheduled + 150 min)─► ENDING
```

- The state is saved to Redis on every transition, so a restarted worker picks up calls still in their time window and re-dials them.
- Every webhook is idempotent and keyed on (`CallSid`, `Status`).

### 3.5 Audio and transcription pipeline

**Gate.** Run Silero VAD (or webrtcvad) on 20 ms frames, plus a cheap hold-music detector: high spectral flatness or tonal energy with a low speech ratio over a 3 s window. The STT socket stays **closed** while the call is in the lobby. AssemblyAI bills for the time the socket is open, and Deepgram bills for audio sent, so either way we stop paying for hold music, which typically runs 5–10 minutes.

**Live lane (speed).** Streaming STT. The default is chosen in the Phase 0 bake-off:

| Candidate | Streaming price (third-party sources, Oct 2026) | Notes |
|---|---|---|
| AssemblyAI Universal-Streaming | **$0.15/hr** (≈₹13) | Cheapest. Billed on session time. Supports keyterms. |
| Deepgram Nova-3 | $0.0048–0.0077/min (≈₹25–40/hr) + keyterms ~$0.0013/min | Strong keyterm prompting. Billed on audio sent. |
| Sarvam (Saarika) | ₹30/hr | Built for Indian accents and Hinglish. Streaming details need checking. |
| Gemini Flash-Lite on 30 s chunks | ~$0.03–0.12/hr audio tokens | Not true streaming (~30–40 s lag), but cheap and context-aware. A candidate for the polish lane. |

- Keep the `open_stt_stream(on_transcript)` interface. Extend the callback to `(text, is_final, start_ms, end_ms, words[conf])`.
- **Per-call keyterms** (built at T-1d): company name and brands, management names (from last quarter's transcript, already cached by `concall_agent._cache_get_transcript`), segments, subsidiaries, sector words (NIM, GNPA, CASA, ARPU, TCV, bps, crore, lakh), and frequent analyst firms.

**Cue parser (speakers for free).** Use regexes plus a small LLM fallback on moderator phrases:
- *"next question is from the line of {Name} from {Firm}"* → opens a Q turn
- *"joining us today … Mr. {Name}, {Designation}"* → the management roster
- *"over to you, sir"* → a speaker handoff

The result is Q&A turns attributed as `Q · Ravi M. (Kotak)` and `A · Management (CFO)`. Within management answers, voice embeddings can be added later to tell the CEO from the CFO. The STT diarization add-on is not needed at first.

**Polish lane (accuracy, ~60 s lag).** Every finalized ~45–60 s block, plus the previous block for context, goes to Flash-Lite. The prompt includes the glossary, the roster, and **the numbers from the published results and investor presentation** (fetched T-0 with the existing `fetch_documents` and `get_pdf_text` helpers). Output:
- corrected text with Indian number formatting (₹4,823 Cr, 24.6%, 30 bps)
- speaker tags
- a `numbers[]` list, each tagged *matches published* or *new disclosure*

The result is emitted as `segment_polished` and replaces the interim text in the UI.

**Final pass (authoritative).** After hang-up, upload the recording to Gemini (reuse `_await_gemini_file`) with the glossary, roster and polished live transcript as reference. Ask for a verbatim, speaker-labelled transcript. Then reconcile: wherever the final pass and the live text disagree on a number, the one that matches the published figures wins. Save the transcript to `/home/live_concall/{call_id}/transcript.json` (or Blob).

**Report-only mode (the "lazy live" saving).** If a call has followers but nobody is in the live room, record it and run only the final pass. When someone opens the live room, start streaming STT right away. A background batch job transcribes the recording so far and backfills it into the room within about a minute.

### 3.6 Live intelligence

- **CallState JSON.** Updated every 2 minutes and at each section boundary. It has these fields:
  - `metrics[]` (name, value, unit, period, vs-prior, source-check)
  - `guidance[]` (item, new value, last quarter's value, change: new | raised | maintained | lowered | softened)
  - `qa[]` (analyst, firm, topic, answer summary, specificity 1–5, evasive flag)
  - `risks[]`, `catalysts[]`, `tone_series[]`, `quotes[]`

  Only the new polished text and the previous state are sent. The fixed prefix (instructions, last quarter's guidance, published numbers) stays byte-identical so Gemini's implicit caching applies.
- **Last-quarter context.** At T-1d, load last quarter's transcript and Concall Agent analysis (both already cached). The live guidance tracker then reports changes like *"said 26–28% last quarter, now 'aspire to 26%+'"*. No other product does this live.
- **Alerts.** Fire on a guidance cut or softening, a new risk, a skipped or evaded question, or a number that doesn't match the published figure. Sent as `lc.alert` to the room, and as a push notification only to users who opted into critical alerts.
- **Ask the call.** Reuse the `concall_agent` chat route, with the running transcript as context and `CHAT_MODEL`.
- **Final report.** Use `CONCALL_ANALYSIS_PROMPT` plus a "vs last quarter" section, on `ANALYSIS_MODEL`, with the final transcript and final CallState. Call `store_latest_result('concall', ticker, {..., 'source': 'live_concall'})` so the Concall Agent and the watchlist warmer use it right away. The warmer should skip tickers that already have a live result for the quarter.
- **Speed targets.**

  | Stage | Target |
  |---|---|
  | Caption latency | p50 < 1.5 s, p95 < 3 s |
  | Polished paragraph | < 75 s |
  | Insight refresh | ≤ 2 min |
  | **Report ready** | **< 3 min after hang-up** |
  | Official sources, for comparison | Audio within 24 h, transcript in ~5 working days (SEBI LODR) |

### 3.7 Orchestrator, budget and capacity

- **What to listen to:** calls with at least one follower, plus an optional admin-set **auto-cover universe** (for example the Nifty 100 or the top-N most-watched tickers).
- **Capacity:** at peak (16:00 IST in results season) up to ~10 calls can overlap. Set `MAX_CONCURRENT_PHONE` to Exotel's channel limit (confirm with Exotel). Webcast-source calls don't use phone channels. When the cap is reached, rank calls by followers × watchlist weight, and put the rest in report-only fallback.
- **Budget:** `LIVE_CONCALL_DAILY_BUDGET_INR` (default ₹5,000). The orchestrator estimates each call's cost before arming it and records actual usage in `lc_usage`. Over budget, new calls go to report-only or the official-recording fallback, and admins get a notification.
- **Per-user limits:** any user can follow any call, because following the same call costs nothing extra. Only the expensive actions are limited: forcing a listen on a call outside the universe that nobody else follows, or a dial-in override. Default: 5 per day for non-admins.

### 3.8 Data model (SQLite `/home/kagger.db`, through `auth/database.py` patterns)

```
lc_calls(call_id PK, company, ticker, call_dt_ist, announcement_url, webcast_url,
         join_plan_json, dialin_json_enc, state, source_used, followers_count,
         started_at, ended_at, report_ready_at, transcript_path, report_md,
         failure_reason, created_at, updated_at)
lc_follows(user_id, call_id, mode{live|report}, created_at, PK(user_id, call_id))
lc_autofollow(user_id PK, watchlist BOOL, portfolio BOOL)
lc_push_subs(user_id, endpoint, p256dh, auth, created_at)
lc_notify_prefs(user_id PK, prefs_json)
lc_usage(call_id PK, phone_secs, stt_secs, llm_in_tok, llm_out_tok, audio_tok, cost_inr)
```

- Live segments and insights go to Redis Streams `lc:{call_id}:seg` and `lc:{call_id}:ins` (seq-numbered, 48 h TTL).
- When the call completes, transcript and report artifacts go to `/home/live_concall/{call_id}/` (Azure persistent storage) or Blob.
- In standalone mode the worker doesn't touch SQLite. It publishes completion to Redis, and the web app saves the result. That keeps a single SQLite writer.

### 3.9 API surface

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/live-concall/calls?from&to&filter=` | login | Schedule with readiness, follower count, my-follow flag, state |
| POST/DELETE | `/api/live-concall/calls/<id>/follow` | login | Follow or unfollow (`mode=live|report`) |
| GET/PUT | `/api/live-concall/autofollow` | login | Watchlist/portfolio auto-follow |
| GET | `/api/live-concall/calls/<id>` | login | State, CallState snapshot, report if ready |
| GET | `/api/live-concall/calls/<id>/transcript?after_seq=` | login | Catch-up or reconnect |
| POST | `/api/live-concall/calls/<id>/ask` | login | Ask-the-call chat |
| POST | `/api/push/subscribe` | login | Save a Web Push subscription |
| PUT | `/api/live-concall/admin/calls/<id>/dialin` | admin | Override join details |
| POST | `/api/live-concall/admin/calls/<id>/listen-now` | admin | Force arm or dial |
| GET | `/api/live-concall/admin/dashboard` | admin | Coverage, join rate, latency, cost |
| POST | `/hooks/exotel/status?k=<secret>` | secret | Worker: call lifecycle and recording |
| WS | `/hooks/exotel/media?k=<secret>` | secret | Worker: audio frames |

Socket.IO: the client emits `lc_join {call_id}` and `lc_leave`. The server emits `lc.state`, `lc.segment`, `lc.segment_polished`, `lc.insight`, `lc.alert`, `lc.report_ready`, and `lc.viewers`. The viewer count also drives the report-only/live switch. Every event carries a `seq`, so the client deduplicates and fills gaps over REST. Interim captions are coalesced to at most 4 per second, because transport is polling-only.

---

## 4. Cost model (60-minute call)

Assumes ₹88/$. Prices are from third-party sources as of Oct 2026, so confirm the Exotel rate card and STT prices during Phase 0.

| Item | Phone path | Webcast path | Lever |
|---|---|---|---|
| Telephony (Exotel ~₹0.8–1.0/min, ~63 billed min) | ₹50–63 | ₹0 | Webcast first. Join at T-3 min unless the call is operator-assisted. Auto hang-up. |
| Streaming STT (~55 gated min) | ₹12 (AssemblyAI) – ₹40 (Deepgram + keyterms) | same | Gate on VAD. Report-only mode when nobody watches. |
| Polish lane (Flash-Lite) | ₹8–12 | same | Send only finalized blocks; short context. |
| Insights (30 updates, cached prefix) | ₹6–9 | same | Send only the delta; keep the prefix byte-stable. |
| Final re-transcription (Gemini audio) | ₹5–12 | same | Always run; this is the accuracy backbone. |
| Final report | ₹3–5 | same | One call. |
| **Total** | **≈ ₹85–140** | **≈ ₹35–80** | |
| Report-only mode (no live viewers) | ≈ ₹65–90 | ≈ ₹10–25 | Skips streaming, polish and live insights. |

- **Busy-season estimate:** about 15 demand-driven calls per day × 22 days ≈ 330 calls, or **≈ ₹25–45k per month in results-season months**. Off-season costs are much lower.
- **Without "listen once, serve many"**, cost would multiply by the number of users per call.
- **The two biggest savings** are the webcast share and avoiding minutes nobody listens to. A call that is never hung up (bot left on hold music until `TimeLimit` = 2 h) costs more than the entire AI stack. The end-of-call detector and the hard cap fix this.

---

## 5. Phased delivery

Each phase can ship on its own. Rough estimates assume one developer.

### Phase 0 — Fix the basics and validate in the field (≈1 week)
1. Fix every bug in §1.2: IST datetimes, rooms, auth and webhook secret, RecordingUrl allow-list, the doubled transcript, model constants, ASCII-only logs, session pruning and timeouts, removing the "only live call" fallback.
2. **Telephony spike** on 4–5 real calls:
   - DiamondPass credentials with DTMF (confirm post-dial digits on Exotel)
   - an operator-assisted universal number: what happens when the caller is silent, and what happens with a TTS intro (needs the Voicebot applet)
   - a Zoom phone bridge
   - Also confirm Exotel's per-minute rate, pulse length, concurrent-channel limit, and Stream/Voicebot availability.
3. **STT bake-off.** Take 10 past calls that have official transcripts (from varied sectors and accents), downsample them to 8 kHz μ-law to mimic PSTN, and run AssemblyAI, Deepgram Nova-3 (with keyterms) and Sarvam. Metrics: **WER, Numeric Entity Error Rate (NEER), proper-noun error rate, latency**. Pick the default by NEER at an acceptable cost.
4. **Webcast spike:** resolve audio from Chorus Call webcast and YouTube Live with Playwright + ffmpeg.
5. **Deliverable:** a decision record (STT vendor, join playbook per bridge type, Exotel settings).

### Phase 1 — Schedule and readiness (≈1 week)
- Add NSE/BSE corporate-announcement feeds (category *Analysts/Institutional Investor Meet/Con. Call Updates*) as a primary source. Add per-source health checks and alerts for a source that returns nothing.
- Resolve tickers against `trendlyne_all_stocks_master.csv` using the existing `_norm_company_name`.
- Refresh at 08:00, 12:00, 16:00 and 20:00 IST. SEBI requires at least 2 working days' notice, so this catches late announcements.
- Pre-extract dial-in details at T-1d, classify `join_plan`, build the keyterm glossary, and pre-load last quarter's context.
- DiamondPass: an admin registration queue, plus an inbox watcher that parses the emailed credentials.
- `lc_calls`, `lc_follows` and `lc_autofollow` tables, follow APIs, and an auto-follow job that expands watchlists and portfolios into follows.
- **Exit:** ≥90% of today's calls have a ticker and a `join_plan`, and ≥60% are *Auto-join* or *Webcast*.

### Phase 2 — Listener worker MVP: phone path and live captions (≈2 weeks)
- The `worker/` package in embedded mode: Exotel source, AudioBus, VAD gate, recorder, STT adapter, state machine, cue parser (roster and Q&A), auto hang-up, retries.
- **Replay simulator** (`sources/replay.py`): streams a recording as Exotel Stream frames at real-time speed. All development, CI and demos run on it, so no phone calls are needed.
- Redis Streams events, Socket.IO rooms through `message_queue`, and the catch-up endpoint.
- Final pass and final report, saved with `store_latest_result`.
- **Exit:** 5 real calls joined end-to-end with no human touch, captions p95 < 3 s, report < 5 min after hang-up, and correct auto hang-up on all 5.

### Phase 3 — Live intelligence (≈1.5 weeks)
- Polish lane with checking against the published numbers, the CallState insights engine with the last-quarter guidance tracker, alerts, and Ask-the-call.
- Report-only/live mode switching driven by viewer count, with backfill.
- **Exit:** on the replay benchmark, NEER after polishing is ≤50% of raw STT NEER, and ≥90% of Q&A turns are attributed to the right analyst and firm.

### Phase 4 — Frontend revamp (≈1.5 weeks, can overlap with Phase 3)
- Move live code into `static/live_concall.js` and `static/live_concall.css`.
- Build the Live tab (agenda, filters, badges, Follow, auto-follow), the live room (transcript with interim and polished states, insights rail, jump-to-live, search, bookmarks, ask box), the live dock, and the report view additions.
- Replace `alert()` with inline errors, and remove the dial modal (admins keep an override drawer).
- Make it responsive (tabs on mobile), keyboard-accessible, and handle Socket.IO reconnects with gap-fill.

### Phase 5 — Notifications (≈0.5 week)
- Service worker with Web Push (VAPID keys in env, `pywebpush`), and a report-ready email template in `auth/email_service.py`.
- Preferences UI. Trigger the existing 10-minute toast only for followers.

### Phase 6 — Webcast path, failover, standalone worker (≈1.5 weeks)
- Webcast source, failover from webcast to phone, and optional dual-path for high-demand calls.
- Deploy the worker as an Azure Container App with scheduled scaling. Move the Exotel webhooks and media WS to the worker's host and switch the web app to consumer-only.

### Phase 7 — Hardening and proof (ongoing)
- `tools/live_concall_eval.py` as a nightly benchmark on the golden set, tracking WER, NEER, speaker accuracy, caption latency and time-to-report. These numbers back up the claim of "fastest and most accurate".
- An admin dashboard for coverage, join success rate by bridge type, cost per call, and budget burn.
- Load test: 12 concurrent replay calls on one worker replica.
- Runbook: Exotel outage means webcast-only plus the fallback; STT vendor outage means switching to the secondary STT adapter, or report-only mode with the batch final pass.

---

## 6. Testing strategy

- **Unit:** cue parser (on a corpus of real moderator phrases), dial-string and DTMF builder, `join_plan` classifier on 50 real announcement PDFs, IST status logic, budget and capacity ranking, segment reconciliation.
- **Integration (no phone):** the replay simulator drives the whole worker. Assertions: state transitions, gating (no STT bytes during hold music), auto hang-up, events in order with no gaps, and final report stored.
- **Field:** a staged rollout in results season. Week 1 is admin-only, week 2 covers watchlist followers, then everyone.
- Remember that editing `handler.py` hot-reloads a running dev server and runs its startup threads for real. Develop worker code outside `handler.py`, and test against the replay source.

## 7. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Operator doesn't admit the bot | DiamondPass-first. TTS intro. Report-only fallback through the official recording. Track join rate per bridge type. |
| Exotel DTMF or Voicebot limits | Spike in Phase 0. Plivo/Twilio adapter behind the same `sources/` interface for passcode calls only. |
| Indian-English numbers misheard | Keyterms, a polish lane that checks against published results, and a final-pass reconciliation. NEER is the north-star metric. |
| Calls overlapping at 16:00 | Separate worker, channel cap, ranked priorities, webcast calls use no phone channel. |
| Cost overrun | Daily budget, per-call estimates, VAD gating, auto hang-up, report-only mode, `lc_usage` tracking. |
| Restarts in the middle of a call | Standalone worker, Redis state, re-dial while the call is still in its window. |
| Legal/terms (registration, redistribution) | Honest identity, no Q&A participation, no raw-audio redistribution, legal review before GA. |

## 8. Decisions needed

1. **Coverage policy:** listen only when users follow a call, or also auto-cover a universe (and which one), and the daily budget cap.
2. **DiamondPass automation:** admin-assisted registration only, or automated form registration from a dedicated research mailbox (after reviewing Chorus Call's terms).
3. **Operator TTS intro:** OK to identify as "Kagger Research" on operator-assisted bridges?
4. **Worker hosting:** Azure Container Apps (recommended) or a second App Service.
5. **Audio:** keep recordings internal only (recommended) or offer key-moment clips to users.
