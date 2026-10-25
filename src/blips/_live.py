"""Live mode: the alternate screen, input, and a frame whenever it's due.

live_loop() runs a render callback on the terminal's alternate screen and
calls it again whenever something happens — a key, a wheel notch, a drag,
a resize, a timer, or a background fetch landing (nudge()).  LiveApp is
the class a view with state subclasses: its hook methods become the
loop's callbacks.

A sibling of linecast's terminal.live, carrying over what that loop
learned since blips forked from it:

- Frames are paced to the terminal.  Each goes out with a cursor position
  query after it, and the next waits for the reply, so a terminal slower
  than the loop is never handed a backlog to play out after the input
  stops; keys and wheel notches that arrive meanwhile fold into the next
  frame.  A terminal that never answers is asked once, then left alone.
- Each frame is one synchronized update (DEC mode 2026), with every row
  addressed and autowrap off, so a terminal never shows half a frame and
  a row drawn wider than measured clips instead of shifting the rest.
- Background threads wake the loop with nudge(), a self-pipe write; no
  more signalling our own process with SIGWINCH.
- Ctrl-Z hands the terminal back before stopping; SIGTERM and SIGHUP
  restore it on the way out; a crash in a worker thread is reported once
  the screen is back.

POSIX only, as blips is.
"""

import os
import re
import signal
import sys
import termios
import threading
import time as _time
import tty

from blips import _keys

# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------
_AUTOWRAP_OFF = "\033[?7l"
_AUTOWRAP_ON = "\033[?7h"
_SYNC_BEGIN = "\033[?2026h"
_SYNC_END = "\033[?2026l"
_CPR_QUERY = "\033[6n"
_MOTION_ON = "\033[?1003h"
_MOTION_OFF = "\033[?1003l"
# how long the next frame waits for the terminal to say it read the last
_ACK_WAIT_S = 1.0
_ACK_FIRST_WAIT_S = 0.25


def frame_body(text):
    r"""A frame with every row addressed, so no row can shift the ones below.

    The clear comes before the row, not after: with autowrap off a row that
    reaches the last column leaves the cursor on that cell, and a clear from
    there would take the cell's glyph with it.
    """
    return "".join(f"\033[{row};1H\033[K{line}"
                   for row, line in enumerate(text.split("\n"), 1))


def frame_paint(body, floating="", rows=None):
    r"""Everything the loop writes for one frame, as one string: a
    synchronized update, autowrap off, the screen below the body cleared,
    the addressed rows, and the cursor-addressed ``floating`` overlay on
    top.  ``rows`` is the terminal's height; when the body fills it the
    clear below is skipped (terminals clamp the row after the last to the
    last row, and the clear would blank the footer mid-update)."""
    n = body.count("\n") + 1
    clear_below = (f"\033[{n + 1};1H\033[J"
                   if rows is None or n < rows else "")
    return (f"{_SYNC_BEGIN}{_AUTOWRAP_OFF}{clear_below}{frame_body(body)}"
            f"\033[0m{floating}\033[0m{_AUTOWRAP_ON}{_SYNC_END}")


def overlay(body, floating="", motion=None):
    """A frame with something floating over it — a chip, a panel, the
    search field — on the channel the loop draws after the body, where
    clearing lines can't touch it.  ``motion=False`` turns any-motion mouse
    reporting off for this frame (a text field wants that: a torn motion
    report reads as Esc); the loop turns it back on for frames that don't
    say so."""
    if motion is False:
        floating = _MOTION_OFF + floating
    if not floating:
        return body
    return f"{body}\x00{floating}"


# ---------------------------------------------------------------------------
# The terminal
# ---------------------------------------------------------------------------
_current = None  # the _Terminal a loop is running on, for nudge()


class _Terminal:
    """Cbreak input plus a wakeup any thread can pull."""

    def __init__(self, fd):
        self.fd = fd
        self._closed = False
        self.suspend_pending = False
        self._old_settings = None
        self._prev_handlers = {}
        self._wake_lock = threading.Lock()
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)

    def install(self):
        global _current

        # os.write to a pipe is async-signal-safe; threading.Event.set()
        # is not, and deadlocks when SIGWINCH re-enters during a resize
        def _on_winch(*_):
            w = self._wake_w
            if w is None:
                return
            try:
                os.write(w, b'\x00')
            except OSError:
                pass

        self._prev_handlers[signal.SIGWINCH] = signal.signal(
            signal.SIGWINCH, _on_winch)

        # SIGTERM/SIGHUP/SIGQUIT through SystemExit, so a closed terminal or
        # `pkill blips` still runs the finally that gives the screen back
        def _exit_on_signal(signum, _frame):
            sys.exit(128 + signum)

        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
            try:
                self._prev_handlers[sig] = signal.signal(sig, _exit_on_signal)
            except (ValueError, OSError):
                pass

        # Ctrl-Z only asks: the loop gives the terminal back between frames
        # and stops itself (suspend())
        def _on_tstp(*_):
            self.suspend_pending = True
            _on_winch()

        try:
            if signal.getsignal(signal.SIGTSTP) is not signal.SIG_IGN:
                self._prev_handlers[signal.SIGTSTP] = signal.signal(
                    signal.SIGTSTP, _on_tstp)
        except (ValueError, OSError):
            pass
        _current = self

    def set_cbreak(self):
        self._old_settings = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        try:
            mode = termios.tcgetattr(self.fd)
            # cbreak leaves IEXTEN on, which keeps Ctrl-V as the terminal's
            # `lnext` ("quote next key") — drop it so every control key we
            # listen for reaches the app as a plain byte
            mode[3] &= ~termios.IEXTEN
            # ^\ (and ctrl-4, which macOS's screenshot shortcut sends on its
            # way through) would SIGQUIT past the finally block: disable it
            vdisable = getattr(termios, "_POSIX_VDISABLE", None)
            if vdisable is None:
                vdisable = os.fpathconf(self.fd, "PC_VDISABLE")
            mode[6][termios.VQUIT] = vdisable
            termios.tcsetattr(self.fd, termios.TCSANOW, mode)
        except (OSError, ValueError, IndexError, termios.error):
            pass

    def wake(self):
        with self._wake_lock:
            if self._wake_w is None:
                return
            try:
                os.write(self._wake_w, b'\x00')
            except OSError:
                pass

    def drain(self):
        """Discard wakeups queued before the frame about to be drawn."""
        if self._wake_r is None:
            return
        try:
            os.read(self._wake_r, 512)
        except OSError:
            pass

    def wait(self, timeout):
        """Block up to `timeout`: 'input', 'wake' or 'timeout'."""
        if _keys._pending:
            return 'input'
        import select
        try:
            ready, _, _ = select.select(
                [self.fd, self._wake_r], [], [], max(0.0, timeout))
        except (InterruptedError, OSError, ValueError):
            return 'timeout'
        # input first: an animating view has a wakeup pending nearly always,
        # and a key must not wait behind it
        if self.fd in ready:
            return 'input'
        if self._wake_r in ready:
            self.drain()
            return 'wake'
        return 'timeout'

    def settle(self, timeout, replies=1):
        """Read and drop what the terminal is still sending, up to its reply
        to the last query, so none of it lands on the shell's prompt."""
        if self._closed:
            return
        if timeout > 0:
            deadline = _time.monotonic() + timeout
            buf = b""
            while _time.monotonic() < deadline:
                if not _keys.wait_readable(self.fd, deadline - _time.monotonic()):
                    break
                try:
                    chunk = os.read(self.fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                if len(re.findall(rb"\033\[\d+;\d+R", buf)) >= replies:
                    break
        _keys.flush_pending()
        try:
            termios.tcflush(self.fd, termios.TCIFLUSH)
        except (OSError, termios.error):
            pass

    def suspend(self, hand_back):
        """Ctrl-Z: give the terminal back, stop, and take it again on `fg`."""
        self.suspend_pending = False
        quiet = {}
        for sig in (signal.SIGTTIN, signal.SIGTTOU):
            quiet[sig] = signal.signal(sig, signal.SIG_IGN)
        try:
            hand_back()
            if self._old_settings is not None:
                try:
                    termios.tcsetattr(self.fd, termios.TCSADRAIN,
                                      self._old_settings)
                except termios.error:
                    pass
            handler = signal.signal(signal.SIGTSTP, signal.SIG_DFL)
            try:
                signal.raise_signal(signal.SIGTSTP)
            finally:
                signal.signal(signal.SIGTSTP, handler)
        finally:
            for sig, previous in quiet.items():
                signal.signal(sig, previous)
        self.set_cbreak()

    def close(self):
        global _current
        if self._closed:
            return
        self._closed = True
        if _current is self:
            _current = None
        _keys.flush_pending()
        # the signal handlers go back before the pipe closes: a fetch that
        # lands after the loop still nudges, and its write must not go to
        # whatever file reused the pipe's descriptor number
        for sig, handler in self._prev_handlers.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError, TypeError):
                pass
        with self._wake_lock:
            for fd in (self._wake_r, self._wake_w):
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._wake_r = self._wake_w = None
        if self._old_settings is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN,
                                  self._old_settings)
            except Exception:
                pass  # the tty may already be gone (SIGHUP)


def _mtime(path):
    if path is None:
        return None
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def nudge():
    """Ask a running live loop to repaint now, from any thread.  Does
    nothing when no loop is running, so background work can call it
    unconditionally."""
    term = _current
    if term is not None:
        term.wake()


def running():
    """Whether a live loop has the terminal right now."""
    return _current is not None


class _WorkerWatch:
    """Catch a crash in a background thread while the screen is taken, and
    say so once it's given back — a traceback printed onto the alternate
    screen would vanish with it."""

    def __init__(self):
        self.crashes = []
        self._prev = None

    def install(self):
        self._prev = threading.excepthook

        def hook(args):
            if args.exc_type is SystemExit:
                return
            self.crashes.append(args)

        threading.excepthook = hook

    def uninstall(self):
        if self._prev is not None:
            threading.excepthook = self._prev

    def report(self):
        if not self.crashes:
            return
        import traceback
        args = self.crashes[0]
        name = getattr(args.thread, "name", "a background thread")
        print(f"blips: {name} crashed during the session:", file=sys.stderr)
        traceback.print_exception(args.exc_type, args.exc_value,
                                  args.exc_traceback, file=sys.stderr)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
def live_loop(render_fn, interval=60, mouse=False, auto_play=False,
              play_interval=0.6, on_action=None, on_drag=None,
              intercept=None, raw_keys=False, on_wheel=None, on_click=None,
              text_mode=None, play_gate=None, hover_idle=None, **_legacy):
    """Run render_fn() in a loop on the alternate screen buffer.

    render_fn: callable(**frame) returning the frame as a string, optionally
               with a cursor-addressed overlay after a ``\\x00`` (overlay()).
               Receives mouse_pos=(col, row) or None when mouse is on, and
               play_frame / playing when auto_play is.
    interval, play_interval: seconds to the next frame while idle / while
               playing — numbers, or callables asked before each wait (an
               animating view returns a short one while it moves).
    mouse: SGR mouse tracking with motion: hover, drags, clicks, the wheel.
    auto_play: space toggles play/pause; render_fn gets play_frame, which
               advances each play_interval (play_gate() may hold it).
    on_action(key): a key the loop doesn't own ('+', 'w', '/' …, case as
               typed; arrows arrive as 'up', 'down', 'left', 'right').
               Truthy to repaint.
    on_drag(dcol, drow, done): a left-button drag's cumulative cell delta,
               live (done=False) and on release (done=True).
    on_click(col, row): a press and release on the same cell, fired before
               the zero-delta on_drag commit.  Truthy to repaint.
    on_wheel(direction, col, row): +1 up / -1 down at the pointer.  Truthy
               to repaint.  Without it the wheel steps play_frame.
    intercept(action): consulted for every key action before the loop's
               own handling; truthy consumes it and repaints.
    text_mode(): while truthy, printable input arrives at intercept as
               'char:<c>' (see _keys.read_key) — a caller-drawn text field.
    raw_keys: every printable as 'key:<c>' (the game's command bar).
    hover_idle: seconds of a still pointer after which mouse_pos goes back
               to None, as if it had left the window (None: never).
    """
    _LiveLoop(render_fn, interval=interval, mouse=mouse, auto_play=auto_play,
              play_interval=play_interval, on_action=on_action,
              on_drag=on_drag, intercept=intercept, raw_keys=raw_keys,
              on_wheel=on_wheel, on_click=on_click, text_mode=text_mode,
              play_gate=play_gate, hover_idle=hover_idle).run()


class _LiveLoop:
    def __init__(self, render_fn, *, interval, mouse, auto_play,
                 play_interval, on_action, on_drag, intercept, raw_keys,
                 on_wheel, on_click, text_mode, play_gate, hover_idle):
        self.render_fn = render_fn
        self.interval = interval
        self.mouse = mouse
        self.auto_play = auto_play
        self.play_interval = play_interval
        self.on_action = on_action
        self.on_drag = on_drag
        self.intercept = intercept
        self.raw_keys = raw_keys
        self.on_wheel = on_wheel
        self.on_click = on_click
        self.text_mode = text_mode
        self.play_gate = play_gate
        self.hover_idle = hover_idle

        self.playing = auto_play
        self.play_frame = 0
        self.mouse_pos = None
        self.hover_until = 0.0
        self.drag_start = None

        self.term = None
        self.theme_watch = None        # (path, mtime) of Omarchy's marker
        self.next_probe = 0.0          # when the colours are next asked
        self.pacing_on = os.environ.get("BLIPS_FRAME_SYNC", "1").strip().lower() \
            not in ("0", "false", "off", "no")
        self.answered = None   # None until asked, then whether it ever replied
        self.owed = 0          # cursor queries sent and not yet answered
        self.init = self.restore = ""

    # -- the run -----------------------------------------------------------
    def run(self):
        is_apple = os.environ.get('TERM_PROGRAM') == 'Apple_Terminal'
        self.init = "\033[?1049h\033[?25l"
        self.restore = _SYNC_END
        if self.mouse:
            self.init += "\033[?1000h\033[?1002h\033[?1003h\033[?1006h"
            self.restore += "\033[?1006l\033[?1003l\033[?1002l\033[?1000l"
            if is_apple:
                # alternate-scroll, for a terminal that doesn't report the
                # wheel as mouse buttons
                self.init += "\033[?1007h"
                self.restore += "\033[?1007l"
        self.restore += f"{_AUTOWRAP_ON}\033[?25h\033[?1049l"

        self.term = _Terminal(sys.stdin.fileno())
        self.term.install()
        watch = _WorkerWatch()
        watch.install()
        try:
            self.term.set_cbreak()
            sys.stdout.write(self.init)
            sys.stdout.flush()
            while True:
                if self._hold() == 'quit':
                    return
                if self.term.suspend_pending:
                    self._suspend()
                self._paint()
                if self._idle() == 'quit':
                    return
        except KeyboardInterrupt:
            pass
        finally:
            self._give_back()
            watch.uninstall()
            watch.report()

    def _wait_s(self):
        return _ACK_WAIT_S if self.answered else _ACK_FIRST_WAIT_S

    def _hold(self):
        """Hold the next frame until the terminal has read the last one;
        input meanwhile changes the state the frame is drawn from."""
        if not (self.pacing_on and self.owed):
            return None
        until = _time.monotonic() + self._wait_s()
        while self.owed:
            left = until - _time.monotonic()
            if left <= 0:
                if not self.answered:
                    self.pacing_on = False  # never answers: stop asking
                self.owed = 0
                break
            event = self.term.wait(min(0.1, left))
            if self.term.suspend_pending:
                break
            if event == 'input' and self._on_input() == 'quit':
                return 'quit'
        return None

    def _suspend(self):
        def hand_back():
            sys.stdout.write(self.restore)
            sys.stdout.flush()
            self.term.settle(0)

        self.term.suspend(hand_back)
        self.owed = 0
        sys.stdout.write(self.init)
        sys.stdout.flush()

    def _paint(self):
        # drain wakeups BEFORE rendering: a fetch that lands mid-render
        # nudges again, and that nudge must survive to the next frame
        self.term.drain()
        kwargs = {}
        if self.mouse:
            kwargs["mouse_pos"] = self.mouse_pos
        if self.auto_play:
            kwargs.update(play_frame=self.play_frame, playing=self.playing)
        output = self.render_fn(**kwargs)
        if isinstance(output, tuple):
            output = output[0]
        body, _, floating = output.partition('\x00')
        if self.mouse and _MOTION_OFF not in floating:
            floating = _MOTION_ON + floating
        try:
            rows = os.get_terminal_size().lines
        except OSError:
            rows = None
        ask = ""
        if self.pacing_on:
            ask = _CPR_QUERY
            self.owed += 1
            if self.answered is None:
                self.answered = False
        sys.stdout.write(frame_paint(body, floating, rows) + ask)
        sys.stdout.flush()

    def _idle(self):
        """Wait for the next frame to be due: input that changes the view,
        a wakeup or a resize, or the interval running out."""
        if self.auto_play and self.playing:
            wait = self.play_interval
        else:
            wait = self.interval
        wait = wait() if callable(wait) else wait
        deadline = _time.time() + wait
        owed = False   # a coalesced input is waiting for its repaint
        while True:
            if owed and not _keys.wait_readable(self.term.fd, 0):
                return None
            remaining = deadline - _time.time()
            if remaining <= 0:
                if self.auto_play and self.playing and (
                        self.play_gate is None or self.play_gate()):
                    self.play_frame += 1
                return None
            if (self.hover_idle and self.mouse_pos is not None
                    and self.drag_start is None):
                left = self.hover_until - _time.monotonic()
                if left <= 0:
                    self.mouse_pos = None  # a still pointer: drop the chip
                    return None
                remaining = min(remaining, left)
            self._theme_look()
            event = self.term.wait(min(remaining, 0.5))
            if event == 'wake':
                return None
            if event == 'input':
                verdict = self._on_input()
                if verdict == 'quit':
                    return 'quit'
                if verdict == 'repaint':
                    return None
                if verdict == 'coalesce':
                    owed = True

    def _theme_look(self):
        """Ask the terminal for its colours when they may have changed:
        every ten seconds, and briskly for a few seconds after Omarchy's
        theme marker moves (the terminal reloads a beat after it's
        written).  The answers come back as input (see _on_input)."""
        from blips import _theme
        if not _theme.theme_available or _theme.theme_legacy_mode:
            return
        now = _time.monotonic()
        if self.theme_watch is None:
            path = _theme.watch_path()
            self.theme_watch = (path, _mtime(path))
            self.next_probe = now + 10.0
            self.burst = ()
        path, seen = self.theme_watch
        if path is not None:
            m = _mtime(path)
            if m != seen:
                self.theme_watch = (path, m)
                self.burst = (now + 0.3, now + 1.0, now + 2.5, now + 5.0)
        due = [t for t in self.burst if t <= now]
        if due or now >= self.next_probe:
            self.burst = tuple(t for t in self.burst if t > now)
            self.next_probe = now + 10.0
            try:
                sys.stdout.write(_theme.PROBE_QUERY)
                sys.stdout.flush()
            except Exception:
                pass

    def _give_back(self):
        term = self.term
        try:
            sys.stdout.write(self.restore + (_CPR_QUERY if self.pacing_on else ""))
            sys.stdout.flush()
        except Exception:
            pass  # tty may already be gone (SIGHUP)
        try:
            term.settle(self._wait_s() if self.pacing_on else 0,
                        replies=self.owed + 1)
        except Exception:
            pass
        finally:
            term.close()

    # -- input -------------------------------------------------------------
    def _coalesce_or_repaint(self):
        """Paint now, or hold the paint while more input is waiting — so a
        burst of scrolling or dragging paints once, at its end."""
        return 'coalesce' if _keys.wait_readable(self.term.fd, 0) else 'repaint'

    def _on_input(self):
        """Read one key or mouse event and apply it: 'quit', 'repaint',
        'coalesce', or None when nothing on screen changed."""
        text = bool(self.text_mode is not None and self.text_mode())
        action = _keys.read_key(self.term.fd, text=text, raw=self.raw_keys)
        if action == 'ack':
            self.answered = True
            self.owed = max(0, self.owed - 1)
            return None
        if isinstance(action, str) and action.startswith('osc:'):
            from blips import _theme
            return 'repaint' if _theme.ingest_osc(action[4:]) else None
        if isinstance(action, tuple):
            if not self.mouse:
                return None
            self.hover_until = _time.monotonic() + (self.hover_idle or 0)
            return self._on_mouse(*action[1:])
        if action is None:
            return None
        if self.intercept is not None and self.intercept(action):
            return self._coalesce_or_repaint()
        if action == 'quit':
            return 'quit'
        if action == 'reset':
            if self.auto_play:
                self.playing = not self.playing  # space = play/pause
                if not self.playing:
                    self.play_frame = 0
            return 'repaint'
        if self.on_action is not None:
            key = action[4:] if action.startswith('key:') else action
            if self.on_action(key):
                return self._coalesce_or_repaint()
        return None

    def _on_mouse(self, cb, cx, cy, is_rel):
        wheel = _keys.wheel_code(cb)
        if wheel in (64, 65):
            direction = 1 if wheel == 64 else -1
            if self.on_wheel is not None:
                handled = self.on_wheel(direction, cx, cy)
                return self._coalesce_or_repaint() if handled else None
            if self.auto_play:
                self.playing = False
                self.play_frame += direction
                return self._coalesce_or_repaint()
            return None
        if is_rel:
            if self.drag_start is None:
                return None
            dcol, drow = cx - self.drag_start[0], cy - self.drag_start[1]
            self.drag_start = None
            clicked = (dcol == 0 and drow == 0 and self.on_click is not None
                       and self.on_click(cx, cy))
            dragged = self.on_drag is not None and self.on_drag(dcol, drow, True)
            return 'repaint' if (clicked or dragged) else None
        if (cb & 0b11) == 0 and not (cb & 0x20):
            # left button press: where a drag (or a click) starts
            if self.on_drag is not None or self.on_click is not None:
                self.drag_start = (cx, cy)
        if cb & 0x20:
            if self.drag_start is not None and (cb & 0b11) == 0:
                dcol, drow = cx - self.drag_start[0], cy - self.drag_start[1]
                if self.on_drag is not None and self.on_drag(dcol, drow, False):
                    return self._coalesce_or_repaint()
                return None
            self.mouse_pos = (cx, cy)
            return self._coalesce_or_repaint()
        if (cb & 0b11) in (0, 1, 2):
            # no motion reporting: a press still places the pointer
            self.mouse_pos = (cx, cy)
            return 'repaint'
        return None


# ---------------------------------------------------------------------------
# Live apps
# ---------------------------------------------------------------------------
class LiveApp:
    """A live view: state, the hooks that change it, and one render per
    repaint.  Override the hooks you need; the loop's tuning is the class
    attributes.  A hook left as the base's is not handed to the loop."""

    interval = 5.0
    play_interval = 0.5
    mouse = True
    auto_play = True
    hover_idle = None

    def render(self, **frame):
        raise NotImplementedError

    def on_action(self, key):
        return False

    def on_drag(self, dcol, drow, done):
        return False

    def on_click(self, col, row):
        return False

    def on_wheel(self, direction, col, row):
        return False

    def intercept(self, action):
        return False

    def text_mode(self):
        return False

    def frame_interval(self):
        """Seconds to the next frame while idle (called before each wait)."""
        return self.interval

    def glide_interval(self):
        """Seconds to the next frame while playing."""
        return self.play_interval

    def _hooks(self):
        hooks = {}
        for name in ("on_action", "on_drag", "on_click", "on_wheel",
                     "intercept", "text_mode"):
            if getattr(type(self), name) is not getattr(LiveApp, name):
                hooks[name] = getattr(self, name)
        return hooks

    def run(self):
        live_loop(self.render, interval=self.frame_interval,
                  play_interval=self.glide_interval, mouse=self.mouse,
                  auto_play=self.auto_play, hover_idle=self.hover_idle,
                  **self._hooks())
