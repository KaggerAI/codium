"""
analyst_prompts.py — Prompts for the Analyst Report Agent.
"""

ANALYST_SYNTHESIS_PROMPT = """You are an elite institutional equity research analyst synthesizing multiple brokerage reports on a single stock.

You will receive:
1. **Domestic Research Reports** — summaries and data from Indian brokerages (Motilal Oswal, ICICI Securities, etc.) sourced from Trendlyne, including AI-generated detailed summaries of full PDF reports where available.
2. **Global Research Reports** — summaries from international brokerages (Morgan Stanley, Goldman Sachs, etc.) sourced via Perplexity Search.

**Your output MUST be structured as follows:**

## 📊 Consensus Snapshot

| Metric | Value |
|--------|-------|
| **Consensus Rating** | Strong Buy / Buy / Hold / Sell |
| **Average Target (Domestic)** | ₹XXX |
| **Average Target (Global)** | ₹XXX |
| **Combined Consensus Target** | ₹XXX |
| **Upside from CMP** | XX% (Calculate from the provided Current Market Price) |
| **# Reports Analyzed** | X domestic + X global |

---

## 🏠 Domestic Research (Indian Brokerages)

For EACH domestic report, provide:

### [Brokerage Name] — [Buy/Sell/Hold] | Target: ₹XXX | [Date] (<u><i><a href="insert Report URL here" target="_blank">Read Report</a></i></u>)

**Key Points:**
- Main thesis
- Financial highlights
- Catalysts

> If a detailed AI summary was generated from the full PDF, include the comprehensive analysis here.

---

## 🌍 Global Research (International Brokerages)

For EACH global report, provide:

### [Brokerage Name] — [Buy/Sell/Hold] | Target: ₹XXX | [Date] (<u><i><a href="insert Report URL here" target="_blank">Read Report</a></i></u>)

**Key Points:**
- Investment thesis
- Target rationale
- Catalysts and risks

---

## 🔍 Key Themes Across Reports

Identify 3-5 recurring themes or consensus views:
1. Theme with supporting brokerage references
2. Theme with data points

---

## ⚔️ Divergent Views

Highlight where brokerages disagree:
- **Bulls:** [Brokerage] sees XX because...
- **Bears:** [Brokerage] is cautious because...

---

## ⚠️ Key Risks (Consensus)

| Risk Factor | Mentioned By | Severity |
|-------------|-------------|----------|
| Risk 1 | Broker A, Broker B | High/Med/Low |

---

## 📝 Bottom Line

A crisp 2-3 sentence synthesis: What is the overall street view? Is this a conviction buy/sell or a divided opinion?

---

**IMPORTANT:** Use actual numbers from the reports. Use ₹ for Indian prices. Keep the analysis data-driven and professional."""


ANALYST_CHAT_PROMPT = """You are an expert equity research analyst assistant. You have just completed a comprehensive analysis of analyst reports for {company_name} ({ticker}).

Here is the complete analysis you produced:

{analysis}

Here are the per-report details used in the analysis (ratings, targets, valuation method,
estimates, SWOT and thesis per brokerage, plus computed metrics):

DOMESTIC REPORTS:
{domestic_reports}

OTHER COVERAGE (news-sourced, incl. global brokerages):
{global_reports}

The user will now ask follow-up questions about these analyst reports. Answer thoroughly using the data available. If asked about something not covered in the reports, say so honestly.

Key guidelines:
- Always cite which brokerage said what, and the report date
- Use specific numbers and data points
- Compare views across brokerages when relevant
- Be candid about limitations (e.g., "Only 2 out of 5 brokerages mentioned this risk",
  or "this target is 5 months old")
"""


# ---------------------------------------------------------------------
# Structured extraction from a single brokerage PDF
# ---------------------------------------------------------------------
ANALYST_PDF_EXTRACTION_PROMPT = """You are a meticulous equity-research data extractor. The attached PDF is a brokerage research report on {company_name} ({ticker}), published by {brokerage} on {report_date}.

Extract the report's content into the JSON schema provided. Rules:

NUMBERS
- Copy numbers exactly from the report's tables; never estimate or invent a number. Use null when the report does not state it.
- Money in INR crore (Rs 10 mn = 1 crore; Rs 1 bn = 100 crore). Convert if the report uses Rs mn / Rs bn / USD mn (state USD conversions only if the report gives the rate; otherwise leave null).
- EPS and book value per share in rupees per share. Percentages as plain numbers (18.5, not 0.185).
- fiscal_year labels MUST look like "FY26A", "FY27E", "FY28E" (Indian April-March fiscal years). Use "CY26E" only if the company reports on a calendar year. "A" = actual, "E" = estimate.
- `estimates`: one row per fiscal year in the report's key-financials / forecast table - include the latest ACTUAL year plus every forecast year. For banks/NBFCs/insurers put Net Interest Income (or total income / net premium) in revenue_cr, PPOP (pre-provision operating profit) in ebitda_cr, and fill bvps / roe_pct / roa_pct.
- `estimate_revisions`: only if the report has a "change in estimates" table or states that it raised/cut estimates; one row per metric-year with the % change.

TARGET HORIZON
- `target_horizon_months`: the period the target price is for, only if the report states it ("12-month target" -> 12, "18-month TP" -> 18).
- `target_date`: only if the report dates the target itself ("Sep-27 TP", "target price for Mar-28", "Dec 2027 target") -> "Sep-27" style. Do NOT infer it from the valuation basis year ("25x Sep-28E EPS" is the valuation basis, not the target date). Else null.

VALUATION
- `valuation.method`: the PRIMARY method used to set the target price.
- `valuation.multiple` + `valuation.basis_year` describe e.g. "25x FY28E EPS" -> multiple 25, basis_year "FY28E", metric "EPS".
- For DCF give WACC / terminal growth in `valuation.details`; for SOTP give the segment break-up in `valuation.details`.
- `valuation.rationale`: why the analyst thinks that multiple / method is right (premium/discount to history or peers, etc.).

QUALITATIVE (write in your own concise words, but ONLY from what this report says)
- `thesis`: the analyst's core investment argument in 2-3 sentences.
- `swot`: 2-4 short points per quadrant (each <= 15 words). If the report has no explicit SWOT, derive it strictly from the report's positives, concerns, opportunities and risks.
- `industry_outlook`: what the report says about the sector/industry - growth rates with numbers wherever given.
- `key_drivers`: ONLY positive forces the analyst expects to push revenue / earnings UP (new orders, capacity, pricing, product launches, market-share gains). Never put slowdowns, competition or degrowth here. If the report names none, return [].
- `headwinds`: pressures the analyst says are holding growth back NOW (competition taking share, demand slowdown, loan-book degrowth, margin pressure). Distinct from `risks`, which are things that MAY go wrong in future.
- `catalysts`, `risks`, `management_guidance`: short bullet phrases.
- `key_quote`: one verbatim sentence from the report that best captures the view (max 40 words).
- `detailed_summary`: an in-depth research write-up in markdown (600-1000 words) that lets an investor skip reading the PDF. It must go BEYOND the thesis / SWOT / bullet fields above, not restate them. Cover, with the report's own numbers: (1) what the report reacts to - results vs the analyst's and street estimates, YoY/QoQ figures; (2) segment / business-line detail and operating metrics (volumes, order book, utilisation, loan growth, asset quality, margins by segment); (3) management commentary and guidance the report relays; (4) estimate changes and the reasons for them; (5) valuation workings - multiple, basis year, peer or historical comparison, bull/bear cases; (6) risks and what would change the analyst's view. Use short headings, bullets and a small markdown table for multi-year figures where useful. Never invent - omit a section the report does not cover.

If a field truly does not apply, use null (scalars) or [] (lists)."""


_N_NUM = {"type": ["number", "null"]}
_N_STR = {"type": ["string", "null"]}
_STR_LIST = {"type": "array", "items": {"type": "string"}}

ANALYST_PDF_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "report_type": {"type": "string", "enum": [
            "Initiation", "Results update", "Company update", "Event / management meet",
            "Thematic / sector", "Technical / trading call", "Other"]},
        "broker_view_found": {"type": "boolean",
                              "description": "false if the material contains no view attributed to this brokerage on this company"},
        "rating_raw": _N_STR,
        "rating": {"type": "string", "enum": ["Buy", "Hold", "Sell", "Not rated"]},
        "target_price": _N_NUM,
        "target_horizon_months": _N_NUM,
        "target_date": {"type": ["string", "null"],
                        "description": "Month the target is set for if the source states it, e.g. 'Sep-27'"},
        "price_in_report": _N_NUM,
        "previous_rating": _N_STR,
        "previous_target_price": _N_NUM,
        "bull_case_target": _N_NUM,
        "bear_case_target": _N_NUM,
        "valuation": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": [
                    "P/E", "EV/EBITDA", "P/B", "P/ABV", "EV/Sales", "DCF", "SOTP",
                    "Dividend yield", "Other", "Not stated"]},
                "multiple": _N_NUM,
                "metric": _N_STR,
                "basis_year": _N_STR,
                "details": _N_STR,
                "rationale": _N_STR,
            },
            "required": ["method"],
        },
        "estimates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fiscal_year": {"type": "string"},
                    "revenue_cr": _N_NUM,
                    "ebitda_cr": _N_NUM,
                    "ebitda_margin_pct": _N_NUM,
                    "pat_cr": _N_NUM,
                    "eps": _N_NUM,
                    "bvps": _N_NUM,
                    "roe_pct": _N_NUM,
                    "roa_pct": _N_NUM,
                    "pe_reported": _N_NUM,
                },
                "required": ["fiscal_year"],
            },
        },
        "estimate_revisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fiscal_year": {"type": "string"},
                    "metric": {"type": "string"},
                    "change_pct": _N_NUM,
                },
                "required": ["fiscal_year", "metric"],
            },
        },
        "thesis": {"type": "string"},
        "key_drivers": {"type": "array", "items": {"type": "string"},
                        "description": "Positive growth drivers only; pressures go in headwinds"},
        "headwinds": {"type": "array", "items": {"type": "string"},
                      "description": "Current pressures holding growth back (not future risks)"},
        "catalysts": _STR_LIST,
        "risks": _STR_LIST,
        "management_guidance": _STR_LIST,
        "swot": {
            "type": "object",
            "properties": {
                "strengths": _STR_LIST, "weaknesses": _STR_LIST,
                "opportunities": _STR_LIST, "threats": _STR_LIST,
            },
            "required": ["strengths", "weaknesses", "opportunities", "threats"],
        },
        "industry_outlook": {
            "type": "object",
            "properties": {
                "summary": _N_STR,
                "growth_figures": _STR_LIST,
            },
        },
        "growth_guidance": {
            "type": "array",
            "description": "Growth rates the broker states, e.g. '25% revenue CAGR over FY26-29E'",
            "items": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string", "description": "Revenue | EBITDA | PAT | EPS | other"},
                    "cagr_pct": _N_NUM,
                    "period": _N_STR,
                },
                "required": ["metric"],
            },
        },
        "key_quote": _N_STR,
        "detailed_summary": {"type": "string"},
    },
    "required": ["rating", "valuation", "estimates", "thesis", "swot", "detailed_summary"],
}


# ---------------------------------------------------------------------
# Full-article extraction for a news-sourced brokerage call
# ---------------------------------------------------------------------
ANALYST_ARTICLE_EXTRACTION_PROMPT = """You are a meticulous equity-research data extractor. The articles below report on a research note by {brokerage} on {company_name} ({ticker}), published around {report_date}. The full note itself is not available, only press coverage of it.

Extract what these articles say ABOUT {brokerage}'S VIEW ON {company_name} into the JSON schema. Rules:
- Use ONLY facts the articles attribute to {brokerage}. Articles often quote several brokerages or other companies: ignore everything not attributed to {brokerage}'s note on {company_name}.
- Copy numbers exactly; never estimate. Use null / [] when the articles don't say.
- Money in INR crore; EPS in rupees; percentages as plain numbers.
- fiscal_year labels like "FY28E" (Indian April-March years). If the articles give a P/E "at the current price" for a year, put it in estimates[].pe_reported for that year.
- Valuation: e.g. "40 times September 2028 EPS" -> method "P/E", multiple 40, metric "EPS", basis_year "Sep-28E".
- Stated growth rates ("25% revenue CAGR over FY26-29E") go in growth_guidance.
- thesis, swot, catalysts, risks, key_drivers, headwinds, industry_outlook, management_guidance: from what the articles report of {brokerage}'s view. Product names, segment targets (e.g. "formulations and biologics ~60% of revenue by FY29") and approvals belong in key_drivers / catalysts.
- key_drivers: ONLY positive forces {brokerage} expects to push growth UP. Slowdowns, competition, degrowth and other pressures the broker cites go in headwinds, never in key_drivers. If the broker names no positive driver, key_drivers is [].
- headwinds: pressures {brokerage} says are holding growth back now. risks: things that may go wrong in future.
- target_horizon_months / target_date: only if the articles state the target's horizon ("12-month target" -> 12) or date ("Sep-27 target" -> "Sep-27"). Never infer the date from the valuation basis year.
- key_quote: a sentence the articles attribute to {brokerage}, verbatim (max 40 words), else null.
- detailed_summary: a complete account in markdown of EVERYTHING the articles attribute to {brokerage} on {company_name} - every number, estimate, segment comment, argument and caveat - organised under short headings, so a reader needs nothing else. Go beyond the thesis / bullet fields: include the supporting figures and reasoning behind them. Length follows the material (typically 250-600 words); never pad, never repeat a point, never add anything the articles don't say, and never calculate a figure the articles don't print (e.g. back out a share price from a target and an upside).
- report_type: "Initiation" if the articles say the broker initiated coverage. "Technical / trading call" if the recommendation is chart / momentum based (technical analyst picks, festive or weekly "stock picks", stop-loss levels) rather than fundamental research.
- broker_view_found: false if the articles contain NO rating, target or opinion that {brokerage} itself expressed on {company_name} (e.g. the broker is only named in passing, or covers a different company in the same article). In that case leave every other field empty.

ARTICLES:
{articles}"""


# ---------------------------------------------------------------------
# Structured extraction of brokerage calls from news / search snippets
# ---------------------------------------------------------------------
ANALYST_NEWS_EXTRACTION_PROMPT = """You are an elite financial data extraction assistant.
Below are real-time web search results about brokerage research on the Indian stock {company_name} ({ticker}). Today is {today}.

Extract EVERY distinct brokerage call (rating and/or target price) on {company_name} that the results report.

RULES
1. Only calls on {company_name} itself - ignore other companies mentioned in the same article.
2. Only calls published within the last {months} months. Take the date from the article's "Published Date" metadata or the text; if no date can be established, skip the call.
3. One entry per brokerage: keep its MOST RECENT call. Use the brokerage's common name (e.g. "Morgan Stanley", "Jefferies", "Kotak Institutional Equities", "Nuvama").
4. Do NOT include these brokerages (their full reports are already covered separately): {exclude}.
5. Copy numbers exactly; target_price is a plain number in rupees. Use null for anything not stated. Never invent valuation multiples or estimates.
5b. Only SEBI-registered brokerages / institutional research houses count. Ignore price-forecast blogs, algorithmic "share price target 2026" pages, tip sites, influencers and the stock-screening sites themselves.
6. `origin` = "Global" for foreign research houses (Morgan Stanley, Goldman Sachs, JPMorgan, Jefferies, CLSA, Nomura, Macquarie, UBS, Citi, HSBC, BofA, Bernstein, Investec, etc.), else "Domestic".
7. source_url must be the EXACT URL of the search result the call came from.

{context_block}"""

ANALYST_NEWS_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "reports": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "brokerage": {"type": "string"},
                    "origin": {"type": "string", "enum": ["Global", "Domestic"]},
                    "rating_raw": _N_STR,
                    "rating": {"type": "string", "enum": ["Buy", "Hold", "Sell", "Not rated"]},
                    "target_price": _N_NUM,
                    "previous_target_price": _N_NUM,
                    "date": {"type": "string", "description": "e.g. 14 Nov 2026"},
                    "valuation_method": _N_STR,
                    "valuation_multiple": _N_NUM,
                    "valuation_basis_year": _N_STR,
                    "eps_estimates": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"fiscal_year": {"type": "string"}, "eps": _N_NUM},
                            "required": ["fiscal_year"],
                        },
                    },
                    "thesis": {"type": "string"},
                    "risks": _STR_LIST,
                    "source_url": {"type": "string"},
                },
                "required": ["brokerage", "origin", "rating", "date", "thesis", "source_url"],
            },
        },
    },
    "required": ["reports"],
}


# ---------------------------------------------------------------------
# Cross-broker synthesis (structured)
# ---------------------------------------------------------------------
ANALYST_SYNTHESIS_STRUCTURED_PROMPT = """You are the head of research consolidating every sell-side view on {company_name} ({ticker}) for a portfolio manager. Today is {today}. Current market price: {cmp}.

You receive (1) per-brokerage extractions from full PDF reports, (2) news-sourced calls (often global brokerages, thinner data), and (3) metrics already COMPUTED in code (consensus target, upside, forward P/E, PEG, EPS dispersion, valuation methods, target-price momentum, LSEG consensus). Treat the computed metrics as ground truth - do not recompute or contradict them; reference them.

Produce the JSON schema provided:
- `verdict_headline`: one punchy sentence on what the street thinks and how strongly (<= 25 words).
- `bottom_line`: 3-5 sentences for the PM: the consensus, the quality of that consensus (fresh vs stale, dispersion, estimate momentum), what has to go right, and the biggest disagreement.
- `swot`: CONSOLIDATE the brokers' SWOT points. Merge points that mean the same thing into one, and list every broker that made it in `brokers`. Order each quadrant by number of brokers (most first). 3-6 points per quadrant, each <= 18 words.
- `debates`: 2-4 issues where brokers genuinely disagree (growth durability, margins, valuation, etc.): the bull view, the bear view, and which brokers are on each side.
- `bull_case` / `bear_case`: the strongest version of each side in 2-3 sentences, with the highest / lowest target and who holds it.
- `valuation_view`: 2-4 sentences on HOW the street values the stock - which methods and multiples dominate, the range of multiples, and whether the targets look demanding versus the computed forward P/E / PEG.
- `industry_outlook`: what the brokers collectively expect for the industry, with growth numbers and who said them; list tailwinds and headwinds.
- `catalysts` and `risks`: consolidated, with the brokers that cite each; risk severity High/Medium/Low by how much it could move earnings and how many brokers flag it.
- `estimate_momentum`: 1-2 sentences on whether estimates / targets are being raised or cut, using the computed momentum data and any estimate-revision tables.

Rules: cite brokerages by name exactly as given; never invent a number that is not in the input; when only one broker supports a point, say so through the `brokers` list rather than overstating consensus.

INPUT DATA (JSON):
{payload}"""

_POINT_LIST = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "point": {"type": "string"},
            "brokers": _STR_LIST,
        },
        "required": ["point", "brokers"],
    },
}

ANALYST_SYNTHESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict_headline": {"type": "string"},
        "bottom_line": {"type": "string"},
        "swot": {
            "type": "object",
            "properties": {
                "strengths": _POINT_LIST, "weaknesses": _POINT_LIST,
                "opportunities": _POINT_LIST, "threats": _POINT_LIST,
            },
            "required": ["strengths", "weaknesses", "opportunities", "threats"],
        },
        "debates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "bull_view": {"type": "string"},
                    "bull_brokers": _STR_LIST,
                    "bear_view": {"type": "string"},
                    "bear_brokers": _STR_LIST,
                },
                "required": ["topic", "bull_view", "bear_view"],
            },
        },
        "bull_case": {"type": "string"},
        "bear_case": {"type": "string"},
        "valuation_view": {"type": "string"},
        "industry_outlook": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "growth_figures": _POINT_LIST,
                "tailwinds": _STR_LIST,
                "headwinds": _STR_LIST,
            },
            "required": ["summary"],
        },
        "catalysts": _POINT_LIST,
        "risks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "point": {"type": "string"},
                    "severity": {"type": "string", "enum": ["High", "Medium", "Low"]},
                    "brokers": _STR_LIST,
                },
                "required": ["point", "severity", "brokers"],
            },
        },
        "estimate_momentum": {"type": "string"},
    },
    "required": ["verdict_headline", "bottom_line", "swot", "debates", "bull_case",
                 "bear_case", "valuation_view", "industry_outlook", "risks"],
}


ANALYST_PDF_SUMMARY_PROMPT = """You are an expert financial analyst. Analyze this brokerage research report and provide a comprehensive, well-structured summary.

**CRITICAL FORMATTING REQUIREMENTS:**
- Use markdown tables for ALL financial data and metrics
- Use headers (##, ###) for clear section organization
- Use bullet points for lists
- Use **bold** for important numbers and metrics
- Use > blockquotes for key analyst opinions
- For revenue, EBITDA, and PAT figures, use crores for INR and millions for USD. Make conversions where required.

**Your output MUST include:**

## 📊 Investment Snapshot

| Metric | Value |
|--------|-------|
| **Recommendation** | BUY/SELL/HOLD |
| **Target Price** | ₹XXX |
| **Upside/Downside** | XX% |
| **Risk Rating** | Low/Medium/High |

---

## 💰 Financial Highlights

Key financials table with quarterly/annual metrics:

| Period | Revenue (Cr) | EBITDA (Cr) | PAT (Cr) | Margins (%) | YoY Growth |
|--------|---|---|---|---|---|

**Key Ratios** (P/E, EV/EBITDA, ROE, ROCE)

---

## 📈 Investment Thesis & Growth Drivers

> **Main Rationale:** [Quote the analyst's core thesis]

- Key growth drivers (3-5 points)
- Catalysts for re-rating

---

## 🏢 Business & Operational Updates

- Recent performance
- New orders/contracts
- Management guidance

---

## ⚠️ Key Risks

| Risk | Probability | Impact |
|------|------------|--------|

---

## 📝 Analyst's Final View

> **Key Quote from the report**

**IMPORTANT:** Extract ACTUAL numbers from the PDF. Use Indian number format (Crores, Lakhs) and ₹ symbol."""
