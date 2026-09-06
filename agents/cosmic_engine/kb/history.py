"""
history.py — a small table of major macro events, for dating historical analogues.

PROVENANCE WARNING — this module is NOT generated from COSMIC_PDF_AUGMENTATION_TEXT, and unlike the
other hand-authored modules it is not derived from the rulebook at all.

KB section 30 Stage 7 requires an analogue for every HIGH CONFIDENCE call: "Reference at least ONE
historical analogue... State outcome similarity (e.g. 'Similar Saturn-Mars conjunction in Capricorn
last occurred 2007-2008 -> preceded global financial restructuring')." The rulebook supplies the
requirement and one illustrative example, but no table. So the model has been producing these from
training memory, undated and unverifiable — the last remaining invented field in the report.

The split that makes this honest:

    DATES come from the ephemeris. analogues.py back-scans for when a configuration actually last
    occurred. Those dates are computed and checkable.

    OUTCOMES come from this table. Every entry is a well-documented macro event with a date range
    that is a matter of public record. Nothing here is inferred from astrology, and no causal claim
    is made or implied - the table only answers "what was happening then".

If a computed date range overlaps no entry here, the analogue reports its dates with `events: []`
and NO outcome. That is the correct output for "this configuration last occurred in 1994-96 and
nothing in our table happened then" — far better than inventing a narrative to fill the field.

The table is deliberately short and confined to events whose dates are uncontroversial. It is a
starting point for the desk to extend, not a complete financial history. Add entries with the same
discipline: a real date range, a factual one-line description, no astrological reasoning.
"""

# (start_year, end_year, label, description). Ranges are inclusive and deliberately coarse — the
# point is to say what era a computed date lands in, not to pin a market turn to a week.
MACRO_EVENTS = (
    (1929, 1932, "Wall Street crash and the Great Depression",
     "October 1929 crash followed by a global depression and widespread bank failures"),
    (1939, 1945, "Second World War",
     "global war economy, rationing, and state direction of industry"),
    (1947, 1947, "Indian independence and partition",
     "independence, partition, and the founding of the Indian republic's economy"),
    (1966, 1967, "Indian rupee devaluation",
     "the 1966 devaluation, drought, and a severe food and foreign-exchange crisis"),
    (1969, 1970, "US recession and the end of the go-go years",
     "the 1969-70 US recession and bear market, with a back-office crisis on Wall Street"),
    (1971, 1973, "End of Bretton Woods and the first oil shock",
     "the dollar leaves gold convertibility; OPEC embargo quadruples crude prices"),
    (1979, 1982, "Second oil shock and the Volcker disinflation",
     "a second crude spike, then US rates above 15% to break inflation; global recession"),
    (1987, 1987, "Black Monday",
     "October 1987, the largest single-day percentage fall in modern equity market history"),
    (1989, 1990, "Japanese asset bubble peak",
     "the Nikkei peaks in December 1989 and Japan's land and equity bubble begins deflating"),
    (1990, 1992, "Indian balance-of-payments crisis and liberalisation",
     "reserves fall to weeks of imports; the 1991 reforms open the Indian economy"),
    (1997, 1998, "Asian financial crisis",
     "currency collapses across East and South-East Asia; the Russian default follows"),
    (2000, 2002, "Dot-com bust",
     "the technology bubble unwinds; a broad capex and telecom downturn"),
    (2007, 2009, "Global financial crisis",
     "US housing and credit collapse, bank failures, and coordinated global stimulus"),
    (2010, 2012, "European sovereign debt crisis",
     "peripheral euro-area debt crises and sustained doubt over the single currency"),
    (2013, 2013, "Taper tantrum",
     "Federal Reserve tapering signals trigger sharp emerging-market currency and bond selloffs"),
    (2014, 2016, "Oil price collapse and the emerging-market selloff",
     "crude falls from about $110 to under $30; China devalues the yuan and EM assets sell off"),
    (2016, 2016, "Indian demonetisation and Brexit vote",
     "withdrawal of high-value Indian currency notes; the United Kingdom votes to leave the EU"),
    (2020, 2021, "COVID-19 crash and reflation",
     "the fastest bear market on record, then unprecedented fiscal and monetary support"),
    (2022, 2023, "Global inflation shock and rate cycle",
     "post-pandemic inflation and the sharpest synchronised rate-hiking cycle in decades"),
)


def events_overlapping(start_year, end_year):
    """
    Curated macro events whose range overlaps [start_year, end_year].

    Returns a list of {"label", "description", "years"} — empty when nothing in the table overlaps,
    which is a valid and expected result.
    """
    out = []
    for ev_start, ev_end, label, description in MACRO_EVENTS:
        if ev_start <= end_year and start_year <= ev_end:
            years = ("%d" % ev_start) if ev_start == ev_end else ("%d-%d" % (ev_start, ev_end))
            out.append({"label": label, "description": description, "years": years})
    return out


def coverage():
    """(earliest_year, latest_year) covered by the table, for reporting search limits honestly."""
    return min(e[0] for e in MACRO_EVENTS), max(e[1] for e in MACRO_EVENTS)
