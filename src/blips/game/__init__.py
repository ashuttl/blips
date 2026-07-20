"""blips.game — the ATC sim: a TRACON sector that flies like one.

The live scope (``blips``) and the game share the same renderer; this
subpackage holds the parts that are the game and nothing else — the sim
itself, the traffic that feeds it, the procedures it flies, the pilots'
voices, and the shift book that remembers you. ``blips --game`` enters here.
"""

from blips.game.app import main

__all__ = ["main"]
