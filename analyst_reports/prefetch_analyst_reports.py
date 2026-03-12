"""
prefetch_analyst_reports.py - Pre-fetch analyst reports from Trendlyne and save to JSON cache.

Run this script LOCALLY (where Trendlyne is accessible) to populate the cache.
Azure will then read from this cached JSON file instead of hitting Trendlyne directly.

Usage:
    python analyst_reports/prefetch_analyst_reports.py              # Fetch all configured stocks
    python analyst_reports/prefetch_analyst_reports.py --quick      # Fetch first 50 only (for testing)

The output file is: analyst_reports/analyst_reports_cache.json
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime

# Fix Windows console encoding for Unicode text from Trendlyne
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# Add the project root to sys.path so we can import the fetcher
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, SCRIPT_DIR)

# The cache file path (relative to project root)
CACHE_FILE = os.path.join(SCRIPT_DIR, "analyst_reports_cache.json")

# Top stocks to pre-fetch reports for
TOP_STOCKS = [
    "RELIANCE","TCS","HDFCBANK","BHARTIARTL","ICICIBANK","INFY","SBIN","ITC",
    "HINDUNILVR","LICI","HCLTECH","LT","SUNPHARMA","BAJFINANCE","NTPC","MARUTI",
    "TMPV","TMCV","ICICIAMC","AXISBANK","KOTAKBANK","M&M","ONGC","ADANIENT",
    "ULTRACEMCO","POWERGRID","TITAN","COALINDIA","ADANIPORTS","BAJAJ_AUTO","WIPRO",
    "ASIANPAINT","HAL","BAJAJFINSV","ADANIGREEN","TRENT","DMART","SIEMENS",
    "JSWSTEEL","ADANIPOWER","HINDZINC","ETERNAL","NESTLEIND","IOC","JIOFIN","DLF",
    "BEL","VBL","TATASTEEL","VEDL","IRFC","GRASIM","LTIM","INDIGO","SBILIFE",
    "ABB","PIDILITIND","HINDALCO","HDFCLIFE","TECHM","DIVISLAB","HYUNDAI","PFC",
    "TATAPOWER","BPCL","GAIL","AMBUJACEM","RECLTD","MOTHERSON","BRITANNIA",
    "GODREJCP","EICHERMOT","BANKBARODA","SHRIRAMFIN","CIPLA","TVSMOTOR","ADANIENSOL",
    "JSWENERGY","CHOLAFIN","PNB","BAJAJHLDNG","TORNTPHARM","CGPOWER","ICICIPRULI",
    "DRREDDY","BAJAJHFL","HAVELLS","BOSCHLTD","UNITDSPR","LODHA","HEROMOTOCO",
    "ZYDUSLIFE","MANKIND","APOLLOHOSP","NAUKRI","INDUSINDBK","TATACONSUM","POLYCAB",
    "IOB","LUPIN","ICICIGI","DABUR","INDHOTEL","HDFCAMC","SUZLON","SOLARINDS",
    "CUMMINSIND","TORNTPOWER","JINDALSTEL","OFSS","INDUSTOWER","RVNL","TIINDIA",
    "SHREECEM","DIXON","CANBK","MAXHEALTH","IDBI","COLPAL","PERSISTENT","GMRINFRA",
    "HINDPETRO","MAZDOCK","UNIONBANK","AUROPHARMA","ATGL","OIL","MARICO",
    "GODREJPROP","NHPC","BHEL","MUTHOOTFIN","POLICYBZR","BHARTIHEXA","PRESTIGE",
    "OBEROIRLTY","KALYANKJIL","ALKEM","LINDEINDIA","SBICARD","INDIANB","SRF",
    "BHARATFORG","IRCTC","PIIND","PATANJALI","ASHOKLEY","BERGEPAINT","NMDC","GICRE",
    "YESBANK","ABBOTINDIA","JSWINFRA","VOLTAS","MPHASIS","BSE","THERMAX",
    "POWERINDIA","SCHAEFFLER","BALKRISIND","IDEA","MOTILALOFS","LTTS","SUPREMEIND",
    "ABCAPITAL","JSL","PHOENIXLTD","UNOMINDA","FACT","IREDA","UCOBANK","PGHH","MRF",
    "UBL","LLOYDSME","SUNDARMFIN","COFORGE","TATACOMM","NYKAA","PETRONET",
    "IDFCFIRSTB","CONCOR","PAYTM","PAGEIND","SAIL","COROMANDEL","ASTRAL","AUBANK",
    "GLENMARK","FLUOROCHEM","FEDERALBNK","CENTRALBK","SONACOMS","BANKINDIA",
    "HONAUT","MFSL","GLAXO","AWL","FORTIS","SJVN","PREMIERENE","GVT&D","TATAELXSI",
    "NAM_INDIA","ACC","TATATECH","NATIONALUM","APLAPOLLO","HUDCO","UPL","IPCALAB",
    "EXIDEIND","JUBLFOOD","APARINDS","BIOCON","BDL","MAHABANK","ESCORTS",
    "BLUESTARCO","KPITTECH","3MINDIA","360ONE","COCHINSHIP","CRISIL","AJANTPHARM",
    "DEEPAKNTR","GUJGASLTD","AIAENG","LTF","KEI","SYNGENE","KAYNES","GODREJIND",
    "CHOLAHLDNG","DALBHARAT","TATAINVEST","MCX","NLCINDIA","PPLPHARMA","PSB",
    "ENDURANCE","GODFRYPHLP","OLAELEC","M&MFIN","LICHSGFIN","ABFRL","NIACL",
    "JKCEMENT","SUVENPHAR","STARHEALTH","BASF","IRB","METROBRAND","GODIGIT",
    "APOLLOTYRE","CDSL","RADICO","MANYAVAR","ABREL","KPRMILL","IGL","FIRSTCRY",
    "JBCHEPHARM","SUNDRMFAST","BANDHANBNK","SUNTV","BRIGADE","MEDANTA","HSCL",
    "BAYERCROP","WHIRLPOOL","AIIL","TATACHEM","HINDCOPPER","EMAMILTD","MSUMI",
    "POONAWALLA","INOXWIND","MRPL","DELHIVERY","ISEC","CARBORUNIV","GLAND",
    "GILLETTE","ZFCVINDIA","EMCURE","AEGISLOG","POLYMED","FIVESTAR","ANGELONE",
    "SUMICHEM","TVSHLTD","SKFINDIA","TIMKEN","CROMPTON","LALPATHLAB","NH","NBCC",
    "CESC","RATNAMANI","NUVAMA","PFIZER","KEC","PNBHOUSING","HATSUN","LAURUSLABS",
    "GRINDWELL","NATCOPHARM","PEL","ANANTRAJ","ARE&M","SHYAMMETL","EIHOTEL","FSL",
    "JYOTICNC","TEJASNET","ATUL","TRITURBINE","CAMS","ASTERDM","GSPL","KANSAINER",
    "ABSLAMC","ITI","AMBER","APLLTD","BIKAJI","KIMS","AFFLE","CASTROLIND","KIOCL",
    "JINDALSAW","JWL","VINATIORGA","DEVYANI","RAMCOCEM","ELGIEQUIP","KPIL",
    "SIGNATURE","CYIENT","BBTC","KAJARIACER","FINCABLES","JBMA","CENTURYPLY",
    "CIEINDIA","IRCON","CONCORDBIO","CHAMBLFERT","RELAXO","WELCORP","JAIBALAJI",
    "FINPIPE","BLUEDART","JYOTHYLAB","AARTIIND","VGUARD","CHALET","GRSE","CELLO",
    "NCC","RRKABEL","SCHNEIDER","TECHNOE","PTCIL","ASTRAZEN","GESHIP","AADHARHFC",
    "BATAINDIA","NEWGEN","KARURVYSYA","APTUS","JUBLPHARMA","NEULANDLAB","ERIS","LMW",
    "IIFL","ASAHIINDIA","WOCKPHARMA","RKFORGE","RPOWER","ANANDRATHI","NAVINFLUOR",
    "TBOTEK","AKZOINDIA","HFCL","SONATSOFTW","KFINTECH","TRIDENT","PCBL","IEX",
    "INDGN","SOBHA","SARDAEN","DCMSHRIRAM","BEML","BLS","CLEAN","ZENTEC","BSOFT",
    "DOMS","TITAGARH","CREDITACC","UTIAMC","KIRLOSENG","ZENSARTECH","PGEL","MGL",
    "GRINFRA","ACE","HBLPOWER","SWANENERGY","INDIAMART","CGCL","SANOFI","PVRINOX",
    "KSB","NETWEB","WELSPUNLIV","FINEORG","SPLPETRO","GODREJAGRO","TTML",
    "DEEPAKFERT","CAPLIPOINT","STAR","RAINBOW","GRAVITA","RITES","IFCI","RAYMONDLSL",
    "EIDPARRY","PRAJIND","IWEL","KIRLOSBROS","OLECTRA","INGERRAND","GRANULES",
    "AKUMS","HONASA","RAILTEL","SWSOLAR","NSLNISP","AAVAS","NAVA","ECLERX",
    "REDINGTON","JMFINANCIL","ELECON","VOLTAMP","GLS","MAHSCOOTER","CRAFTSMAN",
    "DATAPATTNS","JPPOWER","CUB","TARIL","MANAPPURAM","VTL","WESTLIFE","BALRAMCHIN",
    "MARKSANS","REDTAPE","LTFOODS","NUVOCO","USHAMART","GENUSPOWER","TEGA","ZEEL",
    "RHIM","ZYDUSWELL","MINDACORP","GPIL","HAPPSTMNDS","TTKPRESTIG","SYMPHONY",
    "MMTC","GMDCLTD","CEATLTD","IIFLSEC","CHENNPETRO","CANFINHOME","INDIACEM",
    "VESUVIUS","RELINFRA","SAFARI","MAPMYINDIA","SANOFICONR","PRUDENT","METROPOLIS",
    "JSWHL","INTELLECT","JUBLINGREA","ALOKINDS","RAYMOND","ALKYLAMINE","AETHER",
    "QUESS","JKTYRE","SAPPHIRE","EUREKAFORB","WABAG","GRAPHITE","TIMETECHNO",
    "PARADEEP","HEG","ITDCEM","SAMMAANCAP","JKLAKSHMI","BECTORFOOD","DBREALTY",
    "ENGINERSIN","INOXINDIA","HOMEFIRST","ARVIND","INDIASHLTR","GANESHHOUC",
    "PCJEWELLER","BIRLACORPN","ABDL","PGHL","AZAD","MEDPLUS","STARCEMENT",
    "GARFIBRES","ASKAUTOLTD","GMRP&UI","EDELWEISS","POWERMECH","KPIGREEN","TCI",
    "ANURAS","AURIONPRO","SHRIPISTON","TIPSMUSIC","GABRIEL","EPIGRAL","NAZARA",
    "TRIVENI","SYRMA","ISGEC","SCI","GALAXYSURF","CARTRADE","CMSINFO","SUDARSCHEM",
    "KIRLPNU","LATENTVIEW","SFL","RELIGARE","CCL","JINDWORLD","HAPPYFORGE",
    "GRWRHITECH","GSFC","CAMPUS","HCG","GNFC","BALUFORGE","PNGJL","BLACKBUCK",
    "SHAILY","CERA","AARTIPHARM","SANDUMA","HGINFRA","ORIENTCEM","AJAXENGG",
    "SANSERA","RCF","KTKBANK","JUSTDIAL","PRIVISCL","LLOYDSENGG","NETWORK18",
    "IIFLCAPS","EMUDHRA","UJJIVANSFB","SUNDARMHLD","ESABINDIA","DODLA","PNCINFRA",
    "WELENT","DBL","GPPL","CHEMPLASTS","PRSMJOHNSN","ANUP","TMB","KSCL","MOIL",
    "ACI","MASTEK","BORORENEW","SHILPAMED","NESCO","FDC","ASTRAMICRO","VARROC",
    "KRBL","TRANSRAILL","KNRCON","BBOX","THOMASCOOK","TANLA","RUSTOMJEE","V2RETAIL",
    "HNDFDS","LLOYDSENT","BAJAJELEC","SUPRIYA","EQUITASBNK","MAXESTATES","GOKEX",
    "VMART","SOUTHBANK","ELECTCAST","TDPOWERSYS","EPL","MANORAMA","RENUKA","MHRIL",
    "ETHOSLTD","ROUTE","SHOPERSTOP","PDSL","RTNINDIA","GHCL","GREENLAM","PURVA",
    "DHANUKA","RAJESHEXPO","IONEXCHANG","SUNTECK","BANSALWIRE","TIIL","THANGAMAYL",
    "PROTEAN","MANINFRA","AVL","PGIL","JUNIPER","CSBBANK","IXIGO","SURYAROSNI",
    "TEXRAIL","TVSSCS","ICIL","AHLUCONT","ASHOKA","JKPAPER","PRICOLLTD","GULFOILLUB",
    "RTNPOWER","NIITMTS","MIDHANI","RESPONIND","RATEGAIN","REFEX","AVALON","ICRA",
    "KRN","ARVINDFASN","IFBIND","JKIL","SUPRAJIT","WEBELSOLAR","HIKAL","ITDC",
    "INNOVACAP","ENTERO","DIACABS","PTC","LXCHEM","GAEL","BANCOINDIA","SKIPPER",
    "SKYGOLD","AGI","SENCO","GREAVESCOT","HCC","STYRENIX","UNIMECH","JCHAC","EMIL",
    "UNICHEMLAB","SPARC","MAHLIFE","INFIBEAM","SUNCLAY","PILANIINVS","GUJALKALI",
    "SIS","BANARISUG","RAIN","SWARAJENG","ZAGGLE","TI","AWFIS","SHARDACROP",
    "INDIGOPNTS","VSTIND","SUNFLAG","GMMPFAUDLR","MASFIN","HEIDELBERG","CEIGALL",
    "JSFB","SHARDAMOTR","UEL","INOXGREEN","BHARATRAS","YATHARTH","ORIENTELEC",
    "EASEMYTRIP","NEOGEN","DYNAMATECH","VRLLOG","LUXIND","WONDERLA","RALLIS",
    "MPSLTD","DBCORP","TARC","RBA","INDOSTAR","STLTECH","VADILALIND","BOROLTD",
    "BALAMINES","GANECOS","PITTIENG","MTARTECH","NFL","PARAS","JISLDVREQS",
    "IMAGICAA","ORCHPHARMA","JISLJALEQS","VIPIND","JASH","AXISCADES","TCPLPACK",
    "GOLDIAM","EIEL","GOCOLORS","POLYPLEX","BHAGCHEM","INDIAGLYCO","SUBROS",
    "VAIBHAVGBL","RSYSTEMS","OPTIEMUS","LGBBROSLTD","ARTEMISMED","HERITGFOOD",
    "RPGLIFE","THYROCARE","KITEX","KDDL","DATAMATICS","APOLLO","KINGFA","LUMAXTECH",
    "ASHAPURMIN","UFLEX","63MOONS","HEMIPROP","FIEMIND","NSIL","INDRAMEDCO",
    "DCBBANK","MSTCLTD","EPACK","GOKULAGRO","SHAREINDIA","SHANTIGEAR","EMSLIMITED",
    "DPABHUSHAN","KIRLOSIND","CIGNITITEC","MCLOUD","CYIENTDLM","ROLEXRINGS",
    "GREENPLY","AJMERA","SEQUENT","DHANI","DCAL","GUFICBIO","CARERATING","IMFA",
    "KIRIINDUS","ROSSARI","HARSHA","KSL","GOPAL","MEDIASSIST","JTEKTINDIA",
    "PATELENG","SAMHI","SIYSIL","BBL","ADVENZYMES","FEDFINA","BALMLAWRIE","RAMKY",
    "SANATHAN","JAYNECOIND","PARKHOTELS","CAMLINFINE","GLOBUSSPR","AARTIDRUGS",
    "DEEPINDS","PAISALO","VSTTILLERS","ARVSMART","GATEWAY","SGLTL","DALMIASUG",
    "JTLIND","PRAKASH","CAPACITE","ORISSAMINE","JINDALPOLY","NAVNETEDUL","JAMNAAUTO",
    "GUJTHEM","TEAMLEASE","NOCIL","SSWL","VISHNU","ARKADE","NORTHARC","ASHIANA",
    "ALLCARGO","POKARNA","WSTCSTPAPR","GIPCL","E2E","MTNL","MOSCHIP","BOMDYEING",
    "PRINCEPIPE","KKCL","MOREPENLAB","AVANTEL","RPSGVENT","SUVEN","DDEVPLSTIK",
    "KPEL","HUBTOWN","TAJGVK","PANACEABIO","PFOCUS","HPL","STYLAMIND","SJS",
    "GREENPANEL","BEPL","FCL","INTERARCH","ALEMBICLTD","TIRUMALCHM","WINDMACHIN",
    "NITCO","XPROINDIA","SERVOTECH","ADFFOODS","DCXINDIA","SINDHUTRAD","KCP",
    "UTKARSHBNK","MAITHANALL","GENESYS","SDBL","SENORES","MONARCH","MARATHON",
    "ORIENTHOT","SOTL","SBCL","SEAMECLTD","NACLIND","PENIND","VENUSPIPES","SAGCEM",
    "DIAMONDYD","KOLTEPATIL","FLAIR","KRSNAA","ASALCBR","LAOPALA","PSPPROJECT",
    "AUTOAXLES","TCIEXP","RPEL","JINDRILL","VEEDOL","GOODLUCK","QPOWER","SHK",
    "RAMRAT","STYLEBAAZA","INDOTECH","CENTUM","PRECWIRE","BAJAJHIND","MBAPL",
    "SANDHAR","VIMTALABS","LUMAXIND","MARINE","NILKAMAL","DCW","HATHWAY","SULA",
    "STOVEKRAFT","SEPC","MOBIKWIK","RELTD","SMLISUZU","SASKEN","INDOCO","PANAMAPET",
    "DELTACORP","VENKEYS","AEROFLEX","HINDOILEXP","BAJAJCON","EVEREADY","LAXMIDENTL",
    "SHALBY","CANTABIL","SANGHVIMOV","WEL","VIDHIING","SCILAL","DOLLAR","INDOTHAI",
    "HGS","FOSECOIND","QUADFUTURE","MUTHOOTMF","MANGLMCEM","PIXTRANS","EIHAHOTELS",
    "REPCOHOME","SIMPLEXINF","SUMMITSEC","SANGAMIND","TASTYBITE","SOLARA",
    "MANGCHEFER","PARAGMILK","NUCLEUS","ZOTA","MAYURUNIQ","NRBBEARING","WINDLAS",
    "VPRPL","HITECH","BAJAJHCARE","GEOJITFSL","INDIANHUME","RAMCOIND",
]


def load_existing_cache():
    """Load existing cache from disk if it exists."""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"WARN: Could not load existing cache: {e}")
    return {"last_updated": None, "reports": {}}


def save_cache(cache_data):
    """Save cache to disk atomically."""
    cache_data["last_updated"] = datetime.now().isoformat()
    # Write to temp file first, then rename (atomic on most OS)
    temp_file = CACHE_FILE + ".tmp"
    with open(temp_file, 'w', encoding='utf-8') as f:
        json.dump(cache_data, f, ensure_ascii=False, indent=2)
    os.replace(temp_file, CACHE_FILE)


async def prefetch_all(quick_mode=False):
    """Fetch analyst reports for all configured stocks and save to JSON."""
    # Import the fetcher here to avoid circular imports
    from trendlyne_fetcher import fetch_analyst_reports_async, load_url_mapping
    
    # Ensure URL mapping is loaded
    load_url_mapping()
    
    # Deduplicate and clean the stock list
    stocks = list(dict.fromkeys(TOP_STOCKS))  # Preserves order, removes dupes
    if quick_mode:
        stocks = stocks[:50]
    
    total = len(stocks)
    print(f"\n{'='*60}")
    print(f"  Pre-fetching analyst reports for {total} stocks")
    print(f"  Output: {CACHE_FILE}")
    print(f"  Mode: {'QUICK (first 50)' if quick_mode else 'FULL'}")
    print(f"{'='*60}\n")
    
    # Load existing cache to preserve previous results
    cache = load_existing_cache()
    
    success_count = 0
    empty_count = 0
    error_count = 0
    
    for i, ticker in enumerate(stocks, 1):
        try:
            print(f"[{i:4d}/{total}] Fetching {ticker:20s} ... ", end="", flush=True)
            
            # Fetch reports using the existing Playwright + Chrome + stealth approach
            # This calls the LIVE fetcher (bypasses our JSON cache check)
            reports = await fetch_analyst_reports_async(ticker, bypass_cache=True)
            
            if reports and len(reports) > 0:
                cache["reports"][ticker] = reports
                print(f"OK  {len(reports)} reports")
                success_count += 1
            else:
                # Keep old cache entry if we got nothing new
                if ticker not in cache["reports"]:
                    cache["reports"][ticker] = []
                print(f"--  0 reports")
                empty_count += 1
            
            # Save incrementally every 25 stocks (in case of crash)
            if i % 25 == 0:
                save_cache(cache)
                print(f"  [Saved checkpoint at {i}/{total}]")
            
            # Rate limiting: small delay between requests to be polite
            await asyncio.sleep(2)
            
        except Exception as e:
            print(f"ERR {e}")
            error_count += 1
            # Don't stop on individual errors, continue with next stock
            continue
    
    # Final save
    save_cache(cache)
    
    print(f"\n{'='*60}")
    print(f"  COMPLETE!")
    print(f"  Success: {success_count} | Empty: {empty_count} | Errors: {error_count}")
    print(f"  Total stocks in cache: {len(cache['reports'])}")
    print(f"  Cache file: {CACHE_FILE}")
    print(f"  Last updated: {cache['last_updated']}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    quick = "--quick" in sys.argv
    asyncio.run(prefetch_all(quick_mode=quick))
