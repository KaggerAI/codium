# Exotel: Setup and Testing for Live Concall

**Date:** 2026-11-14 · **Code:** [agents/live_concall.py](../agents/live_concall.py) (Exotel section) · **Plans:** [LIVE_CONCALL_PLAN.md](LIVE_CONCALL_PLAN.md), [LIVE_CONCALL_ACCESS_PLAN.md](LIVE_CONCALL_ACCESS_PLAN.md)

Exotel is Kagger's phone line into concalls. It places the call to the conference bridge, enters the passcode/PIN, streams the call audio to Kagger for live transcription, records the call, and tells Kagger when the call ends.

---

## Part A: Setup

### A1. Account and KYC (you; 1–3 business days)

1. Sign up at exotel.com as a business. A trial account works for the first tests, but **can only call phone numbers verified on the account**, so it can't dial a concall bridge until KYC is approved.
2. Submit KYC for the company that will own the service. For a Pvt Ltd / LLP:
   - Certificate of Incorporation
   - GST certificate
   - PAN
   - authorised signatory's ID
   - business address proof (utility bill or bank statement under 3 months old, matching the registered address)

   Name mismatches across documents are the usual cause of rejection.
3. After approval, **buy an ExoPhone** (Dashboard → ExoPhones → Buy New ExoPhone). It's the caller ID the bridge sees. A landline number is fine; you're the one calling out.
4. **Recharge credits.** Testing all of Part B costs well under ₹500 in call minutes.

### A2. Questions to put to your Exotel account manager (send on day 1)

These decide parts of the design, so get them answered in writing:

1. **Post-dial digits.** Can the `From` number on `Calls/connect` carry pauses and digits (e.g. `02262801123,,,12345678#`) that Exotel keys in after the far end answers? If not, is there an API or flag to send DTMF into a live call? *(Not documented. The DiamondPass and Zoom/Webex paths depend on it.)*
2. **Voicebot (AgentStream) applet.** Is it enabled on our account? Can we authenticate with a secret in the URL query (`?k=…`), or must we use IP whitelisting or Basic Auth?
3. **Concurrency.** How many simultaneous outbound calls (channels) does our plan allow? At results-season peak (4 PM IST) we need around 10.
4. **Rates.** Per-minute outbound rate, pulse length (30 s or 60 s), and recording/streaming charges.
5. **Destinations.** Can we dial **ITFS `000 800 …`** and **`1800`** toll-free numbers, and Mumbai landlines such as `+91 22 6280 xxxx` / `+91 22 7115 xxxx` (Chorus Call)?
6. **Call category.** Are outbound calls to conference bridges fine under our account category (transactional/service)? Any DND/TRAI implications?
7. **API cluster.** Is our account on `api.exotel.com` (Singapore) or `api.in.exotel.com` (Mumbai)?

### A3. API credentials (Dashboard → API Settings)

Copy these into Azure App Service → **Configuration → Application settings**, and into your local `.env` for the command-line tests:

| Setting | Where it comes from |
|---|---|
| `EXOTEL_API_KEY` | API Settings → API Key |
| `EXOTEL_API_TOKEN` | API Settings → API Token |
| `EXOTEL_SID` | API Settings → Account SID |
| `EXOTEL_SUBDOMAIN` | `api.in.exotel.com` (Mumbai) or `api.exotel.com` (Singapore), per A2.7 |
| `EXOTEL_CALLER_ID` | The ExoPhone bought in A1.3 |
| `EXOTEL_FLOW_APP_ID` | The flow's ID from A4 (the number in the flow's URL) |
| `LIVE_CONCALL_WEBHOOK_SECRET` | A long random string you choose (e.g. `python -c "import secrets;print(secrets.token_urlsafe(32))"`) |
| `LIVE_CONCALL_STREAMING` | `1` to turn on live audio streaming |
| `DEEPGRAM_API_KEY` | Live transcription. The code currently uses Deepgram; the STT bake-off may change this |

### A4. Build the call flow (Dashboard → App Bazaar → Create new flow)

One flow, three applets:

```
[Voicebot]  ──►  [Passthru]  ──►  [Hangup]
```

| Applet | Setting | Value |
|---|---|---|
| **Voicebot** | URL | `wss://<your-azure-host>/api/live-concall/media-stream?k=<LIVE_CONCALL_WEBHOOK_SECRET>` |
| | Sample rate | 8000 (phone audio is 8 kHz anyway; the code expects 8 kHz linear16) |
| | Record | **On** (the recording URL is passed to the Passthru) |
| **Passthru** | URL | `https://<your-azure-host>/api/live-concall/status?k=<LIVE_CONCALL_WEBHOOK_SECRET>` |
| | Mode | Synchronous off (we only need the notification) |
| **Hangup** | – | – |

Why **Voicebot** (two-way) and not **Stream** (one-way): the same applet then also lets Kagger *speak*, for the operator introduction, and *play keypad tones as audio*, the fallback if post-dial digits don't work. Listening works the same either way.

Save the flow and note its ID: `EXOTEL_FLOW_APP_ID`.

### A5. Azure

- App Service → Configuration → General settings → **Web sockets: On**. Without it, Exotel's audio stream can't connect.
- Add the settings from A3 and restart the app.
- Check: open `https://<your-azure-host>/api/live-concall/schedule` in a browser. It should return JSON. The startup log should show `LIVE_CONCALL: media-stream WebSocket registered`.

### A6. Code support for testing (done)

1. **Recording on every call:** `place_exotel_call` sends `Record=true`, so a recording exists even if the flow's Voicebot recording is off. Passcode and PIN are now separated by a pause (`number,,,passcode#,,pin#`), because bridges prompt for the PIN only after accepting the passcode.
2. **Command-line smoke test:** [tools/exotel_smoke_test.py](../tools/exotel_smoke_test.py). It reads the settings from the environment or `.env`, never prints the API token, asks before every call, and caps test calls at 10 minutes by default.

| Command | What it does |
|---|---|
| `check [--host <azure-host>]` | Settings present, credentials valid, caller ID is an ExoPhone on the account; with `--host`, also Kagger's schedule API and the media-stream WebSocket handshake |
| `call <number> [--digits "..."] [--time-limit 600] [--yes]` | Places a call through the Kagger flow (same caller ID, flow and recording as production), then watches it until it ends and reports PASS/FAIL for connection and recording. `--digits` is sent verbatim after the number; `,` = pause |
| `status <CallSid> [--wait]` | Status, times, duration, price and RecordingUrl from Exotel's call-details API, so tests pass or fail even before webhooks work |
| `recording <CallSid> [--out file.mp3]` | Downloads the recording so you can listen to it (Stage 5) |
| `hangup <CallSid>` | Ends a call that's still running |

Calls placed by the tool aren't tied to a Kagger session. They prove the telephony (dialing, digits, recording, status). The live transcript in the UI needs a call started from Kagger's **Dial In** button.

---

## Part A2: Running the tests on localhost

Exotel must reach your machine for two things: the audio stream (WebSocket) and the call-status callbacks. Run the local server behind a tunnel.

1. **Install the two live-concall packages into the project venv**. Pinned: deepgram-sdk 4+ removed the API the code uses.
   ```
   venv\Scripts\python.exe -m pip install "deepgram-sdk>=3.4,<4" "flask-sock>=0.7.0"
   ```
2. **Install and start a tunnel**, and leave it running for the whole session. A quick tunnel gets a new URL each time it restarts.
   ```
   winget install --id Cloudflare.cloudflared
   cloudflared tunnel --url http://localhost:8000
   ```
   Note the `https://<random>.trycloudflare.com` URL it prints.
3. **Add to `.env`**. Copy the API keys from Azure → App Service → Configuration.
   ```
   EXOTEL_API_KEY=...
   EXOTEL_API_TOKEN=...
   EXOTEL_SID=...
   EXOTEL_SUBDOMAIN=api.in.exotel.com
   EXOTEL_CALLER_ID=0XXXXXXXXXX
   EXOTEL_FLOW_APP_ID=...
   LIVE_CONCALL_WEBHOOK_SECRET=<long random string>
   LIVE_CONCALL_STREAMING=1
   LIVE_CONCALL_PUBLIC_URL=https://<random>.trycloudflare.com
   DEEPGRAM_API_KEY=...
   GOOGLE_API_KEY=...        # post-call transcription + summary
   ```
4. **Point the Exotel flow at the tunnel** (A4): Voicebot URL `wss://<random>.trycloudflare.com/api/live-concall/media-stream?k=<secret>`, Passthru URL `https://<random>.trycloudflare.com/api/live-concall/status?k=<secret>`. If the tunnel URL changes, update the flow and `.env` together.
5. **Start the app**: `venv\Scripts\python.exe handler.py`. Startup jobs run for real (the schedule and stock-master refresh may update the cached files). The dev server restarts when a source file is saved, which drops any live call, so don't edit code mid-call.
6. **Log in** at `http://localhost:8000/login` with your local admin account. Browsing on localhost is fine; `LIVE_CONCALL_PUBLIC_URL` makes callbacks use the tunnel.
7. **Opening the dial dialog without a schedule card.** The schedule may have no upcoming calls, especially between results seasons. On the Agents page, open the browser console and run:
   ```js
   switchAgent('concall'); if (!liveModeActive) toggleLiveMode();
   openLiveDialModal({company_name: 'Test call', ticker: '', call_date: ''});
   ```

## Part B: Testing, in stages

Each stage needs only what the previous one proved. Stages 1–3 need no real concall, and that matters now: Q2 results season is ending (SEBI's results deadline was 14 Nov), so few calls remain until mid-January.

### Stage 0: Credentials (2 min, no call)

- `python tools/exotel_smoke_test.py check`
- **Pass:** 200 from Exotel, account SID and ExoPhone listed.
- **Fail:** wrong subdomain (Singapore vs Mumbai) is the usual cause; then key/token.

### Stage 1: "Call me" (5 min, works on a trial account)

Proves dialing, the flow, the audio stream, recording and status callbacks end to end.

1. Verify your mobile number on the Exotel account (trial accounts only).
2. Telephony first: `python tools/exotel_smoke_test.py call <your-mobile>`. Answer, talk for 30 seconds, hang up. The tool should report PASS for both the connection and the recording. Then `recording <CallSid>` and listen to it.
3. Then end to end in Kagger: Live Concall → Dial In → enter your mobile as the dial-in number → Dial Now. Answer and talk for about 60 seconds (read a paragraph with numbers: "revenue grew 12.4 percent to 4,823 crore"), then hang up.

**Pass when all of these hold:**
- [ ] Phone rings with the ExoPhone as caller ID.
- [ ] Exotel dashboard → Call Logs shows the call, with duration.
- [ ] Azure log: `media-stream started (session=lc_…)` → live transcript lines appear in the Kagger UI within ~2 s of speaking.
- [ ] Azure log: `Exotel status: status=completed … rec=yes`.
- [ ] Kagger downloads the recording, transcribes it, and shows the summary (short, since it's a 1-minute call).
- [ ] The numbers you read out appear correctly in the transcript.

**Common failures:** no audio stream → Azure WebSockets off, wrong `?k=`, or flask-sock under gunicorn; no `rec=yes` → recording off in Voicebot and `Record` not sent; transcript empty → `DEEPGRAM_API_KEY`.

**Also check the recording host.** `status <CallSid>` prints the RecordingUrl. If its host isn't `*.exotel.com`, `*.exotel.in` or Exotel's S3 bucket, Kagger refuses to download it (SSRF guard). Add the host to `LIVE_CONCALL_RECORDING_HOSTS` (comma-separated).

### Stage 2a: Do post-dial digits get sent? (2 min, works on a trial account)

- `python tools/exotel_smoke_test.py call <your-mobile> --digits "1234#"`
- Answer and listen. About 3 seconds in you should hear **five keypad tones**.
- **Pass:** tones heard, so Exotel sends post-dial digits. **Fail:** silence, so go straight to the fallbacks under Stage 2.

### Stage 2: Passcode digits, against a bridge you control (15 min, needs KYC)

Proves the riskiest piece: getting through a bridge's passcode prompt.

1. Start a meeting you host on **Zoom** (or **Webex**) that has India phone dial-in numbers. Note the dial-in number, meeting ID and passcode.
2. Have Kagger dial `<zoom India number>` with digits `<meetingID>#` then `#` (skip participant ID) then `<passcode>#`:
   `python tools/exotel_smoke_test.py call <zoom-india-number> --digits "<meetingID>#,,#,,<passcode>#"`
3. Watch your Zoom window.

**Pass:** a phone participant joins your meeting; when you talk in Zoom, it shows up in Kagger's live transcript.

**If the digits are ignored** (the call sits at "enter your meeting ID"), try in this order:
1. Longer pauses (`,,,,,`), in case the bridge prompt plays longer than the default pause.
2. **In-band DTMF:** have the Voicebot stream *play* the keypad tones as audio. About 40 lines of code (standard dual-tone pairs, 120 ms per digit), worth trying since PSTN carries tones cleanly.
3. Ask Exotel to enable digit sending (A2.1).
4. Last resort: a second carrier with a documented `sendDigits` (Plivo or Twilio), used only for passcode calls.

If Exotel can't reach a Zoom India number, use the in-account alternative: build a second flow on your ExoPhone **Gather → Passthru (to the status URL) → Hangup**, have Kagger call your own ExoPhone with digits, and check the Passthru log for the gathered digits.

### Stage 3: Long-call stability (70 min, can run unattended)

Proves a full-length call survives: WebSocket keepalive (Azure drops silent flows after about 4 minutes), recording size, end-of-call handling.

1. In your Zoom meeting, play a **past concall recording** (any company's IR site or YouTube replay) through screen-share audio, and let Kagger dial in as in Stage 2.
   - **Trial-account alternative:** start the call from Kagger's **Dial In** with your mobile as the number, answer, and put the phone on speaker next to a laptop playing the replay.
2. Leave it for 60+ minutes, then end the Zoom meeting (or hang up the phone).

**Pass:**
- [ ] Transcript streams the whole hour with no gaps over 30 s.
- [ ] The call ends on its own when the meeting ends.
- [ ] The recording arrives and the full post-call report is generated.
- [ ] Exotel billed minutes ≈ call length.

Also compare the transcript against the official transcript for that call, to get a first real accuracy number.

### Stage 4: Real concall via DiamondPass (one real call)

1. Pick an upcoming call on Kagger's Live schedule with a DiamondPass link. Register yourself (with the CAPTCHA) using a Kagger research email; the email gives dial-in number, passcode and PIN.
2. In Kagger: Dial In → enter that number, passcode and PIN → Dial Now at T-3 min.

**Pass:** admitted without an operator ("Express Join"), live transcript through the call, report shortly after the end.

### Stage 5: Real concall via the operator (one real call, listen to the recording)

1. Dial a universal access number with no passcode at T-8 min. The bot stays silent; this is how the code behaves today.
2. Afterwards, **listen to the recording**. Does the operator admit a silent caller, put it on hold, or drop it? What exactly do they ask?

This decides whether to build the spoken introduction (Access plan 1A) and what it must say. Repeat on 3–5 calls, ideally with different hosts.

### Stage 6: Concurrency (results season)

Run 3 calls at once (any mix of Stages 1–3), then 10 when the channel limit allows.

**Pass:** no dropped streams, no cross-talk between sessions (each transcript belongs to its own call), and Azure CPU stays reasonable.

---

## What "working" means

Exotel is ready for launch when Stages 1–3 pass, Stage 4 passes on 2 real calls, and Stage 5 says whether the operator path is viable.

At that point, write down the results:
- whether post-dial digits work, or which fallback was used
- the operator's behaviour
- the per-call cost
- the channel limit

These feed the decision record in the main plan (Phase 0).

## Sources

- [Exotel: Connect Number to Call Flow API](https://developer.exotel.com/docs/voice-v1/api-reference/connect-to-flow) and [Quickstart: Make Your First Call](https://developer.exotel.com/docs/voice-v1/quickstart) (Record, StatusCallback, Mumbai/Singapore hosts)
- [Exotel: Working with the Stream and Voicebot Applet](https://developer.exotel.com/docs/agentstream/stream-voicebot-applet): Voicebot record → Passthru, events, audio format, no outbound DTMF event
- [Exotel: Working with DTMF](https://support.exotel.com/support/solutions/articles/3000080227-working-with-dtmf): inbound digit capture only
- [Exotel: KYC verification](https://developer.exotel.com/docs/getting-started/kyc-verification) and [Account setup FAQs](https://developer.exotel.com/docs/faqs/account-setup)
- [U-M: Join Zoom meetings/webinars by phone](https://teamdynamix.umich.edu/TDClient/30/Portal/KB/PrintArticle?ID=4666)
