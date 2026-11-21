"""
document_summarizer_agent.py - Multi-document synthesis agent ("Doc Summarizer").

Takes 1-5 documents (typically several analyst reports on one company) and produces ONE
consolidated report that is better than reading them separately.

Regulatory disclosure pages (analyst certification, SEBI registration, disclaimers) are dropped on
read: they are ~40% of a typical broker note and carry nothing the report uses.

Two paths, chosen by total cleaned text size:

  Direct (the common case, <= DIRECT_MAX_CHARS, e.g. five broker notes):
      the full text of every document goes straight into ONE synthesis call (gpt-5.4). Small
      metadata calls (author, date, rating, target - for the source list) run concurrently, so
      they add no wall time. Skipping a separate extraction stage is both faster and loses less.

  Extract-then-synthesise (large inputs, e.g. annual reports):
      Stage 1 reduces each document to structured notes in parallel (gpt-5.4-mini, low effort);
      Stage 2 synthesises from the notes.

Synthesis merges everything into a MECE topic structure (organised by topic, never document-by-
document), fills gaps in one document from another, classifies contradictions (timing / definition
/ factual / forecast) and resolves them where the evidence allows, and reports forecast dispersion
as a consensus with ranges. Every sourced statement carries a [D#] citation that the frontend links
to the source list.

The work runs as a background agent job (agents/base.py); the long calls use OpenAI background mode
so no socket sits idle on Azure.
"""

import io
import os
import re
import sys
import json
import time
import datetime
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

import pdfplumber

try:
    import docx
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

from flask import request, jsonify

from agents.base import create_agent_job, update_agent_job, get_agent_job


MAX_DOCS = 5
MAX_FILE_BYTES = 25 * 1024 * 1024          # per uploaded file
MAX_CHARS_PER_DOC = 400000                 # ~100k tokens; keeps each extraction call bounded
ALLOWED_EXT = ('.pdf', '.docx', '.txt', '.md')

EXTRACT_MODEL = os.getenv("DOCSUM_EXTRACT_MODEL", "gpt-5.4-mini").strip() or "gpt-5.4-mini"
SYNTH_MODEL = os.getenv("DOCSUM_SYNTH_MODEL", "gpt-5.4").strip() or "gpt-5.4"
# medium, not high: on a 3-report test high spent ~7 min reasoning for no visible gain in the report.
SYNTH_EFFORT = os.getenv("DOCSUM_SYNTH_EFFORT", "medium").strip() or None
EXTRACT_EFFORT = os.getenv("DOCSUM_EXTRACT_EFFORT", "low").strip() or None
# Above this much cleaned text (all documents together) the extraction stage is used instead of
# feeding full text to the synthesis call. 300k chars is ~75k tokens, well inside gpt-5.4's context.
DIRECT_MAX_CHARS = int(os.getenv("DOCSUM_DIRECT_MAX_CHARS", "300000"))

# A PDF page is regulatory boilerplate when it hits at least this many of these markers.
# Measured on Anand Rathi / Axis / ICICI notes: disclosure pages hit 4-12, content pages 0-2.
BOILERPLATE_MIN_HITS = 4
BOILERPLATE_MARKERS = (
    'analyst certification', 'disclaimer', 'disclosure', 'sebi registration', 'research analyst',
    'registration no', 'conflict of interest', 'rating definitions', 'rating rationale',
    'investment rating', 'compliance officer', 'grievance', 'terms & conditions',
    'terms and conditions', 'not an offer', 'past performance', 'cin:', 'sebi (research analysts)',
    'beneficial ownership', 'compensation',
)


def _log(msg):
    # ASCII only: the Windows console raises on anything else and the broad excepts would hide it.
    print(f"DOC_SUMMARIZER: {msg}".encode('ascii', 'replace').decode('ascii'), file=sys.stderr)


# =====================================================================
# TEXT EXTRACTION
# =====================================================================

def _is_boilerplate_page(text):
    low = text.lower()
    return sum(1 for m in BOILERPLATE_MARKERS if m in low) >= BOILERPLATE_MIN_HITS


def _extract_text(filename, data):
    """Return the document's text. Page markers are kept so the report can cite pages."""
    name = filename.lower()
    if name.endswith('.pdf'):
        pages = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                text = page.extract_text() or ''
                if text.strip():
                    pages.append((i, text))
        # Page 1 always stays (it carries the rating/target box even with a disclaimer footer), and
        # if the filter would remove most of the document it is not boilerplate - keep it all.
        kept = [(i, t) for i, t in pages if i == 1 or not _is_boilerplate_page(t)]
        if len(kept) < max(1, len(pages) // 2):
            kept = pages
        dropped = len(pages) - len(kept)
        if dropped:
            _log(f"{filename}: dropped {dropped} disclosure page(s) of {len(pages)}")
        return "\n\n".join(f"--- Page {i} ---\n{t}" for i, t in kept)
    if name.endswith('.docx'):
        if not DOCX_AVAILABLE:
            raise RuntimeError("python-docx is not installed, so Word files cannot be read")
        d = docx.Document(io.BytesIO(data))
        parts = [p.text for p in d.paragraphs if p.text.strip()]
        # Analyst notes put their estimates in tables; paragraphs alone would drop them.
        for t_idx, table in enumerate(d.tables, 1):
            rows = []
            for row in table.rows:
                cells = [c.text.strip().replace('\n', ' ') for c in row.cells]
                rows.append(" | ".join(cells))
            if rows:
                parts.append(f"--- Table {t_idx} ---\n" + "\n".join(rows))
        return "\n".join(parts)
    # .txt / .md
    for enc in ('utf-8', 'cp1252', 'latin-1'):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='replace')


def _parse_json(text):
    """Best-effort JSON parse of a model reply (tolerates code fences and leading prose)."""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r'^```(?:json)?\s*', '', t)
    t = re.sub(r'\s*```$', '', t)
    try:
        return json.loads(t)
    except Exception:
        pass
    start, end = t.find('{'), t.rfind('}')
    if start != -1 and end > start:
        try:
            return json.loads(t[start:end + 1])
        except Exception:
            return None
    return None


# =====================================================================
# PROMPTS
# =====================================================================

EXTRACTION_PROMPT = """You are a senior buy-side research analyst preparing structured notes on ONE document. \
These notes will later be merged with notes from up to four other documents (often other brokers' reports on the \
same company) into a single consolidated report, so they must be COMPLETE, PRECISE and ATTRIBUTABLE. \
Nothing that is not in your notes will survive into the final report.

Document label: {label}
File name: {filename}

Rules:
- Capture every material fact, number, estimate, assumption, argument and risk. Prefer many short atomic claims \
over a few long ones. Keep exact figures, units, currency, periods (e.g. FY26E, Q2FY26, H1) and definitions \
(consolidated vs standalone, adjusted vs reported, YoY vs QoQ).
- Tag each claim with its kind: "fact" (reported/historical), "estimate" (the author's forecast), "guidance" \
(management's stated outlook), "opinion" (the author's judgement or thesis point), "assumption" (a model input).
- Record the reasoning the author gives for a claim in "rationale" (empty if none is given). A bare assertion \
without support is itself useful information.
- Use the page markers ("--- Page N ---") to fill "page" where possible.
- Note anything the document leaves unexplained, glosses over, or contradicts internally.
- Do not invent, infer or add outside knowledge. If a field is not in the document, use null or [].

Return ONLY a JSON object, no prose, in exactly this shape:
{{
  "meta": {{
    "title": "document title",
    "author_org": "publishing firm / broker / issuer, or null",
    "authors": ["analyst names"],
    "date": "publication date as written, ISO if possible, or null",
    "doc_type": "e.g. initiating coverage, results update, investor presentation, concall transcript, annual report",
    "subject": "company / sector / topic covered",
    "ticker": "ticker if given, else null",
    "rating": "e.g. BUY / ADD / HOLD / REDUCE / SELL or null",
    "target_price": "number with currency, or null",
    "price_at_publication": "CMP quoted in the document, or null",
    "horizon": "e.g. 12 months, or null"
  }},
  "core_thesis": "2-4 sentences: the document's central argument in the author's terms",
  "claims": [
    {{"topic": "short topic label, e.g. Revenue growth, Margins, Order book, Capex, Balance sheet, Management, Industry, Competition, Regulation, Valuation",
      "claim": "one precise statement with figures",
      "kind": "fact | estimate | guidance | opinion | assumption",
      "period": "period the claim refers to, or null",
      "rationale": "the reason/evidence the author gives, or empty",
      "page": "page number or null"}}
  ],
  "estimates": [
    {{"metric": "Revenue / EBITDA / EBITDA margin / PAT / EPS / ROE / etc.", "period": "FY26E", "value": "number", "unit": "e.g. INR bn, %, INR", "note": "basis or change vs previous estimate, or empty"}}
  ],
  "valuation": {{"method": "e.g. 25x FY27E EPS, DCF, SOTP", "basis_period": "e.g. Sep-27E", "key_inputs": "WACC, terminal growth, multiples and why", "peer_comparison": "if given"}},
  "risks": ["each risk with its mechanism and, if given, its magnitude"],
  "catalysts": ["each catalyst with expected timing if given"],
  "unique_angles": ["insights, data or channel checks this document has that a typical report might not"],
  "unexplained_or_weak": ["claims made without support, gaps, or questions the document leaves open"],
  "internal_inconsistencies": ["places where the document contradicts itself, e.g. text vs table numbers"]
}}

DOCUMENT TEXT
=============
{text}
=============
"""


SYNTHESIS_PROMPT = """You are the head of research at an institutional investment firm. Below is {material} \
from {n} source documents, labelled {labels}. Your job is to write ONE consolidated report that is \
strictly better than reading the {n} documents separately: the sum must be greater than the parts.

Today's date: {today}
{focus_block}
HOW TO SYNTHESISE (do all of this reasoning before you write):

1. MECE structure. Organise by TOPIC, never document-by-document. Build a section hierarchy where every topic \
appears exactly once, sections do not overlap, and together they cover everything material in the sources. Each \
fact is stated once, in its single best home, with all supporting sources cited together.

2. Plug the holes. Where one document asserts something without explanation and another supplies the evidence, \
mechanism, data point or context, combine them into one complete statement citing both. Where one document covers \
a topic the others ignore, bring it in. Where an argument has a missing link that NO source fills, say so.

3. Remove contradictions - correctly. For every point of disagreement, first classify it:
   a. Apparent, not real: different dates, periods, definitions (consolidated vs standalone, adjusted vs reported, \
gross vs net) or scope. Reconcile by stating both with their basis; do not present them as a conflict.
   b. Factual conflict (reported/historical numbers): prefer, in order, the figure traceable to a primary disclosure, \
the more recent document, and the figure that is arithmetically consistent with the other numbers. State which \
figure you adopted and why.
   c. Forecast or judgement dispersion: this is not an error to erase. Report the consensus (median where there are \
3+ values, otherwise the range), the high and low with their sources, and - most importantly - WHICH ASSUMPTION \
drives the difference. Then assess which view is better supported by the evidence across all documents.
   d. Genuinely unresolvable: say so plainly and state what data would settle it.
   Also flag any internal inconsistencies within a single document (e.g. its text vs its own tables).

4. Weigh the evidence. Give more weight to claims backed by data or a stated mechanism, to claims corroborated by \
several independent sources, and to more recent documents for time-sensitive facts. Note where the sources share a \
single origin (e.g. all repeating management guidance) - agreement among them is not independent confirmation.

5. Be the truest version. Do not introduce facts, numbers or events that are not in the source material. You may use general \
financial reasoning to interpret, and you may compute derived figures (averages, medians, implied upside, growth \
rates) - mark each computed figure with "(derived)". Label your own analytical judgements explicitly, e.g. \
"**Synthesis view:**". Never smooth over uncertainty to make the narrative tidier.

CITATIONS
- After every sentence, bullet or table row that carries a sourced claim, cite its sources in square brackets using \
the document labels only: [D1] or [D2, D4]. Every factual or numeric statement must be cited. Do not cite your own \
derived figures or synthesis views unless restating a sourced input.
- In tables, put citations in a dedicated "Source" column or at the end of the cell.

OUTPUT FORMAT (GitHub-flavoured Markdown, professional sell-side/buy-side house style):
- First line: "# <Title>", a crisp report title naming the subject, e.g. "Acme Industries: Consolidated View \
Across Five Broker Reports". No date in the title.
- Then sections as "## " headings (do not number them; the renderer numbers sections). Use "### " for \
subsections. Short paragraphs, tight bullets, and tables wherever data is compared across sources or periods.
- Write in a confident, precise analyst voice. No filler, no meta-commentary about being an AI, no "this report \
will...". Indian number conventions (Rs, crore/lakh or bn) as used in the sources.

Use this section plan when the sources are equity research / company analysis (adapt section names and add or merge \
topic sections to stay MECE for the actual content; drop a section only if no source has anything for it):

## Executive Summary
  4-7 bullets: the consensus call, the single most important debate, what the combined evidence says that no single \
  document says alone, and the key number(s). End the section with a paragraph starting "**Bottom line:**".
{focus_section}## Consensus Snapshot
  Table, one row per source: Source | Date | Rating | Target Price | Price at Publication | Implied Upside | Core stance. \
  Then a consensus row (derived) and 2-3 sentences on how views have moved over time if dates differ.
## Investment Thesis
  The merged thesis: the pillars the sources agree on, each with its strongest evidence across sources.
## <Topic sections>
  One "## " section per MECE topic area that the sources cover - e.g. Business & Competitive Position, \
  Industry & Demand Drivers, Operating Performance, Margins & Cost Structure, Capital Allocation & Balance Sheet, \
  Management & Governance. Combine evidence across sources inside each.
## Financial Estimates
  Table of key estimates by source for each forecast year (rows = metric x year, columns = sources, then a \
  Consensus (derived) column and a Spread column). Follow with the assumptions that explain the spread.
## Valuation
  Methods, multiples and basis periods used by each source, reconciled to one comparable basis where possible; \
  what the market price implies.
## Key Risks
  Merged, de-duplicated, ordered by materiality; mechanism and magnitude; which sources flag each.
## Catalysts & Monitorables
  What to watch, with timing, and which datapoints would confirm or break the thesis.
## Where the Sources Disagree
  Table: Issue | View A (sources) | View B (sources) | Type (timing / definition / factual / forecast) | Resolution \
  & reasoning. Then any unresolved debates in prose.
## Open Questions
  Gaps that none of the sources answer, phrased as the questions an investor should put to management.
## Bottom Line
  One paragraph starting "**Bottom line:**" giving the truest overall conclusion the combined evidence supports, \
  including the conditions under which it would be wrong.

If the sources are NOT equity research (e.g. legal, policy, technical or general business documents), keep the same \
principles and the Executive Summary / Where the Sources Disagree / Open Questions / Bottom Line sections, but replace \
the middle sections with a MECE topic structure suited to the material, and drop the Consensus Snapshot, Financial \
Estimates and Valuation sections.
If only ONE document is provided, produce a structured, MECE summary of it in the same house style; drop the \
Consensus Snapshot and Where the Sources Disagree sections, but keep a short section on internal inconsistencies and \
unsupported claims if there are any.

SOURCE MATERIAL
===============
{material_body}
===============
"""


# =====================================================================
# PIPELINE
# =====================================================================

def _source_entry(num, filename, meta):
    """Source-list entry the frontend links [D#] citations to."""
    meta = meta or {}
    org = (meta.get('author_org') or '').strip()
    title = (meta.get('title') or '').strip() or os.path.splitext(filename)[0]
    date = (meta.get('date') or '').strip()
    head = f"{org}: {title}" if org and org.lower() not in title.lower() else title
    if date:
        head += f" ({date})"
    details = []
    for key, label in (('doc_type', None), ('rating', 'Rating'), ('target_price', 'TP'),
                       ('price_at_publication', 'CMP')):
        val = meta.get(key)
        if val not in (None, '', [], 'null'):
            details.append(f"{label} {val}" if label else str(val))
    return {
        'num': num,
        'label': f"D{num}",
        'title': head,
        'filename': filename,
        'excerpt': " | ".join(details + [f"File: {filename}"]),
        'meta': meta,
    }


META_PROMPT = """From the opening of this document, return ONLY a JSON object (no prose) with these keys; use \
null for anything not stated:
{{"title": "...", "author_org": "publishing firm / broker / issuer", "date": "publication date, ISO if possible", \
"doc_type": "e.g. result update, initiating coverage, investor presentation, concall transcript", \
"rating": "BUY / HOLD / SELL etc.", "target_price": "number with currency", "price_at_publication": "CMP quoted"}}

File name: {filename}

{text}
"""


def _extract_meta(call_openai_api_fn, filename, text):
    """Source-list metadata from the first pages. Short, low-effort call; failure just means a bare filename."""
    try:
        raw = call_openai_api_fn([{"role": "user", "content": META_PROMPT.format(filename=filename, text=text[:8000])}],
                                 model=EXTRACT_MODEL, reasoning_effort="low", timeout=120)
        parsed = _parse_json(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception as e:
        _log(f"metadata call failed for {filename}: {e}")
        return {}


def _extract_notes(call_openai_api_fn, label, filename, text):
    prompt = EXTRACTION_PROMPT.format(label=label, filename=filename, text=text)
    raw = call_openai_api_fn([{"role": "user", "content": prompt}], model=EXTRACT_MODEL,
                             reasoning_effort=EXTRACT_EFFORT, background=True, timeout=900)
    parsed = _parse_json(raw)
    if not isinstance(parsed, dict):
        _log(f"{label}: extraction reply was not JSON ({len(raw or '')} chars); passing it through as notes")
        return {'meta': {}, 'raw_notes': raw or ''}
    return parsed


def _run_job(job_id, docs, focus, call_openai_api_fn):
    try:
        n = len(docs)
        total_chars = sum(len(d['text']) for d in docs)
        direct = total_chars <= DIRECT_MAX_CHARS
        _log(f"job {job_id}: {n} docs, {total_chars} chars -> {'direct' if direct else 'extract'} path")
        job_t0 = time.time()
        meta_pool = None

        if direct:
            # Metadata for the source list runs alongside the synthesis call, so it costs no wall time.
            meta_pool = ThreadPoolExecutor(max_workers=min(MAX_DOCS, n))
            meta_futures = [meta_pool.submit(_extract_meta, call_openai_api_fn, d['filename'], d['text'])
                            for d in docs]
            blocks = []
            for idx, d in enumerate(docs, 1):
                note = " (truncated for length; later pages were not read)" if d.get('truncated') else ""
                blocks.append(f"### Source D{idx} - file: {d['filename']}{note}\n{d['text']}")
            material = "the full text (regulatory disclosure pages removed)"
        else:
            update_agent_job(job_id, {'progress': f"Reading {n} large document{'s' if n > 1 else ''} in parallel "
                                                  f"(extracting facts, estimates and arguments)..."})
            done = {'count': 0}
            lock = threading.Lock()

            def work(item):
                idx, d = item
                label = f"D{idx}"
                t0 = time.time()
                notes = _extract_notes(call_openai_api_fn, label, d['filename'], d['text'])
                _log(f"{label} extracted in {int(time.time() - t0)}s: {len(notes.get('claims') or [])} claims")
                with lock:
                    done['count'] += 1
                    update_agent_job(job_id, {'progress': f"Extracted notes from {done['count']} of {n} documents..."})
                return notes

            with ThreadPoolExecutor(max_workers=min(MAX_DOCS, n)) as pool:
                all_notes = list(pool.map(work, list(enumerate(docs, 1))))

            blocks = []
            for idx, (d, notes) in enumerate(zip(docs, all_notes), 1):
                body = dict(notes)
                body['label'] = f"D{idx}"
                body['file_name'] = d['filename']
                if d.get('truncated'):
                    body['extraction_note'] = "Document was truncated for length; later pages were not read."
                blocks.append(f"### Source D{idx}\n" + json.dumps(body, ensure_ascii=False, separators=(',', ':')))
            material = "a set of structured notes extracted"

        if n > 1:
            update_agent_job(job_id, {'progress': "Synthesising: building a MECE structure, filling gaps across "
                                                  "documents and reconciling contradictions (usually 3-5 minutes)..."})
        else:
            update_agent_job(job_id, {'progress': "Writing the structured summary..."})

        focus_block = ""
        focus_section = ""
        if focus:
            focus_block = (f"\nTHE USER'S SPECIFIC QUESTION / INSTRUCTION: \"{focus}\"\n"
                           "Let it shape the emphasis of the whole report, and answer it directly in the section "
                           "noted below.\n")
            focus_section = ("## Response to Your Question\n  A direct, evidence-based answer to the user's question "
                             "drawing on all sources, with citations.\n")

        prompt = SYNTHESIS_PROMPT.format(
            n=n,
            labels=", ".join(f"D{i}" for i in range(1, n + 1)),
            today=datetime.date.today().isoformat(),
            focus_block=focus_block,
            focus_section=focus_section,
            material=material,
            material_body="\n\n".join(blocks),
        )
        _log(f"job {job_id}: synthesis prompt {len(prompt)} chars, model={SYNTH_MODEL}, effort={SYNTH_EFFORT}")
        t0 = time.time()
        report = call_openai_api_fn([{"role": "user", "content": prompt}], model=SYNTH_MODEL,
                                    reasoning_effort=SYNTH_EFFORT, background=True, timeout=1500)
        _log(f"job {job_id}: synthesis done in {int(time.time() - t0)}s, {len(report or '')} chars")
        if not (report or '').strip():
            raise RuntimeError("The synthesis model returned an empty report.")

        if direct:
            metas = [f.result() for f in meta_futures]  # long finished by now; each swallows its own errors
            meta_pool.shutdown(wait=False)
        else:
            metas = [notes.get('meta') for notes in all_notes]
        sources = [_source_entry(idx, d['filename'], meta) for idx, (d, meta) in enumerate(zip(docs, metas), 1)]
        _log(f"job {job_id}: total {int(time.time() - job_t0)}s")

        report = report.strip()
        report = re.sub(r'^```(?:markdown|md)?\s*', '', report)
        report = re.sub(r'\s*```$', '', report)
        title = ''
        m = re.match(r'^\s*#\s+(.+?)\s*\n', report)
        if m:
            title = m.group(1).strip()
            report = report[m.end():].lstrip()

        update_agent_job(job_id, {
            'status': 'complete',
            'progress': 'Done',
            'result': {
                'title': title,
                'markdown': report,
                'sources': sources,
                'doc_count': n,
                'focus': focus,
                'warnings': [d['warning'] for d in docs if d.get('warning')],
                'generated_at': datetime.datetime.utcnow().isoformat() + 'Z',
                'models': {'extract': EXTRACT_MODEL, 'synthesis': SYNTH_MODEL},
            },
        })
    except Exception as e:
        _log(f"job {job_id} failed: {e}")
        traceback.print_exc(file=sys.stderr)
        update_agent_job(job_id, {'status': 'error', 'error': f"Summarization failed: {e}"})


# =====================================================================
# ROUTES
# =====================================================================

def register_doc_summarizer_routes(app, call_openai_api_fn):
    """Register the Doc Summarizer Agent API routes."""

    @app.route("/agent/doc-summarizer/upload", methods=["POST"])
    def agent_doc_summarizer_upload():
        """Accept 1-5 documents, start a synthesis job, return its job_id."""
        try:
            files = [f for f in request.files.getlist('files') if f and f.filename]
            if not files and request.files.get('file') and request.files['file'].filename:
                files = [request.files['file']]  # older single-file clients
            if not files:
                return jsonify({"error": "Please upload at least one document."}), 400
            if len(files) > MAX_DOCS:
                return jsonify({"error": f"You can upload at most {MAX_DOCS} documents at a time."}), 400

            docs, problems = [], []
            for f in files:
                name = os.path.basename(f.filename)
                if not name.lower().endswith(ALLOWED_EXT):
                    problems.append(f"{name}: unsupported type (use PDF, DOCX, TXT or MD; save .doc files as .docx)")
                    continue
                data = f.read()
                if len(data) > MAX_FILE_BYTES:
                    problems.append(f"{name}: larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
                    continue
                try:
                    text = _extract_text(name, data)
                except Exception as read_err:
                    problems.append(f"{name}: could not be read ({read_err})")
                    continue
                if not text.strip():
                    problems.append(f"{name}: no extractable text (it may be a scanned/image-only PDF)")
                    continue
                doc = {'filename': name, 'text': text, 'chars': len(text)}
                if len(text) > MAX_CHARS_PER_DOC:
                    doc['text'] = text[:MAX_CHARS_PER_DOC] + "\n\n...[TRUNCATED FOR LENGTH]..."
                    doc['truncated'] = True
                    doc['warning'] = f"{name} is very long; only the first ~{MAX_CHARS_PER_DOC // 1000}k characters were analysed."
                docs.append(doc)

            if not docs:
                return jsonify({"error": "None of the documents could be used. " + " ".join(problems)}), 400

            # Usable documents proceed; skipped ones are kept in job metadata and reported with the result.
            focus = (request.form.get('user_prompt') or '').strip()[:2000]
            job_id = create_agent_job('docsum', 'DOCS', metadata={
                'files': [d['filename'] for d in docs], 'skipped': problems, 'focus': focus})
            _log(f"job {job_id}: {len(docs)} docs ({', '.join(str(d['chars']) for d in docs)} chars), "
                 f"{len(problems)} skipped")
            threading.Thread(target=_run_job, args=(job_id, docs, focus, call_openai_api_fn),
                             daemon=True).start()
            return jsonify({"status": "processing", "job_id": job_id,
                            "files": [d['filename'] for d in docs], "skipped": problems})
        except Exception as e:
            _log(f"upload error: {e}")
            traceback.print_exc(file=sys.stderr)
            return jsonify({"error": str(e)}), 500

    @app.route("/agent/doc-summarizer/<job_id>/status", methods=["GET"])
    def agent_doc_summarizer_status(job_id):
        job = get_agent_job(job_id)
        if not job:
            return jsonify({"error": "Job not found or expired"}), 404
        if job['status'] == 'processing':
            return jsonify({"status": "processing", "progress": job.get('progress'),
                            "elapsed_seconds": int(time.time() - job['started_at'])})
        if job['status'] == 'complete':
            result = dict(job.get('result') or {})
            skipped = (job.get('metadata') or {}).get('skipped') or []
            result['warnings'] = list(result.get('warnings') or []) + [f"Skipped {p}" for p in skipped]
            return jsonify({"status": "complete", "result": result})
        return jsonify({"status": "error", "error": job.get('error') or 'Unknown error'})
