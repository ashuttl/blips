"""Pilots that talk: macOS ``say``, one voice per flight, one at a time.

blips --game is a radio, and a radio is half-duplex — one transmission at a
time.  This turns the frequency's pilot lines into speech through the system
``say`` command, serialised through a single worker so calls don't talk over
each other.  When the sector's busy the queue fills and the newest call gets
dropped, which sounds exactly like getting stepped on — the authentic failure.

Each flight keeps one voice for its whole life on your frequency, chosen by the
airline's nationality wherever the machine has the accent for it — Speedbird in
a London voice, Shamrock in a Dublin one, Air India in Rishi — and a neutral US
voice for everyone else.  The ATIS gets its own fixed voice, flat and recorded.

Off macOS (no ``say``) the whole thing degrades to a no-op; the caller checks
``Speaker.available()`` before offering the toggle.
"""

import hashlib
import queue
import re
import shutil
import subprocess
import sys
import threading

from blips._commands import _DIGITS

# Airline prefix → the accent to reach for, when the machine has a voice in
# that locale.  Everyone not listed falls to the default (US) bucket — better a
# neutral voice than a wrong one.  Gulf and a couple of European flags borrow a
# British-school RT voice, which is where their radio actually sits.
_ACCENT = {
    "BAW": "en_GB", "VIR": "en_GB",                       # Speedbird, Virgin
    "EIN": "en_IE", "RYR": "en_IE",                       # Shamrock, Ryanair
    "AIC": "en_IN", "IGO": "en_IN",                       # Air India, IndiGo
    "QFA": "en_AU", "JST": "en_AU", "ANZ": "en_AU",       # Qantas, Jetstar, NZ
    "UAE": "en_GB", "QTR": "en_GB", "ETD": "en_GB",       # Emirates, Qatari…
    "SVA": "en_GB",
    # 2026 long-tail: Gulf & British carriers en_GB, South African en_ZA, Australian en_AU
    "AUR": "en_GB",
    "FAD": "en_GB",
    "FCA": "en_AU",
    "FDB": "en_GB",
    "FRE": "en_AU",
    "KEM": "en_ZA",
    "KNE": "en_GB",
    "LNK": "en_ZA",
    "LOG": "en_GB",
    "OMS": "en_GB",
    "RXA": "en_AU",
    "RXI": "en_GB",
    "SFR": "en_ZA",
    "TOM": "en_GB",
}

# Curated conversational voices by locale — bare names, matched against
# whatever ``say -v '?'`` actually lists (which qualifies them, e.g. "Daniel
# (English (UK))").  Novelty voices (Bells, Zarvox, …) are left out on purpose:
# a control frequency is no place for a singing robot.
_GOOD = {
    "en_US": ("Samantha", "Alex", "Ava", "Allison", "Tom", "Nathan",
              "Evan", "Nicky", "Aaron", "Joelle", "Fred"),
    "en_GB": ("Daniel", "Kate", "Oliver", "Serena", "Stephanie"),
    "en_IE": ("Moira",),
    "en_IN": ("Rishi", "Veena"),
    "en_AU": ("Karen", "Lee", "Matilda"),
    "en_ZA": ("Tessa",),
}

# The ATIS is a recording: a plain, slightly flat voice, tried in order.
_ATIS_VOICES = ("Fred", "Daniel", "Alex", "Samantha")


def _spoken_text(line):
    """Speak flight numbers as digits: 'Brickyard 5655' → 'Brickyard five six
    five five'.  Everything else in a radio line is already spelled out
    (altitudes, headings), so only the callsign's number ever needs it."""
    return re.sub(r"\d{2,}",
                  lambda m: " ".join(_DIGITS[c] for c in m.group()), line)


def _discover_voices():
    """Poll ``say`` for installed voices → {locale: [bare names present]}."""
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True,
                             text=True, check=False).stdout
    except Exception:
        return {}
    installed = set()
    for row in out.splitlines():
        # rows read "Name  xx_XX  # sample"; the name may itself carry a
        # parenthetical ("Samantha (English (US))") and the gap before the
        # locale can be a single space, so anchor on the locale token
        m = re.match(r"^(.*?)\s+([a-z]{2}_[A-Z]{2})(?:\s|$)", row)
        if m:
            installed.add(m.group(1).split(" (")[0].strip())
    voices = {}
    for locale, names in _GOOD.items():
        present = [n for n in names if n in installed]
        if present:
            voices[locale] = present
    return voices


class Speaker:
    """A background voice for the frequency.  ``speak(line, key)`` enqueues a
    transmission; a single worker plays them one at a time, so the radio stays
    half-duplex.  ``key`` is a callsign (voiced by its airline's accent) or the
    literal ``"ATIS"``.  Each key resolves to a fixed voice on first use."""

    @staticmethod
    def available():
        return sys.platform == "darwin" and shutil.which("say") is not None

    def __init__(self):
        self._voices = _discover_voices()
        flat = sorted({v for names in self._voices.values() for v in names})
        self._default = (self._voices.get("en_US", [None])[0]
                         or (flat[0] if flat else None))
        self._assigned = {}                  # key → resolved voice name
        self._q = queue.Queue(maxsize=4)     # a short backlog; older gets cut
        self._proc = None
        self._stop = object()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _pick(self, key):
        if key == "ATIS":
            flat = {v for names in self._voices.values() for v in names}
            return next((v for v in _ATIS_VOICES if v in flat), self._default)
        pool = (self._voices.get(_ACCENT.get(key[:3].upper(), "en_US"))
                or self._voices.get("en_US"))
        if not pool:
            return self._default
        idx = int(hashlib.sha1(key.encode()).hexdigest(), 16) % len(pool)
        return pool[idx]

    def _voice_for(self, key):
        if key not in self._assigned:
            self._assigned[key] = self._pick(key)
        return self._assigned[key]

    def speak(self, line, key):
        item = (self._voice_for(key), "176" if key == "ATIS" else "192",
                _spoken_text(line))
        try:
            self._q.put_nowait(item)
        except queue.Full:
            pass                             # frequency's busy — stepped on

    def _run(self):
        while True:
            item = self._q.get()
            if item is self._stop:
                return
            name, rate, text = item
            argv = ["say", "-r", rate, text]
            if name:
                argv[1:1] = ["-v", name]
            try:
                self._proc = subprocess.Popen(
                    argv, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._proc.wait()
            except Exception:
                pass
            finally:
                self._proc = None

    def close(self):
        """Stop taking calls and cut off whatever's mid-sentence."""
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        self._q.put(self._stop)
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
