"""
guidance_tracker.py - Management guidance database built from concall transcripts.

A two-stage pipeline:

  STAGE 1 - EXTRACTION. A Gemini Flash-Lite model reads one transcript (with
  page markers) and returns forward-looking statements as JSON in a fixed
  schema: quantitative guidance, revisions, qualitative outlook, commentary and
  risks, plus an explicit "not disclosed" list for the sector's core metrics.

  STAGE 2 - VALIDATION. Nothing the model says is trusted on its own. Each row
  is checked in code against the transcript it came from:
    * the quote is located in the transcript (fuzzy, shingle-voting match), which
      also yields the real page number and the surrounding passage;
    * the speaker at that point in the transcript is identified from the
      "Name:" labels and the moderator's "question comes from the line of X"
      introductions, so an analyst's number can never pass as guidance;
    * every number in value_low/value_high must appear in the quote itself (or
      in an unambiguous phrase such as "mid-teens"); otherwise the number is
      REMOVED - qualitative optimism stays qualitative;
    * each sector core metric with no surviving guidance gets an explicit
      "Not disclosed" row.
  Each stored row keeps the verbatim statement, speaker, page, reporting
  period, model confidence, validator status/flags and a source URL that opens
  the PDF at that page, so a human can confirm any row in one click.

Storage is one row per (company, quarter, metric) in SQLite (the same kagger.db
as auth), deliberately long rather than wide: banks, IT services, manufacturers
and EPC companies guide on entirely different things.
"""

import os
import io
import re
import sys
import csv
import json
import zlib
import time
import asyncio
import threading
import traceback
from bisect import bisect_right
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from agents.prompts.guidance_prompts import (
    GUIDANCE_RESPONSE_SCHEMA, SECTOR_CORE_METRICS, SECTOR_LABELS, build_guidance_prompt,
)

# =====================================================================
# CONFIGURATION
# =====================================================================
# Flash-Lite is the right tier for this: the work is careful reading of one
# document with the answer always present in the text, not open-ended
# reasoning, and it runs across hundreds of transcripts a quarter.
EXTRACTION_MODEL = os.getenv("GUIDANCE_EXTRACTION_MODEL", "gemini-3.5-flash-lite").strip()
EXTRACTION_THINKING = os.getenv("GUIDANCE_EXTRACTION_THINKING", "HIGH").strip()
EXTRACTION_TIMEOUT_MS = int(os.getenv("GUIDANCE_EXTRACTION_TIMEOUT_MS", "300000"))

# A full call is 50k-120k characters; Flash-Lite's context takes all of it.
# The cap only guards against a mis-identified 400-page annual report.
MAX_TRANSCRIPT_CHARS = 250000
MAX_PAGES = 120
# Below this much extractable text the PDF is a scan; the pilot does not OCR.
MIN_TRANSCRIPT_CHARS = 3000

# Quote-match thresholds (fraction of the quote's 3-word shingles found in
# alignment in the transcript).
MATCH_VERIFIED = 0.85
MATCH_PARTIAL = 0.60

DEFAULT_WORKERS = int(os.getenv("GUIDANCE_WORKERS", "4"))

# Validator status, worst last. 'rejected' rows are kept for audit but hidden
# from the default views.
STATUS_ORDER = ['verified', 'needs_review', 'rejected']

# =====================================================================
# PILOT UNIVERSE - Nifty 500 names spread across sectors that guide
# very differently. Used by the "pilot" preset.
# =====================================================================
PILOT_UNIVERSE = {
    # Banks
    'HDFCBANK': ('bank', 'Banks'), 'ICICIBANK': ('bank', 'Banks'),
    'AXISBANK': ('bank', 'Banks'), 'KOTAKBANK': ('bank', 'Banks'),
    'SBIN': ('bank', 'Banks'), 'FEDERALBNK': ('bank', 'Banks'),
    # NBFC / insurance
    'BAJFINANCE': ('nbfc', 'NBFC'), 'CHOLAFIN': ('nbfc', 'NBFC'),
    'SHRIRAMFIN': ('nbfc', 'NBFC'), 'HDFCLIFE': ('insurance', 'Insurance'),
    # IT services
    'TCS': ('it', 'IT Services'), 'INFY': ('it', 'IT Services'),
    'HCLTECH': ('it', 'IT Services'), 'WIPRO': ('it', 'IT Services'),
    'LTIM': ('it', 'IT Services'), 'PERSISTENT': ('it', 'IT Services'),
    'COFORGE': ('it', 'IT Services'),
    # Infrastructure / capital goods / defence
    'LT': ('infra', 'Infrastructure & EPC'), 'KEC': ('infra', 'Infrastructure & EPC'),
    'KPIL': ('infra', 'Infrastructure & EPC'), 'NCC': ('infra', 'Infrastructure & EPC'),
    'BEL': ('infra', 'Defence'), 'HAL': ('infra', 'Defence'),
    # Cement / metals / power
    'ULTRACEMCO': ('general', 'Cement'), 'AMBUJACEM': ('general', 'Cement'),
    'TATASTEEL': ('general', 'Metals'), 'JSWSTEEL': ('general', 'Metals'),
    'NTPC': ('general', 'Power'), 'TATAPOWER': ('general', 'Power'),
    # Auto
    'MARUTI': ('general', 'Automobiles'), 'TVSMOTOR': ('general', 'Automobiles'),
    'M&M': ('general', 'Automobiles'),
    # Pharma / healthcare
    'SUNPHARMA': ('general', 'Pharma'), 'DRREDDY': ('general', 'Pharma'),
    'CIPLA': ('general', 'Pharma'), 'APOLLOHOSP': ('general', 'Healthcare'),
    # Consumer
    'HINDUNILVR': ('general', 'FMCG'), 'NESTLEIND': ('general', 'FMCG'),
    'DABUR': ('general', 'FMCG'), 'TRENT': ('general', 'Retail'),
    'HAVELLS': ('general', 'Consumer Durables'), 'POLYCAB': ('general', 'Cables & Wires'),
    # Chemicals / telecom / realty
    'PIDILITIND': ('general', 'Chemicals'), 'SRF': ('general', 'Chemicals'),
    'BHARTIARTL': ('general', 'Telecom'),
    'DLF': ('realty', 'Real Estate'), 'GODREJPROP': ('realty', 'Real Estate'),
}


def sector_bucket(sector_name: str = '', industry: str = '', company: str = '') -> str:
    """Map a free-text sector/industry to one of the guidance buckets."""
    s = f"{sector_name} {industry} {company}".lower()
    if not s.strip():
        return 'general'
    if 'insurance' in s:
        return 'insurance'
    if re.search(r'\bbank', s) and 'investment bank' not in s:
        return 'bank'
    if re.search(r'nbfc|finance|financial services|housing fin|lending|credit', s):
        return 'nbfc'
    if re.search(r'software|it consult|it services|information tech', s):
        return 'it'
    if re.search(r'real ?estate|realty', s):
        return 'realty'
    if re.search(r'construction|engineering|capital goods|infrastructure|epc|defen[cs]e|aerospace', s):
        return 'infra'
    return 'general'


_MASTER_CACHE = None


def _stock_master():
    """ticker -> (name, sector, industry) from the Trendlyne stock master."""
    global _MASTER_CACHE
    if _MASTER_CACHE is not None:
        return _MASTER_CACHE
    out = {}
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'trendlyne_all_stocks_master.csv')
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for row in csv.DictReader(f):
                t = (row.get('Ticker') or '').strip().upper()
                if t:
                    out[t] = ((row.get('Stock Name') or '').strip(),
                              (row.get('Sector Name') or '').strip(),
                              (row.get('Industry Name') or '').strip())
    except Exception as e:
        print(f"GUIDANCE: could not read stock master: {e}", file=sys.stderr)
    _MASTER_CACHE = out
    return out


# No listed Indian company is worth more than ~Rs 20 lakh crore (2e13 INR); the
# master has a few corrupt caps far above that (Ai Champdany at 3.9e16) which
# would otherwise top the ranking. Anything above this ceiling is bad data.
MAX_SANE_MARKET_CAP = 5e13
MAX_TOP_N = 1000


def top_companies_by_market_cap(n: int):
    """
    The `n` largest companies in the Trendlyne stock master by market cap - a
    close proxy for the Nifty 500 when n = 500 (the index is market-cap ranked,
    with liquidity filters on top).

    The master is kept current by the daily Trendlyne sitemap sync
    (fetchers/stock_master_updater.py); caps come from yfinance via
    enrich_market_caps(), which fills blanks only, so the ranking is a snapshot.

    Returns (tickers, skipped) where skipped lists rows dropped as bad data.
    """
    n = max(1, min(MAX_TOP_N, int(n)))
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'trendlyne_all_stocks_master.csv')
    rows, skipped = [], []
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        for r in csv.DictReader(f):
            t = (r.get('Ticker') or '').strip().upper()
            try:
                cap = float(r.get('MarketCap') or '')
            except ValueError:
                continue
            if not t or cap != cap or cap <= 0:
                continue
            if cap > MAX_SANE_MARKET_CAP:
                skipped.append({'ticker': t, 'name': (r.get('Stock Name') or '').strip(),
                                'reason': f'implausible market cap {cap:.3g}'})
                continue
            rows.append((cap, t, (r.get('Trendlyne ID') or '').strip(),
                         _norm_text(r.get('Stock Name') or '')))
    rows.sort(reverse=True)
    out, seen_ids, seen_names, seen_tickers = [], set(), set(), set()
    for cap, t, tid, name in rows:
        # The master holds a few duplicate listings of one company (same
        # Trendlyne ID, e.g. LTM / LTIM); keep only the larger-cap row.
        if t in seen_tickers or (tid and tid in seen_ids) or (name and name in seen_names):
            skipped.append({'ticker': t, 'name': name, 'reason': 'duplicate listing'})
            continue
        seen_tickers.add(t)
        if tid:
            seen_ids.add(tid)
        if name:
            seen_names.add(name)
        out.append(t)
        if len(out) >= n:
            break
    return out, skipped


def company_profile(ticker: str):
    """(company_name, sector_label, bucket) for a ticker."""
    t = ticker.upper()
    name, sector, industry = _stock_master().get(t, ('', '', ''))
    if t in PILOT_UNIVERSE:
        bucket, label = PILOT_UNIVERSE[t]
        return (name or t), label, bucket
    bucket = sector_bucket(sector, industry, name)
    return (name or t), (sector or industry or SECTOR_LABELS.get(bucket, '')), bucket


# =====================================================================
# QUARTERS
# =====================================================================
_MONTHS = {'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
           'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12}


def quarter_key(quarter: str) -> int:
    """'Sep 2026' -> 202609, sortable. 0 when unparseable."""
    m = re.match(r'^([A-Za-z]{3})\s+(\d{4})$', (quarter or '').strip())
    if not m:
        return 0
    return int(m.group(2)) * 100 + _MONTHS.get(m.group(1).title(), 0)


def quarter_label(quarter: str) -> str:
    """'Sep 2026' -> 'Q2 FY27'."""
    try:
        from agents.concall_agent import _get_q_fy_from_quarter
        return _get_q_fy_from_quarter(quarter)
    except Exception:
        return quarter


# =====================================================================
# DATABASE
# =====================================================================
_DB_INIT_LOCK = threading.Lock()
_DB_READY = False


def _db():
    from auth.database import get_db
    return get_db()


SCHEMA_SQL = [
    '''CREATE TABLE IF NOT EXISTS guidance_transcripts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        company_name TEXT,
        sector TEXT,
        sector_bucket TEXT,
        quarter TEXT NOT NULL,
        quarter_key INTEGER,
        quarter_label TEXT,
        call_date TEXT,
        source_url TEXT,
        source TEXT,
        page_count INTEGER,
        char_count INTEGER,
        pages_z BLOB,
        identity_check TEXT,
        status TEXT,
        error TEXT,
        call_summary TEXT,
        outlook_tone TEXT,
        guidance_headline TEXT,
        model TEXT,
        prompt_tokens INTEGER,
        output_tokens INTEGER,
        extracted_at TEXT,
        UNIQUE(ticker, quarter)
    )''',
    '''CREATE TABLE IF NOT EXISTS guidance_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        transcript_id INTEGER NOT NULL,
        ticker TEXT NOT NULL,
        quarter TEXT NOT NULL,
        quarter_key INTEGER,
        sector_bucket TEXT,
        category TEXT,
        metric TEXT,
        metric_label TEXT,
        metric_family TEXT,
        value_low REAL,
        value_high REAL,
        value_text TEXT,
        unit TEXT,
        target_period TEXT,
        basis TEXT,
        revision TEXT,
        prior_value_text TEXT,
        vs_prior_call TEXT,
        prior_call_value_text TEXT,
        prior_call_quarter TEXT,
        statement TEXT,
        speaker_name TEXT,
        speaker_role TEXT,
        speaker_type TEXT,
        speaker_detected TEXT,
        page INTEGER,
        confidence TEXT,
        sentiment TEXT,
        rationale TEXT,
        model_value_text TEXT,
        match_score REAL,
        verify_status TEXT,
        verify_flags TEXT,
        context TEXT,
        hl_start INTEGER,
        hl_end INTEGER,
        source_ref TEXT,
        created_at TEXT
    )''',
    'CREATE INDEX IF NOT EXISTS ix_gi_transcript ON guidance_items(transcript_id)',
    'CREATE INDEX IF NOT EXISTS ix_gi_family ON guidance_items(metric_family, basis)',
    'CREATE INDEX IF NOT EXISTS ix_gi_ticker ON guidance_items(ticker, quarter_key)',
    # Reviews snapshot the item they judged, so the evaluation history survives
    # a re-extraction that replaces the items.
    '''CREATE TABLE IF NOT EXISTS guidance_reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER,
        transcript_id INTEGER,
        ticker TEXT,
        quarter TEXT,
        model TEXT,
        category TEXT,
        metric TEXT,
        value_text TEXT,
        statement TEXT,
        verify_status TEXT,
        verdict TEXT NOT NULL,
        note TEXT,
        reviewer TEXT,
        created_at TEXT
    )''',
    'CREATE INDEX IF NOT EXISTS ix_gr_item ON guidance_reviews(item_id)',
    # Guidance the model missed - the recall half of the evaluation.
    '''CREATE TABLE IF NOT EXISTS guidance_misses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        transcript_id INTEGER,
        ticker TEXT,
        quarter TEXT,
        model TEXT,
        metric TEXT,
        statement TEXT,
        page INTEGER,
        note TEXT,
        reviewer TEXT,
        created_at TEXT
    )''',
]


def init_guidance_db():
    global _DB_READY
    if _DB_READY:
        return
    with _DB_INIT_LOCK:
        if _DB_READY:
            return
        with _db() as conn:
            for stmt in SCHEMA_SQL:
                conn.execute(stmt)
        _DB_READY = True


def _now():
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')


# =====================================================================
# TRANSCRIPT FETCHING (page-preserving)
# =====================================================================
def _pdf_pages_from_bytes(pdf_bytes: bytes):
    import pdfplumber
    pages = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages[:MAX_PAGES]:
            try:
                pages.append(page.extract_text() or '')
            except Exception:
                pages.append('')
    return pages


async def fetch_transcript_pages(url: str):
    """
    Download a transcript and return its text page by page.

    The existing concall reader flattens the PDF and trims the cover letter,
    which loses page numbers - and the page number is half of what makes a
    guidance row checkable. So this path keeps every page as-is.
    """
    from fetchers.screener_fetcher import _stealth_download_pdf_async
    content, content_type, status = await _stealth_download_pdf_async(url)
    if not content:
        raise RuntimeError(f'download failed (HTTP {status})')
    if content.strip().startswith(b'%PDF'):
        return await asyncio.to_thread(_pdf_pages_from_bytes, content)
    # HTML transcript (some IR sites): one "page".
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(content.decode('utf-8', errors='replace'), 'html.parser')
    for tag in soup(['script', 'style', 'nav', 'header', 'footer']):
        tag.decompose()
    return [soup.get_text('\n', strip=True)]


def _pages_for_model(pages):
    parts, total = [], 0
    for i, text in enumerate(pages, 1):
        block = f"=== PAGE {i} ===\n{text.strip()}\n"
        if total + len(block) > MAX_TRANSCRIPT_CHARS:
            break
        parts.append(block)
        total += len(block)
    return '\n'.join(parts)


def _call_date_from_pages(pages):
    head = ' '.join(pages[:3])
    m = re.search(r'\b((?:January|February|March|April|May|June|July|August|September|'
                  r'October|November|December)\s+\d{1,2},\s+\d{4})\b', head)
    return m.group(1) if m else ''


# =====================================================================
# STAGE 2a - LOCATING A QUOTE IN THE TRANSCRIPT
# =====================================================================
def _norm_text(s: str) -> str:
    out, last_space = [], True
    for ch in s or '':
        if ch.isalnum():
            out.append(ch.lower())
            last_space = False
        elif not last_space:
            out.append(' ')
            last_space = True
    return ''.join(out).strip()


class TranscriptDoc:
    """
    A normalised, token-indexed view of a transcript that maps every match back
    to (page, offset in the original page text).
    """

    def __init__(self, pages):
        self.pages = pages
        chars, idx = [], []
        for pno, text in enumerate(pages, 1):
            for i, ch in enumerate(text):
                if ch.isalnum():
                    chars.append(ch.lower())
                    idx.append((pno, i))
                elif chars and chars[-1] != ' ':
                    chars.append(' ')
                    idx.append((pno, i))
            if chars and chars[-1] != ' ':
                chars.append(' ')
                idx.append((pno, len(text)))
        self.norm = ''.join(chars)
        self.idx = idx
        self.tok, self.tok_start, self.tok_end = [], [], []
        for m in re.finditer(r'\S+', self.norm):
            self.tok.append(m.group())
            self.tok_start.append(m.start())
            self.tok_end.append(m.end())
        self.tri = {}
        for i in range(len(self.tok) - 2):
            self.tri.setdefault((self.tok[i], self.tok[i + 1], self.tok[i + 2]), []).append(i)
        self._labels = None
        self._analysts = None

    # ---- matching ----
    def locate(self, quote: str):
        """
        Best alignment of `quote` in the transcript.

        Returns {score, tok_a, tok_b, page, off_a, off_b} or None. Score is the
        share of the quote's 3-word shingles that land on the same alignment
        (+-3 tokens of drift), so a quote with a word or two changed still
        scores ~0.8, while a paraphrase or an invented quote scores near 0.
        """
        qt = _norm_text(quote).split()
        if len(qt) < 3:
            return None
        n = len(qt) - 2
        # Exact match first: cheap and unambiguous.
        joined = ' '.join(qt)
        pos = self.norm.find(joined)
        if pos >= 0:
            a = bisect_right(self.tok_start, pos) - 1
            return self._span(1.0, a, a + len(qt))
        votes = Counter()
        for j in range(n):
            hits = self.tri.get((qt[j], qt[j + 1], qt[j + 2]))
            if not hits or len(hits) > 60:  # stop-phrase shingles carry no signal
                continue
            for i in hits:
                votes[i - j] += 1
        if not votes:
            return None
        best_align, _ = votes.most_common(1)[0]
        support = sum(votes.get(best_align + d, 0) for d in range(-3, 4))
        score = min(1.0, support / n)
        a = max(0, best_align)
        b = min(len(self.tok), best_align + len(qt))
        if b <= a:
            return None
        return self._span(score, a, b)

    def _span(self, score, a, b):
        pa, oa = self.idx[self.tok_start[a]]
        pb, ob = self.idx[max(self.tok_end[b - 1] - 1, self.tok_start[b - 1])]
        if pb != pa:
            ob = len(self.pages[pa - 1])
        else:
            ob = ob + 1
        return {'score': round(score, 3), 'tok_a': a, 'tok_b': b,
                'page': pa, 'off_a': oa, 'off_b': ob}

    def context(self, span, pad=500):
        text = self.pages[span['page'] - 1]
        s = max(0, span['off_a'] - pad)
        e = min(len(text), span['off_b'] + pad)
        return text[s:e], span['off_a'] - s, span['off_b'] - s

    def window_text(self, span, pad_tokens=60):
        """Normalised text of the match plus surrounding tokens."""
        a = max(0, span['tok_a'] - pad_tokens)
        b = min(len(self.tok), span['tok_b'] + pad_tokens)
        return ' '.join(self.tok[a:b])

    # ---- speakers ----
    _LABEL_RE = re.compile(r"(?m)^\s*([A-Z][A-Za-z.'\- ]{1,48}?)\s*(?:[-–—][^:\n]{0,60})?:")
    _NOT_SPEAKERS = {'cin', 'email', 'e mail', 'website', 'sub', 'subject', 'note', 'tel',
                     'tel no', 'ref no', 'scrip code', 'symbol', 'disclaimer', 'page',
                     'management', 'participants', 'fax', 'phone', 'regd office', 'date'}

    def labels(self):
        """[(page, offset, normalised_name)] in document order."""
        if self._labels is None:
            out = []
            for pno, text in enumerate(self.pages, 1):
                for m in self._LABEL_RE.finditer(text):
                    raw_words = m.group(1).split()
                    # Speaker labels are names: every word capitalised ("P. Ramakrishnan",
                    # "Renu Baid Pugalia"). This keeps "Our outlook for the year:" out.
                    if not raw_words or len(raw_words) > 5 or \
                            not all(w[0].isupper() for w in raw_words):
                        continue
                    name = _norm_text(m.group(1))
                    if not name or name in self._NOT_SPEAKERS:
                        continue
                    out.append((pno, m.start(), name))
            self._labels = out
        return self._labels

    _ANALYST_RE = re.compile(
        r"(?:line of|question (?:is |comes |come )?from|questions? from)\s+"
        r"(?:(?:Mr|Ms|Mrs|Dr)\.?\s+)?"
        r"([A-Z][\w.']*(?:\s+[A-Z][\w.']*){0,3}?)\s+(?:from|with|of|at|representing)\b")

    def analysts(self):
        """Normalised names the moderator introduced as questioners."""
        if self._analysts is None:
            flat = re.sub(r'\s+', ' ', ' '.join(self.pages))
            self._analysts = {_norm_text(m.group(1)) for m in self._ANALYST_RE.finditer(flat)}
            self._analysts.discard('')
        return self._analysts

    def speaker_at(self, page, offset):
        """(label, 'management'|'analyst'|'moderator'|'unknown') speaking at a point."""
        last = None
        for lab in self.labels():
            if (lab[0], lab[1]) <= (page, offset):
                last = lab
            else:
                break
        if not last:
            return '', 'unknown'
        name = last[2]
        if name == 'moderator' or name.startswith('operator'):
            return name, 'moderator'
        if _name_in(name, self.analysts()):
            return name, 'analyst'
        return name, 'management'


def _name_in(name: str, names: set) -> bool:
    toks = set(name.split())
    for other in names:
        ot = set(other.split())
        if toks and ot and (toks <= ot or ot <= toks):
            return True
    return False


# =====================================================================
# STAGE 2b - NUMBER CHECK
# =====================================================================
_NUM_RE = re.compile(r'(?<![A-Za-z\d])\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|(?<![A-Za-z\d])\d+(?:\.\d+)?')

_PHRASE_NUMBERS = [
    (r'high single digit', range(6, 10)),
    (r'mid single digit', range(4, 7)),
    (r'low single digit', range(1, 4)),
    (r'single digit', range(1, 10)),
    (r'double digit', [10]),
    (r'low double digit', range(10, 14)),
    (r'low teens', range(10, 14)),
    (r'mid teens', range(13, 18)),
    (r'high teens', range(16, 20)),
    (r'teens', range(10, 20)),
    (r'low twenties', range(20, 24)),
    (r'mid twenties', range(23, 28)),
    (r'high twenties', range(26, 30)),
    (r'twenties', range(20, 30)),
    (r'double', [2, 100]),
    (r'triple', [3, 200]),
    (r'half', [50, 0.5]),
]
_WORD_NUMBERS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7,
    'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12, 'thirteen': 13,
    'fourteen': 14, 'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18,
    'nineteen': 19, 'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50,
    'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90, 'hundred': 100,
}


# A figure stated in one unit and stored in another is accepted only when the
# quote names the unit that justifies it ("50 bps" -> 0.5 %, "Rs 2,500 crore"
# -> 25 INR billion). A blind powers-of-ten match would let "2,500" vouch for 25.
_UNIT_CONVERSIONS = [
    (r'lakh\s*crore|lac\s*crore|lakh\s*cr', [1e5, 1e3]),      # -> crore, -> billion
    (r'bps|basis\s*points?|bp\b', [0.01]),                     # -> %
    (r'%|per\s*cent|percent', [100]),                          # -> bps
    (r'crores?|cr\b|crs\b', [0.01, 0.00001]),                  # -> billion, -> lakh crore
    (r'trillion|tn\b', [1e3, 1e5]),                            # -> billion, -> crore
    (r'billion|bn\b', [100, 1e3, 1e-3]),                       # -> crore, -> million, -> trillion
    (r'million|mn\b', [0.1, 1e-3]),                            # -> crore, -> billion
]


def numbers_in(text: str):
    """(digit_numbers, phrase_numbers, unit_converted_numbers) mentioned in text."""
    text = text or ''
    digits, converted = set(), set()
    for m in _NUM_RE.finditer(text):
        try:
            v = float(m.group().replace(',', ''))
        except ValueError:
            continue
        digits.add(v)
        tail = text[m.end():m.end() + 16].lower().lstrip()
        for pat, factors in _UNIT_CONVERSIONS:
            if re.match(pat, tail):
                converted.update(v * f for f in factors)
                break
    norm = _norm_text(text)
    phrases = set()
    for pat, vals in _PHRASE_NUMBERS:
        if re.search(r'\b' + pat + r'\b', norm):
            phrases.update(float(v) for v in vals)
    for w, v in _WORD_NUMBERS.items():
        if re.search(r'\b' + w + r'\b', norm):
            phrases.add(float(v))
    return digits, phrases, converted


def _close(a, b):
    return abs(a - b) <= max(1e-6, 1e-4 * max(abs(a), abs(b)))


def _number_support(v, found):
    """'direct' | 'phrase' | 'converted' | None for one value against numbers_in() output."""
    digits, phrases, converted = found
    if any(_close(v, d) for d in digits):
        return 'direct'
    if any(_close(v, p) for p in phrases):
        return 'phrase'
    if any(_close(v, c) for c in converted):
        return 'converted'
    return None


# =====================================================================
# METRIC NORMALISATION
# =====================================================================
_METRIC_ALIASES = {
    'sales_growth': 'revenue_growth', 'topline_growth': 'revenue_growth',
    'top_line_growth': 'revenue_growth', 'cc_revenue_growth': 'revenue_growth',
    'constant_currency_revenue_growth': 'revenue_growth', 'revenue_growth_cc': 'revenue_growth',
    'usd_revenue_growth': 'revenue_growth', 'total_revenue_growth': 'revenue_growth',
    'consolidated_revenue_growth': 'revenue_growth', 'income_growth': 'revenue_growth',
    'sales': 'revenue', 'topline': 'revenue', 'turnover': 'revenue',
    'nim': 'net_interest_margin', 'nims': 'net_interest_margin', 'margin_nim': 'net_interest_margin',
    'credit_costs': 'credit_cost', 'advances_growth': 'loan_growth', 'credit_growth': 'loan_growth',
    'loan_book_growth': 'loan_growth', 'advance_growth': 'loan_growth',
    'deposits_growth': 'deposit_growth', 'aum': 'aum_growth',
    'presales': 'pre_sales', 'sales_bookings': 'pre_sales', 'bookings': 'pre_sales',
    'ebitda_margins': 'ebitda_margin', 'operating_margin': 'ebit_margin',
    'ebit_margins': 'ebit_margin', 'order_inflows': 'order_inflow',
    'capital_expenditure': 'capex', 'capex_guidance': 'capex',
    'roe_guidance': 'roe', 'gross_npa': 'gnpa', 'premium': 'premium_growth',
    'ape_growth': 'premium_growth', 'vnb': 'vnb_margin',
}

_FAMILY_RULES = [
    ('asset_quality', r'credit_cost|npa|slippage|provision|asset_quality|write_off'),
    ('profitability', r'margin|nim|net_interest|ebitda|ebit|pat|profit|eps|earnings|spread|vnb'),
    ('revenue', r'revenue|(?<!pre_)sales(?!_bookings)|top_?line|turnover'),
    ('capex', r'capex|capital_exp|capacity_addition|investment|plant|expansion'),
    ('growth_drivers', r'loan|advance|deposit|aum|order|volume|pre_sales|booking|deal|tcv|'
                       r'premium|(?:^|_)ape(?:_|$)|launch|subscriber|arpu|realisation|realization|'
                       r'market_share|utili[sz]ation|execution|disbursement|capacity'),
    ('returns', r'(?:^|_)(?:roe|roa|roce)(?:_|$)|return_on'),
    ('balance_sheet', r'debt|leverage|working_capital|cash|dividend|payout|cost_of_funds|'
                      r'liquidity|capital_adequacy|(?:^|_)car(?:_|$)|crar'),
    ('costs', r'cost|opex|employee|wage|tax_rate|headcount|attrition'),
]

METRIC_FAMILIES = ['revenue', 'profitability', 'growth_drivers', 'capex', 'asset_quality',
                   'returns', 'balance_sheet', 'costs', 'risk', 'other']


def _snake(s: str) -> str:
    return re.sub(r'_+', '_', re.sub(r'[^a-z0-9]+', '_', (s or '').lower())).strip('_')


def _norm_unit(unit: str) -> str:
    u = (unit or '').strip()
    low = u.lower().replace('.', '')
    if low in ('%', 'percent', 'per cent', 'pct', 'percentage', '% yoy', '% growth'):
        return '%'
    if low in ('bps', 'bp', 'basis points', 'basis point'):
        return 'bps'
    if re.fullmatch(r'(inr|rs|rupees?)?\s*(cr|crs|crore|crores)', low):
        return 'INR crore'
    if re.fullmatch(r'(inr|rs|rupees?)?\s*(bn|billion)', low) and 'usd' not in low:
        return 'INR billion'
    if re.fullmatch(r'(usd|us\$|\$)\s*(mn|million)', low):
        return 'USD million'
    if re.fullmatch(r'(usd|us\$|\$)\s*(bn|billion)', low):
        return 'USD billion'
    return u


def canonical_metric(metric: str, label: str, unit: str, category: str):
    m = _snake(metric) or _snake(label) or 'unspecified'
    m = _METRIC_ALIASES.get(m, m)
    if m == 'revenue' and unit == '%':
        m = 'revenue_growth'
    elif m == 'revenue_growth' and unit and unit not in ('%', 'bps'):
        m = 'revenue'
    if category == 'risk':
        return m, 'risk'
    # The metric key decides; the free-text label is only a fallback, because a
    # label like "NWC to revenue" would otherwise file working capital as revenue.
    for hay in (m, _snake(label)):
        for fam, pat in _FAMILY_RULES:
            if re.search(pat, hay):
                return m, fam
    return m, 'other'


def _norm_period(p: str) -> str:
    return re.sub(r"[\s'`]+", '', (p or '').upper())


# =====================================================================
# STAGE 1 - EXTRACTION MODEL CALL
# =====================================================================
_CLIENT = None
_CLIENT_LOCK = threading.Lock()


def _client():
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            from google import genai
            from google.genai import types
            key = os.getenv("GOOGLE_API_KEY")
            if not key:
                raise RuntimeError('GOOGLE_API_KEY is not set')
            _CLIENT = genai.Client(api_key=key,
                                   http_options=types.HttpOptions(timeout=EXTRACTION_TIMEOUT_MS))
        return _CLIENT


def call_extraction_model(prompt: str, model: str = None):
    """Run the Flash-Lite extraction. Returns (parsed_json, usage_dict)."""
    from google.genai import types
    model = model or EXTRACTION_MODEL
    cfg = {
        'response_mime_type': 'application/json',
        'response_json_schema': GUIDANCE_RESPONSE_SCHEMA,
    }
    if EXTRACTION_THINKING and 'gemini-3' in model:
        cfg['thinking_config'] = types.ThinkingConfig(thinking_level=EXTRACTION_THINKING)
    config = types.GenerateContentConfig(**cfg)

    last_err = None
    for attempt in range(4):
        try:
            resp = _client().models.generate_content(model=model, contents=prompt, config=config)
            try:
                from agents.llm_usage import log_gemini_usage
                log_gemini_usage(resp, model, 'concall.guidance')
            except Exception:
                pass
            text = (resp.text or '').strip()
            data = json.loads(text)
            um = getattr(resp, 'usage_metadata', None)
            usage = {
                'prompt_tokens': getattr(um, 'prompt_token_count', None) if um else None,
                'output_tokens': getattr(um, 'candidates_token_count', None) if um else None,
            }
            return data, usage
        except json.JSONDecodeError as e:
            last_err = RuntimeError(f'model returned invalid JSON: {e}')
        except Exception as e:
            last_err = e
            msg = str(e)
            if not any(code in msg for code in ('429', '500', '503', '504', 'UNAVAILABLE',
                                                'RESOURCE_EXHAUSTED', 'DEADLINE', 'timed out')):
                raise
        wait = 5 * (2 ** attempt)
        print(f"GUIDANCE: extraction attempt {attempt + 1} failed ({str(last_err)[:120]}); "
              f"retrying in {wait}s", file=sys.stderr)
        time.sleep(wait)
    raise last_err


# =====================================================================
# STAGE 2 - VALIDATION
# =====================================================================
def _source_ref(url: str, page) -> str:
    if not url:
        return ''
    base = url.split('#', 1)[0]
    return f"{base}#page={page}" if page else base


def validate_items(raw_items, doc: TranscriptDoc, source_url: str):
    """Check every model item against the transcript. Returns validated dicts."""
    out = []
    for it in raw_items or []:
        if not isinstance(it, dict):
            continue
        flags = []
        status = 'verified'

        def demote(level):
            nonlocal status
            if STATUS_ORDER.index(level) > STATUS_ORDER.index(status):
                status = level

        category = it.get('category') or 'management_commentary'
        unit = _norm_unit(it.get('unit') or '')
        metric, family = canonical_metric(it.get('metric'), it.get('metric_label'), unit, category)
        statement = (it.get('statement') or '').strip()
        vlow, vhigh = it.get('value_low'), it.get('value_high')
        vlow = float(vlow) if isinstance(vlow, (int, float)) else None
        vhigh = float(vhigh) if isinstance(vhigh, (int, float)) else None
        basis = it.get('basis') if it.get('basis') in ('explicit', 'inferred') else 'inferred'
        model_value_text = it.get('value_text') or ''

        # -- 1. Is the quote really in the transcript? --
        span = doc.locate(statement) if statement else None
        score = span['score'] if span else 0.0
        page = span['page'] if span and score >= MATCH_PARTIAL else (it.get('page') or None)
        context, hl_a, hl_b = ('', None, None)
        if span and score >= MATCH_PARTIAL:
            context, hl_a, hl_b = doc.context(span)
        if not statement:
            flags.append('no_statement')
            demote('rejected')
        elif score < MATCH_PARTIAL:
            flags.append('quote_not_found')
            demote('rejected')
        elif score < MATCH_VERIFIED:
            flags.append('quote_paraphrased')
            demote('needs_review')

        # -- 2. Who was speaking there? --
        detected_label, detected_type = '', 'unknown'
        if span and score >= MATCH_PARTIAL:
            detected_label, detected_type = doc.speaker_at(span['page'], span['off_a'])
        model_type = it.get('speaker_type') or 'management'
        if detected_type in ('analyst', 'moderator'):
            flags.append(f'{detected_type}_speaker')
            demote('rejected')
        elif model_type in ('analyst', 'moderator'):
            flags.append(f'model_says_{model_type}')
            demote('rejected')
        elif detected_type == 'unknown' and score >= MATCH_PARTIAL:
            flags.append('speaker_unconfirmed')
            demote('needs_review')

        # -- 3. Are the numbers really management's? --
        values = [v for v in (vlow, vhigh) if v is not None]
        if values:
            q_found = numbers_in(statement)
            supports = [_number_support(v, q_found) for v in values]
            if all(s == 'direct' for s in supports):
                pass
            elif all(s is not None for s in supports):
                if 'phrase' in supports:
                    flags.append('number_from_phrase')
                    basis = 'inferred'
                if 'converted' in supports:
                    flags.append('number_converted')
                    demote('needs_review')
            else:
                # Not in the quote. Look in the passage around it before giving up -
                # the model often quotes one sentence short.
                w_found = (set(), set(), set())
                if span and score >= MATCH_PARTIAL:
                    w_found = numbers_in(doc.window_text(span))
                if all(_number_support(v, w_found) for v in values):
                    flags.append('number_in_context_only')
                    demote('needs_review')
                else:
                    # Never let an unsupported figure stand: qualitative stays qualitative.
                    flags.append('number_removed')
                    vlow = vhigh = None
                    if category in ('quantitative_guidance', 'guidance_revision'):
                        category = 'qualitative_outlook'
                    demote('needs_review')
        elif category == 'quantitative_guidance':
            # Quantitative with no number is a contradiction; file it as outlook.
            category = 'qualitative_outlook'
            flags.append('no_number_recategorised')

        if it.get('confidence') == 'low':
            flags.append('low_model_confidence')
            demote('needs_review')

        value_text = model_value_text
        if 'number_removed' in flags:
            value_text = 'Qualitative (no number stated)'

        out.append({
            'category': category,
            'metric': metric,
            'metric_label': it.get('metric_label') or metric.replace('_', ' '),
            'metric_family': family,
            'value_low': vlow,
            'value_high': vhigh,
            'value_text': value_text,
            'unit': unit if (vlow is not None or vhigh is not None) else '',
            'target_period': it.get('target_period') or '',
            'basis': basis,
            'revision': it.get('revision') or 'none',
            'prior_value_text': it.get('prior_value_text') or '',
            'statement': statement,
            'speaker_name': it.get('speaker_name') or '',
            'speaker_role': it.get('speaker_role') or '',
            'speaker_type': model_type,
            'speaker_detected': f"{detected_label} ({detected_type})" if detected_label else detected_type,
            'page': page,
            'confidence': it.get('confidence') or 'medium',
            'sentiment': it.get('sentiment') or 'neutral',
            'rationale': it.get('rationale') or '',
            'model_value_text': model_value_text,
            'match_score': score,
            'verify_status': status,
            'verify_flags': flags,
            'context': context,
            'hl_start': hl_a,
            'hl_end': hl_b,
            'source_ref': _source_ref(source_url, page),
        })
    return out


def add_not_disclosed(items, model_not_disclosed, bucket):
    """
    Explicit "Not disclosed" rows for every core metric with no surviving
    management guidance (plus any other metric the model said was not
    disclosed). Done in code, not left to the prompt, so a silent call can
    never look like a missed extraction.
    """
    live = [i for i in items if i['verify_status'] != 'rejected' and i['category'] != 'risk']
    covered = {i['metric'] for i in live}
    # A metric the model itself reported as not disclosed ("we don't give annual
    # revenue guidance") keeps that row unless a NUMERIC target survived - a
    # segment-level qualitative remark is not revenue guidance.
    covered_numeric = {i['metric'] for i in live
                       if i['value_low'] is not None or i['value_high'] is not None}
    notes = {}
    for nd in model_not_disclosed or []:
        if isinstance(nd, dict) and nd.get('metric'):
            m, _ = canonical_metric(nd['metric'], '', '', 'quantitative_guidance')
            notes.setdefault(m, (nd.get('note') or '').strip())
    wanted = list(SECTOR_CORE_METRICS.get(bucket) or SECTOR_CORE_METRICS['general'])
    for m in notes:
        if m not in wanted:
            wanted.append(m)
    for m in wanted:
        if m in covered_numeric or (m in covered and m not in notes):
            continue
        metric, family = canonical_metric(m, '', '', 'quantitative_guidance')
        note = notes.get(m) or 'No management guidance found on the call.'
        items.append({
            'category': 'quantitative_guidance', 'metric': metric,
            'metric_label': metric.replace('_', ' '), 'metric_family': family,
            'value_low': None, 'value_high': None, 'value_text': 'Not disclosed',
            'unit': '', 'target_period': '', 'basis': 'not_disclosed', 'revision': 'none',
            'prior_value_text': '', 'statement': '', 'speaker_name': '', 'speaker_role': '',
            'speaker_type': '', 'speaker_detected': '', 'page': None, 'confidence': 'high',
            'sentiment': 'neutral', 'rationale': note, 'model_value_text': '',
            'match_score': None, 'verify_status': 'verified',
            'verify_flags': [] if m in notes else ['auto_not_disclosed'],
            'context': '', 'hl_start': None, 'hl_end': None, 'source_ref': '',
        })
    return items


def _mid(lo, hi):
    vals = [v for v in (lo, hi) if v is not None]
    return sum(vals) / len(vals) if vals else None


def compare_with_prior_call(conn, ticker, qkey, items):
    """Fill vs_prior_call from the same metric+period in the company's previous call."""
    prev = conn.execute(
        'SELECT id, quarter FROM guidance_transcripts WHERE ticker = ? AND quarter_key < ? '
        "AND status = 'ok' ORDER BY quarter_key DESC LIMIT 1", (ticker, qkey)).fetchone()
    if not prev:
        return
    rows = conn.execute(
        'SELECT metric, target_period, unit, value_low, value_high, value_text FROM guidance_items '
        "WHERE transcript_id = ? AND verify_status != 'rejected' AND basis != 'not_disclosed' "
        'AND (value_low IS NOT NULL OR value_high IS NOT NULL)', (prev['id'],)).fetchall()
    by_key = {(r['metric'], _norm_period(r['target_period']), r['unit']): r for r in rows}
    for it in items:
        if it['value_low'] is None and it['value_high'] is None:
            continue
        r = by_key.get((it['metric'], _norm_period(it['target_period']), it['unit']))
        if not r:
            continue
        old, new = _mid(r['value_low'], r['value_high']), _mid(it['value_low'], it['value_high'])
        if old is None or new is None:
            continue
        it['vs_prior_call'] = 'raised' if new > old + 1e-9 else ('lowered' if new < old - 1e-9 else 'maintained')
        it['prior_call_value_text'] = r['value_text']
        it['prior_call_quarter'] = prev['quarter']


# =====================================================================
# PIPELINE - ONE COMPANY
# =====================================================================
async def _exchange_transcripts(ticker, quarter):
    """
    Transcript filings for `quarter` on NSE and BSE -> [{quarter, transcript_url, source}].

    Both are returned, not just the first: one exchange often carries only a
    cover letter ("we have uploaded the transcript on our website") while the
    other has the full PDF. Only an exact quarter match is accepted - these
    fetchers scan recent filings and can surface the previous call.
    """
    from agents.concall_agent import _fetch_nse_transcript_candidate, _fetch_bse_transcript_candidate
    out = []
    for source, fn in (('nse', _fetch_nse_transcript_candidate),
                       ('bse', _fetch_bse_transcript_candidate)):
        try:
            cand = await fn(ticker, quarter)
        except Exception as e:
            print(f"GUIDANCE: {source.upper()} transcript lookup failed for {ticker} {quarter}: {e}",
                  file=sys.stderr)
            continue
        url = cand.get('url')
        if url and cand.get('quarter') == quarter and url not in {c['transcript_url'] for c in out}:
            out.append({'quarter': quarter, 'transcript_url': url, 'source': source})
    return out


_LETTER_HINT_RE = re.compile(r'upload|hosted|available on|placed on|web ?link|website', re.I)
_URL_RE = re.compile(r'https?://[^\s<>()\]\[]+')


def _cover_letter_link(pages):
    """
    The website link in an exchange cover letter, or '' if this is not one.

    Many companies satisfy the transcript rule by filing a one-page letter that
    says the transcript is on their investor site. That letter must not be read
    as a (scanned) transcript, and the link is what an admin needs to know.
    """
    text = '\n'.join(pages)
    if len(text) >= MIN_TRANSCRIPT_CHARS or 'transcript' not in text.lower() \
            or not _LETTER_HINT_RE.search(text):
        return ''
    urls = [u.rstrip('.,;') for u in _URL_RE.findall(text)
            if not re.search(r'nseindia|bseindia|sebi\.gov', u, re.I)]
    urls.sort(key=lambda u: not re.search(r'transcript|investor|concall|conference', u, re.I))
    return urls[0] if urls else 'the company website'


async def _pick_transcript(ticker, quarter=None):
    """
    Transcripts to try, best first -> ([{quarter, transcript_url, source}], checked).

    Screener is the primary source (it reaches back years). NSE and BSE are the
    fallback for the gap Screener leaves around results season, when a call has
    happened and its transcript is filed with the exchanges before Screener
    links it:
      * a specific quarter Screener has no transcript for;
      * in "latest" mode, any quarter newer than Screener's newest transcript -
        the quarter currently being reported, or a newer call Screener lists
        without a transcript. Otherwise "latest" would quietly mean last quarter.
    process_company() walks the list and takes the first document that is a
    real transcript of this company, so a cover-letter filing for the new
    quarter falls back to Screener's newest full transcript.
    `checked` names the sources tried, for the no-transcript message.
    """
    from fetchers.screener_fetcher import fetch_concall_history_async
    from agents.concall_agent import (_date_to_quarter, _normalize_quarter_label,
                                      _expected_results_quarters)
    try:
        history = await fetch_concall_history_async(ticker, date_to_quarter=_date_to_quarter)
    except Exception as e:
        print(f"GUIDANCE: Screener history failed for {ticker}: {e}", file=sys.stderr)
        history = []

    def screener(row):
        return {'quarter': row['quarter'], 'transcript_url': row['transcript_url'], 'source': 'screener'}

    if quarter:
        quarter = _normalize_quarter_label(quarter) or quarter
        row = next((r for r in history if r.get('quarter') == quarter), None)
        if row and row.get('transcript_url'):
            return [screener(row)], 'Screener'
        return await _exchange_transcripts(ticker, quarter), 'Screener, NSE and BSE'

    latest = next((r for r in history if r.get('transcript_url')), None)
    floor = quarter_key(latest['quarter']) if latest else 0
    wanted = set(_expected_results_quarters())
    wanted.update(r['quarter'] for r in history[:2] if r.get('quarter') and not r.get('transcript_url'))
    cands = []
    for q in sorted((q for q in wanted if quarter_key(q) > floor), key=quarter_key, reverse=True):
        cands += await _exchange_transcripts(ticker, q)
    if latest:
        cands.append(screener(latest))
    return cands, 'Screener, NSE and BSE'


def _share_transcript_with_concall_agent(ticker, quarter, chosen, pages, verdict):
    """
    Put this transcript in the Concall Agent's per-quarter transcript cache.

    Its past-quarter tabs, chat and on-demand quarter analysis all read
    transcripts through fetch_past_concall_transcript(), which checks this
    cache (keyed ticker|quarter, so a new call is always a miss) before
    downloading. Sharing it means those features skip the download and read
    the very document the guidance rows came from - including an NSE/BSE
    filing Screener does not link yet.

    The text is trimmed exactly as get_text_from_pdf_url_async() trims it -
    start at the first page naming the moderator (else page 3), at most 75
    pages, capped at the agent's TRANSCRIPT_MAX_CHARS - so a cached transcript
    reads the same as one the agent fetched itself.
    """
    try:
        from agents.concall_agent import _cache_put_transcript, TRANSCRIPT_MAX_CHARS
        start = 2 if len(pages) > 2 else 0
        for i, text in enumerate(pages[:10]):
            if 'moderator' in (text or '').lower():
                start = i
                break
        text = '\n'.join(p for p in pages[start:start + 75] if p)[:TRANSCRIPT_MAX_CHARS]
        _cache_put_transcript(ticker, quarter, {
            'quarter': quarter, 'text': text, 'url': chosen['transcript_url'],
            'source': chosen['source'], 'verified': verdict.get('quarter') == 'confirmed',
        })
    except Exception as e:
        print(f"GUIDANCE: could not share {ticker} {quarter} transcript with the Concall Agent: {e}",
              file=sys.stderr)


def _save_transcript(conn, rec):
    cols = ['ticker', 'company_name', 'sector', 'sector_bucket', 'quarter', 'quarter_key',
            'quarter_label', 'call_date', 'source_url', 'source', 'page_count', 'char_count',
            'pages_z', 'identity_check', 'status', 'error', 'call_summary', 'outlook_tone',
            'guidance_headline', 'model', 'prompt_tokens', 'output_tokens', 'extracted_at']
    vals = [rec.get(c) for c in cols]
    conn.execute(
        f"INSERT INTO guidance_transcripts ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
        f"ON CONFLICT(ticker, quarter) DO UPDATE SET "
        + ', '.join(f"{c} = excluded.{c}" for c in cols if c not in ('ticker', 'quarter')),
        vals)
    return conn.execute('SELECT id FROM guidance_transcripts WHERE ticker = ? AND quarter = ?',
                        (rec['ticker'], rec['quarter'])).fetchone()['id']


def _save_items(conn, tid, ticker, quarter, qkey, bucket, items):
    conn.execute('DELETE FROM guidance_items WHERE transcript_id = ?', (tid,))
    cols = ['transcript_id', 'ticker', 'quarter', 'quarter_key', 'sector_bucket', 'category',
            'metric', 'metric_label', 'metric_family', 'value_low', 'value_high', 'value_text',
            'unit', 'target_period', 'basis', 'revision', 'prior_value_text', 'vs_prior_call',
            'prior_call_value_text', 'prior_call_quarter', 'statement', 'speaker_name',
            'speaker_role', 'speaker_type', 'speaker_detected', 'page', 'confidence', 'sentiment',
            'rationale', 'model_value_text', 'match_score', 'verify_status', 'verify_flags',
            'context', 'hl_start', 'hl_end', 'source_ref', 'created_at']
    now = _now()
    for it in items:
        row = dict(it)
        row.update({'transcript_id': tid, 'ticker': ticker, 'quarter': quarter,
                    'quarter_key': qkey, 'sector_bucket': bucket, 'created_at': now,
                    'verify_flags': json.dumps(it.get('verify_flags') or [])})
        conn.execute(f"INSERT INTO guidance_items ({', '.join(cols)}) "
                     f"VALUES ({', '.join('?' * len(cols))})", [row.get(c) for c in cols])


def process_company(ticker: str, quarter: str = None, force: bool = False, model: str = None,
                    progress=None) -> dict:
    """Fetch -> extract -> validate -> store for one company. Returns a summary dict."""
    init_guidance_db()
    ticker = ticker.strip().upper()
    company, sector, bucket = company_profile(ticker)
    say = progress or (lambda msg: None)

    say(f'{ticker}: finding transcript')
    try:
        cands, checked = asyncio.run(_pick_transcript(ticker, quarter))
    except Exception as e:
        return {'ticker': ticker, 'status': 'error', 'error': f'transcript lookup failed: {e}'}

    def record_failure(fq, status, err):
        # Never downgrade a call that was already extracted successfully.
        with _db() as conn:
            ok = conn.execute("SELECT 1 FROM guidance_transcripts WHERE ticker = ? AND quarter = ? "
                              "AND status = 'ok'", (ticker, fq)).fetchone()
            if not ok:
                _save_transcript(conn, {'ticker': ticker, 'company_name': company, 'sector': sector,
                                        'sector_bucket': bucket, 'quarter': fq,
                                        'quarter_key': quarter_key(fq), 'quarter_label': quarter_label(fq),
                                        'status': status, 'error': err, 'extracted_at': _now(),
                                        'model': model or EXTRACTION_MODEL})
        return {'ticker': ticker, 'quarter': fq, 'status': status, 'error': err}

    if not cands:
        msg = (f'no transcript for {quarter} on {checked}' if quarter
               else f'no transcript found on {checked}')
        if quarter:
            return record_failure(quarter, 'no_transcript', msg)
        return {'ticker': ticker, 'status': 'no_transcript', 'error': msg}

    # Walk the candidates, best first, and take the first document that is a
    # full transcript of this company. Everything skipped is reported.
    from agents.concall_agent import _verify_transcript_identity
    notes, chosen, pages, verdict = [], None, None, {}
    for cand in cands:
        cq, src = cand['quarter'], cand['source'].upper()
        if not force:
            with _db() as conn:
                hit = conn.execute("SELECT id FROM guidance_transcripts WHERE ticker = ? "
                                   "AND quarter = ? AND status = 'ok'", (ticker, cq)).fetchone()
            if hit:
                note = '; '.join(notes + ['already extracted (use force to redo)'])
                return {'ticker': ticker, 'quarter': cq, 'status': 'skipped', 'note': note,
                        'transcript_id': hit['id']}
        say(f'{ticker} {cq}: reading transcript ({src})')
        try:
            cpages = asyncio.run(fetch_transcript_pages(cand['transcript_url']))
        except Exception as e:
            notes.append(f'{src} {cq}: download failed ({e})')
            continue
        chars = sum(len(p) for p in cpages)
        if chars < MIN_TRANSCRIPT_CHARS:
            link = _cover_letter_link(cpages)
            notes.append(f'{src} {cq}: filing is a cover letter; transcript is on {link}' if link
                         else f'{src} {cq}: only {chars} characters of text (scanned PDF?)')
            continue
        try:
            v = _verify_transcript_identity('\n'.join(cpages)[:120000], company, ticker, cq,
                                            allow_model=False)
        except Exception as e:
            v = {'company': 'unconfirmed', 'quarter': 'unconfirmed', 'detail': f'check skipped: {e}'}
        if v.get('quarter') == 'contradicted' or v.get('company') == 'contradicted':
            notes.append(f"{src} {cq}: wrong document ({v.get('detail')})")
            continue
        # Screener's link is curated per company; an exchange search result is
        # not, so it must name this company before it is trusted.
        if cand['source'] != 'screener' and v.get('company') != 'confirmed':
            notes.append(f"{src} {cq}: document does not name {company}")
            continue
        chosen, pages, verdict = cand, cpages, v
        break

    if not chosen:
        letter = next((n for n in notes if 'cover letter' in n), '')
        status = 'transcript_on_website' if letter else 'unreadable'
        return record_failure(cands[0]['quarter'], status, '; '.join(notes))

    q = chosen['quarter']
    qkey = quarter_key(q)
    _share_transcript_with_concall_agent(ticker, q, chosen, pages, verdict)
    chars = sum(len(p) for p in pages)
    rec = {'ticker': ticker, 'company_name': company, 'sector': sector, 'sector_bucket': bucket,
           'quarter': q, 'quarter_key': qkey, 'quarter_label': quarter_label(q),
           'source_url': chosen['transcript_url'], 'source': chosen['source'], 'extracted_at': _now(),
           'model': model or EXTRACTION_MODEL, 'page_count': len(pages), 'char_count': chars,
           'call_date': _call_date_from_pages(pages),
           'pages_z': zlib.compress(json.dumps(pages).encode('utf-8')),
           'identity_check': verdict.get('detail', '')}

    def fail(status, err):
        rec.update({'status': status, 'error': err})
        with _db() as conn:
            _save_transcript(conn, rec)
        return {'ticker': ticker, 'quarter': q, 'status': status, 'error': err,
                'note': '; '.join(notes)}

    say(f'{ticker} {q}: extracting guidance ({len(pages)} pages)')
    prompt = build_guidance_prompt(company, ticker, bucket, quarter_label(q), _pages_for_model(pages))
    try:
        data, usage = call_extraction_model(prompt, model=model)
    except Exception as e:
        traceback.print_exc()
        return fail('error', f'extraction failed: {e}')

    say(f'{ticker} {q}: validating')
    doc = TranscriptDoc(pages)
    items = validate_items(data.get('items'), doc, chosen['transcript_url'])
    items = add_not_disclosed(items, data.get('not_disclosed'), bucket)

    rec.update({'status': 'ok', 'error': None,
                'call_summary': data.get('call_summary') or '',
                'outlook_tone': data.get('outlook_tone') or '',
                'guidance_headline': data.get('guidance_headline') or '',
                'prompt_tokens': usage.get('prompt_tokens'),
                'output_tokens': usage.get('output_tokens')})
    with _db() as conn:
        compare_with_prior_call(conn, ticker, qkey, items)
        tid = _save_transcript(conn, rec)
        _save_items(conn, tid, ticker, q, qkey, bucket, items)

    st = Counter(i['verify_status'] for i in items)
    flags = Counter(f for i in items for f in i['verify_flags'])
    return {'ticker': ticker, 'quarter': q, 'status': 'ok', 'transcript_id': tid, 'source': chosen['source'],
            'note': '; '.join(notes),
            'items': len(items), 'verified': st.get('verified', 0),
            'needs_review': st.get('needs_review', 0), 'rejected': st.get('rejected', 0),
            'numbers_removed': flags.get('number_removed', 0),
            'quotes_not_found': flags.get('quote_not_found', 0),
            'analyst_rows_blocked': flags.get('analyst_speaker', 0) + flags.get('model_says_analyst', 0)}


# =====================================================================
# BATCH RUNNER
# =====================================================================
def run_batch(tickers, quarter=None, force=False, workers=DEFAULT_WORKERS, model=None,
              on_progress=None):
    """Process many companies in parallel. Returns the per-company summaries."""
    init_guidance_db()
    tickers = [t.strip().upper() for t in tickers if t and t.strip()]
    results, lock = [], threading.Lock()
    state = {'done': 0, 'total': len(tickers), 'current': ''}

    def report(msg):
        state['current'] = msg
        if on_progress:
            on_progress(dict(state), list(results))

    def one(t):
        try:
            res = process_company(t, quarter=quarter, force=force, model=model, progress=report)
        except Exception as e:
            traceback.print_exc()
            res = {'ticker': t, 'status': 'error', 'error': str(e)}
        with lock:
            results.append(res)
            state['done'] += 1
        print(f"GUIDANCE: [{state['done']}/{state['total']}] {t}: {res.get('status')} "
              f"{res.get('error') or ''}", file=sys.stderr)
        report(f"{t}: {res.get('status')}")
        return res

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        list(pool.map(one, tickers))
    return results


def _start_batch_job(tickers, quarter, force, workers, model):
    from agents.base import create_agent_job, update_agent_job
    job_id = create_agent_job('concall_guidance', 'BATCH', {'tickers': tickers, 'quarter': quarter})

    def progress(state, results):
        update_agent_job(job_id, {'progress': f"{state['done']}/{state['total']} - {state['current']}",
                                  'result': {'state': state, 'results': results}})

    def run():
        try:
            results = run_batch(tickers, quarter=quarter, force=force, workers=workers,
                                model=model, on_progress=progress)
            update_agent_job(job_id, {'status': 'complete',
                                      'progress': f'{len(results)}/{len(tickers)} done',
                                      'result': {'state': {'done': len(results), 'total': len(tickers),
                                                           'current': ''}, 'results': results}})
        except Exception as e:
            traceback.print_exc()
            update_agent_job(job_id, {'status': 'error', 'error': str(e)})

    threading.Thread(target=run, daemon=True).start()
    return job_id


# =====================================================================
# QUERIES
# =====================================================================
_ITEM_PUBLIC_COLS = (
    'i.id, i.transcript_id, i.ticker, i.quarter, i.quarter_key, i.sector_bucket, i.category, '
    'i.metric, i.metric_label, i.metric_family, i.value_low, i.value_high, i.value_text, i.unit, '
    'i.target_period, i.basis, i.revision, i.prior_value_text, i.vs_prior_call, '
    'i.prior_call_value_text, i.prior_call_quarter, i.statement, i.speaker_name, i.speaker_role, '
    'i.speaker_type, i.speaker_detected, i.page, i.confidence, i.sentiment, i.rationale, '
    'i.model_value_text, i.match_score, i.verify_status, i.verify_flags, i.context, i.hl_start, '
    'i.hl_end, i.source_ref, t.company_name, t.sector, t.quarter_label, t.source_url, t.model, '
    '(SELECT verdict FROM guidance_reviews r WHERE r.item_id = i.id ORDER BY r.id DESC LIMIT 1) AS review'
)

_LATEST_JOIN = (
    "JOIN (SELECT ticker AS lt, MAX(quarter_key) AS lq FROM guidance_transcripts "
    "WHERE status = 'ok' GROUP BY ticker) latest ON latest.lt = t.ticker AND latest.lq = t.quarter_key"
)


def _row_item(r):
    d = dict(r)
    try:
        d['verify_flags'] = json.loads(d.get('verify_flags') or '[]')
    except Exception:
        d['verify_flags'] = []
    return d


def query_items(args) -> list:
    init_guidance_db()
    where, params = ["t.status = 'ok'"], []
    join = ''
    quarter = (args.get('quarter') or 'latest').strip()
    if quarter == 'latest':
        join = _LATEST_JOIN
    elif quarter != 'all':
        where.append('i.quarter = ?')
        params.append(quarter)

    family = (args.get('family') or '').strip()
    if family and family != 'all':
        where.append('i.metric_family = ?')
        params.append(family)
    if args.get('metric'):
        where.append('i.metric = ?')
        params.append(args['metric'].strip())
    cats = [c for c in (args.get('category') or '').split(',') if c]
    if cats:
        where.append(f"i.category IN ({', '.join('?' * len(cats))})")
        params.extend(cats)
    basis = (args.get('basis') or '').strip()
    if basis == 'guided':
        where.append("i.basis IN ('explicit', 'inferred')")
    elif basis in ('explicit', 'inferred', 'not_disclosed'):
        where.append('i.basis = ?')
        params.append(basis)
    if args.get('revision'):
        rv = args['revision'].strip()
        where.append('(i.revision = ? OR i.vs_prior_call = ?)')
        params.extend([rv, rv])
    if args.get('sector'):
        where.append('(i.sector_bucket = ? OR t.sector = ?)')
        params.extend([args['sector'], args['sector']])
    if args.get('unit'):
        where.append('i.unit = ?')
        params.append(args['unit'])
    if args.get('sentiment'):
        where.append('i.sentiment = ?')
        params.append(args['sentiment'])
    for key, expr, op in (('min_value', 'COALESCE(i.value_high, i.value_low)', '>='),
                          ('max_value', 'COALESCE(i.value_low, i.value_high)', '<=')):
        raw = (args.get(key) or '').strip()
        if raw:
            try:
                params.append(float(raw))
                where.append(f'{expr} {op} ?')
            except ValueError:
                pass
    if args.get('has_number') in ('1', 'true'):
        where.append('(i.value_low IS NOT NULL OR i.value_high IS NOT NULL)')
    status = (args.get('status') or 'not_rejected').strip()
    if status == 'verified':
        where.append("i.verify_status = 'verified'")
    elif status == 'not_rejected':
        where.append("i.verify_status != 'rejected'")
    elif status in ('needs_review', 'rejected'):
        where.append('i.verify_status = ?')
        params.append(status)
    if args.get('review') == 'unreviewed':
        where.append('NOT EXISTS (SELECT 1 FROM guidance_reviews r WHERE r.item_id = i.id)')
    q = (args.get('q') or '').strip()
    if q:
        where.append('(i.ticker LIKE ? OR t.company_name LIKE ? OR i.statement LIKE ? OR i.metric_label LIKE ?)')
        params.extend([f'%{q}%'] * 4)
    if args.get('ticker'):
        where.append('i.ticker = ?')
        params.append(args['ticker'].strip().upper())

    sort = args.get('sort') or 'company'
    order = {
        'value_desc': 'COALESCE(i.value_high, i.value_low) IS NULL, COALESCE(i.value_high, i.value_low) DESC',
        'value_asc': 'COALESCE(i.value_low, i.value_high) IS NULL, COALESCE(i.value_low, i.value_high) ASC',
        'quarter': 'i.quarter_key DESC, t.company_name',
    }.get(sort, "t.company_name, i.quarter_key DESC, "
                "CASE i.basis WHEN 'not_disclosed' THEN 1 ELSE 0 END, i.metric_family, i.metric")
    try:
        limit = max(1, min(2000, int(args.get('limit') or 500)))
    except ValueError:
        limit = 500
    sql = (f"SELECT {_ITEM_PUBLIC_COLS} FROM guidance_items i "
           f"JOIN guidance_transcripts t ON t.id = i.transcript_id {join} "
           f"WHERE {' AND '.join(where)} ORDER BY {order} LIMIT {limit}")
    with _db() as conn:
        return [_row_item(r) for r in conn.execute(sql, params).fetchall()]


def query_summaries(args) -> list:
    """One card per call: summary, tone, headline and the revenue guidance row."""
    init_guidance_db()
    where, params, join = [], [], ''
    quarter = (args.get('quarter') or 'latest').strip()
    if quarter == 'latest':
        join = _LATEST_JOIN
    elif quarter != 'all':
        where.append('t.quarter = ?')
        params.append(quarter)
    if args.get('sector'):
        where.append('(t.sector_bucket = ? OR t.sector = ?)')
        params.extend([args['sector'], args['sector']])
    if args.get('tone'):
        where.append('t.outlook_tone = ?')
        params.append(args['tone'])
    q = (args.get('q') or '').strip()
    if q:
        where.append('(t.ticker LIKE ? OR t.company_name LIKE ?)')
        params.extend([f'%{q}%'] * 2)
    if args.get('include_failed') not in ('1', 'true'):
        where.append("t.status = 'ok'")
    sql = ("SELECT t.id, t.ticker, t.company_name, t.sector, t.sector_bucket, t.quarter, "
           "t.quarter_label, t.call_date, t.source_url, t.source, t.page_count, t.status, t.error, "
           "t.call_summary, t.outlook_tone, t.guidance_headline, t.model, t.extracted_at "
           f"FROM guidance_transcripts t {join} "
           + (f"WHERE {' AND '.join(where)} " if where else '')
           + "ORDER BY t.company_name")
    with _db() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for r in rows:
            items = conn.execute(
                "SELECT id, category, metric, metric_label, metric_family, value_low, value_high, "
                "value_text, unit, target_period, basis, verify_status, page, source_ref, statement, "
                "revision, vs_prior_call FROM guidance_items WHERE transcript_id = ? "
                "AND verify_status != 'rejected'", (r['id'],)).fetchall()
            items = [dict(i) for i in items]
            r['counts'] = dict(Counter(i['category'] for i in items if i['basis'] != 'not_disclosed'))
            r['not_disclosed'] = [i['metric_label'] for i in items if i['basis'] == 'not_disclosed']
            rev = [i for i in items if i['metric_family'] == 'revenue']
            # Headline pick: a numeric target, else the explicit "not disclosed",
            # else a qualitative remark (often segment-level, so the weakest signal).
            rev.sort(key=lambda i: (0 if (i['value_low'] is not None or i['value_high'] is not None)
                                    else (1 if i['basis'] == 'not_disclosed' else 2),
                                    i['verify_status'] != 'verified'))
            r['revenue_guidance'] = rev[0] if rev else None
            r['key_guidance'] = [i for i in items if i['category'] in
                                 ('quantitative_guidance', 'guidance_revision')
                                 and i['basis'] != 'not_disclosed'][:6]
            r['risks'] = [i for i in items if i['category'] == 'risk'][:4]
    return rows


def recent_quarter_options(stored=None, count=8, today=None):
    """
    Quarter filter choices, newest first: the last `count` completed fiscal
    quarters plus any older quarter that has stored calls. Values are Screener's
    quarter-end labels ('Sep 2026'); labels are fiscal ('Q2 FY27').
    """
    today = today or datetime.now()
    # Most recent quarter that has ENDED (calls for it are being held now).
    month = today.month - (today.month % 3) if today.month % 3 else today.month - 3
    year = today.year
    if month <= 0:
        month += 12
        year -= 1
    names = {3: 'Mar', 6: 'Jun', 9: 'Sep', 12: 'Dec'}
    qs = set()
    for _ in range(count):
        qs.add(f"{names[month]} {year}")
        month -= 3
        if month <= 0:
            month += 12
            year -= 1
    for q in stored or []:
        if q.get('quarter'):
            qs.add(q['quarter'])
    return [{'quarter': q, 'quarter_label': quarter_label(q)}
            for q in sorted(qs, key=quarter_key, reverse=True) if quarter_key(q)]


def query_meta() -> dict:
    init_guidance_db()
    with _db() as conn:
        quarters = [dict(r) for r in conn.execute(
            "SELECT quarter, quarter_label, COUNT(*) AS n FROM guidance_transcripts "
            "WHERE status = 'ok' GROUP BY quarter ORDER BY MAX(quarter_key) DESC").fetchall()]
        sectors = [dict(r) for r in conn.execute(
            "SELECT sector, sector_bucket, COUNT(*) AS n FROM guidance_transcripts "
            "WHERE status = 'ok' GROUP BY sector ORDER BY n DESC").fetchall()]
        units = [r['unit'] for r in conn.execute(
            "SELECT unit, COUNT(*) AS n FROM guidance_items WHERE unit != '' "
            "GROUP BY unit ORDER BY n DESC LIMIT 15").fetchall()]
        metrics = [dict(r) for r in conn.execute(
            "SELECT metric, metric_family, COUNT(*) AS n FROM guidance_items "
            "WHERE category != 'risk' GROUP BY metric ORDER BY n DESC LIMIT 80").fetchall()]
        status = [dict(r) for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM guidance_transcripts GROUP BY status").fetchall()]
    return {'quarters': quarters, 'quarter_options': recent_quarter_options(quarters),
            'sectors': sectors, 'units': units, 'metrics': metrics,
            'families': METRIC_FAMILIES, 'transcript_status': status,
            'model': EXTRACTION_MODEL, 'pilot_size': len(PILOT_UNIVERSE),
            'sector_labels': SECTOR_LABELS}


def transcript_detail(tid: int, with_pages=False):
    init_guidance_db()
    with _db() as conn:
        t = conn.execute('SELECT * FROM guidance_transcripts WHERE id = ?', (tid,)).fetchone()
        if not t:
            return None
        t = dict(t)
        pages_z = t.pop('pages_z', None)
        if with_pages and pages_z:
            t['pages'] = json.loads(zlib.decompress(pages_z).decode('utf-8'))
        t['misses'] = [dict(r) for r in conn.execute(
            'SELECT * FROM guidance_misses WHERE transcript_id = ? ORDER BY id', (tid,)).fetchall()]
    t['items'] = query_items({'ticker': t['ticker'], 'quarter': t['quarter'], 'status': 'all',
                              'limit': '2000'})
    return t


def transcript_page(tid: int, page: int):
    init_guidance_db()
    with _db() as conn:
        r = conn.execute('SELECT pages_z, source_url FROM guidance_transcripts WHERE id = ?',
                         (tid,)).fetchone()
    if not r or not r['pages_z']:
        return None
    pages = json.loads(zlib.decompress(r['pages_z']).decode('utf-8'))
    if page < 1 or page > len(pages):
        return None
    return {'page': page, 'page_count': len(pages), 'text': pages[page - 1],
            'source_ref': _source_ref(r['source_url'], page)}


REVIEW_VERDICTS = {
    'correct': 'Correct',
    'wrong_value': 'Wrong value / unit / period',
    'invented': 'Invented figure',
    'not_guidance': 'Not guidance (results or analyst)',
    'wrong_speaker': 'Wrong speaker',
    'wrong_not_disclosed': 'Marked not disclosed but was guided',
}


def save_review(item_id: int, verdict: str, note: str, reviewer: str):
    init_guidance_db()
    if verdict not in REVIEW_VERDICTS:
        raise ValueError('unknown verdict')
    with _db() as conn:
        r = conn.execute('SELECT i.*, t.model FROM guidance_items i JOIN guidance_transcripts t '
                         'ON t.id = i.transcript_id WHERE i.id = ?', (item_id,)).fetchone()
        if not r:
            raise LookupError('item not found')
        conn.execute(
            'INSERT INTO guidance_reviews (item_id, transcript_id, ticker, quarter, model, category, '
            'metric, value_text, statement, verify_status, verdict, note, reviewer, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (item_id, r['transcript_id'], r['ticker'], r['quarter'], r['model'], r['category'],
             r['metric'], r['value_text'], r['statement'], r['verify_status'], verdict,
             (note or '')[:2000], reviewer, _now()))


def save_miss(tid: int, metric: str, statement: str, page, note: str, reviewer: str):
    init_guidance_db()
    with _db() as conn:
        t = conn.execute('SELECT ticker, quarter, model FROM guidance_transcripts WHERE id = ?',
                         (tid,)).fetchone()
        if not t:
            raise LookupError('transcript not found')
        conn.execute(
            'INSERT INTO guidance_misses (transcript_id, ticker, quarter, model, metric, statement, '
            'page, note, reviewer, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (tid, t['ticker'], t['quarter'], t['model'], (metric or '')[:80],
             (statement or '')[:4000], page, (note or '')[:2000], reviewer, _now()))


def evaluation_stats() -> dict:
    """
    Pilot accuracy. Precision and invented-figure rate come from reviewer
    verdicts on the latest review of each item; recall is approximated from the
    guidance reviewers report as missed.
    """
    init_guidance_db()
    with _db() as conn:
        latest = conn.execute(
            'SELECT r.* FROM guidance_reviews r JOIN (SELECT item_id, MAX(id) AS mid FROM '
            'guidance_reviews GROUP BY item_id) x ON x.mid = r.id').fetchall()
        misses = conn.execute('SELECT COUNT(*) AS n FROM guidance_misses').fetchone()['n']
        auto = conn.execute(
            "SELECT verify_status, COUNT(*) AS n FROM guidance_items GROUP BY verify_status").fetchall()
        flags = Counter()
        for r in conn.execute('SELECT verify_flags FROM guidance_items').fetchall():
            for f in json.loads(r['verify_flags'] or '[]'):
                flags[f] += 1
        tstat = conn.execute('SELECT status, COUNT(*) AS n FROM guidance_transcripts '
                             'GROUP BY status').fetchall()
        reviewed_calls = conn.execute(
            'SELECT COUNT(DISTINCT transcript_id) AS n FROM guidance_reviews').fetchone()['n']
        by_sector = conn.execute(
            "SELECT t.sector_bucket AS bucket, r.verdict, COUNT(*) AS n FROM guidance_reviews r "
            "JOIN (SELECT item_id, MAX(id) AS mid FROM guidance_reviews GROUP BY item_id) x "
            "ON x.mid = r.id JOIN guidance_transcripts t ON t.id = r.transcript_id "
            "GROUP BY t.sector_bucket, r.verdict").fetchall()
        tokens = conn.execute("SELECT SUM(prompt_tokens) AS p, SUM(output_tokens) AS o, COUNT(*) AS n "
                              "FROM guidance_transcripts WHERE status = 'ok'").fetchone()

    verdicts = Counter(r['verdict'] for r in latest)
    numeric = [r for r in latest if r['category'] in ('quantitative_guidance', 'guidance_revision')
               and r['value_text'] not in ('Not disclosed', '')]
    total = len(latest)
    correct = verdicts.get('correct', 0)
    sector = {}
    for r in by_sector:
        s = sector.setdefault(r['bucket'] or 'general', {'reviewed': 0, 'correct': 0})
        s['reviewed'] += r['n']
        if r['verdict'] == 'correct':
            s['correct'] += r['n']
    for s in sector.values():
        s['precision'] = round(s['correct'] / s['reviewed'], 3) if s['reviewed'] else None

    def pct(a, b):
        return round(a / b, 3) if b else None

    return {
        'reviewed_items': total,
        'reviewed_calls': reviewed_calls,
        'verdicts': dict(verdicts),
        'precision': pct(correct, total),
        'invented_rate_numeric': pct(sum(1 for r in numeric if r['verdict'] == 'invented'), len(numeric)),
        'not_guidance_rate': pct(verdicts.get('not_guidance', 0) + verdicts.get('wrong_speaker', 0), total),
        'missed_reported': misses,
        'recall_estimate': pct(correct, correct + misses),
        'by_sector': sector,
        'auto_status': {r['verify_status']: r['n'] for r in auto},
        'auto_flags': dict(flags.most_common()),
        'transcripts': {r['status']: r['n'] for r in tstat},
        'tokens': {'prompt': tokens['p'], 'output': tokens['o'], 'calls': tokens['n']},
        'verdict_labels': REVIEW_VERDICTS,
    }


def export_rows():
    """Every item with its latest review, for offline spreadsheet evaluation."""
    init_guidance_db()
    sql = (f"SELECT {_ITEM_PUBLIC_COLS}, "
           "(SELECT note FROM guidance_reviews r WHERE r.item_id = i.id ORDER BY r.id DESC LIMIT 1) "
           "AS review_note FROM guidance_items i JOIN guidance_transcripts t ON t.id = i.transcript_id "
           "ORDER BY t.company_name, i.quarter_key DESC, i.metric_family, i.metric")
    with _db() as conn:
        return [_row_item(r) for r in conn.execute(sql).fetchall()]


EXPORT_COLUMNS = ['id', 'ticker', 'company_name', 'sector', 'quarter', 'quarter_label', 'category',
                  'metric', 'metric_label', 'metric_family', 'value_low', 'value_high', 'value_text',
                  'unit', 'target_period', 'basis', 'revision', 'vs_prior_call', 'statement',
                  'speaker_name', 'speaker_role', 'speaker_detected', 'page', 'confidence',
                  'match_score', 'verify_status', 'verify_flags', 'model_value_text', 'rationale',
                  'source_ref', 'model', 'review', 'review_note']


def write_export_csv(fh):
    w = csv.writer(fh)
    w.writerow(EXPORT_COLUMNS)
    for r in export_rows():
        r['verify_flags'] = ';'.join(r.get('verify_flags') or [])
        w.writerow([r.get(c, '') if r.get(c) is not None else '' for c in EXPORT_COLUMNS])


# =====================================================================
# ROUTES
# =====================================================================
def register_guidance_routes(app):
    from flask import request, jsonify, Response
    from auth.routes import login_required, admin_required
    from agents.base import get_agent_job

    try:
        init_guidance_db()
    except Exception as e:
        print(f"GUIDANCE: database init failed: {e}", file=sys.stderr)

    def _reviewer():
        u = getattr(request, 'current_user', None)
        return getattr(u, 'email', None) or str(getattr(u, 'id', '') or '')

    @app.route('/api/concall-guidance/meta', methods=['GET'])
    @login_required
    def guidance_meta():
        return jsonify(query_meta())

    @app.route('/api/concall-guidance/summaries', methods=['GET'])
    @login_required
    def guidance_summaries():
        return jsonify({'calls': query_summaries(request.args)})

    @app.route('/api/concall-guidance/items', methods=['GET'])
    @login_required
    def guidance_items():
        return jsonify({'items': query_items(request.args)})

    @app.route('/api/concall-guidance/transcript/<int:tid>', methods=['GET'])
    @login_required
    def guidance_transcript(tid):
        t = transcript_detail(tid)
        return (jsonify(t) if t else (jsonify({'error': 'not found'}), 404))

    @app.route('/api/concall-guidance/transcript/<int:tid>/page/<int:page>', methods=['GET'])
    @login_required
    def guidance_transcript_page(tid, page):
        p = transcript_page(tid, page)
        return (jsonify(p) if p else (jsonify({'error': 'not found'}), 404))

    @app.route('/api/concall-guidance/items/<int:item_id>/review', methods=['POST'])
    @login_required
    def guidance_review(item_id):
        data = request.get_json(force=True) or {}
        try:
            save_review(item_id, data.get('verdict', ''), data.get('note', ''), _reviewer())
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except LookupError as e:
            return jsonify({'error': str(e)}), 404
        return jsonify({'ok': True})

    @app.route('/api/concall-guidance/transcript/<int:tid>/missed', methods=['POST'])
    @login_required
    def guidance_missed(tid):
        data = request.get_json(force=True) or {}
        if not (data.get('statement') or data.get('note')):
            return jsonify({'error': 'describe the missed guidance'}), 400
        page = data.get('page')
        try:
            page = int(page) if page not in (None, '') else None
        except (TypeError, ValueError):
            page = None
        try:
            save_miss(tid, data.get('metric', ''), data.get('statement', ''), page,
                      data.get('note', ''), _reviewer())
        except LookupError as e:
            return jsonify({'error': str(e)}), 404
        return jsonify({'ok': True})

    @app.route('/api/concall-guidance/eval', methods=['GET'])
    @login_required
    def guidance_eval():
        return jsonify(evaluation_stats())

    @app.route('/api/concall-guidance/export.csv', methods=['GET'])
    @login_required
    def guidance_export():
        buf = io.StringIO()
        write_export_csv(buf)
        return Response(buf.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': 'attachment; filename=concall_guidance.csv'})

    @app.route('/api/concall-guidance/run', methods=['POST'])
    @admin_required
    def guidance_run():
        data = request.get_json(force=True) or {}
        mode = (data.get('mode') or '').strip()
        skipped = []
        if mode == 'top':
            try:
                top_n = int(data.get('top_n'))
            except (TypeError, ValueError):
                return jsonify({'error': 'enter how many companies to run (a whole number)'}), 400
            if not 1 <= top_n <= MAX_TOP_N:
                return jsonify({'error': f'number of companies must be between 1 and {MAX_TOP_N}'}), 400
            try:
                tickers, skipped = top_companies_by_market_cap(top_n)
            except Exception as e:
                return jsonify({'error': f'could not read the stock master: {e}'}), 500
            source = f'top {len(tickers)} by market cap'
        elif mode == 'tickers':
            tickers = data.get('tickers') or []
            if isinstance(tickers, str):
                tickers = re.split(r'[\s,]+', tickers)
            tickers = [t.strip().upper() for t in tickers if t and t.strip()]
            if not tickers:
                return jsonify({'error': 'enter at least one ticker'}), 400
            source = 'tickers'
        else:
            tickers = list(PILOT_UNIVERSE)
            source = 'pilot set'
        tickers = list(dict.fromkeys(tickers))[:MAX_TOP_N]
        job_id = _start_batch_job(tickers, (data.get('quarter') or '').strip() or None,
                                  bool(data.get('force')),
                                  data.get('workers') or DEFAULT_WORKERS,
                                  (data.get('model') or '').strip() or None)
        print(f"GUIDANCE: run {job_id} started by {_reviewer()} - {source}, "
              f"{len(tickers)} companies", file=sys.stderr)
        return jsonify({'job_id': job_id, 'tickers': len(tickers), 'source': source,
                        'skipped': skipped[:20]})

    @app.route('/api/concall-guidance/top-preview', methods=['GET'])
    @admin_required
    def guidance_top_preview():
        try:
            n = int(request.args.get('n') or 0)
        except ValueError:
            n = 0
        if not 1 <= n <= MAX_TOP_N:
            return jsonify({'error': f'between 1 and {MAX_TOP_N}'}), 400
        tickers, skipped = top_companies_by_market_cap(n)
        master = _stock_master()
        return jsonify({'count': len(tickers),
                        'first': [{'ticker': t, 'name': master.get(t, ('',))[0]} for t in tickers[:5]],
                        'last': [{'ticker': t, 'name': master.get(t, ('',))[0]} for t in tickers[-3:]],
                        'skipped': skipped[:20]})

    @app.route('/api/concall-guidance/run/<job_id>', methods=['GET'])
    @admin_required
    def guidance_run_status(job_id):
        job = get_agent_job(job_id)
        if not job:
            return jsonify({'error': 'job not found (it may have finished before a restart)'}), 404
        return jsonify({'status': job['status'], 'progress': job.get('progress'),
                        'error': job.get('error'), 'result': job.get('result')})

    print("GUIDANCE: routes registered (/api/concall-guidance/*)", file=sys.stderr)
