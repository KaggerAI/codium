# Live Concall: How Kagger Gets Into the Call

**Status:** proposal · **Date:** 2026-10-25 · **Companion to:** [LIVE_CONCALL_PLAN.md](LIVE_CONCALL_PLAN.md) (architecture, cost, phases)

**Question:** how can Kagger join and listen to every concall a customer follows, with as little effort from anyone as possible, and how does a customer quickly finish any step that can't be automated?

---

## 1. Start from the customer

**What the customer wants:** *"Tell me what management said on the calls I care about, while they're saying it, without me having to do anything."*

The ideal experience, which this plan aims to deliver for most calls:

1. The customer follows a company, or turns on auto-follow for their watchlist.
2. At call time a notification arrives: *"TCS call is live. Open the live transcript."*
3. The live room shows the transcript and insights; 3 minutes after the call ends the report arrives.

The customer never sees a dial-in number, a passcode, or a registration form. **Joining is Kagger's job, not the customer's.**

When a call can't be joined automatically, the customer is told early (the day before, not at call time). They get **one clear action that takes under a minute**, and the action helps every follower, not just them:

> *"HDFC Life's call needs a registration we can't complete automatically. **Register for Kagger** (opens the form with our details ready to copy) · 1 min · helps 41 followers."*

If nobody does it, the customer still gets the full analysis from the company's official recording, which SEBI requires within 24 hours, and Kagger tells them which outcome to expect.

### Principles

- **Listen once, serve everyone.** One Kagger listener per call. Any manual step done by one person (a customer or Kagger's ops desk) unlocks the call for every follower.
- **Prepare the day before.** Invites are filed 1–2 days ahead. Classify, register and collect credentials at T-1 day so problems surface while there's time to fix them.
- **Always say what will happen.** Every call card shows its expected outcome: *Live (auto)*, *Live (needs 1 step)*, or *Report after the call*.
- **Honest identity.** Kagger registers and joins as "Kagger Research" with a real research mailbox. It never impersonates a person and never speaks or asks questions on a call.

---

## 2. How Indian concalls are accessed

**Survey:** all 90 calls on Kagger's schedule for 17–26 Oct 2026 (Q2 FY27 results season, about 88 companies). For each call I classified the joining details in the linked exchange filing, or in the company's newer NSE call-update filing when the linked one had none.

| # | Access method | Calls | Share | Examples | What a listener needs |
|---|---|---|---|---|---|
| 1 | **Chorus Call phone bridge**: universal access numbers (operator) + DiamondPass pre-registration | 72 | **80%** | ICICI Bank, Axis, UltraTech, Bajaj Finance, Adani Green, JSW Steel | Phone. Either speak to an operator, or pre-register on DiamondPass (has a **CAPTCHA**) for a passcode + PIN that skips the operator |
| 1a | ↳ also offers a **Chorus Call audio webcast** (listen-only, registration form, no CAPTCHA seen) | 3 | 3% | DLF, IndusInd Bank, Swiggy | Browser |
| 2 | **Zoom webinar**, mandatory pre-registration | 7 | 8% | Eternal, Coforge, Cyient, CSB Bank (hosted by YES Securities), Dr Reddy's | Register → email with personal join link (and usually phone numbers + webinar ID). Sagility also printed the webinar ID + passcode |
| 3 | **Webex** meeting/webinar, public meeting number + password | 4 | 4% | Bank of India, Punjab & Sind Bank, LMW | Phone (access code + numeric password) or web |
| 4 | **Microsoft Teams** live event (listen-only, Q&A by text) | 1 | 1% | Tata Motors | Browser |
| 5 | **Third-party webcast platform** (RMG webcast, YouTube underneath) | 1 | 1% | Mahindra Lifespaces | Browser, after a registration form |
| 6 | **Open Exchange** registration platform (Zoom underneath, ITFS numbers) | 1 | 1% | Mastek | Register → email |
| 7 | **No usable details** (invite only on company site, unreadable PDF, not yet filed) | 4 | 4% | Oberoi Realty (filing links to an invite PDF on its site), Aye Finance, Sat Kartar | Follow the link, or wait for a newer filing |

Further observations:

- **Chorus Call dominates.** 59 filings link `services.choruscall.in` DiamondPass pages, 47 list universal access numbers, 15 advertise "Express Join with DiamondPass (no wait time)", and 7 mention an operator explicitly. A few print a passcode, and these are mostly hosted by brokers (e.g. SBI Funds, Bajaj Finance).
- **Public webcasts are rare.** Only about 6% of calls offer a listen-only webcast (Chorus Call audio webcast, Teams event, RMG/YouTube). IndiaMART also streams on YouTube Live (seen in an earlier filing). These are the cheapest and easiest to automate, but they cover a small share.
- **Almost every call mentions registration** (84 of 90), but for Chorus Call it's optional: the universal numbers work without it through an operator.
- **Brokers host some calls**, e.g. CSB Bank's call via YES Securities on Zoom. The invite still reaches the exchange filing.
- The survey covers one results-season week, so shares will shift a little by quarter and company size. The pattern (Chorus Call ≫ Zoom > Webex > webcasts) matches what the invites I checked by hand show.

---

## 3. Plan for each access method

The overall flow for every call is:

```
T-2d…T-1d  find invite filing → classify access methods → pick join plan → register / collect credentials
T-3h       re-check for newer filings (revised numbers, late webcast links); confirm credentials
T-1h       if still blocked: escalate to ops desk + "needs 1 step" prompt to followers
T-10m      start listener (webcast T-2m, phone T-3m, operator bridge T-8m)
LIVE       transcript → insights → end-of-call detection → report → notify
fallback   official recording (≤24h) → batch analysis → notify
```

When a call offers several methods, the listener uses this order: **public webcast → meeting bot/web client → phone with credentials → phone via operator**. Webcasts are free of telephony cost and have better (wideband) audio. A call with both a webcast and a phone bridge can use the phone as a hot standby if the webcast stalls.

### Method 1: Chorus Call phone bridge (80% of calls)

This decides whether the product works, so it gets three paths, tried in this order.

**1A. Operator-assisted join with a spoken introduction (fully automatic, needs validating)**
- **How it works today for humans:** dial a universal access number (e.g. `+91 22 6280 1xxx` / `+91 22 7115 8xxx`). A Chorus Call operator answers, asks for your name and company (sometimes the event name), and adds you to the call.
- **Automation:** Exotel dials with the **bidirectional Voicebot stream**. The listener transcribes the line, detects the operator's prompt, and answers with a short pre-rendered TTS clip: *"Hello, this is Kagger Research, joining the {Company} earnings call."* If the operator asks a follow-up ("Are you an analyst or an investor?", "Which firm?"), a tiny scripted voice agent answers from a fixed set of honest replies. If it hears anything else, it stays silent and the call drops to 1B/1C. After admission it stays muted.
- **Join time:** T-8 to T-10 min, since operator queues build up just before the start.
- **Cost:** phone minutes (~₹0.8–1/min) plus 5–10 min of hold time; the TTS clip is rendered once per call.
- **Why it matters:** if operators routinely admit "Kagger Research", this one path makes **80% of calls fully automatic with no CAPTCHA and no registration.**
- **Spike first:** 5–10 real calls across different hosts, measuring admission rate, wait time, and which questions operators actually ask. Run it with the Phase 0 telephony spike.

**1B. DiamondPass pre-registration (automatic join, one human step for registration)**
- **For humans:** register on the DiamondPass page (name, email, company, image CAPTCHA). An email arrives with dial-in numbers, a passcode and a PIN, and "Express Join" skips the operator.
- **What can't be automated:** the **CAPTCHA**. Kagger will not bypass CAPTCHAs, so each call needs one human to submit the form.
- **Automation around that step:**
  - T-1d: the call shows in the ops-desk queue (4.5) with the DiamondPass link and Kagger's details pre-filled for copying.
  - The registration email goes to `calls+<callid>@<kagger-domain>`. An inbox watcher (IMAP / Gmail API) parses the number, passcode and PIN with the dial-in extractor already built, and stores them on the call.
  - At T-3 min, Exotel dials and enters the passcode and PIN by DTMF. The post-dial DTMF still needs confirming in the Phase 0 spike.
- **Customer fallback:** any follower can do the registration via "Register for Kagger" (4.1), so ops is not a bottleneck.
- **Effort:** about 30 seconds per call. Even with 60–70 Chorus Call events a day in peak season, that's about 35 minutes of one person's time the day before.

**1C. Chorus Call audio webcast (fully automatic, when offered)**
- `event.choruscall.com/mediaframe/webcast.html?webcastid=…` serves a registration form (name/email/company, no CAPTCHA seen) and then an audio player.
- **Automation:** Playwright (already in the container image) opens the page, submits Kagger's details, and starts the player. A network listener captures the HLS/DASH manifest URL, and ffmpeg pulls the audio from it at 16 kHz into the listener's AudioBus. If no manifest is found, play the page into a PulseAudio null sink and record that instead.
- **Cost:** no telephony, better audio. Use it first whenever the invite includes the link, with 1A as hot standby.

**Long-term fix: a Chorus Call partnership.** Chorus Call runs about 80% of Indian concalls. A commercial agreement (a listen-only feed, bulk/API registration, or a webcast link for every event) would make this whole method automatic in one move. This is a business-development task to start in parallel with the technical work.

### Method 2: Zoom webinars (8%)

- **For humans:** mandatory registration on `zoom.us/webinar/register/WN_…`. The confirmation email contains a personal join link (`…/w/<id>?tk=…`), and often the webinar ID, passcode and India phone numbers.
- **Automation:**
  1. **Register** with Playwright, using Kagger's identity and the `+callid` mailbox. Zoom's registration page may show a reCAPTCHA; if so, the call drops to the human step in 4.1 or 4.5.
  2. **Parse the confirmation email** for the join link, webinar ID, passcode and phone numbers.
  3. **Join**, preferably with a **meeting-bot vendor** such as Recall.ai: ~$0.50/recording-hour, handles Zoom/Teams/Webex/Meet, uses the registrant join link, and gives a clean audio stream. The alternative is **phoning in** via Exotel: Zoom India number → webinar ID# → passcode#. Whether a registration-only webinar admits an unregistered phone caller is unconfirmed, so this needs testing.
- **Shortcut:** when the filing already prints the webinar ID and passcode (Sagility), skip registration and join directly.
- **Cost:** bot ~₹44/hr (no telephony), or phone ~₹50–60/hr.

### Method 3: Webex (4%)

- Invites print the meeting number, the password, and a **numeric password for phone** (e.g. `Boi@1234 (26411234 when dialing from a phone…)`).
- **Fully automatic:** Exotel dials a Webex India access number, then sends access code# and numeric password# by DTMF. Or a meeting bot joins via the link.
- Some banks also ask for a Google-form "reserve your seat" registration. It's optional for joining; skip it, or fill it with Kagger's details where no CAPTCHA appears.

### Method 4: Teams live events (1%)

- Public `events.teams.microsoft.com/event/…` links. Attendees listen in the browser with no sign-in.
- **Fully automatic:** a meeting-bot vendor joins as a listener, or Playwright opens the event page and captures page audio (PulseAudio sink). Q&A is by text, so the bot never needs to talk.

### Method 5: Public webcasts: YouTube Live, RMG and other players, IR-site webcasts (~2–3%, plus 1C)

- **YouTube Live** (IndiaMART, the RMG webcast for Mahindra Lifespaces): resolve the live URL, then `yt-dlp` provides the live HLS stream → ffmpeg → AudioBus. The project already uses `yt-dlp` and `nodejs` for the Concall Agent.
- **Players behind a registration form** (RMG webcast): Playwright submits the form, then the same network-sniff approach as 1C extracts the stream (an embedded YouTube iframe → `yt-dlp`).
- Fully automatic, no telephony cost. Re-check at T-1h for links added late; companies often share the YouTube Live URL only on the day.

### Method 6: Registration platforms: Open Exchange and similar (1%)

- Register (form) → the email contains a personal link → usually Zoom underneath (Mastek's "Other International Numbers" link is `openexc.zoom.us`). Same flow as Method 2 once the email arrives.
- Its India numbers are ITFS (`000 800 …`); check that Exotel can dial them before relying on the phone path.

### Method 7: Details missing or elsewhere (4%)

- **Invite hosted on the company site** (Oberoi Realty): the filing links to `…/concall_invite.pdf`. The dial-in resolver should **follow PDF links** in the filing and re-run classification on the linked document. Automatic.
- **Not filed yet:** keep polling NSE call-update filings (already built) at T-1d, T-3h and T-1h.
- **Invite-only broker calls or unreadable filings:** show *"Report after call"* and use the official recording fallback (4.6).

---

## 4. When automation isn't enough: customer-assisted joining

These paths are built so that **one person's minute helps every follower**. They're ordered from least to most effort.

### 4.1 "Register for Kagger" (registration blocked by CAPTCHA or email verification)

- **When:** a registration form (DiamondPass, Zoom, Open Exchange, webcast) blocks automated sign-up.
- **Customer sees:** a card on the call: *"Needs 1 step: register Kagger for this call (about 1 min)."* Tapping it opens a panel with the registration link and Kagger's details (name *Kagger Research*, email `calls+<callid>@<kagger-domain>`, company *Kagger Research*), each with a copy button.
- **What happens:** the customer fills the form with Kagger's details and solves the CAPTCHA. The confirmation email goes to Kagger's mailbox, the inbox watcher parses it, and Kagger joins automatically. The `+<callid>` tag matches the email to the call even if several calls are registered at once.
- **The customer never shares their own email or credentials.**

### 4.2 "Paste your joining details"

- **When:** the customer has already registered themselves (common for analysts and fund managers), or got an invite from a broker.
- **Customer sees:** a text box: *"Paste the joining email or link."* An LLM extracts the number, passcode, PIN, meeting ID or join link, and Kagger confirms back: *"Got it: Zoom webinar 812 3456 7890, passcode found. Kagger will join at 3:58 PM."*
- **Note:** personal links (Zoom `tk=` tokens, DiamondPass PINs) identify the person who registered. Kagger should use them only with the customer's explicit consent, shown in the UI, and should prefer Kagger's own registration when possible.

### 4.3 "Listen through my browser" (tab audio sharing)

- **When:** anything the customer can hear in a desktop browser tab: a webcast Kagger can't reach, a Teams or Webex web client, an IR site player.
- **Customer sees:** *"Open the call in another tab, then click **Share tab audio**."* Chrome's tab picker appears with "Share tab audio" ticked. The live transcript starts within seconds.
- **How:** `getDisplayMedia({audio: true})` in the browser. The audio track (Opus/PCM) streams over a WebSocket to the listener worker, which treats it like any other source. Works in Chrome and Edge on desktop. Not supported in Safari, Firefox or on mobile, so the button is shown only where it works.
- **Trade-off:** the customer's tab must stay open, so it suits a customer who's listening anyway. Every other follower still gets the live transcript.

### 4.4 "Merge Kagger into your phone call"

- **When:** operator-assisted bridges where a human must answer the operator, and the customer is on their phone.
- **Customer sees:** *"Dial in as usual. Once you're in, add a call to **Kagger: 080-XXXX-XXXX** and tap **Merge**."* The number is shown with a copy button and a calendar entry is added.
- **How:** an inbound Exotel number. Kagger matches the merged call to the right session by the customer's caller ID plus the call's time window, or the customer enters the 4-digit code shown on the card. Exotel bills the inbound minutes at a lower rate than outbound.

### 4.5 Ops desk (Kagger staff) for the long tail

- A queue of calls that need a human, ranked by follower count. It mainly handles CAPTCHA registrations, revised numbers, and calls whose invite was filed late.
- At peak (results season, ~30–60 calls a day) the batch registrations take one person about 30–45 minutes the day before. That one-time daily effort covers every customer.
- The same panels as 4.1 and 4.2, plus overrides: set join method, edit credentials, force-listen.

### 4.6 Fallback: official recording

- Companies must publish the call audio within 24 hours and the transcript within 5 working days (SEBI LODR).
- The existing Concall Agent already finds and analyses these. For followed calls that couldn't be joined, the warmer re-runs it as soon as the audio appears and notifies followers: *"Live joining wasn't possible for X; here's the full analysis from the official recording."*

---

## 5. What the customer sees: call states

| Badge on the call card | Meaning | Customer action |
|---|---|---|
| 🟢 **Live: auto** | Kagger has a working join plan (webcast, registered, or credentials in hand) | None |
| 🟡 **Live: needs 1 step** | Joining needs a human step (4.1–4.4) | Optional; one tap opens the step |
| 🔵 **Report after call** | Live joining isn't possible (invite-only, no public access) | None; report arrives from the official recording |
| ⏳ **Details pending** | Invite not filed yet; re-checked every few hours | None |

The live room shows which source it's using ("Listening via Chorus Call webcast", "Listening by phone") and handles failover without the customer noticing beyond a short status line.

Notifications: *"Needs 1 step"* goes out at T-1 day and T-2 hours, only to followers who opted in to helping, and stops as soon as someone completes the step.

---

## 6. Build order

### What can be automated

| Method | Share | Fully automatic? | Human step (if any) | Listener |
|---|---|---|---|---|
| Chorus Call: operator path (1A) | 80% | **Yes, if the spike confirms operators admit the bot** | None | Exotel Voicebot (bidirectional) |
| Chorus Call: DiamondPass (1B) | (same 80%) | Join yes; registration no | Solve 1 CAPTCHA per call (ops desk or any follower) | Exotel + DTMF |
| Chorus Call: audio webcast (1C) | 3% | Yes | None | Playwright + HLS |
| Zoom webinar | 8% | Yes, unless the registration page shows a CAPTCHA | CAPTCHA registration, if shown | Meeting bot or phone |
| Webex | 4% | Yes | None | Phone + DTMF or bot |
| Teams live event | 1% | Yes | None | Bot or Playwright |
| YouTube / RMG / IR webcasts | 1–3% | Yes | None | yt-dlp / Playwright |
| Open Exchange & similar | 1% | Yes, unless CAPTCHA | CAPTCHA registration, if shown | Bot or phone |
| Invite linked from filing | 1% | Yes (follow link) | None | Per resolved method |
| No details / invite-only | 3% | No | None; official recording fallback | Batch |

**Coverage this implies:**

| Scenario | Live, fully automatic | Live with a 1-minute human step |
|---|---|---|
| Without the operator path | ~15% | ~95% |
| If the operator path (1A) works | **~95%** | — |

The remainder gets the report from the official recording.

### Recommended order

1. **Webcast and web-client capture (2 weeks).** Playwright + HLS capture for Chorus Call mediaframe, YouTube Live, RMG and Teams events. Webex by phone. Follow linked invite PDFs. Add the **"Listen through my browser"** tab-share (4.3): it's quick to build and works for any source a customer can open, so it's the universal safety net from day one.
2. **Chorus Call (2–3 weeks, starting with the spike).** Validate the operator path (1A) on real calls. In parallel, build the Kagger mailbox and email parser, the ops-desk queue, and "Register for Kagger" (1B / 4.1). Start the partnership conversation with Chorus Call.
3. **Zoom and registration platforms (2 weeks).** Automated registration where there's no CAPTCHA, email parsing, meeting-bot integration, and "Paste your joining details" (4.2).
4. **Polish.** "Merge Kagger into your call" (4.4), per-method success dashboards, and the T-1d / T-2h "needs 1 step" notifications.

### Data needed per call (extends `join_plan` from the main plan)

```
join_plan = {
  methods: [ {type: chorus_operator|diamondpass|chorus_webcast|zoom|webex|teams|youtube|webcast|open_exchange,
              url, numbers[], meeting_id, passcode, numeric_password, registration_url,
              needs_human: bool, human_reason: captcha|operator|none} ],
  chosen: <index>, standby: <index>,
  registration: {status: none|pending|done|blocked, by: auto|ops|customer:<user_id>, email_alias},
  credentials: {source: filing|email|customer_paste, received_at, ...},
  outlook: live_auto | live_needs_step | report_after_call | details_pending
}
```

The survey script that produced section 2 is the starting point for the production classifier. It already tags these methods from filing text and links using the dial-in extractor's PDF handling.

---

## 7. Risks and open questions

| Risk | Mitigation |
|---|---|
| Registration forms add CAPTCHAs or block automation | Never bypass CAPTCHAs. Route to 4.1 / the ops desk. Track block rate per provider. |
| Providers' terms on automated registration and recording | Legal review before launch. Honest identity, listen-only, no audio redistribution. Ask Chorus Call about a data partnership (they run most Indian calls). |
| Webcast player changes break the HLS capture | Network-sniffing approach (works across player UI changes), plus PulseAudio capture as a fallback, plus daily synthetic tests against a known webcast. |
| Personal join tokens shared by customers | Explicit consent, used only for that call, deleted afterwards; prefer Kagger's own registration. |
| Exotel can't dial ITFS `000 800` numbers or send post-dial DTMF | Verify in the Phase 0 spike. Fall back to universal access numbers or a second carrier for these calls. |
| Zoom requires approved apps for bots (OBF tokens) | Use a meeting-bot vendor that handles this (e.g. Recall.ai), or phone into the webinar. |

---

## Sources

- Survey data: Kagger's concall schedule (Screener calendar, 17–26 Oct 2026) and the linked BSE/NSE filings, classified locally.
- [HDFC Life Q1FY27 earnings call invite](https://www.hdfclife.com/content/dam/hdfclifeinsurancecompany/about-us/pdf/investor-relations/financial-information/investor-presentation/HDFC-Life-Q1FY27-Earnings-call-invite.pdf): DiamondPass pre-registration emails dial-in numbers, passcode and PIN.
- [ICICI Prudential AMC analyst call intimation](https://nsearchives.nseindia.com/corporate/AMCSECRETARIAL_08072026180151_ICICIINTIMATIONANALYST.pdf): universal numbers are routed to an operator.
- [Ecoline earnings call notice](https://nsearchives.nseindia.com/corporate/ECOLINE_07112025234325_Earnings_Con_Call.pdf): "Express Join with DiamondPass".
- [IndusInd Bank Q1FY27 earnings call intimation](https://www.indusind.bank.in/content/dam/indusind-corporate/corporate-announcement/Others/SE-intimation-Earnings-call-Q1FY27.pdf): Chorus Call listen-only audio webcast alongside dial-in.
- [IndiaMART Q2 FY2027 investor call intimation](https://nsearchives.nseindia.com/corporate/INDIAMART_20102026172324_SE_Intimation_of_Investor_Call_2026.pdf): Zoom registration plus YouTube Live.
- [Recall.ai 2026 pricing](https://recall.ai/blog/new-recall-ai-pricing-for-2026) ([pricing page](https://www.recall.ai/pricing)): $0.50/recording-hour across Zoom, Teams, Webex, Meet.
- [U-M: Join Zoom meetings/webinars by phone](https://teamdynamix.umich.edu/TDClient/30/Portal/KB/PrintArticle?ID=4666): ID# then passcode; participant ID only for already-joined attendees.
- [NAIC: joining Chorus Call and Webex meetings](https://content.naic.org/meetings-call-instructions.htm).
