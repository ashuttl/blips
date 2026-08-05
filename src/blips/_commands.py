"""The game's command language: terse in, phraseology out.

A transmission is a callsign followed by chained instructions:

    rpa5655 l 230 c 240 s 210
    → Turn left heading two three zero, climb and maintain flight level
      two four zero, reduce speed two one zero, Brickyard 5655.

``parse()`` turns the text into instruction dicts without needing any
aircraft state; ``resolve_callsign()`` matches the (possibly abbreviated)
callsign against the traffic.  The sim words the readback itself, once it
has applied the instructions and knows which way is up.

Altitudes are typed in hundreds of feet, exactly as the data blocks show
them: ``c 40`` is 4,000 ft, ``d 240`` is FL240.  Direction is always the
player's to give: turns are ``l`` or ``r`` (no shortest-way shortcut),
climbs and descents must use the correct verb, speed is ``rs``/``is`` —
holding the picture is the game, so the language never picks a direction
for you.  The sim validates verbs against aircraft state and answers a
wrong one with a puzzled pilot, not a silent fix.
"""

import re

# ICAO telephony designators — how the airline reads back.  The spawner
# only issues callsigns from this table, so coverage is guaranteed.
TELEPHONY = {
    # US majors + cargo
    "AAL": "American", "DAL": "Delta", "UAL": "United", "SWA": "Southwest",
    "JBU": "JetBlue", "ASA": "Alaska", "FFT": "Frontier Flight",
    "AAY": "Allegiant", "HAL": "Hawaiian",
    "SCX": "Sun Country", "MXY": "Moxy", "FDX": "FedEx", "UPS": "UPS",
    "GTI": "Giant", "ATN": "Air Transport",
    # US regionals
    "SKW": "SkyWest", "RPA": "Brickyard", "EDV": "Endeavor", "ENY": "Envoy",
    "PDT": "Piedmont", "JIA": "Blue Streak", "AWI": "Wisconsin",
    # bizjet fleets
    "EJA": "ExecJet", "LXJ": "Flexjet", "VJT": "Vistajet",
    # military transport/tanker wings — the lifters a TRACON actually
    # works, not the fast movers
    "RCH": "Reach", "CNV": "Convoy", "RRR": "Ascot", "CFC": "Canforce",
    "ASY": "Aussie", "GAF": "German Air Force", "CTM": "Cotam",
    # Canada + Latin America
    "ACA": "Air Canada", "WJA": "WestJet", "POE": "Porter", "JZA": "Jazz",
    "TSC": "Transat", "ROU": "Rouge", "AMX": "Aeromexico", "VOI": "Volaris",
    "CMP": "Copa", "AVA": "Avianca", "GLO": "Gol",
    # Europe
    "BAW": "Speedbird", "VIR": "Virgin", "DLH": "Lufthansa",
    "AFR": "Air France", "KLM": "KLM", "RYR": "Ryanair", "EZY": "Easy",
    "WZZ": "Wizz Air", "IBE": "Iberia", "TAP": "Air Portugal",
    "SAS": "Scandinavian", "FIN": "Finnair", "SWR": "Swiss",
    "AUA": "Austrian", "BEL": "Beeline", "EIN": "Shamrock",
    "ICE": "Iceair", "THY": "Turkish", "ELY": "ElAl",
    # Middle East / Asia / Pacific
    "UAE": "Emirates", "QTR": "Qatari", "ETD": "Etihad", "SVA": "Saudia",
    "ANA": "All Nippon", "JAL": "Japan Air", "KAL": "Korean Air",
    "AAR": "Asiana", "CPA": "Cathay", "CCA": "Air China",
    "CES": "China Eastern", "CSN": "China Southern", "SIA": "Singapore",
    "MAS": "Malaysian", "THA": "Thai", "EVA": "Eva", "CAL": "Dynasty",
    "AIC": "Air India", "IGO": "Ifly", "QFA": "Qantas",
    "ANZ": "New Zealand", "JST": "Jetstar", "FJI": "Fiji",
    # 2026 entrants, feeders & carriers reached via real-schedule spawning
    "VXP": "Avelo", "JSX": "Bigstripe", "VTE": "Volunteer",
    "QXE": "Horizon Air", "FLE": "Flair", "GJS": "Lindbergh",
    "UCA": "CommutAir", "ABX": "Abex", "CJT": "Cargojet",
    "NBT": "Longship", "LOT": "Lot", "VLG": "Vueling", "NAX": "Nor Shuttle",
    "EWG": "Eurowings", "CFG": "Condor", "TRA": "Transavia", "PGT": "Sunturk",
    "AEE": "Aegean", "AEA": "Europa", "ITY": "Itarrow",
    "LAN": "Lan", "AZU": "Azul", "VIV": "Viva", "JAT": "Rocksmart",
    "ETH": "Ethiopian", "KQA": "Kenya", "RWD": "Rwandair", "MSR": "Egyptair",
    "RAM": "Royalair Maroc", "APK": "Peace Bird",
    "HVN": "Vietnam Airlines", "PAL": "Philippine", "GIA": "Indonesia",
    "VJC": "Vietjet Air", "AXM": "Red Cap", "CEB": "Cebu",
    "TGW": "Scooter", "AKJ": "Akasa",
    # --- 2026 long-tail expansion: real operators for the added airports ---
    "ACI": "Aircalin",
    "ADO": "Air Do",
    "AEZ": "Aeroitalia",
    "AFL": "Aeroflot",
    "AIZ": "Arkia",
    "AKX": "Alfa Wing",
    "ANT": "Air North",
    "APJ": "Air Peach",
    "APZ": "Air Premia",
    "ARG": "Argentina",
    "ASV": "Air Seoul",
    "AUR": "Ayline",
    "AYN": "Arystan",
    "AZB": "Air Zambia",
    "BAV": "Bamboo",
    "BDR": "Badr Air",
    "BMA": "Gosling",
    "BOV": "Boliviana",
    "BRU": "Belarus Avia",
    "BTI": "Airbaltic",
    "BTK": "Batik",
    "BTN": "Bhutan Air",
    "CBJ": "Capital Jet",
    "CCD": "Xiangjian",
    "CDC": "Loong Air",
    "CEY": "Century Flight",
    "CGH": "Welkin",
    "CGZ": "Colorful",
    "CHB": "West China",
    "CJX": "Air Crane",
    "CRL": "Corsair",
    "CSW": "Eiger",
    "CTV": "Supergreen",
    "CUH": "Loulan",
    "CXA": "Xiamen Air",
    "DAH": "Air Algerie",
    "DJT": "Dreamjet",
    "DKH": "Air Juneyao",
    "DQA": "Island Aviation",
    "DRK": "Royal Bhutan",
    "DWI": "Dominican",
    "ENT": "Enterair",
    "EOK": "Aero Hanguk",
    "EPA": "Donghai Air",
    "ERO": "Echo Romeo",
    "EVE": "Evelop",
    "FAD": "Adeal",
    "FBU": "French Bee",
    "FBZ": "Bondi",
    "FCA": "Linkair",
    "FDB": "Skydubai",
    "FHY": "Freebird Air",
    "FIA": "Fia Airlines",
    "FRE": "Pelican",
    "GLR": "Glacier",
    "HGB": "Greater Bay",
    "HLF": "Homeland",
    "HYM": "Sky Moldova",
    "IWY": "Islandways",
    "JYH": "Trans Jade",
    "KAP": "Cair",
    "KEM": "Cemair",
    "KMM": "Sky Knight",
    "KNA": "Kunming Air",
    "KNE": "Nas Express",
    "LAA": "Libair",
    "LAM": "Mozambique",
    "LBT": "Nouvelair",
    "LER": "Laser",
    "LKE": "Lucky Air",
    "LNI": "Lion Inter",
    "LNK": "Link",
    "LOG": "Logan",
    "LVL": "Dali",
    "LYM": "Key Lime",
    "MAI": "Mauritania",
    "MFX": "Whitebird",
    "MGH": "Mavi",
    "MNE": "Mount Eagle",
    "MSC": "Air Cairo",
    "MWI": "Malawian",
    "MXD": "Malindo",
    "NOS": "Moonflower",
    "OCN": "Ocean",
    "OKA": "Okayjet",
    "OMS": "Mazoon",
    "PAS": "Pelita",
    "PFZ": "Proflight Zambia",
    "PUE": "Spanish",
    "QDA": "Sky Legend",
    "QNT": "Qanot Sharq",
    "RLH": "Sendi",
    "RPB": "Aerorepublica",
    "RUC": "Rutaca",
    "RXA": "Rex",
    "RXI": "Riyadh Air",
    "RZO": "Air Azores",
    "SDM": "Rossiya",
    "SFJ": "Starflyer",
    "SFR": "Safair",
    "SHH": "Sky High",
    "SJV": "Prosper",
    "SJX": "Starwalker",
    "SKK": "Asky Airline",
    "SKU": "Aerosky",
    "SMR": "Somon Air",
    "SNJ": "Newsky",
    "SQP": "Skyup",
    "SZL": "Eswatini",
    "SZN": "Air Senegal",
    "TBA": "Tibet Air",
    "TCV": "Caboverde",
    "TKJ": "Anatolia",
    "TLM": "Mentari",
    "TNU": "Transnusa",
    "TOM": "Tomjet",
    "TTW": "Smart Cat",
    "TVS": "Skytravel",
    "TWB": "Teeway",
    "TZP": "Zippy",
    "UEA": "United Eagle",
    "UGD": "Crested",
    "UZS": "Samarkand",
    "VRE": "Cote D'Ivoire",
    "VSV": "Vlasta",
    "VTU": "Turpial",
    "WFL": "Blue World",
    "YZR": "Yangtze River",
    # --- names for OpenFlights-resolved operators already in the schedule data ---
    "ABL": "Air Busan",
    "ABY": "Arabia",
    "ACP": "Astral Cargo",
    "ALK": "Sri Lankan",
    "ASL": "Air Serbia",
    "ATC": "Tanzania",
    "AUI": "Ukraine International",
    "AXB": "Express India",
    "BHS": "Bahamas",
    "CAI": "Corendon",
    "CDG": "Shandong",
    "CHH": "Hainan",
    "CQH": "Air Spring",
    "CQN": "Chongqing",
    "CRK": "Bauhinia",
    "CSC": "Si Chuan",
    "CSH": "Shanghai Air",
    "CSZ": "Shenzhen Air",
    "CTN": "Croatia",
    "CUA": "Lianhang",
    "CYP": "Cyprus",
    "DLA": "Dolomiti",
    "DTA": "Angola",
    "EDW": "Edelweiss",
    "ESR": "Eastar Jet",
    "EXS": "Channex",
    "FZA": "Fuzhou Air",
    "GCR": "Bo Hai",
    "GEC": "Lufthansa Cargo",
    "GFA": "Gulf Air",
    "HBH": "Hebei Air",
    "IAW": "Iraqi",
    "IBB": "Binter",
    "IBS": "Iberexpres",
    "ISR": "Israir",
    "JJA": "Jeju Air",
    "JJP": "Orange Liner",
    "JNA": "Jin Air",
    "JZR": "Jazeera",
    "KAC": "Kuwaiti",
    "KZR": "Astanaline",
    "LGL": "Luxair",
    "LZB": "Flying Bulgaria",
    "MEA": "Cedar Jet",
    "NIA": "Nile Bird",
    "NSE": "Satena",
    "OMA": "Oman Air",
    "PIA": "Pakistan",
    "RJA": "Jordanian",
    "ROT": "Tarom",
    "SEH": "Air Crete",
    "SEJ": "Spicejet",
    "SKY": "Skymark",
    "SQC": "Singcargo",
    "SXS": "Sunexpress",
    "TAR": "Tunair",
    "TVJ": "Thaiviet Jet",
    "UZB": "Uzbek",
    "VCV": "Conviasa",
    "VOO": "Volotea",
    "VOZ": "Velocity",
    "VTA": "Air Tahiti",
    "WIF": "Wideroe",
}

_DIGITS = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
           "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "niner"}

_NATO = {"A": "alfa", "B": "bravo", "C": "charlie", "D": "delta",
         "E": "echo", "F": "foxtrot", "G": "golf", "H": "hotel",
         "I": "india", "J": "juliett", "K": "kilo", "L": "lima",
         "M": "mike", "N": "november", "O": "oscar", "P": "papa",
         "Q": "quebec", "R": "romeo", "S": "sierra", "T": "tango",
         "U": "uniform", "V": "victor", "W": "whiskey", "X": "x-ray",
         "Y": "yankee", "Z": "zulu"}


def _reg_shaped(cs):
    """Does this look like a registration rather than an airline flight?
    The shapes the spawner issues: N-numbers (N423TB), all-letter regs
    (GBKLX — G-ABCD undashed), and letters-then-digits (JA8231)."""
    if cs[:1] == "N" and cs[1:2].isdigit():
        return cs.isalnum()
    if cs.isalpha():
        return 4 <= len(cs) <= 6
    return len(cs) >= 4 and cs[:2].isalpha() and cs[2:].isdigit()

TRANSITION_FT = 18000  # FL above, thousands below (US convention)


class CommandError(Exception):
    """Bad transmission — the message is what the pilot says back."""


# every word the grammar claims, so a hold fix can't shadow a command
_WORDS = frozenset((
    "l", "r", "left", "right", "c", "d", "climb", "descend",
    "rs", "is", "reduce", "increase", "s", "dct", "direct",
    "i", "ils", "ho", "handoff", "co", "hold", "tfc", "traffic",
    "u", "unable", "neg", "via",
))


def say_digits(n, width=0):
    """Digit-by-digit radio numbers: 230 → 'two three zero', 9 → 'niner'."""
    s = str(int(n))
    if width:
        s = s.zfill(width)
    return " ".join(_DIGITS[c] for c in s)


def say_altitude(alt_ft):
    """11,000 → 'one one thousand'; 24,000 → 'flight level two four zero'."""
    if alt_ft >= TRANSITION_FT:
        return f"flight level {say_digits(round(alt_ft / 100), 3)}"
    thousands, hundreds = divmod(round(alt_ft), 1000)
    parts = []
    if thousands:
        parts.append(f"{say_digits(thousands)} thousand")
    if hundreds:
        parts.append(f"{say_digits(hundreds // 100)} hundred")
    return " ".join(parts) or "zero"


def telephony(callsign):
    """'RPA5655' → 'Brickyard 5655'; a registration reads phonetically —
    'N423TB' → 'november four two three tango bravo'; anything else as
    typed."""
    prefix, flight = callsign[:3], callsign[3:]
    name = TELEPHONY.get(prefix.upper())
    if name and flight:
        return f"{name} {flight}"
    cs = callsign.upper()
    if _reg_shaped(cs):
        words = " ".join(_NATO[c] if c in _NATO else _DIGITS[c] for c in cs)
        return words[0].upper() + words[1:]
    return callsign


def airline_name(callsign):
    """'RPA5655' → 'Brickyard'; None for GA tails and unknown prefixes."""
    if len(callsign) > 3 and callsign[3].isdigit():
        return TELEPHONY.get(callsign[:3].upper())
    return None


def _int_arg(tokens, i, what, lo, hi):
    if i >= len(tokens):
        raise CommandError(f"say {what} again — no value given")
    try:
        v = int(tokens[i])
    except ValueError:
        raise CommandError(f"unreadable {what} \"{tokens[i]}\"")
    if not (lo <= v <= hi):
        raise CommandError(f"{what} {tokens[i]} out of range")
    return v


def parse(text):
    """Transmission text → (callsign_query, [instruction dicts]).

    Instructions (direction is always explicit — see module docstring):
        {"kind": "turn",    "hdg": 230, "dir": "l"|"r"}
        {"kind": "alt",     "alt_ft": 24000, "verb": "c"|"d"}
        {"kind": "speed",   "kt": 210, "dir": "reduce"|"increase"}
        {"kind": "speed",   "kt": None}              # resume normal speed
        {"kind": "direct",  "fix": "LOOSE"}
        {"kind": "procedure", "name": "CDOGG4"}       # join a SID/STAR
        {"kind": "ils",     "rwy": "19L"|None}
        {"kind": "handoff"}
        {"kind": "traffic"}                           # call the VFR target
        {"kind": "unable"}                            # decline their request

    Raises CommandError with a pilot-flavoured message on bad input.
    """
    tokens = text.strip().split()
    if not tokens:
        raise CommandError("say again?")
    query, tokens = tokens[0], [t.lower() for t in tokens[1:]]
    # forgive a space inside the callsign ("ual 71" ≡ "ual71"): letters
    # followed by a number can only be a split callsign, since every
    # instruction starts with a command word, never a bare value
    if (tokens and query.isalpha()
            and tokens[0][0].isdigit() and tokens[0].isalnum()):
        query, tokens = query + tokens[0], tokens[1:]
    if not tokens:
        raise CommandError("say again — callsign but no instruction "
                           f"(try \"{query} l 230\")")

    out = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("l", "r", "left", "right"):
            hdg = _int_arg(tokens, i + 1, "heading", 1, 360)
            out.append({"kind": "turn", "hdg": hdg, "dir": t[0]})
            i += 2
        elif t in ("c", "d", "climb", "descend"):
            # the newcomer's trap: an altitude typed in feet — teach the
            # hundreds convention instead of stonewalling
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if nxt.isdigit() and 1000 <= int(nxt) <= 45000 \
                    and int(nxt) % 100 == 0:
                raise CommandError(
                    f"altitude {nxt} out of range — altitudes read like "
                    f"the data blocks, say {t} {int(nxt) // 100} for "
                    f"{int(nxt):,} ft")
            hundreds = _int_arg(tokens, i + 1, "altitude", 10, 450)
            out.append({"kind": "alt", "alt_ft": hundreds * 100,
                        "verb": t[0]})
            i += 2
        elif t in ("rs", "is", "reduce", "increase"):
            kt = _int_arg(tokens, i + 1, "speed", 100, 400)
            out.append({"kind": "speed", "kt": kt,
                        "dir": "reduce" if t[0] == "r" else "increase"})
            i += 2
        elif t == "s":
            if i + 1 < len(tokens) and tokens[i + 1].isdigit():
                raise CommandError("say again — bare s resumes normal "
                                   "speed; say rs 250 or is 250")
            out.append({"kind": "speed", "kt": None})
            i += 1
        elif t in ("dct", "direct"):
            if i + 1 >= len(tokens):
                raise CommandError("direct where?")
            out.append({"kind": "direct", "fix": tokens[i + 1].upper()})
            i += 2
        elif t == "via":
            if i + 1 >= len(tokens):
                raise CommandError("via which procedure?")
            out.append({"kind": "procedure", "name": tokens[i + 1].upper()})
            i += 2
        elif t in ("i", "ils"):
            rwy = None
            if i + 1 < len(tokens) and tokens[i + 1][0].isdigit():
                rwy = tokens[i + 1].upper()
                i += 1
            out.append({"kind": "ils", "rwy": rwy})
            i += 1
        elif t in ("ho", "handoff", "co"):
            out.append({"kind": "handoff"})
            i += 1
        elif t in ("tfc", "traffic"):
            out.append({"kind": "traffic"})
            i += 1
        elif t in ("u", "unable", "neg"):
            out.append({"kind": "unable"})
            i += 1
        elif t == "hold":
            fix = None
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if nxt.isalpha() and nxt not in _WORDS:
                fix = nxt.upper()
                i += 1
            out.append({"kind": "hold", "fix": fix})
            i += 1
        else:
            # forgive a missing space between verb and value: l230 ≡ l 230
            m = re.fullmatch(r"(l|r|c|d|rs|is)(\d+)", t)
            if m:
                tokens[i:i + 1] = [m.group(1), m.group(2)]
                continue
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if t in ("h", "fh", "hdg", "heading", "t", "turn", "fly") \
                    and nxt.isdigit():
                raise CommandError("say again — direction is yours to "
                                   "give: say l 230 or r 230")
            raise CommandError(f"say again — didn't catch \"{t}\" "
                               "(? for the commands)")
    return query, out


def resolve_callsign(query, aircraft):
    """Match a typed (possibly abbreviated) callsign against the traffic.

    Exact match wins; otherwise any unique suffix works, so 5655 and 55
    both raise Brickyard 5655.  Raises CommandError when nobody (or more
    than one body) answers.
    """
    q = query.upper()
    exact = [ac for ac in aircraft if ac["callsign"].upper() == q]
    if len(exact) == 1:
        return exact[0]
    matches = [ac for ac in aircraft if ac["callsign"].upper().endswith(q)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise CommandError(f"nobody on frequency answers \"{query}\"")
    names = ", ".join(ac["callsign"] for ac in matches[:4])
    raise CommandError(f"multiple aircraft match \"{query}\": {names}")
