"""
planets.py — GENERATED FILE. Do not edit by hand.

Source: KB section 1, 14, 16, 20 of COSMIC_PDF_AUGMENTATION_TEXT in agents/prompts/cosmic_prompts.py
Regenerate: python tools/extract_cosmic_kb.py

Content is verbatim from the knowledge base. To change a mapping, edit the KB and regenerate,
so the prompt text and the engine data can never disagree.
"""


PLANET_COMMODITY = {
    'Jupiter': {
        'governs_list': ['Yellow sapphire', 'turmeric', 'wax', 'yellow goods', 'wheat', 'barley', 'sugarcane', 'camphor', 'mustard'],
        'native': 'बृहस्पति',
    },
    'Ketu': {
        'governs_list': ['Same as Rahu plus bone', 'kanguni', 'til', 'urad'],
        'native': 'केतु',
    },
    'Mars': {
        'governs_list': ['Red-colored goods', 'jaggery (gur)', 'copper', 'arms', 'coral (moonga)', 'mercury metal', 'masoor dal'],
        'native': 'मंगल',
    },
    'Mercury': {
        'governs_list': ['Moong', 'peas', 'arhar', 'split pulses', 'vegetables', 'green items', 'oilseeds', 'emerald', 'almond', 'birds'],
        'native': 'बुध',
    },
    'Moon': {
        'governs_list': ['Silver', 'rice', 'wheat', 'barley', 'honey', 'fruits', 'flowers', 'pearl', 'sugarcane', 'conch', 'salt', 'liquids', 'dairy'],
        'native': 'चन्द्र',
    },
    'Rahu': {
        'governs_list': ['Gomed', 'lead', 'glass', 'iron', 'inferior rice', 'blue goods', 'pig', 'donkey'],
        'native': 'राहु',
    },
    'Saturn': {
        'governs_list': ['Blue sapphire', 'buffalo', 'gram', 'urad', 'til (sesame)', 'wool', 'leather', 'iron', 'black salt', 'blue goods'],
        'native': 'शनि',
    },
    'Sun': {
        'governs_list': ['Gold', 'copper', 'ruby', 'silk', 'leather', 'grain', 'currency', 'mustard', 'seeds', 'oily/smooth goods'],
        'native': 'सूर्य',
    },
    'Venus': {
        'governs_list': ['Diamond', 'precious stones', 'silk', 'fine cotton', 'jewelry', 'silver', 'cotton', 'dry fruits', 'spices'],
        'native': 'शुक्र',
    },
}

PLANET_REGION = {
    'Jupiter': {
        'native': 'बृहस्पति',
        'regions_list': ['Eastern Punjab', 'Western UP', 'Afghanistan', 'Kashmir', 'Pakistan'],
    },
    'Ketu': {
        'native': 'केतु',
        'regions_list': ['Kanchi', 'Thanjavur', 'Tiruchirappalli', 'Afghanistan', 'Iran', 'China'],
    },
    'Mars': {
        'native': 'मंगल',
        'regions_list': ['Andhra Pradesh', 'Konkan coast', 'Malaysia', 'Kerala'],
    },
    'Mercury': {
        'native': 'बुध',
        'regions_list': ['Himalayas', 'Northern & Western India'],
    },
    'Moon': {
        'native': 'चन्द्र',
        'regions_list': ['Kerala', 'Thanjavur', 'Tiruchirappalli'],
    },
    'Rahu': {
        'native': 'राहु',
        'regions_list': ['Hill/mountain regions', 'tribal areas', 'cave-dwelling communities'],
    },
    'Saturn': {
        'native': 'शनि',
        'regions_list': ['Rajasthan', 'Saurashtra', 'Thaneshwar', 'Punjab', 'Haryana', 'Delhi'],
    },
    'Sun': {
        'native': 'सूर्य',
        'regions_list': ['Bengal', 'Bihar', 'Orissa', 'Tamil Nadu', 'Burma', 'China'],
    },
    'Venus': {
        'native': 'शुक्र',
        'regions_list': ['Taxila', 'Gandhara', 'Kashmir', 'Pakistan'],
    },
}

PLANET_MUNDANE = {
    'Jupiter': {
        'native': 'बृहस्पति',
        'significations': 'National progress, industrial expansion, judiciary, charitable institutions, big government projects, revenue department, monasteries, foreign affairs, cabinet members, religious institutions',
    },
    'Ketu': {
        'native': 'केतु',
        'significations': 'Akin to Mars — maximum trouble to enemies in war, death in war, capture by enemies, suicidal tendencies, assassination, murder, poisoning, fire, sinful activities. Evil multiplied when conjunct Mars/Saturn/Uranus/Neptune/Pluto',
    },
    'Mars': {
        'native': 'मंगल',
        'significations': 'Violence, industrial accidents, mass murders, unnatural deaths, wars, fire accidents, political fanaticism, martial law, intelligence services, police, violent crimes, enemies of nation, emergencies, rebellions, robbery, chemistry',
    },
    'Mercury': {
        'native': 'बुध',
        'significations': 'Literature, scientific organizations, computer programs, business, textile industries, transportation, railroads, food supply chains, doctors, lawyers, reporters, public health issues',
    },
    'Moon': {
        'native': 'चन्द्र',
        'significations': 'Common people, women, essential commodities business, fishing, navigation, merchants, water-connected occupations, hotels & lodging, beverages, musicians',
    },
    'Neptune': {
        'native': 'नेपच्यून',
        'significations': 'Rebellion, revolution, strikes, assassination, conspiracies, sudden death, ammunition, bombs, aircraft, spaceships, missiles, electrocution, earthquakes, volcanic eruptions, storms, bomb explosions, secret societies, underground plotting, crimes, slavery, maritime disasters',
    },
    'Pluto': {
        'native': 'प्लूटो',
        'significations': 'Death-causing planet — loss of human life through global war, massacre, mass tragedies, annihilation, mass calamities, infectious diseases, nuclear/atomic explosions, espionage, socialistic political movements, dictators, unreliable authorities, group activities against national peace',
    },
    'Rahu': {
        'native': 'राहु',
        'significations': 'War & extreme violence (especially when conjunct Saturn/Uranus/Neptune/Pluto/Mars), political plots, banishment, foreigner troubles, litigation, inflammable gas incidents, gambling, mass social deviance',
    },
    'Saturn': {
        'native': 'शनि',
        'significations': 'Wars, political parties, intelligence services, violent criminal activities, drastic law changes, political fanaticism, rebellion, bloodshed, forgery, emergencies, diseases, land, buildings, minerals, farmers, miners, fourth-class laborers, national debts, overthrow of rulers, chaotic conditions, national prestige danger',
    },
    'Sun': {
        'native': 'सूर्य',
        'significations': 'Royal people, King/President/PM, heads of institutions, government officials, political leaders, religious leaders, top military officers, banks, stock exchanges, playgrounds, battle fields, military HQs',
    },
    'Uranus': {
        'native': 'युरेनस',
        'significations': 'Unforeseen revolution, sudden accidents, assassination of leaders at any time, dethroning, unexpected political agitation, volcanic eruptions. Planet of LIBERTY & FREEDOM — overthrows tyrants, dictators, and unruly ruling class',
    },
    'Venus': {
        'native': 'शुक्र',
        'significations': 'Community harmony, wealth circulation, public festivals, gardens, amusement parks, art & architecture, music, dance, fine arts. When afflicted by malefics → epidemic diseases, sexually transmitted diseases',
    },
}

PLANET_SECTOR = {
    'Jupiter': {
        'native': 'बृहस्पति',
        'sectors_list': ['Banking (SBI, HDFC, ICICI)', 'insurance', 'mutual funds', 'gold', 'education tech', 'legal', 'large-cap indices'],
    },
    'Ketu': {
        'native': 'केतु',
        'sectors_list': ["Pharma (Sun Pharma, Dr. Reddy's)", 'wellness', 'contrarian value plays', 'short-selling', 'VIX/volatility products'],
    },
    'Mars': {
        'native': 'मंगल',
        'sectors_list': ['Defense stocks', 'steel/iron companies', 'crude oil futures', 'real estate', 'pharma (surgical)', 'mining', 'auto'],
    },
    'Mercury': {
        'native': 'बुध',
        'sectors_list': ['IT services (TCS, Infosys)', 'telecom (Airtel, Jio)', 'media', 'logistics', 'fintech', 'e-commerce', 'currency derivatives'],
    },
    'Moon': {
        'native': 'चन्द्र',
        'sectors_list': ['FMCG', 'dairy', 'water utilities', 'hospitality', 'silver ETFs', 'consumer staples', 'maritime shipping'],
    },
    'Neptune': {
        'native': 'नेपच्यून',
        'sectors_list': ['Oil & gas', 'maritime shipping', 'chemicals', 'film/entertainment', 'virtual reality', 'water utilities'],
    },
    'Pluto': {
        'native': 'प्लूटो',
        'sectors_list': ['Nuclear energy', 'defense/weapons', 'private equity', 'restructuring plays', 'distressed assets'],
    },
    'Rahu': {
        'native': 'राहु',
        'sectors_list': ['Cryptocurrency', 'AI/tech disruption', 'biotech', 'speculative small-caps', 'derivatives', 'EVs', 'space tech'],
    },
    'Saturn': {
        'native': 'शनि',
        'sectors_list': ['Infrastructure (L&T, Adani)', 'coal', 'iron ore', 'real estate', 'construction', 'govt bonds', 'PSU banks', 'value stocks'],
    },
    'Sun': {
        'native': 'सूर्य',
        'sectors_list': ['Government securities', 'sovereign bonds', 'gold ETFs', 'PSU stocks', 'solar energy', 'leadership governance'],
    },
    'Uranus': {
        'native': 'युरेनस',
        'sectors_list': ['Disruptive tech', 'EVs', 'renewable energy', 'space tech', 'fintech disruptors', 'quantum computing'],
    },
    'Venus': {
        'native': 'शुक्र',
        'sectors_list': ['Luxury goods (Titan)', 'entertainment', 'tourism', 'textiles', 'cosmetics', 'FMCG premium', 'forex', 'diamond/jewelry'],
    },
}
