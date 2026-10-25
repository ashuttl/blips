"""What the bytes a terminal sends mean: keys, mouse reports, and its
replies to the live loop's queries.

read_key() reads one of them from stdin in cbreak mode and says what it
is: an action string, a mouse tuple, or 'ack' for the cursor position
reply the live loop asks for after every frame (see _live._Pacing).

A sibling of linecast's terminal.keys, trimmed to POSIX and to what
blips needs: arrows read as directions (the scope pans with them; the
game walks its command history), and a text field gets its characters
whole, multi-byte UTF-8 included.

Mouse protocol references:
  - SGR (1006): https://invisible-island.net/xterm/ctlseqs/ctlseqs.html#h3-Extended-coordinates
  - Legacy X10:  https://invisible-island.net/xterm/ctlseqs/ctlseqs.html#h3-Normal-tracking-mode
"""

import os
import select

# Input read but not yet taken: a byte read too far while finishing one
# sequence (the ESC that starts the next) goes back here and is read first.
_pending = bytearray()


def read_byte(fd):
    """One byte of input, or None at EOF."""
    if _pending:
        return bytes([_pending.pop(0)])
    try:
        data = os.read(fd, 1)
    except OSError:
        return None
    return data or None


def unread(data):
    """Put bytes back, to be read before anything the terminal sends next."""
    _pending[:0] = data


def wait_readable(fd, timeout):
    """Whether a byte is available within `timeout` seconds."""
    if _pending:
        return True
    try:
        return bool(select.select([fd], [], [], timeout)[0])
    except (InterruptedError, OSError, ValueError):
        return False


def flush_pending():
    _pending.clear()


def _read_byte_timeout(fd, timeout):
    if wait_readable(fd, timeout):
        return read_byte(fd)
    return None


# ---------------------------------------------------------------------------
# Mouse decoding
# ---------------------------------------------------------------------------
def decode_sgr_mouse(seq):
    """Decode an SGR mouse sequence payload like b'<64;10;20M'.

    SGR encoding (mode 1006) sends: CSI < Cb ; Cx ; Cy M/m
    where M = press, m = release.
    """
    if not seq.startswith(b'<') or seq[-1:] not in (b'M', b'm'):
        return None
    try:
        parts = seq[1:-1].decode("ascii").split(";")
        cb, cx, cy = int(parts[0]), int(parts[1]), int(parts[2])
    except (ValueError, IndexError, UnicodeDecodeError):
        return None
    # A motion report is never a release, whatever terminator was used:
    # xterm ends motion with 'M', Windows Terminal ends button-less motion
    # with 'm'.  The motion bit (0x20) settles it on both.
    is_rel = seq[-1:] == b'm' and not (cb & 0x20)
    return ('mouse', cb, cx, cy, is_rel)


def decode_legacy_mouse(payload):
    """Decode legacy X10/VT200 mouse payload bytes (Cb, Cx, Cy).

    Legacy encoding sends: CSI M Cb Cx Cy
    where each byte is the value + 32 (to avoid control characters).
    """
    if len(payload) != 3:
        return None
    cb = payload[0] - 32
    cx = payload[1] - 32
    cy = payload[2] - 32
    if cb < 0 or cx < 1 or cy < 1:
        return None
    is_rel = (cb & 0b11) == 0b11 and not (cb & 0x40) and not (cb & 0x20)
    return ('mouse', cb, cx, cy, is_rel)


def wheel_code(cb):
    """Return canonical wheel code 64 (up) / 65 (down), or None.

    Wheel events set bit 6 (0x40). The low two bits encode direction:
    0 = scroll up, 1 = scroll down. Modifier keys (shift/ctrl/meta) set
    bits 2–4 but don't change the direction, so we mask them off.
    """
    if not (cb & 0x40):
        return None
    base = cb & 0b11
    if base in (0, 1):
        return 64 + base
    return None


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------
_ARROWS = {b'A': 'up', b'B': 'down', b'C': 'right', b'D': 'left',
           b'H': 'home', b'F': 'end', b'Z': 'shift-tab'}
_TILDE = {b'1': 'home', b'4': 'end', b'5': 'pgup', b'6': 'pgdn',
          b'7': 'home', b'8': 'end', b'3': 'delete'}


def _read_utf8(fd, lead):
    """The character a UTF-8 lead byte starts, or None if it is broken."""
    o = lead[0]
    if 0xC0 <= o < 0xE0:
        extra = 1
    elif 0xE0 <= o < 0xF0:
        extra = 2
    elif 0xF0 <= o < 0xF8:
        extra = 3
    else:
        return None  # stray continuation byte or invalid lead
    buf = bytearray(lead)
    for _ in range(extra):
        c = _read_byte_timeout(fd, 0.05)
        if c is None:
            return None
        buf.extend(c)
    try:
        return buf.decode('utf-8')
    except UnicodeDecodeError:
        return None


def _read_escape(fd):
    """What an ESC just read begins: an arrow, a mouse report, the
    terminal's answer to a query, or the Esc key alone.

    Reads the whole of a CSI or SS3 sequence, so none of its bytes is
    later taken for a key.
    """
    # 150ms — 50ms is too short when the system is busy rendering; mouse
    # release sequences (\033[<0;x;ym) can arrive late and the \033 would
    # be read as a bare ESC.
    b2 = _read_byte_timeout(fd, 0.15)
    if b2 is None:
        return 'escape'
    if b2 == b'\033':
        # Esc, with the next sequence already arriving behind it: the
        # second ESC goes back to start the next read
        unread(b2)
        return 'escape'
    if b2 == b'[':
        seq = bytearray()
        while True:
            c = _read_byte_timeout(fd, 0.15)
            if c is None:
                break
            if c == b'\033':
                # a sequence cut short by the next one: drop this one,
                # keep the next whole
                unread(c)
                return None
            seq.extend(c)
            # Legacy mouse: \033[M Cb Cx Cy
            if c == b'M' and len(seq) == 1:
                tail = bytearray()
                for _ in range(3):
                    c_tail = _read_byte_timeout(fd, 0.15)
                    if c_tail is None:
                        return None
                    tail.extend(c_tail)
                return decode_legacy_mouse(bytes(tail))
            c0 = c[0]
            if (65 <= c0 <= 90) or (97 <= c0 <= 122) or c0 == 126:
                break
        action = decode_sgr_mouse(bytes(seq))
        if action is not None:
            return action
        final = bytes(seq[-1:]) if seq else b''
        if final == b'R' and seq[:1].isdigit():
            # a cursor position report: the terminal has reached the query
            # the loop sent after its last frame
            return 'ack'
        if final == b'~':
            return _TILDE.get(bytes(seq[:1]))
        return _ARROWS.get(final)
    if b2 == b'O':
        # SS3 sequence (some terminals send arrows this way)
        b3 = _read_byte_timeout(fd, 0.15)
        if b3 is not None:
            return _ARROWS.get(b3)
        return None
    # Alt+key arrives as ESC then the key; read it as the key alone
    unread(b2)
    return 'escape'


# Control bytes the game's desk listens for under cbreak (Ctrl-C still
# signals; Ctrl-S/Q stay with flow control, so they're avoided).
_CTRL = {b'\x02': 'b', b'\x0c': 'l', b'\x10': 'p', b'\x0f': 'o',
         b'\x16': 'v', b'\x17': 'w'}


def read_key(fd, text=False, raw=False):
    """Read one key or mouse report from stdin in cbreak mode.

    Returns an action: 'up'/'down'/'left'/'right' (and 'home', 'end',
    'pgup', 'pgdn', 'tab', 'shift-tab'), 'escape', 'ack', a mouse tuple
    ('mouse', cb, col, row, released), 'key:enter', 'key:backspace', or
    a key — or None for input that means nothing here.

    By default a printable key is 'key:<c>' as typed (case kept), except
    q (quit) and space ('reset': play/pause).  ``raw=True`` hands every
    printable over as 'key:<c>', space and q included, plus Ctrl-letter
    keys as 'key:ctrl-<c>' — for the game's command bar.  ``text=True``
    (a caller-drawn text field is open) gives 'char:<c>' for any
    printable character, multi-byte UTF-8 included, plus 'key:kill'
    (Ctrl-U) and 'key:ctrl-w' for editing.  Escape sequences decode the
    same in every mode, so arrows still move a result list while typing.
    """
    b = read_byte(fd)
    if b is None:
        return None
    if b == b'\033':
        return _read_escape(fd)
    if b in (b'\r', b'\n'):
        return 'key:enter'
    if b in (b'\x7f', b'\x08'):
        return 'key:backspace'
    if b == b'\t':
        return 'tab'
    if text:
        if b == b'\x15':
            return 'key:kill'
        if b == b'\x17':
            return 'key:ctrl-w'
        o = b[0]
        if o < 0x20:
            return None
        if o < 0x80:
            return 'char:' + chr(o)
        ch = _read_utf8(fd, b)
        return 'char:' + ch if ch is not None else None
    if raw:
        ctrl = _CTRL.get(b)
        if ctrl is not None:
            return f'key:ctrl-{ctrl}'
        if 32 <= b[0] < 127:
            return f"key:{b.decode('ascii')}"
        return None
    if b in (b'q', b'Q'):
        return 'quit'
    if b == b' ':
        return 'reset'
    ctrl = _CTRL.get(b)
    if ctrl is not None:
        return f'key:ctrl-{ctrl}'
    if 32 < b[0] < 127:
        return f"key:{b.decode('ascii')}"
    return None
