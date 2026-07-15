"""CLI parsing, live-mode resolution, and debug logging."""

import argparse
import sys

_DEBUG = False


def set_debug(value):
    global _DEBUG
    _DEBUG = bool(value)


def debug_log(msg):
    """Print a diagnostic message to stderr when --debug is active."""
    if _DEBUG:
        print(f"[blips] {msg}", file=sys.stderr)


def blips_parser():
    from blips import __version__
    p = argparse.ArgumentParser(
        prog="blips",
        description="Live air traffic on a braille basemap — an ambient "
                    "terminal scope fed by community ADS-B aggregators.")
    p.add_argument("--version", action="version",
                   version=f"blips {__version__}")
    p.add_argument("--location", default=None,
                   help="scope centre as 'lat,lng' or a place name")
    p.add_argument("--zoom", type=float, default=3.0,
                   help="degrees of latitude shown top-to-bottom (default 3)")
    p.add_argument("--weather", action=argparse.BooleanOptionalAction,
                   default=True,
                   help="weather-radar underlay, on by default "
                        "(--no-weather to disable; toggle live with 'w')")
    p.add_argument("--wx-theme", default=None, metavar="NAME",
                   help="weather colour scheme (e.g. dark-sky, nexrad, "
                        "rainbow); see LibreWXR themes")
    p.add_argument("--print", dest="print_mode", action="store_true",
                   help="single static snapshot (no live mode)")
    p.add_argument("--live", action="store_true",
                   help="force live mode (default when interactive)")
    p.add_argument("--classic-colors", action="store_true",
                   help="use the fixed dark palette instead of the "
                        "terminal theme")
    p.add_argument("--legacy-colors", action="store_true",
                   help="alias for --classic-colors")
    p.add_argument("--debug", action="store_true",
                   help="show diagnostic info on stderr")
    # the easter egg: an approach-control sim on the same scope.  Takes an
    # airport (ICAO/IATA/name); bare --game plays wherever you are.
    p.add_argument("--game", nargs="?", const="", default=None,
                   metavar="AIRPORT", help=argparse.SUPPRESS)
    # a seeded shift replays the same traffic script (synthetic cast, so
    # it reproduces regardless of who's really flying today)
    p.add_argument("--seed", type=int, default=None,
                   help=argparse.SUPPRESS)
    return p


def resolve_live(args):
    """Live mode is on by default when both ends are a TTY."""
    if args.print_mode:
        return False
    if args.live:
        return True
    try:
        return sys.stdout.isatty() and sys.stdin.isatty()
    except Exception:
        return False
