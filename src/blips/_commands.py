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
    "EJA": "ExecJet", "LXJ": "Flexjet",
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
}

_DIGITS = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
           "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "niner"}

TRANSITION_FT = 18000  # FL above, thousands below (US convention)


class CommandError(Exception):
    """Bad transmission — the message is what the pilot says back."""


# every word the grammar claims, so a hold fix can't shadow a command
_WORDS = frozenset((
    "l", "r", "left", "right", "c", "d", "climb", "descend",
    "rs", "is", "reduce", "increase", "s", "dct", "direct",
    "i", "ils", "ho", "handoff", "co", "hold", "tfc", "traffic",
    "u", "unable", "neg",
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
    """'RPA5655' → 'Brickyard 5655'; unknown prefixes read back as typed."""
    prefix, flight = callsign[:3], callsign[3:]
    name = TELEPHONY.get(prefix.upper())
    if name and flight:
        return f"{name} {flight}"
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
        raise CommandError("say again — callsign but no instruction")

    out = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("l", "r", "left", "right"):
            hdg = _int_arg(tokens, i + 1, "heading", 1, 360)
            out.append({"kind": "turn", "hdg": hdg, "dir": t[0]})
            i += 2
        elif t in ("c", "d", "climb", "descend"):
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
            out.append({"kind": "speed", "kt": None})
            i += 1
        elif t in ("dct", "direct"):
            if i + 1 >= len(tokens):
                raise CommandError("direct where?")
            out.append({"kind": "direct", "fix": tokens[i + 1].upper()})
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
            raise CommandError(f"say again — didn't catch \"{t}\"")
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
