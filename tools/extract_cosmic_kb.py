"""
extract_cosmic_kb.py — generate agents/cosmic_engine/kb/*.py from COSMIC_PDF_AUGMENTATION_TEXT.

The knowledge base sections that are pure lookup tables must move from prompt prose into Python
data *verbatim* (docs/COSMIC_ENGINE_MIGRATION.md Phase 1). Hand-transcribing 108 pada rows and
nine planet/sign tables is exactly the kind of work that silently introduces typos, so we parse
the KB instead and generate the modules.

Run:  python tools/extract_cosmic_kb.py
      python tools/extract_cosmic_kb.py --check      (verify generated files are up to date)

Every parser below asserts its expected row count. A KB edit that changes a table's shape makes
this script fail loudly rather than emit a half-populated dict.

ASCII-only on stdout: the KB contains Devanagari and degree signs, and the Windows console
encodes with cp1252 (see the ascii-only-print constraint in the migration doc).
"""

import argparse
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KB_SOURCE = os.path.join(ROOT, "agents", "prompts", "cosmic_prompts.py")
OUT_DIR = os.path.join(ROOT, "agents", "cosmic_engine", "kb")

# Canonical orders. NAKSHATRA_NAMES matches agents/utils/ephemeris.py exactly so the engine and
# the existing ephemeris report agree on indices; the KB uses abbreviated spellings, mapped below.
SIGNS = [
    "Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo",
    "Libra", "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces",
]
NAKSHATRA_NAMES = [
    "Ashwini", "Bharani", "Krittika", "Rohini", "Mrigashira", "Ardra",
    "Punarvasu", "Pushya", "Ashlesha", "Magha", "Purva Phalguni", "Uttara Phalguni",
    "Hasta", "Chitra", "Swati", "Vishakha", "Anuradha", "Jyeshtha",
    "Mula", "Purva Ashadha", "Uttara Ashadha", "Shravana", "Dhanishta", "Shatabhisha",
    "Purva Bhadrapada", "Uttara Bhadrapada", "Revati",
]
# KB spelling -> canonical index. The KB abbreviates Purva/Uttara as P./U. and spells a few
# nakshatras differently from ephemeris.py (Dhanishtha vs Dhanishta, P.Bhadra vs Purva
# Bhadrapada). Mapping explicitly means a new spelling fails loudly instead of silently missing.
KB_NAKSHATRA_ALIASES = {
    "Ashwini": 0, "Bharani": 1, "Krittika": 2, "Rohini": 3, "Mrigashira": 4, "Ardra": 5,
    "Punarvasu": 6, "Pushya": 7, "Ashlesha": 8, "Magha": 9, "P.Phalguni": 10,
    "U.Phalguni": 11, "Hasta": 12, "Chitra": 13, "Swati": 14, "Vishakha": 15,
    "Anuradha": 16, "Jyeshtha": 17, "Mula": 18, "P.Ashadha": 19, "U.Ashadha": 20,
    "Shravana": 21, "Dhanishtha": 22, "Shatabhisha": 23, "P.Bhadra": 24,
    "U.Bhadra": 25, "Revati": 26,
}
PLANET_ALIASES = {
    "Sun": "Sun", "Moon": "Moon", "Mars": "Mars", "Mercury": "Mercury", "Jupiter": "Jupiter",
    "Venus": "Venus", "Saturn": "Saturn", "Rahu": "Rahu", "Ketu": "Ketu",
    "Uranus": "Uranus", "Neptune": "Neptune", "Pluto": "Pluto",
}

GENERATED_HEADER = '''"""
{module} — GENERATED FILE. Do not edit by hand.

Source: KB section {section} of COSMIC_PDF_AUGMENTATION_TEXT in agents/prompts/cosmic_prompts.py
Regenerate: python tools/extract_cosmic_kb.py

Content is verbatim from the knowledge base. To change a mapping, edit the KB and regenerate,
so the prompt text and the engine data can never disagree.
"""

'''


# ---------------------------------------------------------------------------
# KB access
# ---------------------------------------------------------------------------

def load_kb():
    src = io.open(KB_SOURCE, encoding="utf-8").read()
    marker = 'COSMIC_PDF_AUGMENTATION_TEXT = """'
    start = src.index(marker) + len(marker)
    end = src.index('"""', start)
    return src[start:end]


def section(kb, number):
    """Return the body of '### <number>. ...' up to the next '### ' heading."""
    m = re.search(r"^### %s\. .*$" % re.escape(str(number)), kb, re.M)
    if not m:
        raise LookupError("KB section %s not found" % number)
    rest = kb[m.end():]
    nxt = re.search(r"^### ", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def table_rows(body, expected_cols):
    """
    Parse a markdown pipe table into a list of column-lists. Skips the header row and the
    |---|---| separator. Blank cells are preserved as empty strings.
    """
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [c.strip() for c in line[1:-1].split("|")]
        if len(cells) != expected_cols:
            continue
        if all(set(c) <= set("-: ") and c for c in cells):
            continue  # separator row
        rows.append(cells)
    return rows[1:]  # drop header


def strip_devanagari(text):
    """'Sun (सूर्य)' -> ('Sun', 'सूर्य'). Returns (ascii_name, native_or_empty)."""
    m = re.match(r"^\s*([A-Za-z][A-Za-z .'/-]*?)\s*\(([^)]*)\)\s*$", text)
    if m and not re.match(r"^[A-Za-z0-9 ,.'/-]+$", m.group(2)):
        return m.group(1).strip(), m.group(2).strip()
    return text.strip(), ""


def split_list(text):
    """Split a comma-separated KB cell into a clean list, preserving parenthesised groups."""
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            if cur.strip():
                out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------

def parse_padas(kb):
    """
    Section 25: 108 bullet rows, zodiacal order starting Ashwini P1.

    '- Ashwini P1 (Aries 0 deg-3 deg20', Aries navamsa, Mars): <sub-sector> - *<names>*'

    Keyed by (nakshatra_index, pada) rather than by name: chart.py derives both from longitude,
    and the KB's abbreviated spellings differ from ephemeris.py's.
    """
    body = section(kb, 25)
    pat = re.compile(
        r"^- ([A-Za-z.]+(?:\s[A-Za-z]+)?) P([1-4]) "
        r"\(([A-Za-z]+) [^,]*, ([A-Za-z]+) nav(?:amsa)?, ([A-Za-z]+)\): "
        r"([^\n]*?)(?: — \*([^*]*)\*)?$",
        re.M,
    )
    matches = pat.findall(body)
    assert len(matches) == 108, "section 25: expected 108 padas, parsed %d" % len(matches)

    out = {}
    for i, (nak_kb, pada_s, sign, nav_sign, nav_lord, sub, names) in enumerate(matches):
        expected_idx, expected_pada = i // 4, i % 4 + 1
        idx = KB_NAKSHATRA_ALIASES.get(nak_kb)
        assert idx is not None, "unknown KB nakshatra spelling %r" % nak_kb
        assert idx == expected_idx, (
            "section 25 out of order at row %d: got %s (idx %d), expected idx %d"
            % (i, nak_kb, idx, expected_idx))
        assert int(pada_s) == expected_pada, (
            "section 25 pada mismatch at row %d: got P%s expected P%d" % (i, pada_s, expected_pada))
        assert sign in SIGNS and nav_sign in SIGNS, "bad sign in row %d: %s / %s" % (i, sign, nav_sign)
        out[(idx, expected_pada)] = {
            "nakshatra": NAKSHATRA_NAMES[idx],
            "kb_label": "%s P%s" % (nak_kb, pada_s),
            "sign": sign,
            "navamsa_sign": nav_sign,
            "navamsa_lord": PLANET_ALIASES[nav_lord],
            "sub_sector": sub.strip(),
            "names": split_list(names) if names else [],
        }
    return out


def parse_planet_table(kb, number, cols, key_field, value_fields):
    """Generic planet-keyed table: section 1, 14, 16, 20."""
    rows = table_rows(section(kb, number), cols)
    out = {}
    for cells in rows:
        name, native = strip_devanagari(cells[0])
        canonical = PLANET_ALIASES.get(name)
        if canonical is None:
            continue  # non-planet row (e.g. a trailing note)
        entry = {"native": native}
        for field, cell in zip(value_fields, cells[1:]):
            entry[field] = split_list(cell) if field.endswith("_list") else cell
        out[canonical] = entry
    assert len(out) >= 9, "section %s: expected >=9 planets, parsed %d" % (number, len(out))
    return out


def parse_sign_table(kb, number, cols, value_fields):
    """Generic sign-keyed table: section 2, 12, 17, 18."""
    rows = table_rows(section(kb, number), cols)
    out = {}
    for cells in rows:
        name, native = strip_devanagari(cells[0])
        if name not in SIGNS:
            continue
        entry = {"native": native}
        for field, cell in zip(value_fields, cells[1:]):
            entry[field] = split_list(cell) if field.endswith("_list") else cell
        out[name] = entry
    assert len(out) == 12, "section %s: expected 12 signs, parsed %d" % (number, len(out))
    return out


def parse_zones(kb):
    """Section 13: nakshatra -> Indian regional zone. Inverted to nakshatra_index -> zone."""
    rows = table_rows(section(kb, 13), 3)
    out = {}
    for zone, naks, regions in rows:
        for nak in split_list(naks):
            nak = nak.strip()
            # Section 13 spells two nakshatras differently again (Revathi, Moola).
            alias = {"Revathi": "Revati", "Moola": "Mula", "Uttara Phalguni": "U.Phalguni",
                     "Purva Phalguni": "P.Phalguni", "Purva Ashadha": "P.Ashadha",
                     "Uttara Ashadha": "U.Ashadha", "Purva Bhadrapada": "P.Bhadra",
                     "Uttara Bhadrapada": "U.Bhadra"}.get(nak, nak)
            idx = KB_NAKSHATRA_ALIASES.get(alias)
            assert idx is not None, "section 13: unknown nakshatra %r" % nak
            out[idx] = {"zone": zone, "regions": split_list(regions)}
    assert len(out) == 27, "section 13: expected 27 nakshatras mapped, got %d" % len(out)
    return out


def parse_decanates(kb):
    """Section 26: the three value-chain stages. Lord formula lives in chart.py."""
    rows = table_rows(section(kb, 26), 3)
    out = {}
    for dec, stage, character in rows:
        m = re.match(r"^(\d)", dec)
        if not m:
            continue
        out[int(m.group(1))] = {"stage": stage, "character": character}
    assert sorted(out) == [1, 2, 3], "section 26: expected decanates 1-3, got %s" % sorted(out)
    return out


def parse_bridge(kb):
    """Section 21: classical commodity -> modern instrument + exchange."""
    rows = table_rows(section(kb, 21), 3)
    out = {}
    for classical, instrument, exchange in rows:
        name, native = strip_devanagari(classical)
        out[name] = {"native": native, "instrument": instrument,
                     "exchanges": split_list(exchange)}
    assert len(out) >= 15, "section 21: expected >=15 rows, parsed %d" % len(out)
    return out


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------

def fmt(obj, indent=0):
    """Deterministic repr: sorted dict keys, one entry per line. Keeps diffs reviewable."""
    pad = "    " * indent
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        lines = ["{"]
        for k in sorted(obj, key=lambda x: (isinstance(x, tuple), x)):
            lines.append("%s    %r: %s," % (pad, k, fmt(obj[k], indent + 1)))
        lines.append(pad + "}")
        return "\n".join(lines)
    if isinstance(obj, list):
        return "[" + ", ".join(repr(v) for v in obj) + "]" if obj else "[]"
    return repr(obj)


def module_source(module, sec, assignments):
    parts = [GENERATED_HEADER.format(module=module, section=sec)]
    for name, value in assignments:
        parts.append("%s = %s\n" % (name, fmt(value)))
    return "\n".join(parts)


def build(kb):
    """Return {filename: source}. One module per KB concern."""
    files = {}

    files["padas.py"] = module_source(
        "padas.py", "25",
        [("PADA_SUBSECTOR", parse_padas(kb))])

    files["decanates.py"] = module_source(
        "decanates.py", "26",
        [("DECANATE_STAGE", parse_decanates(kb))])

    files["planets.py"] = module_source(
        "planets.py", "1, 14, 16, 20",
        [("PLANET_COMMODITY", parse_planet_table(
            kb, 1, 2, "planet", ["governs_list"])),
         ("PLANET_REGION", parse_planet_table(
             kb, 14, 2, "planet", ["regions_list"])),
         ("PLANET_MUNDANE", parse_planet_table(
             kb, 16, 2, "planet", ["significations"])),
         ("PLANET_SECTOR", parse_planet_table(
             kb, 20, 2, "planet", ["sectors_list"]))])

    files["signs.py"] = module_source(
        "signs.py", "2, 12, 17, 18",
        [("SIGN_COMMODITY", parse_sign_table(kb, 2, 2, ["commodities_list"])),
         ("SIGN_COUNTRY", parse_sign_table(kb, 12, 3, ["countries_list", "cities_list"])),
         ("SOLAR_ECLIPSE_EFFECT", parse_sign_table(kb, 17, 2, ["effects"])),
         ("LUNAR_ECLIPSE_EFFECT", parse_sign_table(kb, 18, 2, ["effects"]))])

    files["zones.py"] = module_source(
        "zones.py", "13",
        [("NAKSHATRA_ZONE", parse_zones(kb))])

    files["bridge.py"] = module_source(
        "bridge.py", "21",
        [("CLASSICAL_TO_MODERN", parse_bridge(kb))])

    return files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="fail if generated files differ from the KB (for CI)")
    args = ap.parse_args()

    kb = load_kb()
    files = build(kb)

    stale = []
    for name, src in sorted(files.items()):
        path = os.path.join(OUT_DIR, name)
        old = io.open(path, encoding="utf-8").read() if os.path.exists(path) else None
        if old == src:
            print("unchanged  kb/%s" % name)
            continue
        stale.append(name)
        if args.check:
            print("STALE      kb/%s" % name)
        else:
            io.open(path, "w", encoding="utf-8", newline="\n").write(src)
            print("wrote      kb/%s  (%d bytes)" % (name, len(src.encode("utf-8"))))

    if args.check and stale:
        print("\n%d generated module(s) out of date; run: python tools/extract_cosmic_kb.py"
              % len(stale))
        return 1
    print("\nOK - %d module(s) generated from KB" % len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
