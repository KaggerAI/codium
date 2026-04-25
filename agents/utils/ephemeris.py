import os
import datetime
import math
import swisseph as swe

# Constants
ZODIAC_SIGNS = [
    "Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo",
    "Libra", "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces"
]

NAKSHATRAS = [
    "Ashwini", "Bharani", "Krittika", "Rohini", "Mrigashira", "Ardra",
    "Punarvasu", "Pushya", "Ashlesha", "Magha", "Purva Phalguni", "Uttara Phalguni",
    "Hasta", "Chitra", "Swati", "Vishakha", "Anuradha", "Jyeshtha",
    "Mula", "Purva Ashadha", "Uttara Ashadha", "Shravana", "Dhanishta", "Shatabhisha",
    "Purva Bhadrapada", "Uttara Bhadrapada", "Revati"
]

COMBUSTION_ORBS = {
    swe.MOON: 12.0,
    swe.MERCURY: 14.0,
    swe.VENUS: 10.0,
    swe.MARS: 17.0,
    swe.JUPITER: 11.0,
    swe.SATURN: 15.0
}

PLANETS = {
    "Sun": swe.SUN,
    "Moon": swe.MOON,
    "Mercury": swe.MERCURY,
    "Venus": swe.VENUS,
    "Mars": swe.MARS,
    "Jupiter": swe.JUPITER,
    "Saturn": swe.SATURN,
    "Rahu (True)": swe.TRUE_NODE,
    "Ketu (True)": swe.TRUE_NODE, # Calculated as Rahu + 180
    "Uranus": swe.URANUS,
    "Neptune": swe.NEPTUNE,
    "Pluto": swe.PLUTO
}

def get_julian_day(date_obj: datetime.datetime):
    # swisseph expects UTC time
    year, month, day = date_obj.year, date_obj.month, date_obj.day
    hour = date_obj.hour + date_obj.minute / 60.0 + date_obj.second / 3600.0
    return swe.julday(year, month, day, hour)

def get_zodiac_sign_and_degree(longitude):
    sign_index = int(longitude / 30.0)
    degree_in_sign = longitude % 30.0
    return ZODIAC_SIGNS[sign_index], degree_in_sign

def get_nakshatra_info(longitude):
    nak_index = int(longitude / (360.0 / 27.0))
    pada = int((longitude % (360.0 / 27.0)) / (360.0 / 108.0)) + 1
    return NAKSHATRAS[nak_index], pada

def get_tithi(sun_lon, moon_lon):
    diff = moon_lon - sun_lon
    if diff < 0:
        diff += 360.0
    tithi_val = diff / 12.0
    tithi_index = int(tithi_val) + 1
    paksha = "Shukla" if tithi_index <= 15 else "Krishna"
    tithi_name = tithi_index if tithi_index <= 15 else tithi_index - 15
    return f"{paksha} Paksha, Tithi {tithi_name} ({tithi_index}/30)"

def format_degree(deg):
    d = int(deg)
    m = int((deg - d) * 60)
    s = int((((deg - d) * 60) - m) * 60)
    return f"{d}°{m}'{s}\""

def generate_cosmic_data_report():
    # Set to Lahiri Ayanamsa
    swe.set_sid_mode(swe.SIDM_LAHIRI)
    
    now = datetime.datetime.utcnow()
    jd = get_julian_day(now)
    
    report = []
    report.append(f"ASTROLOGICAL DATA REPORT (Generated via Swiss Ephemeris)")
    report.append(f"Date/Time (UTC): {now.strftime('%Y-%m-%d %H:%M:%S')}")
    report.append(f"Ayanamsa: Chitrapaksha (Lahiri)")
    report.append("="*60 + "\n")
    
    report.append("1. CURRENT PLANETARY POSITIONS (SIDEREAL / Vedic):")
    
    sidereal_flags = swe.FLG_SWIEPH | swe.FLG_SIDEREAL | swe.FLG_SPEED
    tropical_flags = swe.FLG_SWIEPH | swe.FLG_SPEED
    
    planet_data = {}
    
    for name, p_id in PLANETS.items():
        if name == "Ketu (True)":
            # Ketu is exactly 180 degrees opposite to Rahu
            rahu_res = swe.calc_ut(jd, swe.TRUE_NODE, sidereal_flags)[0]
            sid_lon = (rahu_res[0] + 180.0) % 360.0
            sid_speed = rahu_res[3]
            
            rahu_trop = swe.calc_ut(jd, swe.TRUE_NODE, tropical_flags)[0]
            trop_lon = (rahu_trop[0] + 180.0) % 360.0
        else:
            sid_res = swe.calc_ut(jd, p_id, sidereal_flags)[0]
            sid_lon = sid_res[0]
            sid_speed = sid_res[3]
            
            trop_res = swe.calc_ut(jd, p_id, tropical_flags)[0]
            trop_lon = trop_res[0]
            
        sid_sign, sid_deg = get_zodiac_sign_and_degree(sid_lon)
        trop_sign, trop_deg = get_zodiac_sign_and_degree(trop_lon)
        nak_name, nak_pada = get_nakshatra_info(sid_lon)
        
        is_retrograde = sid_speed < 0 and name not in ["Sun", "Moon", "Rahu (True)", "Ketu (True)"]
        retro_str = " [RETROGRADE]" if is_retrograde else ""
        
        planet_data[name] = {
            "sid_lon": sid_lon,
            "speed": sid_speed,
            "is_retrograde": is_retrograde
        }
        
        report.append(f"- {name}:")
        report.append(f"  Sidereal: {sid_sign} {format_degree(sid_deg)} | Nakshatra: {nak_name} (Pada {nak_pada}){retro_str}")
        report.append(f"  Tropical: {trop_sign} {format_degree(trop_deg)}")
        
    report.append("\n2. ACTIVE RETROGRADES:")
    retro_planets = [p for p, d in planet_data.items() if d["is_retrograde"]]
    if retro_planets:
        report.append(f"Currently retrograde: {', '.join(retro_planets)}")
    else:
        report.append("No major planets are currently retrograde.")
        
    report.append("\n3. COMBUSTION STATUS:")
    sun_lon = planet_data["Sun"]["sid_lon"]
    combust_planets = []
    for p_name, orb in COMBUSTION_ORBS.items():
        name_str = "Moon" if p_name == swe.MOON else "Mercury" if p_name == swe.MERCURY else "Venus" if p_name == swe.VENUS else "Mars" if p_name == swe.MARS else "Jupiter" if p_name == swe.JUPITER else "Saturn"
        p_lon = planet_data[name_str]["sid_lon"]
        
        # Calculate shortest angular distance
        diff = abs(sun_lon - p_lon)
        if diff > 180.0:
            diff = 360.0 - diff
            
        if diff <= orb:
            combust_planets.append(f"{name_str} (within {format_degree(diff)} of Sun)")
            
    if combust_planets:
        report.append(f"Currently combust: {', '.join(combust_planets)}")
    else:
        report.append("No planets are currently combust.")
        
    report.append("\n4. VEDIC CALENDAR (PANCHANG):")
    report.append(f"- Tithi: {get_tithi(planet_data['Sun']['sid_lon'], planet_data['Moon']['sid_lon'])}")
    
    return "\n".join(report)

if __name__ == "__main__":
    print(generate_cosmic_data_report())
