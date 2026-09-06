"""
aliases.py — modern equivalents for the archaic place names in the knowledge base.

PROVENANCE WARNING — this module is NOT generated from COSMIC_PDF_AUGMENTATION_TEXT.

Section 12's geography is attributed to "Ptolemy's Tetrabiblos, updated by Prof. R.M. Palaniappan",
and it reads like it: Aquarius rules Abyssinia, Piedmont and Prussia; Leo rules Chaldea; Scorpio
rules Transvaal. Those were correct when written and are unusable in a market report now — a signal
that says "Transvaal faces upheaval at 78% confidence" is not something a reader can act on.

The fix is applied HERE rather than by editing the rulebook, deliberately. The generated `kb/`
modules are verbatim transcriptions, enforced by `tools/extract_cosmic_kb.py --check`, and that
guarantee is what makes the prompt text and the engine data provably identical. So the rulebook keeps
its classical names and stays a faithful record of its cited authority, while signal targets and
brief rows carry the modern name. Each signal's `basis` still quotes the original wording, so
"Ethiopia" remains traceable to "S12 sign-country: Aquarius -> Abyssinia (Ethiopia)".

Two rules followed when building the map:

  * EXACT MATCH ONLY, no regex on parentheses. "India (overall)" would have yielded a country called
    "overall" under a rule that just prefers the bracketed text.
  * A SUB-NATIONAL PLACE KEEPS ITS PARENT. "Piedmont" becomes "Italy (Piedmont)" rather than
    "Italy", because the rulebook is pointing at a specific region and flattening that would
    overstate the claim's scope.

Anything absent from the map passes through unchanged, which is the right default: most of the
table (France, Japan, Brazil, Australia) needs no translation.
"""

# Section 12 - countries. Only entries that are archaic, renamed, or ambiguous appear here.
COUNTRY_ALIASES = {
    "Abyssinia (Ethiopia)": "Ethiopia",
    "Asia Minor": "Turkey (Anatolia)",
    "Babylonia": "Iraq",
    "Bohemia": "Czech Republic",
    "Burma (Myanmar)": "Myanmar",
    "Chaldea": "Iraq (southern)",
    "Holland": "Netherlands",
    "India (overall)": "India",
    "Indochina": "Vietnam, Cambodia and Laos",
    "Naples": "Italy (Naples)",
    "Normandy": "France (Normandy)",
    "North China": "China (northern)",
    "Persia (Iran)": "Iran",
    "Persia": "Iran",
    "Piedmont": "Italy (Piedmont)",
    "Prussia": "Germany (northern)",
    "Punjab": "Punjab (India and Pakistan)",
    "Queensland": "Australia (Queensland)",
    "Sahara region": "Sahara (North Africa)",
    "Transvaal": "South Africa (Gauteng)",
    "West Indies": "Caribbean",
}

# Section 12 - cities.
CITY_ALIASES = {
    "Bombay (Mumbai)": "Mumbai",
    "Madras (Chennai)": "Chennai",
    "Hastinapur": "Hastinapur (Meerut, Uttar Pradesh)",
    "Lancashire": "Lancashire (United Kingdom)",
    "Newfoundland": "Newfoundland (Canada)",
    "Washington DC": "Washington DC",
}

# Sections 13 and 14 - Indian regions. Odisha was renamed from Orissa in 2011; the rest are either
# classical toponyms or partitioned regions that need their modern state named.
REGION_ALIASES = {
    "Orissa": "Odisha",
    "Part of Bengal": "West Bengal",
    "Kanchi": "Kanchipuram (Tamil Nadu)",
    "Thanjavur": "Thanjavur (Tamil Nadu)",
    "Tiruchirappalli": "Tiruchirappalli (Tamil Nadu)",
    "Thaneshwar": "Thanesar (Haryana)",
    "Taxila": "Taxila (Punjab, Pakistan)",
    "Gandhara": "Gandhara (north-west Pakistan)",
    "West Punjab": "Punjab (Pakistan)",
    "Eastern Punjab": "Punjab (India)",
    "Konkan coast": "Konkan coast (Maharashtra and Goa)",
    "Kulu Valley": "Kullu Valley (Himachal Pradesh)",
    "Vindhya region": "Vindhya region (Madhya Pradesh)",
    "Southern Andhra": "southern Andhra Pradesh",
    "Saurashtra": "Saurashtra (Gujarat)",
    "Himalayan region": "Himalayan region",
    "Burma": "Myanmar",
}

# Merged lookup. Country aliases take precedence, then cities, then regions — the three tables do
# not currently collide, and this ordering is asserted in the tests so a future addition that would
# collide fails loudly instead of resolving by dictionary order.
ALL_ALIASES = {}
ALL_ALIASES.update(REGION_ALIASES)
ALL_ALIASES.update(CITY_ALIASES)
ALL_ALIASES.update(COUNTRY_ALIASES)

# Entries that name a region rather than a sovereign state. Exposed, not yet acted on: whether the
# eclipse-geopolitics generator should treat a province differently from a country is a content
# decision that has not been taken. See the Phase 4 notes in the migration doc.
SUBNATIONAL = frozenset((
    "Naples", "Normandy", "North China", "Piedmont", "Prussia", "Punjab", "Queensland",
    "Sahara region", "Transvaal", "West Indies", "Asia Minor", "Lancashire", "Newfoundland",
))


def modern(name):
    """
    Modern equivalent of a knowledge-base place name, or the name unchanged.

    Exact match only. Callers should pass the KB string verbatim.
    """
    if not name:
        return name
    return ALL_ALIASES.get(name.strip(), name)


def modern_list(names):
    """Map `modern` across a list, preserving order and dropping nothing."""
    return [modern(n) for n in (names or [])]


def is_subnational(name):
    """True when the KB entry names a province or region rather than a sovereign state."""
    return (name or "").strip() in SUBNATIONAL
