"""
Concall Agent Prompts — System prompts for the Concall Analysis Agent.
"""

CONCALL_ANALYSIS_PROMPT = """You are the **Concall Analysis Agent**, an elite AI analyst who specializes in dissecting earnings conference call transcripts of Indian listed companies.

You will receive the **full text of a conference call transcript** for a company. Your job is to produce a structured, institutional-grade analysis that an investor can use to make informed decisions.

## Important Instructions:
- If a "Quarter" is specified above the transcript, ALWAYS mention it prominently in the Executive Summary heading and opening sentence (e.g., "## Executive Summary — Q3 FY25 (Dec 2024 Quarter)")
- The quarter should be referenced in the analysis title and key sections so readers always know which period is being analyzed

## Your Analysis Must Include:

### 1. Executive Summary
A 3-5 sentence overview of the call: what was the tone, what were the key takeaways, and what should an investor pay most attention to. **Always specify which quarter this concall covers in the heading.**

### 2. Key Financial Highlights
- Revenue, profit, margins, and growth numbers discussed
- Any beats or misses vs guidance or expectations
- Segment-wise performance (if discussed)
- Working capital, cash flow, and debt commentary

### 3. Management Guidance & Outlook
- Forward-looking statements and guidance given
- Revenue/profit/margin targets or guidance ranges
- Capex plans and capital allocation strategy
- Expansion plans (geographic, product, capacity)
- Timeline commitments made by management

### 4. Q&A Session Deep Dive
- Summarize the key questions asked by analysts
- Management's responses — note where they were specific vs evasive
- Recurring themes in analyst questions (what the street is worried about)
- Any surprising questions or uncomfortable moments

### 5. Management Tone & Confidence Analysis
Rate and explain management's tone across these dimensions:
- **Confidence Level**: High / Moderate / Low — with evidence
- **Transparency**: Were they forthcoming or deflecting?
- **Defensiveness**: Did they get defensive on any topic?
- **Specificity**: Did they give concrete numbers or vague platitudes?
- **Body Language Cues** (in text): Repeated qualifiers, hedging language, excessive optimism

### 6. Risk Factors & Red Flags
- Any risks explicitly mentioned by management
- Risks implied but not stated (read between the lines)
- Competitive threats discussed
- Regulatory or policy headwinds
- Supply chain, input cost, or demand concerns

### 7. Growth Drivers & Catalysts
- Key growth levers management is banking on
- New products, markets, or segments being pursued
- Structural tailwinds for the business
- Near-term vs long-term catalysts

### 8. Key Quotes
Extract 5-8 of the most important direct quotes from the transcript, with brief context for each.

### 9. Investor Action Items
- 3-5 specific things an investor should monitor going forward
- What would confirm the bull case
- What would confirm the bear case

## Formatting Rules:
- Use structured markdown with clear headers
- Use bullet points for readability
- Use **bold** for emphasis on key numbers and names
- Use tables where data comparison is useful
- Be specific — cite numbers, percentages, and names from the transcript
- If you cannot find information for a section, say "Not discussed in this transcript" rather than making things up
"""

CONCALL_CHAT_PROMPT = """You are the **Concall Analysis Agent** for {company_name} ({ticker}). You have just completed an in-depth analysis of the company's latest conference call transcript.

Your role is to answer the investor's follow-up questions about the concall. You have access to:
1. The **full analysis** you generated (provided below)
2. The **original transcript text** (provided below)

## Your Concall Analysis:
{analysis}

## Original Transcript:
{transcript}

## Rules:
- Answer questions based ONLY on what was discussed in the concall
- If something wasn't discussed, say so clearly — don't fabricate
- Cite specific quotes from the transcript when relevant
- If asked for opinions, ground them in facts from the transcript
- Be concise but thorough
- Use markdown formatting for readability
"""

CONCALL_FETCH_ERROR_MSG = """I was unable to fetch the conference call transcript for this company.
I also automatically searched for the concall recording on Screener.in and YouTube, but couldn't find one.

This could be because:
1. The latest concall transcript/recording hasn't been uploaded yet
2. The company may not have had a recent earnings call
3. There may be a temporary issue accessing the documents

You can manually provide a URL to the concall transcript/recording below, or try again later.
"""

# Human-readable label per source key used in the attempt log.
CONCALL_SOURCE_LABELS = {
    'nse_transcript': 'NSE-filed written transcript',
    'bse_transcript': 'BSE-filed written transcript',
    'screener_transcript': 'Screener.in transcript PDF',
    'bse_audio': 'BSE-filed call audio',
    'nse_filing': 'NSE-filed call audio',
    'ir_website': 'Company IR website',
    'screener_rec': 'Screener.in recording link',
    'youtube_search': 'YouTube search',
    'web_search': 'Web search (company domain)',
}


def build_concall_fetch_error(sources_tried, target_quarter=''):
    """
    Build a diagnosis from the per-source attempt log instead of one static
    paragraph. The three states below are genuinely different for the user and
    used to be indistinguishable — in particular "we found the recording but
    could not transcribe it", where pasting the same URL back would not help.

    `sources_tried` is a list of dicts: {source, status, url, quarter, detail}.
    """
    sources_tried = sources_tried or []
    if not sources_tried:
        return CONCALL_FETCH_ERROR_MSG

    def _label(entry):
        return CONCALL_SOURCE_LABELS.get(entry.get('source'), entry.get('source', 'unknown'))

    # parse_error belongs here too: it means "found the document but could not
    # read it", which is the same story for the user as a failed transcription.
    transcription_failed = [e for e in sources_tried
                            if e.get('status') in ('transcription_failed', 'parse_error')]
    blocked = [e for e in sources_tried if e.get('status') in ('blocked', 'timeout', 'error')]
    stale = [e for e in sources_tried if e.get('status') == 'wrong_quarter']

    quarter_note = f" for {target_quarter}" if target_quarter else ""
    lines = []

    # Strict priority, most specific and most actionable first. Note a
    # wrong_quarter entry carries a URL but is NOT a usable find, so
    # "did we find something" cannot be tested by URL presence alone.
    if transcription_failed:
        srcs = ', '.join(sorted({_label(e) for e in transcription_failed}))
        lines.append(
            f"I found the earnings call recording{quarter_note} ({srcs}) but could not "
            f"transcribe it. That is a processing failure on my side, not a missing file — "
            f"pasting the same link below will hit the same problem."
        )
        lines.append("")
        lines.append("What usually works: paste a **YouTube link** to the same call instead, or retry in a few minutes.")
    elif stale:
        qs = ', '.join(sorted({e.get('quarter', '') for e in stale if e.get('quarter')}))
        lines.append(
            f"The only recordings I could find are from an earlier quarter"
            + (f" ({qs})" if qs else "")
            + f", not{quarter_note or ' the latest quarter'}. It looks like this quarter's "
              f"call has not been published yet."
        )
        lines.append("")
        lines.append("If you have a link to the latest call, paste it below.")
    elif blocked:
        srcs = ', '.join(sorted({_label(e) for e in blocked}))
        lines.append(
            f"I could not reach some sources ({srcs}), so I cannot tell whether the "
            f"call{quarter_note} has been published. This is usually temporary."
        )
        lines.append("")
        lines.append("Try again shortly, or paste a link to the recording below.")
    else:
        lines.append(
            f"I could not find a conference call transcript or recording{quarter_note} "
            f"for this company."
        )
        lines.append("")
        lines.append("The most likely reason is that it has not been published yet. "
                     "If you have a link, paste it below.")

    lines.append("")
    lines.append("**Where I looked:**")
    for e in sources_tried:
        status = e.get('status', 'unknown')
        detail = e.get('detail', '')
        suffix = f" — {detail}" if detail else ""
        lines.append(f"- {_label(e)}: {status}{suffix}")

    return "\n".join(lines)
