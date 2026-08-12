"""Pilots that talk: one voice per flight, one at a time, on any OS with one.

blips --game is a radio, and a radio is half-duplex — one transmission at a
time.  This turns the frequency's pilot lines into speech, serialised through
a single worker so calls don't talk over each other.  When the sector's busy
the queue fills and the newest call gets dropped, which sounds exactly like
getting stepped on — the authentic failure.

Each flight keeps one voice for its whole life on your frequency, chosen by
the airline's nationality wherever the machine has the accent for it —
Speedbird in a London voice, Shamrock in a Dublin one, Air India in Rishi —
and a neutral US voice for everyone else.  The ATIS gets its own fixed voice,
flat and recorded.

Two ways for the machine to have a voice.  On macOS it's the system ``say``
command and its installed voice catalogue.  Everywhere else it's Piper
(``pip install blips[voice]``), a local neural TTS: two multi-speaker models —
~900 American voices and ~100 British-corpus voices — fetched once into the
blips cache (~150 MB) and synthesised on the CPU faster than the words come
out.  Piper's British corpus genuinely contains Scottish, Irish and further
Commonwealth speakers, but retrained multi-speaker models are known to
shuffle who's who (coqui-ai/TTS#2258), so the non-US accent buckets share the
British pool wholesale rather than pretending to per-speaker precision.
``_PIPER_CAST`` is the place to curate ids after an audition.

With neither backend (or no way to play audio) the whole thing degrades to a
no-op; the caller checks ``Speaker.available()`` before offering the toggle.
"""

import hashlib
import importlib.util
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import wave

from blips._cache import CACHE_ROOT
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
    # OpenFlights-resolved: Gulf carriers en_GB, Indian en_IN, Jet2/Virgin AU
    "ABY": "en_GB",
    "AXB": "en_IN",
    "EXS": "en_GB",
    "GFA": "en_GB",
    "JZR": "en_GB",
    "KAC": "en_GB",
    "MEA": "en_GB",
    "OMA": "en_GB",
    "RJA": "en_GB",
    "SEJ": "en_IN",
    "VOZ": "en_AU",
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

# ---------------------------------------------------------------------------
# Piper casting.  Two multi-speaker models cover the whole sky: LibriTTS-R
# for the neutral US bucket (904 speakers) and VCTK for everything
# British-school (109 speakers).  A voice token is (model, speaker_id).
# Accent buckets map to a model and, optionally, a curated tuple of speaker
# ids; None means the full company.  Per-speaker accents inside VCTK are
# deliberately unclaimed until someone's ears have auditioned them.
_PIPER_DIR = CACHE_ROOT / "voices"
_PIPER_MODELS = {
    "en_US-libritts_r-medium": 904,
    "en_GB-vctk-medium": 109,
}
_PIPER_CAST = {
    "en_US": ("en_US-libritts_r-medium", None),
    "en_GB": ("en_GB-vctk-medium", None),
    "en_IE": ("en_GB-vctk-medium", None),
    "en_IN": ("en_GB-vctk-medium", None),
    "en_AU": ("en_GB-vctk-medium", None),
    "en_ZA": ("en_GB-vctk-medium", None),
}
_PIPER_ATIS = ("en_US-libritts_r-medium", 0)

# What can actually make a speaker cone move, tried in order.  pw-play first
# because that's the native door on a PipeWire desktop; aplay is the ALSA
# floor that's nearly always there.
_PLAYERS = (
    ("pw-play", []),
    ("paplay", []),
    ("aplay", ["-q"]),
    ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "error"]),
    ("mpv", ["--really-quiet", "--no-video"]),
)


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


class _SayBackend:
    """macOS ``say``: the original radio.  Voice tokens are bare voice names."""

    atis_pref = _ATIS_VOICES

    @staticmethod
    def available():
        return sys.platform == "darwin" and shutil.which("say") is not None

    @staticmethod
    def needs_download():
        return False

    @staticmethod
    def voices():
        return _discover_voices()

    def __init__(self):
        self._proc = None

    def play(self, name, style, text):
        """Blocking: speak one transmission through ``say``."""
        argv = ["say", "-r", "176" if style == "atis" else "192", text]
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

    def stop(self):
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass


class _PiperBackend:
    """Piper neural TTS: local, CPU, any OS.  Voice tokens are
    (model, speaker_id).  Models live in the blips cache and are fetched on
    the worker thread the first time the radio keys up, so a fresh install's
    first transmission arrives late rather than freezing the scope."""

    atis_pref = (_PIPER_ATIS,)

    @staticmethod
    def _player():
        for cmd, flags in _PLAYERS:
            if shutil.which(cmd):
                return [cmd, *flags]
        return None

    @staticmethod
    def available():
        return (importlib.util.find_spec("piper") is not None
                and _PiperBackend._player() is not None)

    @staticmethod
    def needs_download():
        return not all((_PIPER_DIR / f"{m}.onnx").exists()
                       and (_PIPER_DIR / f"{m}.onnx.json").exists()
                       for m in _PIPER_MODELS)

    @staticmethod
    def voices():
        return {locale: [(model, sid) for sid in
                         (ids if ids is not None
                          else range(_PIPER_MODELS[model]))]
                for locale, (model, ids) in _PIPER_CAST.items()}

    def __init__(self):
        self._proc = None
        self._loaded = {}                    # model name → PiperVoice
        self._stopping = False

    def _ensure_models(self):
        from piper.download_voices import download_voice
        _PIPER_DIR.mkdir(parents=True, exist_ok=True)
        for model in _PIPER_MODELS:
            if not ((_PIPER_DIR / f"{model}.onnx").exists()
                    and (_PIPER_DIR / f"{model}.onnx.json").exists()):
                download_voice(model, _PIPER_DIR)

    def _voice(self, model):
        if model not in self._loaded:
            from piper import PiperVoice
            self._ensure_models()
            self._loaded[model] = PiperVoice.load(
                str(_PIPER_DIR / f"{model}.onnx"))
        return self._loaded[model]

    def play(self, token, style, text):
        """Blocking: synthesise one transmission to a wav, hand it to the
        first audio player the machine owns."""
        from piper import SynthesisConfig
        model, sid = token or _PIPER_ATIS
        try:
            voice = self._voice(model)
        except Exception:
            return                           # no net, no model — stay quiet
        cfg = SynthesisConfig(
            speaker_id=sid,
            # the ATIS reads like a recording; pilots are brisker
            length_scale=1.05 if style == "atis" else 0.9)
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="blips-radio-")
        try:
            with os.fdopen(fd, "wb") as f, wave.open(f, "wb") as w:
                voice.synthesize_wav(text, w, syn_config=cfg)
            if self._stopping:
                return
            self._proc = subprocess.Popen(
                [*self._player(), path], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._proc.wait()
        except Exception:
            pass
        finally:
            self._proc = None
            try:
                os.unlink(path)
            except OSError:
                pass

    def stop(self):
        self._stopping = True
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass


_BACKENDS = (_SayBackend, _PiperBackend)


def _backend_cls():
    return next((b for b in _BACKENDS if b.available()), None)


class Speaker:
    """A background voice for the frequency.  ``speak(line, key)`` enqueues a
    transmission; a single worker plays them one at a time, so the radio stays
    half-duplex.  ``key`` is a callsign (voiced by its airline's accent) or the
    literal ``"ATIS"``.  Each key resolves to a fixed voice on first use."""

    _atis_pref = _ATIS_VOICES               # backend overrides at init

    @staticmethod
    def available():
        return _backend_cls() is not None

    @staticmethod
    def needs_download():
        """True when turning voices on will fetch models first — the app can
        warn that the first transmission takes a moment."""
        cls = _backend_cls()
        return cls is not None and cls.needs_download()

    @staticmethod
    def hint():
        """Why there's no voice, phrased as what would give the machine one."""
        if sys.platform == "darwin":
            return "voices need macOS ‘say’"
        if importlib.util.find_spec("piper") is None:
            return "voices need piper — pip install blips[voice]"
        return "voices need an audio player (pw-play, paplay, aplay…)"

    def __init__(self):
        self._backend = _backend_cls()()
        self._atis_pref = self._backend.atis_pref
        self._voices = self._backend.voices()
        flat = sorted({v for names in self._voices.values() for v in names})
        self._default = (self._voices.get("en_US", [None])[0]
                         or (flat[0] if flat else None))
        self._assigned = {}                  # key → resolved voice token
        self._q = queue.Queue(maxsize=4)     # a short backlog; older gets cut
        self._stop = object()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _pick(self, key):
        if key == "ATIS":
            flat = {v for names in self._voices.values() for v in names}
            return next((v for v in self._atis_pref if v in flat),
                        self._default)
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
        item = (self._voice_for(key), "atis" if key == "ATIS" else "pilot",
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
            voice, style, text = item
            self._backend.play(voice, style, text)

    def close(self):
        """Stop taking calls and cut off whatever's mid-sentence."""
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        self._q.put(self._stop)
        self._backend.stop()
