"""
guidance_prompts.py - prompt and response schema for the Guidance Tracker.

Stage 1 of the guidance pipeline: a Flash-Lite model reads one earnings-call
transcript and returns every forward-looking statement as structured rows.
Stage 2 (agents/guidance_tracker.py) then checks each row against the
transcript in code, so nothing here is trusted on its own - but the prompt is
still the first line of defence, and its three hard rules are the same three
the validator enforces:

  1. Management targets only. A number an analyst puts forward is not guidance,
     even when management does not push back on it.
  2. No invented numbers. Qualitative optimism stays qualitative.
  3. Silence is recorded as silence ("not disclosed"), never filled in.
"""

# Metrics each sector is expected to guide on. For every one of these the
# model must return either a guidance item or a not_disclosed entry, so that
# "management gave no revenue guidance" is an explicit row in the database
# rather than an absence nobody can tell apart from an extraction miss.
SECTOR_CORE_METRICS = {
    'bank': ['loan_growth', 'deposit_growth', 'net_interest_margin', 'credit_cost'],
    'nbfc': ['aum_growth', 'net_interest_margin', 'credit_cost'],
    'insurance': ['premium_growth', 'vnb_margin'],
    'it': ['revenue_growth', 'ebit_margin'],
    'infra': ['order_inflow', 'revenue_growth', 'ebitda_margin'],
    'realty': ['pre_sales', 'project_launches'],
    'general': ['revenue_growth', 'ebitda_margin', 'capex'],
}

SECTOR_LABELS = {
    'bank': 'Bank',
    'nbfc': 'NBFC / lender',
    'insurance': 'Insurance',
    'it': 'IT services',
    'infra': 'Infrastructure / capital goods / EPC',
    'realty': 'Real estate',
    'general': 'Manufacturing / consumer / other',
}

# Suggested metric vocabulary. Not exhaustive - the model may use another
# snake_case name when none of these fits - but sticking to these keys is what
# lets the same metric from two companies land in the same filter.
METRIC_VOCABULARY = [
    'revenue_growth', 'revenue', 'segment_revenue_growth', 'volume_growth',
    'ebitda_margin', 'ebit_margin', 'gross_margin', 'pat_margin', 'pat_growth',
    'capex', 'order_inflow', 'order_book', 'execution', 'deal_wins_tcv',
    'loan_growth', 'deposit_growth', 'aum_growth', 'net_interest_margin',
    'credit_cost', 'gnpa', 'cost_to_income', 'roa', 'roe', 'roce',
    'premium_growth', 'vnb_margin', 'pre_sales', 'project_launches',
    'capacity_addition', 'capacity_utilisation', 'net_debt', 'working_capital',
    'dividend_payout', 'tax_rate', 'headcount', 'realisation', 'market_share',
]


GUIDANCE_EXTRACTION_PROMPT = """You are an equity research analyst extracting MANAGEMENT GUIDANCE from an Indian listed company's earnings conference call transcript. Your output is loaded into a database that investors filter and act on, and every row will be checked word-for-word against the transcript. Accuracy matters far more than coverage.

COMPANY: {company_name} ({ticker})
SECTOR: {sector_label}
REPORTING PERIOD OF THE CALL: {quarter_label}

The transcript is split into pages marked "=== PAGE n ===". Use those page numbers.

=====================================================================
WHAT TO EXTRACT
=====================================================================
Return one item per (metric, target period) that management speaks to, in these categories:

- quantitative_guidance: management states a number, range, or numeric threshold for a future period ("revenue growth of 12-14% in FY27", "capex of Rs 2,000 crore next year", "margins in the 24-26% band").
- guidance_revision: management changes a target it gave before ("we are raising our FY27 loan growth guidance from 14% to 16%"). Fill prior_value_text with the old target.
- qualitative_outlook: a forward-looking directional view with NO number ("we expect demand to remain strong in H2", "margins should improve from here").
- management_commentary: other material forward-looking remarks (strategy, demand environment, pricing, new capacity timelines) that an investor would track.
- risk: risks or headwinds management itself flags (tariffs, raw-material inflation, regulatory change, slower client spending).

=====================================================================
THREE HARD RULES
=====================================================================
RULE 1 - MANAGEMENT ONLY. Guidance is what the company's management (CEO, CFO, MD, business heads, IR) says. Analysts and the moderator never give guidance.
  - If an analyst says "so you are guiding to 15%?" and management replies "yes, that's right" or "we are comfortable with that", you MAY record it, with basis = "inferred", and statement = management's reply (not the analyst's question). Put the analyst's number in value_text and explain in rationale.
  - If management does not clearly confirm the analyst's number, do NOT record it.
  - speaker_name must be the person who actually said the statement.

RULE 2 - NEVER INVENT NUMBERS. Qualitative optimism stays qualitative.
  - "We expect strong growth" -> qualitative_outlook, value_low = null, value_high = null.
  - Only put a number in value_low/value_high if that number (or an unambiguous phrase like "double-digit" = 10, "mid-teens" = 14 to 16, "high single digit" = 7 to 9) appears in management's statement itself.
  - When the number comes from such a phrase rather than digits, basis = "inferred".
  - Never compute, annualise, convert or estimate a number management did not say.
  - Never use the company's reported results for the quarter just ended as guidance. Guidance is about the FUTURE.

RULE 3 - SILENCE IS "NOT DISCLOSED". For each of these core metrics for this sector:
  {core_metrics}
  either return at least one item for it, or list it in not_disclosed with a short note (e.g. "Management declined to give FY27 guidance", "Not discussed on the call"). Never fill a core metric with a guess to avoid listing it as not disclosed.

=====================================================================
FIELD RULES
=====================================================================
- statement: copy management's words EXACTLY from the transcript, verbatim, one to three sentences, enough to contain the guidance and its number. Do not paraphrase, do not fix grammar, do not join sentences from different places. This quote is searched for in the transcript; a paraphrase will be flagged as unverifiable.
- metric: a snake_case key. Prefer one of: {metric_vocabulary}. Use another short snake_case key only if none fits. For risk items use a short topic key (e.g. "us_tariffs").
- metric_label: the metric as management phrased it ("constant currency revenue growth", "consolidated EBITDA margin").
- value_low / value_high: numbers only, no units. A point target sets both to the same number. "At least 12%" -> value_low = 12, value_high = null. "Up to 8%" -> value_low = null, value_high = 8. Null when no number.
- value_text: the target exactly as stated ("12-14%", "~Rs 2,000 crore", "double-digit", "Not disclosed").
- unit: one of "%", "bps", "INR crore", "INR billion", "USD million", "USD billion", "x", "units", "MW", "MTPA", or a short unit as stated. Empty string when no number.
- target_period: the period the guidance is FOR ("FY27", "H2 FY27", "Q3 FY27", "next 2-3 years", "medium term"). Empty string if management gives none.
- revision: "new" (first time stated), "maintained" (reiterated), "raised", "lowered", "withdrawn", or "none" (for outlook/commentary/risk items).
- speaker_type: "management", "analyst" or "moderator". Items should almost always be "management"; use "analyst" only for an inferred item where you also explain the confirmation in rationale.
- speaker_role: e.g. "CFO", "MD & CEO", "Head - Investor Relations". Empty string if unknown.
- page: the page number where the statement appears.
- confidence: "high" when the statement is unambiguous guidance, "medium" when interpretation is needed, "low" when you are unsure whether it is guidance at all.
- sentiment: "positive", "neutral" or "negative" from the company's point of view.
- rationale: one short sentence on why this is guidance and, for inferred items, what it was inferred from.

Also return:
- call_summary: 4-6 sentences summarising the call for an investor: results context, the key guidance, the tone, the main risks.
- outlook_tone: "positive", "cautious", "negative" or "mixed".
- guidance_headline: one line, the single most important piece of forward guidance, or "No explicit guidance given".

Return between 0 and 40 items. Prefer fewer, correct items over many doubtful ones.

=====================================================================
TRANSCRIPT
=====================================================================
{transcript}
"""


_ITEM_SCHEMA = {
    'type': 'object',
    'properties': {
        'category': {'type': 'string', 'enum': [
            'quantitative_guidance', 'guidance_revision', 'qualitative_outlook',
            'management_commentary', 'risk']},
        'metric': {'type': 'string'},
        'metric_label': {'type': 'string'},
        'value_low': {'type': ['number', 'null']},
        'value_high': {'type': ['number', 'null']},
        'value_text': {'type': 'string'},
        'unit': {'type': 'string'},
        'target_period': {'type': 'string'},
        'basis': {'type': 'string', 'enum': ['explicit', 'inferred']},
        'revision': {'type': 'string', 'enum': [
            'new', 'maintained', 'raised', 'lowered', 'withdrawn', 'none']},
        'prior_value_text': {'type': 'string'},
        'statement': {'type': 'string'},
        'speaker_name': {'type': 'string'},
        'speaker_role': {'type': 'string'},
        'speaker_type': {'type': 'string', 'enum': ['management', 'analyst', 'moderator']},
        'page': {'type': 'integer'},
        'confidence': {'type': 'string', 'enum': ['high', 'medium', 'low']},
        'sentiment': {'type': 'string', 'enum': ['positive', 'neutral', 'negative']},
        'rationale': {'type': 'string'},
    },
    'required': ['category', 'metric', 'metric_label', 'value_low', 'value_high',
                 'value_text', 'unit', 'target_period', 'basis', 'revision',
                 'statement', 'speaker_name', 'speaker_type', 'page', 'confidence'],
}

GUIDANCE_RESPONSE_SCHEMA = {
    'type': 'object',
    'properties': {
        'call_summary': {'type': 'string'},
        'outlook_tone': {'type': 'string', 'enum': ['positive', 'cautious', 'negative', 'mixed']},
        'guidance_headline': {'type': 'string'},
        'items': {'type': 'array', 'items': _ITEM_SCHEMA},
        'not_disclosed': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'metric': {'type': 'string'},
                    'note': {'type': 'string'},
                },
                'required': ['metric', 'note'],
            },
        },
    },
    'required': ['call_summary', 'outlook_tone', 'guidance_headline', 'items', 'not_disclosed'],
}


def build_guidance_prompt(company_name, ticker, sector_bucket, quarter_label, transcript):
    core = SECTOR_CORE_METRICS.get(sector_bucket) or SECTOR_CORE_METRICS['general']
    return GUIDANCE_EXTRACTION_PROMPT.format(
        company_name=company_name,
        ticker=ticker,
        sector_label=SECTOR_LABELS.get(sector_bucket, SECTOR_LABELS['general']),
        quarter_label=quarter_label or 'unknown',
        core_metrics=', '.join(core),
        metric_vocabulary=', '.join(METRIC_VOCABULARY),
        transcript=transcript,
    )
