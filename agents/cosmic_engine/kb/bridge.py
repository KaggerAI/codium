"""
bridge.py — GENERATED FILE. Do not edit by hand.

Source: KB section 21 of COSMIC_PDF_AUGMENTATION_TEXT in agents/prompts/cosmic_prompts.py
Regenerate: python tools/extract_cosmic_kb.py

Content is verbatim from the knowledge base. To change a mapping, edit the KB and regenerate,
so the prompt text and the engine data can never disagree.
"""


CLASSICAL_TO_MODERN = {
    'Copper': {
        'exchanges': ['MCX', 'LME'],
        'instrument': 'Copper futures',
        'native': 'ताम्बा',
    },
    'Cotton': {
        'exchanges': ['MCX', 'NCDEX'],
        'instrument': 'Cotton futures, textile stocks',
        'native': 'कपास',
    },
    'Crude Oil': {
        'exchanges': ['MCX', 'NYMEX'],
        'instrument': 'Crude oil futures',
        'native': 'तेल',
    },
    'Diamond/Precious Stones': {
        'exchanges': ['BSE/NSE'],
        'instrument': 'Titan, Kalyan Jewellers, PC Jeweller',
        'native': '',
    },
    'Gold': {
        'exchanges': ['MCX', 'COMEX'],
        'instrument': 'Gold futures, Gold ETFs (GLD, GOLDBEES)',
        'native': 'सोना',
    },
    'Gram/Chana': {
        'exchanges': ['NCDEX'],
        'instrument': 'Chana futures',
        'native': 'चना',
    },
    'Iron': {
        'exchanges': ['MCX', 'BSE'],
        'instrument': 'Steel futures, Tata Steel, JSW Steel',
        'native': 'लोहा',
    },
    'Mustard/Oilseeds': {
        'exchanges': ['NCDEX'],
        'instrument': 'Mustard oil futures, edible oil companies',
        'native': 'सरसों',
    },
    'Natural Gas': {
        'exchanges': ['MCX', 'NYMEX'],
        'instrument': 'Natural gas futures',
        'native': '',
    },
    'Rice': {
        'exchanges': ['NCDEX'],
        'instrument': 'Rice futures, agri stocks',
        'native': 'चावल',
    },
    'Silk/Textiles': {
        'exchanges': ['BSE/NSE'],
        'instrument': 'Textile stocks (Page Industries, Arvind)',
        'native': 'रेशम',
    },
    'Silver': {
        'exchanges': ['MCX', 'COMEX'],
        'instrument': 'Silver futures, Silver ETFs',
        'native': 'चांदी',
    },
    'Sugar/Gur': {
        'exchanges': ['NCDEX', 'MCX'],
        'instrument': 'Sugar futures, Balrampur Chini, Dhampur Sugar',
        'native': 'गुड़',
    },
    'Wheat': {
        'exchanges': ['NCDEX', 'CBOT'],
        'instrument': 'Wheat futures',
        'native': 'गेहूँ',
    },
    'Wool/Leather': {
        'exchanges': ['BSE/NSE'],
        'instrument': 'Leather export stocks, Relaxo, Bata',
        'native': '',
    },
}
