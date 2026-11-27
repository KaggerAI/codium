"""
analyst_extract.py - Structured extraction for the Analyst Report Agent.

  * extract_pdf_report()   - one brokerage PDF -> schema'd JSON (rating, target,
                             valuation method, multi-year estimates, SWOT, outlook...)
  * fetch_news_calls()     - several targeted Perplexity searches in parallel ->
                             schema'd brokerage calls (mostly global houses whose
                             PDFs are paywalled, plus domestic institutional desks
                             Trendlyne doesn't carry)
  * gemini_json()          - shared JSON-mode Gemini call with retries / fallback

PDF extractions are cached on disk keyed by the PDF URL (Trendlyne's document id is
unique per report and a published report never changes), so re-running the agent
for a ticker only pays for reports it has not read before.
"""

import os
import sys
import json
import re
import time
import hashlib
import datetime
import traceback
import concurrent.futures
from io import BytesIO

from google import genai
from google.genai import types

from agents.prompts.analyst_prompts import (
    ANALYST_PDF_EXTRACTION_PROMPT, ANALYST_PDF_EXTRACTION_SCHEMA,
    ANALYST_NEWS_EXTRACTION_PROMPT, ANALYST_NEWS_EXTRACTION_SCHEMA, ANALYST_ARTICLE_EXTRACTION_PROMPT,
)

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")


def _log(*parts):
    """stderr log that cannot raise on a non-ASCII Windows console (cp1252)."""
    try:
        print(*parts, file=sys.stderr)
    except UnicodeEncodeError:
        print(*(str(p).encode("ascii", "replace").decode() for p in parts), file=sys.stderr)

# Model + thinking level per pipeline step, overridable by env without a code change:
#   ANALYST_<STEP>_MODEL / ANALYST_<STEP>_THINKING   (STEP = PDF, NEWS, SYNTHESIS)
# Thinking levels: MINIMAL | LOW | MEDIUM | HIGH.
#   pdf       - numbers out of dense broker tables + unit conversion (Rs mn -> crore).
#               Date-dependent default, decided on measured cost (eval_extraction_models.py,
#               8 real reports, Nov 2026):
#                 * flash-lite at MINIMAL / LOW made 10x unit errors -> never use those.
#                 * flash-lite at HIGH: 93% agreement, no unit errors, ~4.3k thinking tokens
#                   per report -> $0.0179/report vs $0.0140 for 3.8-flash at its introductory
#                   price ($0.75 / $3.75 per 1M), which doubles on 1 Jan 2027 ($0.0280/report).
#               So: 3.8-flash LOW until 31 Dec 2026, flash-lite HIGH from 1 Jan 2027 (IST).
#   news      - copying ratings/targets out of short news snippets: cheap model is enough.
#   synthesis - merging SWOT / debates across brokers: judgement, one call per run.
_STEP_DEFAULTS = {
    "pdf": ("gemini-3.8-flash", "LOW"),
    "news": ("gemini-3.5-flash-lite", "MINIMAL"),
    "synthesis": ("gemini-3.8-flash", "MEDIUM"),
}
_PDF_SWITCH_DATE = datetime.date(2027, 1, 1)
_PDF_DEFAULT_FROM_SWITCH = ("gemini-3.5-flash-lite", "HIGH")


def _today_ist() -> datetime.date:
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)).date()


def step_config(step: str, today: datetime.date | None = None) -> dict:
    """{'model': ..., 'thinking_level': ...} for a pipeline step, env overrides applied.
    Evaluated per call, so a long-running server picks up the 1 Jan 2027 switch by itself."""
    model, thinking = _STEP_DEFAULTS[step]
    if step == "pdf" and (today or _today_ist()) >= _PDF_SWITCH_DATE:
        model, thinking = _PDF_DEFAULT_FROM_SWITCH
    key = step.upper()
    return {
        "model": (os.getenv(f"ANALYST_{key}_MODEL") or model).strip(),
        "thinking_level": (os.getenv(f"ANALYST_{key}_THINKING") or thinking).strip().upper(),
    }


EXTRACT_MODEL = step_config("pdf")["model"]   # kept for back-compat with older callers
FALLBACK_MODEL = "gemini-3.5-flash-lite"

# Bump when the extraction schema/prompt changes so stale cached extractions are ignored.
EXTRACTION_VERSION = "v1"
_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "temp_reports", "analyst_extractions")

_client = None


def _get_client():
    global _client
    if _client is None:
        if not GOOGLE_API_KEY:
            raise ValueError("GOOGLE_API_KEY not configured")
        _client = genai.Client(api_key=GOOGLE_API_KEY)
    return _client


def _parse_json(text: str):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    return json.loads(text)


def gemini_json(contents, schema: dict, usage_tag: str, model: str | None = None,
                thinking_level: str | None = "LOW", temperature: float = 0.2,
                usage_out: dict | None = None):
    """
    JSON-mode Gemini call. Retries transient errors, then falls back to flash-lite.
    If a model rejects the thinking level (e.g. MINIMAL unsupported) it is retried
    without a thinking config rather than failing the step.
    usage_out, if given, receives the model used and token counts (for evaluation).
    """
    from agents.llm_usage import log_gemini_usage

    client = _get_client()
    models = [model or EXTRACT_MODEL]
    if FALLBACK_MODEL not in models:
        models.append(FALLBACK_MODEL)

    last_err = None
    for m in models:
        level = thinking_level
        attempt = 0
        while attempt < 3:
            attempt += 1
            try:
                cfg_args = dict(
                    temperature=temperature,
                    response_mime_type="application/json",
                    response_json_schema=schema,
                )
                if level:
                    cfg_args["thinking_config"] = types.ThinkingConfig(thinking_level=level)
                resp = client.models.generate_content(
                    model=m, contents=contents, config=types.GenerateContentConfig(**cfg_args))
                log_gemini_usage(resp, m, usage_tag)
                if usage_out is not None:
                    um = getattr(resp, "usage_metadata", None)
                    usage_out.update({
                        "model": m, "thinking_level": level,
                        "prompt_tokens": getattr(um, "prompt_token_count", 0) or 0,
                        "output_tokens": getattr(um, "candidates_token_count", 0) or 0,
                        "thinking_tokens": getattr(um, "thoughts_token_count", 0) or 0,
                    })
                return _parse_json(resp.text)
            except json.JSONDecodeError as e:
                last_err = e
                _log(f"ANALYST_EXTRACT: bad JSON from {m} (attempt {attempt}): {e}")
            except Exception as e:
                last_err = e
                msg = str(e)
                _log(f"ANALYST_EXTRACT: {m} attempt {attempt} failed: {msg[:200]}")
                if level and "thinking" in msg.lower() and ("400" in msg or "INVALID" in msg):
                    _log(f"ANALYST_EXTRACT: {m} rejected thinking level {level}; retrying without it")
                    level = None
                    attempt -= 1
                    continue
                transient = any(s in msg for s in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "overloaded", "500"))
                if not transient:
                    break  # e.g. model not found -> go straight to the fallback model
                time.sleep(2 * attempt)
    raise RuntimeError(f"Gemini JSON call failed: {last_err}")


# =====================================================================
# PDF extraction
# =====================================================================

def _cache_path(pdf_url: str) -> str:
    h = hashlib.sha1(f"{EXTRACTION_VERSION}|{pdf_url}".encode()).hexdigest()[:20]
    return os.path.join(_CACHE_DIR, f"{h}.json")


def load_cached_extraction(pdf_url: str) -> dict | None:
    if not pdf_url:
        return None
    p = _cache_path(pdf_url)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _save_cached_extraction(pdf_url: str, data: dict):
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_cache_path(pdf_url), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        _log(f"ANALYST_EXTRACT: could not cache extraction: {e}")


def extract_pdf_report(pdf_bytes: bytes, pdf_url: str, brokerage: str, ticker: str,
                       company_name: str, report_date: str, model: str | None = None,
                       thinking_level: str | None = None, save_cache: bool = True,
                       usage_out: dict | None = None) -> dict:
    """Synchronous. Upload PDF, extract to schema, delete upload, cache result.
    model / thinking_level default to step_config('pdf'); the evaluation script passes
    its own and save_cache=False so comparisons never overwrite production extractions."""
    cfg = step_config("pdf")
    model = model or cfg["model"]
    thinking_level = thinking_level or cfg["thinking_level"]
    client = _get_client()
    uploaded = client.files.upload(
        file=BytesIO(pdf_bytes),
        config=types.UploadFileConfig(
            display_name=f"{brokerage}_{ticker}_report.pdf"[:120],
            mime_type="application/pdf",
        ),
    )
    try:
        prompt = ANALYST_PDF_EXTRACTION_PROMPT.format(
            company_name=company_name, ticker=ticker,
            brokerage=brokerage, report_date=report_date or "unknown date",
        )
        data = gemini_json(
            [types.Part.from_text(text=prompt), uploaded],
            ANALYST_PDF_EXTRACTION_SCHEMA,
            usage_tag="analyst.pdf_extract",
            model=model, thinking_level=thinking_level, usage_out=usage_out,
        )
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass

    if not isinstance(data, dict):
        raise RuntimeError("extraction did not return an object")
    data["_extracted_at"] = time.time()
    data["_model"] = (usage_out or {}).get("model", model)
    if pdf_url and save_cache:
        _save_cached_extraction(pdf_url, data)
    return data


# =====================================================================
# News-sourced brokerage calls (global houses + domestic institutional)
# =====================================================================

NEWS_DOMAINS = [
    "reuters.com", "bloomberg.com", "cnbctv18.com", "moneycontrol.com",
    "economictimes.indiatimes.com", "livemint.com", "business-standard.com",
    "financialexpress.com", "ndtvprofit.com", "thehindubusinessline.com",
    "investing.com", "marketscreener.com", "zeebiz.com", "businesstoday.in",
    "trendlyne.com", "goodreturns.in", "cnbc.com", "outlookbusiness.com",
    "tradingview.com",   # republishes Moneycontrol stories (moneycontrol.com itself is often thin)
]

BROKER_GROUPS = [
    "Morgan Stanley, Goldman Sachs, JPMorgan",
    "Jefferies, CLSA, Nomura",
    "Macquarie, UBS, Citi",
    "HSBC, BofA Securities, Bernstein, Investec",
    "Kotak Institutional Equities, Nuvama, IIFL Securities, Emkay",
    "Elara Capital, Ambit Capital, Avendus Spark, Antique Stock Broking, Systematix",
]


def fetch_news_calls(ticker: str, company_name: str, call_perplexity_search_api_fn,
                     exclude_brokers: list[str], months: int = 6) -> tuple[list[dict], int]:
    """
    Run one Perplexity search per broker group plus a generic one, in parallel; pool the
    unique results; extract structured calls with Gemini. Returns (calls, n_sources).
    """
    since = datetime.datetime.now() - datetime.timedelta(days=30 * months)
    date_filter = f"{since.month}/{since.day}/{since.year}"

    short_name = _short_name(company_name)
    # (query, restrict to NEWS_DOMAINS?)  Broker-group queries find the big houses; the
    # open-web ones catch small/mid-cap coverage that never reaches the main news sites.
    queries = [
        (f"{short_name} ({ticker}) {group} rating target price research note", True)
        for group in BROKER_GROUPS
    ]
    queries += [
        (f"{short_name} share price target brokerage raises cuts upgrade downgrade", True),
        (f"{short_name} brokerages views after results target price", True),
        (f"{short_name} brokerage initiates coverage buy target price", False),
        (f"{short_name} stock research report brokerage rating target price", False),
    ]

    def _one(q):
        query, restricted = q
        try:
            return call_perplexity_search_api_fn(
                query=query, search_domain_filter=NEWS_DOMAINS if restricted else None,
                search_after_date_filter=date_filter,
                max_results=10, max_tokens_per_page=900, timeout=90,
            ) or []
        except Exception as e:
            _log(f"ANALYST_EXTRACT: search failed for '{query[:60]}': {e}")
            return []

    pooled, seen = [], set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(queries)) as ex:
        for results in ex.map(_one, queries):
            for r in results:
                u = (r.get("url") or "").split("#")[0]
                if u and u not in seen:
                    seen.add(u)
                    pooled.append(r)

    if not pooled:
        return [], 0, []

    # Keep only results that mention the company at all - generic market wraps dilute extraction.
    key = short_name.split()[0].lower()
    relevant = [r for r in pooled
                if key in (r.get("title", "") + " " + r.get("snippet", "")).lower()
                or ticker.lower() in (r.get("title", "") + " " + r.get("snippet", "")).lower()]
    relevant = [r for r in relevant
                if not any(_host(r.get("url") or "") == d or _host(r.get("url") or "").endswith("." + d)
                           for d in LOW_QUALITY_DOMAINS)][:50]
    if not relevant:
        return [], 0, []

    ctx = "SEARCH RESULTS:\n"
    for i, r in enumerate(relevant, 1):
        ctx += f"[{i}] Title: {r.get('title')}\nURL: {r.get('url')}\n"
        if r.get("date"):
            ctx += f"Published Date: {r.get('date')}\n"
        ctx += f"Snippet: {(r.get('snippet') or '')[:2500]}\n\n"

    prompt = ANALYST_NEWS_EXTRACTION_PROMPT.format(
        company_name=company_name, ticker=ticker,
        today=datetime.date.today().strftime("%d %b %Y"), months=months,
        exclude=", ".join(exclude_brokers) or "none",
        context_block=ctx,
    )
    try:
        data = gemini_json([types.Part.from_text(text=prompt)], ANALYST_NEWS_EXTRACTION_SCHEMA,
                           usage_tag="analyst.news_extract", **step_config("news"))
        calls = data.get("reports", []) if isinstance(data, dict) else []
    except Exception as e:
        _log(f"ANALYST_EXTRACT: news extraction failed: {e}")
        traceback.print_exc(file=sys.stderr)
        calls = []

    # The URL must be one we actually gave the model.
    valid_urls = {r.get("url") for r in relevant}
    for c in calls:
        if c.get("source_url") not in valid_urls:
            c["source_url"] = None
    return calls, len(relevant), relevant


KNOWN_BROKERS = [
    # domestic (retail + institutional), incl. small-cap specialists
    "Motilal Oswal", "ICICI Securities", "ICICI Direct", "HDFC Securities", "Kotak Institutional Equities",
    "Kotak Securities", "Axis Securities", "Axis Direct", "Nuvama", "Edelweiss", "IIFL", "Emkay", "Elara",
    "Ambit", "Avendus Spark", "Antique", "Systematix", "DAM Capital", "Ventura", "Choice Broking",
    "Arihant Capital", "Nirmal Bang", "SMIFS", "KR Choksey", "Deven Choksey", "Monarch Networth",
    "Asian Markets Securities", "Dolat Capital", "Centrum", "Equirus", "PhillipCapital", "Sharekhan",
    "Anand Rathi", "Prabhudas Lilladher", "PL Capital", "Geojit", "BOB Capital", "IDBI Capital",
    "JM Financial", "Yes Securities", "InCred", "Batlivala", "B&K Securities", "Mirae Asset",
    "SBI Securities", "Religare", "Hem Securities", "Ashika", "Way2Wealth", "Bajaj Broking", "BP Equities",
    "Marwadi", "Swastika", "LKP Securities", "Keynote", "Tata Securities", "Investec", "Spark Capital",
    "Haitong", "Jainam", "Ajcon", "Sushil Finance", "Indsec",
    # global
    "Morgan Stanley", "Goldman Sachs", "JPMorgan", "J.P. Morgan", "Jefferies", "CLSA", "Nomura",
    "Macquarie", "UBS", "Citi", "HSBC", "BofA", "Bank of America", "Bernstein", "Barclays", "BNP Paribas",
]


def scan_broker_mentions(results: list[dict], company_name: str, ticker: str) -> list[dict]:
    """
    Deterministic lead finder: any known brokerage named in a search result that also names
    the company. Catches calls the LLM extractor skipped or that never made it into a
    structured call (e.g. 'Antique suggests buying Shilpa Medicare, anticipating a 22% rise').
    Returns [{'brokerage', 'date', 'url'}] - newest mention per broker.
    """
    ckey = _short_name(company_name).split()[0].lower()
    found: dict[str, dict] = {}
    for r in results:
        blob = f"{r.get('title', '')} {r.get('snippet') or ''}"
        low = blob.lower()
        if ckey not in low and ticker.lower() not in low:
            continue
        for b in KNOWN_BROKERS:
            if re.search(r"(?<![a-z])" + re.escape(b.lower()) + r"(?![a-z])", low):
                prev = found.get(b)
                if not prev or (r.get("date") or "") > (prev.get("date") or ""):
                    found[b] = {"brokerage": b, "date": r.get("date"), "url": r.get("url")}
    return list(found.values())


def _sonar_roster(prompt: str) -> list[dict]:
    """Perplexity sonar-pro: answers from a live web search (unlike a bare LLM)."""
    import requests
    key = os.getenv("PERPLEXITY_API_KEY")
    if not key:
        return []
    r = requests.post("https://api.perplexity.ai/chat/completions", timeout=90,
                      headers={"Authorization": f"Bearer {key}"},
                      json={"model": "sonar-pro", "temperature": 0.1,
                            "messages": [{"role": "user", "content": prompt}]})
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"]
    m = re.search(r"\[.*\]", text, re.S)
    return json.loads(m.group(0)) if m else []


def discover_coverage_roster(ticker: str, company_name: str, months: int = 6) -> list[dict]:
    """
    Which brokerages have published on this company recently? Keyword searches that bundle
    broker names miss small/mid-cap coverage, so ask two search-backed models for the roster
    (Perplexity sonar-pro and Gemini with Google Search grounding) and merge them. Every name
    is only a LEAD: the caller keeps a broker only if a targeted search then finds real
    articles naming it and the company (enrich_news_call).
    """
    client = _get_client()
    from agents.llm_usage import log_gemini_usage
    prompt = (
        f"Using Google Search, list every brokerage or institutional research house (Indian or global) "
        f"that published a research report, initiation, rating or target price on {company_name} "
        f"(NSE: {ticker}) in the last {months} months (today is {datetime.date.today():%d %b %Y}). "
        "Include small brokerages (e.g. Systematix, Antique, DAM Capital, Ventura, Choice, Arihant, "
        "Nirmal Bang, SMIFS, KR Choksey, Monarch, Asian Markets, Dolat, Centrum, Equirus, PhillipCapital, "
        "Sharekhan, Anand Rathi, Elara, Investec) if they covered it. Exclude blogs, tip sites and "
        "price-forecast pages.\nReturn ONLY a JSON array, no prose: "
        '[{"brokerage": "...", "date": "DD Mon YYYY or null", "rating": "Buy/Hold/Sell/... or null", '
        '"target_price": number or null}]. Return [] if none.'
    )
    # flash-lite returned [] without searching in testing; the stronger model is needed here.
    model = "gemini-3.8-flash"

    def _gemini():
        resp = client.models.generate_content(
            model=model, contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2, tools=[types.Tool(google_search=types.GoogleSearch())]),
        )
        log_gemini_usage(resp, model, "analyst.coverage_roster")
        m = re.search(r"\[.*\]", resp.text or "", re.S)
        return json.loads(m.group(0)) if m else []

    roster: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(_sonar_roster, prompt): "sonar-pro", ex.submit(_gemini): "gemini"}
        for f in concurrent.futures.as_completed(futs):
            try:
                for d in f.result() or []:
                    if isinstance(d, dict) and d.get("brokerage"):
                        roster.append({**d, "roster_source": futs[f]})
            except Exception as e:
                _log(f"ANALYST_EXTRACT: coverage roster ({futs[f]}) failed for {ticker}: {str(e)[:160]}")
    _log(f"ANALYST_EXTRACT: coverage roster for {ticker}: {[(d['brokerage'], d['roster_source']) for d in roster]}")
    return roster


def _short_name(company_name: str) -> str:
    return re.sub(r"\s+(Ltd\.?|Limited)$", "", company_name.strip(), flags=re.I).strip()


# =====================================================================
# Full-article enrichment of a news-sourced call
# =====================================================================
# Perplexity's search returns ~900 characters per page whatever is asked for, so the
# rich part of a "Buy, says <broker>" article (CAGRs, valuation basis, drivers, risks)
# never reaches the extractor. For each call we read the full source article, plus other
# outlets covering the same note (the original may be paywalled), and extract with the
# same schema as a broker PDF. Paywalled pages are simply skipped - nothing is bypassed.

_ARTICLE_MAX_CHARS = 9000


def fetch_article_text(url: str, timeout: int = 20) -> str | None:
    """Main text of a public news page (paragraph text), or None if unreadable/paywalled."""
    if not url or not url.startswith("http") or any(h in url for h in ("x.com/", "twitter.com/", "scribd.com/")):
        return None
    try:
        from curl_cffi import requests as cr
        from bs4 import BeautifulSoup
        resp = cr.get(url, impersonate="chrome131", timeout=timeout, allow_redirects=True)
        if resp.status_code != 200 or "html" not in (resp.headers.get("content-type") or ""):
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        root = soup.find("article") or soup
        paras = [p.get_text(" ", strip=True) for p in root.find_all(["p", "li", "h2", "h3"])]
        paras = [p for p in paras if len(p) > 40]
        if not paras and root is not soup:
            paras = [p.get_text(" ", strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 40]
        text = "\n".join(dict.fromkeys(paras))     # de-duplicate repeated blocks, keep order
        return text[:_ARTICLE_MAX_CHARS] if len(text) > 200 else None
    except Exception as e:
        _log(f"ANALYST_EXTRACT: article fetch failed for {url[:80]}: {str(e)[:120]}")
        return None


def _broker_key(name: str) -> str:
    toks = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).split()
    stop = {"capital", "securities", "institutional", "equities", "ltd", "limited", "broking", "india",
            "research", "stock", "financial", "services", "markets"}
    toks = [t for t in toks if t not in stop]
    return toks[0] if toks else (name or "").lower()


# Sites that restate (or generate) "broker targets" without being the press: never used as a
# source. Trusted = established financial press / wires; a coverage LEAD needs one of these.
LOW_QUALITY_DOMAINS = [
    "univest.in", "stocktipsarena.com", "askfuzz.ai", "multibagg.ai", "simplywall.st", "sharpely.in",
    "indmoney.com", "brokeragecalls.com", "greyoakcapital.com", "github.com", "scribd.com", "x.com",
    "twitter.com", "youtube.com", "facebook.com", "reddit.com", "quora.com", "medium.com",
    "stockinvest.us", "walletinvestor.com", "coincodex.com", "tickertape.in", "smallcase.com",
    "groww.in", "5paisa.com", "angelone.in", "dhan.co", "upstox.com", "zerodha.com", "investing.com/pro",
]
TRUSTED_DOMAINS = [d for d in NEWS_DOMAINS if d not in ("trendlyne.com",)] + [
    "informistmedia.com", "moneycontrol.com", "cnbctv18.com", "ndtvprofit.com", "businesstoday.in",
    "economictimes.com", "indiatimes.com", "etnownews.com", "thehindubusinessline.com", "zeebiz.com",
    "financialexpress.com", "business-standard.com", "livemint.com", "reuters.com", "bloomberg.com",
    "tradingview.com", "marketscreener.com", "investmentguruindia.com",
]


def _host(url: str) -> str:
    try:
        return re.sub(r"^www\.", "", url.split("/")[2].lower())
    except IndexError:
        return url.lower()


def is_trusted_domain(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in TRUSTED_DOMAINS)


def enrich_news_call(call: dict, company_name: str, ticker: str, call_perplexity_search_api_fn,
                     max_sources: int = 4) -> tuple[dict | None, list[dict]]:
    """
    Read the full article behind a news-sourced call plus alternate coverage of the same
    note, and extract it with the PDF schema. Returns (extraction or None, sources used).
    """
    broker = call.get("brokerage") or ""
    bkey = _broker_key(broker)
    ckey = _short_name(company_name).split()[0].lower()
    candidates: list[dict] = []
    if call.get("source_url"):
        candidates.append({"url": call["source_url"], "title": "", "snippet": ""})
    try:
        since = datetime.datetime.now() - datetime.timedelta(days=200)
        res = call_perplexity_search_api_fn(
            query=f"{broker} {_short_name(company_name)} target price rating report",
            search_after_date_filter=f"{since.month}/{since.day}/{since.year}",
            max_results=8, max_tokens_per_page=900, timeout=60,
        ) or []
        for r in res:
            blob = (r.get("title", "") + " " + (r.get("snippet") or "")).lower()
            if bkey in blob and ckey in blob:
                candidates.append(r)
    except Exception as e:
        _log(f"ANALYST_EXTRACT: alternate-source search failed for {broker}: {str(e)[:120]}")

    seen, sources, blocks = set(), [], []
    for c in candidates:
        url = (c.get("url") or "").split("#")[0]
        if not url or url in seen:
            continue
        seen.add(url)
        host = _host(url)
        if any(host == d or host.endswith("." + d) for d in LOW_QUALITY_DOMAINS):
            continue                        # blogs / aggregators: can restate or invent calls
        text = fetch_article_text(url)
        if text and bkey not in text.lower():
            text = None                     # page loaded but isn't about this broker's note
        body = text or (c.get("snippet") or "")
        if len(body) < 150:
            continue
        sources.append({"url": url, "site": host, "full_text": bool(text), "trusted": is_trusted_domain(host)})
        blocks.append(f"--- SOURCE: {host} ({'full article' if text else 'search excerpt'})\n{body}")
        if len(sources) >= max_sources:
            break
    if not blocks:
        return None, []

    prompt = ANALYST_ARTICLE_EXTRACTION_PROMPT.format(
        brokerage=broker, company_name=company_name, ticker=ticker,
        report_date=call.get("date") or "unknown date", articles="\n\n".join(blocks),
    )
    try:
        data = gemini_json([types.Part.from_text(text=prompt)], ANALYST_PDF_EXTRACTION_SCHEMA,
                           usage_tag="analyst.article_extract", **step_config("pdf"))
    except Exception as e:
        _log(f"ANALYST_EXTRACT: article extraction failed for {broker}: {str(e)[:160]}")
        return None, sources
    return (data if isinstance(data, dict) else None), sources
