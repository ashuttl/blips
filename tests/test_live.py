"""live_loop input handling: rapid mouse motion coalesces into few renders.

A fast swipe across the scope arrives as one SGR motion event per cell
crossed.  Each render is a full-screen composite (~10ms), so rendering
once per event queues seconds of work and the hover chip trails the
pointer.  live_loop must drain pending motion and render once at the
final position — the same fix linecast's live loop got.
"""

import os
import pty
import sys

import pytest

from blips._live import live_loop


@pytest.fixture
def fake_tty(monkeypatch):
    """Route live_loop's stdin to the slave end of a pty we can write to."""
    master, slave = os.openpty()

    class _Stdin:
        def fileno(self):
            return slave

    monkeypatch.setattr(sys, "stdin", _Stdin())
    yield master
    for fd in (master, slave):
        try:
            os.close(fd)
        except OSError:
            pass


def test_motion_burst_coalesces_renders(fake_tty, capsys):
    # a 50-cell swipe, one SGR any-motion event per cell, then quit
    burst = b"".join(b"\033[<35;%d;20M" % col for col in range(10, 60))

    renders = []

    def render_fn(offset_minutes=0, mouse_pos=None, active_alert=None,
                  modal_scroll=0):
        # inject the burst from the first render: live_loop's setcbreak
        # (TCSAFLUSH) discards input queued before the loop starts, and the
        # first render always precedes the first read.  Quit only once the
        # pointer's final position has rendered — anything already buffered
        # behind the motion gets coalesced past.
        if not renders:
            os.write(fake_tty, burst)
        elif mouse_pos == (59, 20):
            os.write(fake_tty, b"q")
        renders.append(mouse_pos)
        return "frame"

    live_loop(render_fn, interval=60, mouse=True)

    # uncoalesced this would be 51 renders (initial + one per event);
    # coalesced it's the initial render plus a handful at most
    assert len(renders) <= 5, f"{len(renders)} renders for one swipe"
    # the render that did happen saw the pointer's final position
    assert renders[-1] == (59, 20)
