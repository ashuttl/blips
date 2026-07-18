#!/usr/bin/env python3
"""Build src/blips/data/schedules.json.gz from open Wikipedia route tables.

For a curated set of airports, fetch each field's Wikipedia "Airlines and
destinations" table and distill it into a compact, vendored spawn profile:
real operator x real aircraft type x real far-end airport.  The game reads
this to spawn genuine flights — an arrival "from Charleston" is a route
Breeze actually flies out of Portland, on metal Breeze actually owns.

The data is open and slow-moving (a route map changes over years, not
minutes), so this is a build-time tool: run it once every few years and
commit the gzip.  No API key, no scraping of a blocked site — just the one
open MediaWiki table per airport, fetched gently.

Output — a gzipped JSON object keyed by ICAO, each a list of weighted route
tuples usable for BOTH arrivals (from the far end) and departures (to it):

    {"KPWM": {"routes": [["MXY", "A223", "CHS", 3],
                         ["RPA", "E175", "LGA", 4], ...]}, ...}

    route = [callsign_prefix, actype, far_end, weight]
      callsign_prefix : ICAO 3-letter code of the OPERATING carrier
      actype          : an aircraft type code present in blips PERF
      far_end         : the other airport's IATA when resolvable, else a
                        cleaned display-city string
      weight          : 3 year-round, 1 seasonal (dedup keeps year-round)

Usage:  PYTHONPATH=src uv run python tools/build_schedules.py
"""

import csv
import gzip
import json
import re
import sys
import time
import urllib.parse
import urllib.request

from blips._airports import _load, find_airport
from blips._commands import TELEPHONY
from blips._geo import haversine_nm
from blips._sim import FLEETS, PERF

UA = ("blips-schedule-builder/1.0 "
      "(https://github.com/ashuttl/blips; build-time route distiller)")
SLEEP_S = 0.3                       # gentle on Wikipedia between requests
OUT = "src/blips/data/schedules.json.gz"
OPENFLIGHTS_URL = ("https://raw.githubusercontent.com/jpatokal/openflights/"
                   "master/data/airlines.dat")

# Airports to build.  Easy to extend — anything find_airport resolves that
# also has a Wikipedia route table gets a profile; the rest are logged.
AIRPORTS = [
    # US
    "KPWM", "KBOS", "KJFK", "KLGA", "KEWR", "KDCA", "KIAD", "KBWI", "KPHL",
    "KATL", "KCLT", "KORD", "KDFW", "KDEN", "KLAX", "KSFO", "KSEA", "KLAS",
    "KMCO", "KMIA", "KTPA", "KMSP", "KDTW", "KSAN", "KSLC", "KBNA", "KAUS",
    # International
    "CYYZ", "CYVR", "EGLL", "EGKK", "EHAM", "LFPG", "EDDF", "LEMD", "LIRF",
    "LSZH", "LTFM", "OMDB", "OTHH", "VHHH", "RJTT", "RJAA", "WSSS", "YSSY",
    "NZAA", "CYUL", "SBGR", "MMMX", "FAOR", "HECA", "DNMM", "HKJK",
]


# ---------------------------------------------------------------------------
# Wikipedia fetch + parse  (parser logic proven in the KPWM/Lagos prototype)
# ---------------------------------------------------------------------------
def _wiki_get(params):
    time.sleep(SLEEP_S)
    url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def find_section(page, want):
    d = _wiki_get({"action": "parse", "page": page, "prop": "sections",
                   "format": "json", "redirects": 1})
    for s in d.get("parse", {}).get("sections", []):
        if want.lower() in s["line"].lower():
            return s["index"]
    return None


def wiki_full_wikitext(page):
    """Whole-article wikitext — so a route table nested under a Passenger or
    Cargo subsection (as the big hubs do it) is still in view."""
    d = _wiki_get({"action": "parse", "page": page, "prop": "wikitext",
                   "format": "json", "redirects": 1})
    return d["parse"]["wikitext"]["*"]


def wiki_search(query, limit=4):
    d = _wiki_get({"action": "query", "list": "search", "srsearch": query,
                   "srlimit": limit, "format": "json"})
    return [hit["title"] for hit in d.get("query", {}).get("search", [])]


def resolve_page(ap):
    """Wikipedia page title carrying an airport's route table, or None.

    Tries the airport's formal name first (usually the article title, and
    parse follows redirects), then a Wikipedia search — accepting the first
    candidate whose article has an "Airlines and destinations" section.
    """
    tried = set()
    candidates = [ap["name"]]
    if ap["city"]:
        candidates.append(f"{ap['city']} Airport")
    candidates += wiki_search(f"{ap['name']} airlines destinations")
    for title in candidates:
        key = title.lower()
        if key in tried:
            continue
        tried.add(key)
        try:
            idx = find_section(title, "Airlines and destinations")
        except Exception:
            continue
        if idx is not None:
            return title
    return None


def strip_refs(t):
    t = re.sub(r"<ref[^>]*/>", "", t)
    t = re.sub(r"<ref[^>]*>.*?</ref>", "", t, flags=re.S)
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    t = re.sub(r"\{\{nowrap\|(.*?)\}\}", r"\1", t)
    return t


def split_top(s, sep="|"):
    """Split on ``sep`` at bracket/brace depth 0."""
    out, depth, cur, i = [], 0, "", 0
    while i < len(s):
        two = s[i:i + 2]
        if two in ("[[", "{{"):
            depth += 1; cur += two; i += 2; continue
        if two in ("]]", "}}"):
            depth -= 1; cur += two; i += 2; continue
        if s[i] == sep and depth == 0:
            out.append(cur); cur = ""; i += 1; continue
        cur += s[i]; i += 1
    out.append(cur)
    return out


def link_parts(link):
    """[[Formal|Display]] -> ('Formal', 'Display').  Plain text -> (t, t).

    The display is the human label (a city); the formal target is the
    article name (usually the full airport name), which is what we match
    against the airport database to recover an IATA code.
    """
    m = re.search(r"\[\[([^\]]+)\]\]", link)
    if not m:
        txt = _clean(link)
        return txt, txt
    inner = m.group(1)
    if "|" in inner:
        formal, display = inner.split("|", 1)
    else:
        formal = display = inner
    return _clean(formal), _clean(display)


def _clean(name):
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name)     # drop trailing (SC)
    name = name.replace("[[", "").replace("]]", "")
    return name.strip()


_SEASONAL = re.compile(r"'''\s*Seasonal[^']*?'''")
# the route-table template shows up under two spellings across articles
_DEST_TEMPLATE = re.compile(r"\{\{\s*Airport[- ]?dest(?:ination)?[- ]?list",
                            re.IGNORECASE)


def _cities(seg):
    for br in ("<br />", "<br/>", "<br>"):
        seg = seg.replace(br, " ")
    return [link_parts(piece) for piece in split_top(seg, ",")
            if "[[" in piece]


def parse_dest_table(wikitext):
    """{airline_display: {"year": [(formal, display), ...], "seasonal": [...]}}.

    Parses every {{Airport destination list}} block on the page (a hub has
    separate passenger and cargo tables) and merges them.  Rows are
    comment-delimited inside the template; split on the row markers BEFORE
    stripping comments so we don't lose them.  The seasonal marker comes in
    variants ('''Seasonal:''', '''Seasonal charter:'''), so match loosely.
    """
    rows = {}
    blocks = _DEST_TEMPLATE.split(wikitext)
    for block in blocks[1:]:
        # rows are delimited by a "blank" HTML comment whose spelling varies
        # by article (<!-- -->, <!--+-->, <!--*-->) — split on those, but not
        # on editorial comments like <!--DO NOT ADD...-->
        for ch in re.split(r"<!--[\s+*.\-]*-->", block):
            ch = strip_refs(ch).strip()
            if not ch.startswith("|"):
                continue
            parts = [p.strip() for p in split_top(ch) if p.strip()]
            if len(parts) < 2 or "[[" not in parts[0]:
                continue
            airline = link_parts(parts[0])[1]
            seg = _SEASONAL.split(parts[1], maxsplit=1)
            year, seasonal = seg[0], (seg[1] if len(seg) > 1 else "")
            entry = rows.setdefault(airline, {"year": [], "seasonal": []})
            entry["year"] += _cities(year)
            entry["seasonal"] += _cities(seasonal)
    return rows


# ---------------------------------------------------------------------------
# Operator name -> operating-carrier ICAO callsign prefix
# ---------------------------------------------------------------------------
def _norm(name):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", name.lower())).strip()


# Source 1+2: curated feed-brand / marketing map and a hand map of full
# airline names -> operating carrier, covering the carriers the game knows.
# Regional feed brands map to the carrier that actually flies the metal, so
# an "American Eagle" arrival checks in as "Brickyard", not "American".
_CURATED_RAW = {
    # US mainline
    "American Airlines": "AAL", "Delta Air Lines": "DAL",
    "United Airlines": "UAL", "Southwest Airlines": "SWA",
    "JetBlue": "JBU", "JetBlue Airways": "JBU", "Alaska Airlines": "ASA",
    "Frontier Airlines": "FFT", "Allegiant Air": "AAY", "Allegiant": "AAY",
    "Hawaiian Airlines": "HAL", "Sun Country Airlines": "SCX",
    "Breeze Airways": "MXY", "Avelo Airlines": "VXP", "JSX": "JSX",
    "Spirit Airlines": "FFT",   # no NKS in-game; Frontier is the closest ULCC
    # US feed brands -> operating regional carrier
    "American Eagle": "RPA", "Delta Connection": "EDV",
    "United Express": "RPA", "US Airways Express": "RPA",
    "Alaska Horizon": "QXE", "Horizon Air": "QXE",
    "Contour Airlines": "VTE", "Contour": "VTE",
    "SkyWest Airlines": "SKW", "Republic Airways": "RPA",
    "Endeavor Air": "EDV", "Envoy Air": "ENY", "PSA Airlines": "JIA",
    "Piedmont Airlines": "PDT", "Air Wisconsin": "AWI",
    "GoJet Airlines": "GJS", "CommutAir": "UCA",
    # US cargo
    "FedEx Express": "FDX", "FedEx": "FDX", "Federal Express": "FDX",
    "UPS Airlines": "UPS", "United Parcel Service": "UPS", "Atlas Air": "GTI",
    "ABX Air": "ABX", "Cargojet": "CJT", "Cargojet Airways": "CJT",
    "Air Transport International": "ATN",
    # bizjet
    "NetJets": "EJA", "Flexjet": "LXJ",
    # Canada + feed
    "Air Canada": "ACA", "Air Canada Express": "JZA",
    "Air Canada Rouge": "ROU", "Air Canada Jazz": "JZA", "Jazz": "JZA",
    "WestJet": "WJA", "WestJet Encore": "WJA", "Porter Airlines": "POE",
    "Flair Airlines": "FLE", "Air Transat": "TSC",
    # Latin America
    "Aeromexico": "AMX", "Aeroméxico": "AMX", "Aeromexico Connect": "AMX",
    "Volaris": "VOI", "Viva Aerobus": "VIV", "VivaAerobus": "VIV",
    "Copa Airlines": "CMP", "Avianca": "AVA", "Gol": "GLO",
    "Gol Linhas Aereas": "GLO", "Gol Transportes Aereos": "GLO",
    "LATAM": "LAN", "LATAM Airlines": "LAN", "LATAM Brasil": "LAN",
    "LATAM Chile": "LAN", "LAN Airlines": "LAN",
    "Azul": "AZU", "Azul Brazilian Airlines": "AZU",
    "Azul Linhas Aereas Brasileiras": "AZU",
    # UK / Europe legacy
    "British Airways": "BAW", "Virgin Atlantic": "VIR", "Lufthansa": "DLH",
    "Air France": "AFR", "KLM": "KLM", "KLM Royal Dutch Airlines": "KLM",
    "Iberia": "IBE", "TAP Air Portugal": "TAP", "TAP Portugal": "TAP",
    "Scandinavian Airlines": "SAS", "SAS": "SAS", "Finnair": "FIN",
    "Swiss International Air Lines": "SWR", "Swiss": "SWR",
    "Austrian Airlines": "AUA", "Brussels Airlines": "BEL",
    "Aer Lingus": "EIN", "Icelandair": "ICE", "LOT Polish Airlines": "LOT",
    "LOT": "LOT", "Vueling": "VLG", "ITA Airways": "ITY", "Alitalia": "ITY",
    "Air Europa": "AEA", "Aegean Airlines": "AEE", "Aegean": "AEE",
    "Eurowings": "EWG", "Condor": "CFG", "Transavia": "TRA",
    "Transavia France": "TRA",
    # Europe LCC
    "Ryanair": "RYR", "easyJet": "EZY", "EasyJet": "EZY", "Wizz Air": "WZZ",
    "Wizz Air Malta": "WZZ", "Pegasus Airlines": "PGT", "Pegasus": "PGT",
    "Norwegian": "NAX", "Norwegian Air Shuttle": "NAX",
    "Norse Atlantic Airways": "NBT",
    # Middle East / Turkey / Israel
    "Turkish Airlines": "THY", "El Al": "ELY", "Emirates": "UAE",
    "Qatar Airways": "QTR", "Etihad Airways": "ETD", "Etihad": "ETD",
    "Saudia": "SVA", "Saudi Arabian Airlines": "SVA",
    # East Asia
    "All Nippon Airways": "ANA", "Japan Airlines": "JAL", "Korean Air": "KAL",
    "Asiana Airlines": "AAR", "Cathay Pacific": "CPA", "Air China": "CCA",
    "China Eastern Airlines": "CES", "China Eastern": "CES",
    "China Southern Airlines": "CSN", "China Southern": "CSN",
    "China Airlines": "CAL", "EVA Air": "EVA",
    # SE / South Asia / Oceania
    "Singapore Airlines": "SIA", "Malaysia Airlines": "MAS",
    "Thai Airways": "THA", "Thai Airways International": "THA",
    "Air India": "AIC", "IndiGo": "IGO", "Qantas": "QFA", "QantasLink": "QFA",
    "Air New Zealand": "ANZ", "Jetstar": "JST", "Jetstar Airways": "JST",
    "Fiji Airways": "FJI", "Vietnam Airlines": "HVN",
    "Philippine Airlines": "PAL", "Garuda Indonesia": "GIA",
    "VietJet Air": "VJC", "VietJet": "VJC", "AirAsia": "AXM",
    "Cebu Pacific": "CEB", "Scoot": "TGW", "Akasa Air": "AKJ",
    # Africa
    "Ethiopian Airlines": "ETH", "Kenya Airways": "KQA", "RwandAir": "RWD",
    "EgyptAir": "MSR", "Royal Air Maroc": "RAM", "Air Peace": "APK",
    "South African Airways": "RWD",   # no SAA in-game; a regional stand-in
}
CURATED = {_norm(k): v for k, v in _CURATED_RAW.items()}

# Source 2 (continued): reverse of the game's TELEPHONY — many spoken names
# ARE the airline name on Wikipedia (Iberia, Finnair, Qantas, Emirates...).
TELE_REV = {_norm(name): pfx for pfx, name in TELEPHONY.items()}


def load_openflights(path=None):
    """Source 3: OpenFlights airlines.dat -> {norm(name): ICAO}, active only."""
    if path is None:
        req = urllib.request.Request(OPENFLIGHTS_URL,
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=40) as r:
            text = r.read().decode("utf-8", "replace")
        lines = text.splitlines()
    else:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    out = {}
    for row in csv.reader(lines):
        if len(row) < 8:
            continue
        _id, name, alias, _iata, icao, _call, _country, active = row[:8]
        if active != "Y" or len(icao) != 3 or not icao.isalpha():
            continue
        for nm in (name, alias):
            k = _norm(nm)
            if k and k not in out:      # first active carrier wins
                out[k] = icao.upper()
    return out


def make_resolver(openflights):
    """name -> ICAO prefix, curated/telephony over OpenFlights on conflict."""
    def resolve(name):
        k = _norm(name)
        if not k:
            return None
        return CURATED.get(k) or TELE_REV.get(k) or openflights.get(k)
    return resolve


# ---------------------------------------------------------------------------
# Destination display -> IATA (or cleaned city string)
# ---------------------------------------------------------------------------
# words that carry no identity — stripped before matching so a link and a
# database name align on their DISTINCTIVE tokens.  (Raw substring matching
# is a trap: "Nal Airport" is a substring of "internatioNAL AiRPORT".)
_GENERIC_TOKENS = frozenset((
    "international", "airport", "regional", "municipal", "intl", "airfield",
    "aerodrome", "field", "airpark", "metropolitan", "county",
))


def _tokens(name):
    return frozenset(t for t in _norm(name).split()
                     if t not in _GENERIC_TOKENS)


_AIRPORTS = _load()
_NAME_INDEX = {}                       # norm(name) -> record (large preferred)
_AP_TOKENS = []                        # [(distinctive-token set, record)]
for _ap in _AIRPORTS:
    _k = _norm(_ap["name"])
    if _k and (_k not in _NAME_INDEX or
               (_ap["large"] and not _NAME_INDEX[_k]["large"])):
        _NAME_INDEX[_k] = _ap
    _AP_TOKENS.append((_tokens(_ap["name"]), _ap))


def match_airport(formal):
    """Airport record for a formal airport-article name, or None.

    Exact normalized name first; then a distinctive-token match — one name's
    identity tokens a subset of the other's (so "O'Hare" finds "Chicago
    O'Hare"), or a strong two-plus-token overlap.  Ties break toward the
    most prominent airport, so a bare city lands at its major field.
    """
    nf = _norm(formal)
    if not nf:
        return None
    hit = _NAME_INDEX.get(nf)
    if hit:
        return hit
    ft = _tokens(formal)
    if not ft:
        return None
    best = best_key = None
    for at, ap in _AP_TOKENS:
        if at and (at <= ft or ft <= at):
            overlap = len(at & ft)
            key = (overlap, ap["large"], ap["rwys"][0]["len"])
            if overlap and (best is None or key > best_key):
                best, best_key = ap, key
    if best is not None:
        return best
    for at, ap in _AP_TOKENS:           # looser: strong distinctive overlap
        overlap = len(at & ft)
        if overlap >= 2:
            key = (overlap, ap["large"], ap["rwys"][0]["len"])
            if best is None or key > best_key:
                best, best_key = ap, key
    return best


# bare "City–Airport" labels (no piped formal name) for the handful of
# multi-airport cities whose distinctive token can't pick the right field.
_CITY_ALIAS = {_norm(k): v for k, v in {
    "Houston–Intercontinental": "IAH", "Houston–Hobby": "HOU",
    "Chicago–O'Hare": "ORD", "Chicago–Midway": "MDW",
    "Washington–National": "DCA", "Washington–Reagan": "DCA",
    "Washington–Reagan National": "DCA", "Washington–Dulles": "IAD",
    "New York–JFK": "JFK", "New York–Kennedy": "JFK",
    "New York–LaGuardia": "LGA", "New York–Newark": "EWR", "Newark": "EWR",
    "Dallas–Fort Worth": "DFW", "Dallas/Fort Worth": "DFW",
    "Dallas–Love": "DAL", "Dallas–Love Field": "DAL",
}.items()}


def resolve_dest(formal, display, home):
    """(far_end_str, far_airport_record|None) for one destination link.

    A CONFIDENT airport match yields the IATA and coordinates (which the
    type inference needs); a bare "City–Suffix" label resolves through a
    small alias table.  With no confident match we keep the cleaned display
    string — a readable city beats a wrong airport code.
    """
    ap = match_airport(formal) or match_airport(display)
    if ap is None:
        iata = _CITY_ALIAS.get(_norm(display)) or _CITY_ALIAS.get(_norm(formal))
        if iata:
            ap = find_airport(iata)
    if ap and ap["iata"] and ap["icao"] != home["icao"]:
        return ap["iata"], ap
    return display, None


# ---------------------------------------------------------------------------
# Aircraft type inference by great-circle distance
# ---------------------------------------------------------------------------
REGIONAL = {"E175", "E190", "E290", "CRJ9", "CRJ7", "DH8D", "AT76"}
NARROW = {"B738", "B739", "A320", "A321", "A20N", "B752", "A223"}
WIDE = {"B763", "B77W", "A388", "B788", "A359", "A339"}
DEFAULT_REGIONAL = ("E175", "CRJ9")
DEFAULT_NARROW = ("B738", "A320", "A321", "A20N")
DEFAULT_WIDE = ("B788", "A359")


def _stable_pick(seq, key):
    """Deterministic pick from a sequence — varied per route, no randomness."""
    seq = list(seq)
    if not seq:
        return None
    h = 0
    for c in key:
        h = (h * 131 + ord(c)) & 0xFFFFFFFF
    return seq[h % len(seq)]


def infer_actype(prefix, dist_nm, far_end):
    """A PERF type code plausible for this carrier on a route this long."""
    dist = 1500.0 if dist_nm is None else dist_nm
    if prefix in FLEETS:
        fleet = FLEETS[prefix]
        if dist < 900:
            cands = [t for t in fleet if t in REGIONAL or t in NARROW]
        elif dist > 2500:
            cands = ([t for t in fleet if t in WIDE]
                     or [t for t in fleet if t in NARROW])
        else:
            cands = ([t for t in fleet if t in NARROW]
                     or [t for t in fleet if t in REGIONAL])
        cands = cands or list(fleet)
    elif dist < 600:
        cands = DEFAULT_REGIONAL
    elif dist < 2500:
        cands = DEFAULT_NARROW
    else:
        cands = DEFAULT_WIDE
    pick = _stable_pick(cands, prefix + far_end)
    return pick if pick in PERF else "A320"


# ---------------------------------------------------------------------------
# Per-airport profile
# ---------------------------------------------------------------------------
def build_profile(home, rows, resolve, unresolved):
    """Distill parsed rows into a deduped list of weighted route tuples."""
    best = {}                          # (prefix, far_end) -> [prefix, ac, fe, w]
    for airline, d in rows.items():
        prefix = resolve(airline)
        if prefix is None:
            unresolved[airline] = unresolved.get(airline, 0) + 1
            continue
        for weight, dests in ((3, d["year"]), (1, d["seasonal"])):
            for formal, display in dests:
                far_end, far_ap = resolve_dest(formal, display, home)
                if not far_end or far_end == home["iata"]:
                    continue
                dist = (haversine_nm(home["lat"], home["lon"],
                                     far_ap["lat"], far_ap["lon"])
                        if far_ap else None)
                actype = infer_actype(prefix, dist, far_end)
                key = (prefix, far_end)
                cur = best.get(key)
                if cur is None or weight > cur[3]:
                    best[key] = [prefix, actype, far_end, weight]
    routes = sorted(best.values(),
                    key=lambda r: (r[0], r[2], r[1], -r[3]))
    return routes


def main():
    print("fetching OpenFlights airlines.dat ...", file=sys.stderr)
    openflights = load_openflights()
    resolve = make_resolver(openflights)
    print(f"  {len(openflights)} active carriers", file=sys.stderr)

    schedules = {}
    unresolved = {}
    skipped = []
    for code in AIRPORTS:
        ap = find_airport(code)
        if ap is None:
            skipped.append((code, "airport not in DB"))
            print(f"SKIP {code}: not in airport DB", file=sys.stderr)
            continue
        try:
            page = resolve_page(ap)
            if page is None:
                skipped.append((ap["icao"], "no route table"))
                print(f"SKIP {ap['icao']} ({ap['name']}): "
                      "no Airlines-and-destinations table", file=sys.stderr)
                continue
            wikitext = wiki_full_wikitext(page)
            rows = parse_dest_table(wikitext)
            routes = build_profile(ap, rows, resolve, unresolved)
        except Exception as exc:       # one bad field never aborts the run
            skipped.append((ap["icao"], repr(exc)))
            print(f"SKIP {ap['icao']} ({ap['name']}): {exc!r}",
                  file=sys.stderr)
            continue
        if not routes:
            skipped.append((ap["icao"], "no routes resolved"))
            print(f"SKIP {ap['icao']} ({ap['name']}): no routes resolved",
                  file=sys.stderr)
            continue
        schedules[ap["icao"]] = {"routes": routes}
        print(f"  {ap['icao']} ({page}): {len(routes)} routes",
              file=sys.stderr)

    payload = json.dumps(schedules, separators=(",", ":"), sort_keys=True)
    with gzip.open(OUT, "wt", encoding="utf-8", compresslevel=9) as fh:
        fh.write(payload)

    total_routes = sum(len(v["routes"]) for v in schedules.values())
    print("\n" + "=" * 66)
    print(f"built {len(schedules)} airports, {total_routes} routes -> {OUT}")
    print(f"skipped {len(skipped)} airports; "
          f"{len(unresolved)} distinct operators unresolved")
    if unresolved:
        print("\nunresolved operators (name x times seen):")
        for name, n in sorted(unresolved.items(), key=lambda kv: -kv[1]):
            print(f"  {n:3d}  {name}")
    for code in ("KPWM", "DNMM"):
        prof = schedules.get(code)
        print(f"\n--- {code} profile "
              f"({len(prof['routes']) if prof else 0} routes) ---")
        if prof:
            for r in prof["routes"]:
                print(f"  {r}")


if __name__ == "__main__":
    main()
