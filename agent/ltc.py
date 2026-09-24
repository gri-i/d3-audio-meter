"""SMPTE LTC decoder for one audio channel, fed block by block.

LTC is biphase-mark coded: every bit cell starts with a transition, a "1" has
an extra transition mid-cell. 80 bits per frame, ending in the sync word
0011 1111 1111 1101. Besides the timecode this tracks what matters for a show
feed: lock, frame rate, continuity (jumps/repeats), dropouts and edge jitter.
"""

import time
from collections import deque

import numpy as np

SYNC_FWD = 0x3FFD  # bits 64..79 as received, oldest bit highest
SYNC_REV = 0xBFFC  # same word when the tape runs backwards
MASK80 = (1 << 80) - 1
RATES = (23.976, 24.0, 25.0, 29.97, 30.0)
ENV_WINDOW = 64  # samples for the threshold envelope: ~2.5 bit cells at 24 fps / 48 kHz


def _base(rate):
    return 24 if rate < 24.5 else 25 if rate < 27.5 else 30


def _tc_str(tc, df):
    h, m, s, f = tc
    return f"{h:02d}:{m:02d}:{s:02d}{';' if df else ':'}{f:02d}"


def _frame_index(tc, base, df):
    """Absolute frame number of a timecode (drop-frame aware), for jump sizes."""
    h, m, s, f = tc
    n = (3600 * h + 60 * m + s) * base + f
    if df:
        minutes = 60 * h + m
        n -= 2 * (minutes - minutes // 10)
    return n


def _next_tc(tc, base, df):
    h, m, s, f = tc
    f += 1
    if f >= base:
        f, s = 0, s + 1
        if s >= 60:
            s, m = 0, m + 1
            if m >= 60:
                m, h = 0, (h + 1) % 24
            # Drop-frame skips frames 0 and 1 at every minute not divisible by 10.
            if df and m % 10 != 0:
                f = 2
    return (h, m, s, f)


class LtcDecoder:
    def __init__(self, samplerate):
        self.sr = samplerate
        self.pos = 0  # absolute sample index of the start of the next block
        self.state = 0  # last non-zero sign, for hysteresis across blocks
        self.last_edge = None
        self.tail = np.zeros(ENV_WINDOW - 1, dtype=np.float32)  # |x| carried into the next block
        self.T = None  # samples per bit cell
        self.startup = []
        self.half = False  # saw the first half of a "1"
        self.reg = 0
        self.jitter = 0.0

        self.tc = None
        self.df = False
        self.reverse = False
        self.frame_pos = []  # sample positions of recent frames, for the rate
        self.last_frame_at = None
        self.locked = False
        self.rate = None

        self.frames = 0
        self.jumps = 0
        self.dropouts = 0
        self.bit_errors = 0  # counted only while locked: acquiring lock is not a fault
        self.good = 0  # frames that arrived in sequence
        self.lost = 0  # frame slots with no usable frame: unreadable, corrupted or during a dropout
        self.loss_log = deque()  # (wall time, frames) for the last-minute figure
        self.pending = None  # candidate new position after an unexpected frame
        self.last_event = None  # (wall time, text)

    # ---- edge / bit level ----

    def process(self, x):
        n = len(x)
        if not n:
            return
        # Threshold from the local peak over ~2 bit cells, per sample: a sudden
        # level drop (or a block mixing loud and quiet parts) must not blind the
        # decoder for several blocks and look like a dropout.
        xa = np.concatenate((self.tail, np.abs(x)))
        local = np.lib.stride_tricks.sliding_window_view(xa, ENV_WINDOW).max(axis=1)
        self.tail = xa[-(ENV_WINDOW - 1):]
        h = np.maximum(0.15 * local, 0.003)  # below ~-50 dBFS nothing is decoded
        s = np.where(x > h, 1, np.where(x < -h, -1, 0)).astype(np.int8)
        # Forward-fill the dead zone with the previous sign (hysteresis).
        nz = s != 0
        if nz.any():
            idx = np.where(nz, np.arange(n), -1)
            np.maximum.accumulate(idx, out=idx)
            filled = np.where(idx >= 0, s[np.maximum(idx, 0)], self.state)
            prev = np.concatenate(([self.state], filled[:-1]))
            edges = np.nonzero((filled != prev) & (filled != 0) & (prev != 0))[0]
            for e in edges:
                self._edge(self.pos + int(e))
            self.state = int(filled[-1])
        self.pos += n
        self._check_dropout()

    def _edge(self, p):
        if self.last_edge is None:
            self.last_edge = p
            return
        d = p - self.last_edge
        self.last_edge = p
        if self.T is None:
            self.startup.append(d)
            if len(self.startup) >= 40:
                # Sync words guarantee plenty of full-cell ("0") intervals.
                self.T = float(np.percentile(self.startup, 90))
                self.startup = []
            return
        T = self.T
        if d > 1.5 * T or d < 0.3 * T:
            # Gap or glitch: drop partial state; a long gap means we lost the signal.
            self.half = False
            if d < 6 * T:
                self.bit_errors += self.locked
            else:
                self.T = None
            return
        if d > 0.75 * T:  # full cell: "0"
            if self.half:
                self.bit_errors += self.locked
                self.half = False
            self._jit(d / T)
            self.T = 0.95 * T + 0.05 * d
            self._bit(0, p)
        else:  # half cell
            self._jit(2 * d / T)
            self.T = 0.95 * T + 0.05 * 2 * d
            if self.half:
                self.half = False
                self._bit(1, p)
            else:
                self.half = True

    def _jit(self, ratio):
        self.jitter = 0.98 * self.jitter + 0.02 * abs(ratio - 1.0)

    # ---- frame level ----

    def _bit(self, b, p):
        self.reg = ((self.reg << 1) | b) & MASK80
        low = self.reg & 0xFFFF
        if low == SYNC_FWD:
            self._frame(p, reverse=False)
        elif low == SYNC_REV:
            self._frame(p, reverse=True)

    def _field(self, bit, start, n):
        return sum(bit(start + i) << i for i in range(n))

    def _frame(self, p, reverse):
        reg = self.reg
        if reverse:
            # Received last-bit-first: bit k sits k+16 places from the bottom.
            bit = lambda k: (reg >> (k + 16)) & 1  # noqa: E731
        else:
            bit = lambda k: (reg >> (79 - k)) & 1  # noqa: E731
        f = self._field(bit, 0, 4) + 10 * self._field(bit, 8, 2)
        s = self._field(bit, 16, 4) + 10 * self._field(bit, 24, 3)
        m = self._field(bit, 32, 4) + 10 * self._field(bit, 40, 3)
        h = self._field(bit, 48, 4) + 10 * self._field(bit, 56, 2)
        if f > 29 or s > 59 or m > 59 or h > 23:
            self.bit_errors += self.locked
            return
        tc, df = (h, m, s, f), bool(bit(10))
        was_locked = self.locked

        # Frame slots since the previous decoded frame (>1 when noise ate a sync word).
        elapsed = 1
        if was_locked:
            elapsed = max(1, round((p - self.last_frame_at) / self.frame_len))
        self.frame_pos.append((p, elapsed))
        self.frame_pos = self.frame_pos[-50:]
        if len(self.frame_pos) >= 10:
            span = self.frame_pos[-1][0] - self.frame_pos[0][0]
            slots = sum(e for _, e in self.frame_pos[1:])
            measured = slots * self.sr / span
            self.rate = min(RATES, key=lambda r: abs(r - measured))
            self.measured = measured

        shown = tc
        if was_locked and self.tc is not None and self.rate and not reverse:
            base = _base(self.rate)
            # Frames whose sync was lost to noise still took their time slot:
            # advance by the elapsed frame count, not by one.
            self._lose(elapsed - 1)

            def advance(t):
                for _ in range(elapsed):
                    t = _next_tc(t, base, df)
                return t

            expect = advance(self.tc)
            if tc == expect:
                self.pending = None
                self.good += 1
            elif self.pending and tc == advance(self.pending):
                # Two consecutive frames agree on the new position: a real jump.
                # The first frame of it was counted lost while unconfirmed; it wasn't.
                old = self.tc
                self.lost -= 1
                t, n = self.loss_log.pop()
                if n > 1:
                    self.loss_log.append((t, n - 1))
                self.good += 2
                self.jumps += 1
                delta = _frame_index(self.pending, base, df) - _frame_index(old, base, df)
                if delta == -1:
                    what = "повтор кадра"
                else:
                    what = f"скачок {delta:+d} кадров ({delta / self.rate:+.2f} с)"
                self._event(f"{what}: ждали {_tc_str(old, df)}, пришло {_tc_str(self.pending, df)}")
                self.pending = None
            else:
                # Unconfirmed: either a corrupted frame or the first frame of a jump.
                # Keep counting on the old sequence until the next frame decides.
                self.pending = tc
                self._lose(1)
                shown = expect
        elif not was_locked and self.frames:
            # Back after a dropout: every frame slot of the gap was lost.
            gap = max(0, round((p - self.last_frame_at) / self.frame_len) - 1)
            self._lose(gap)
            self.good += 1
            rate = self.rate or self.sr / self.frame_len
            self._event(f"сигнал вернулся на {_tc_str(tc, df)}: пропало {gap} кадров ({gap / rate:.2f} с)")
        else:
            self.good += 1

        self.tc, self.df, self.reverse = shown, df, reverse
        self.last_frame_at = p
        self.frame_len = 80 * self.T  # kept even if the bit clock is lost later
        self.locked = True
        self.frames += 1

    def _check_dropout(self):
        if not self.locked:
            return
        if self.pos - self.last_frame_at > 4 * self.frame_len:
            self.locked = False
            self.frame_pos = []
            self.dropouts += 1
            self._event(f"пропал сигнал после {_tc_str(self.tc, self.df)}")

    def _lose(self, n):
        if n > 0:
            self.lost += n
            self.loss_log.append((time.time(), n))

    def _event(self, text):
        self.last_event = (time.time(), text)

    def _lost_last_minute(self):
        cutoff = time.time() - 60
        while self.loss_log and self.loss_log[0][0] < cutoff:
            self.loss_log.popleft()
        return sum(n for _, n in self.loss_log)

    def summary(self):
        if not self.frames:
            return None
        rate = None
        if self.rate:
            rate = f"{self.rate:g}" + (" DF" if self.df and self.rate > 29 else "")
        return {
            "tc": _tc_str(self.tc, self.df),
            "locked": self.locked,
            "rate": rate,
            "measured": round(getattr(self, "measured", 0.0), 3) or None,
            "reverse": self.reverse,
            "frames": self.frames,
            "jumps": self.jumps,
            "dropouts": self.dropouts,
            "bitErrors": self.bit_errors,
            "good": self.good,
            "lost": self.lost,
            "lostMinute": self._lost_last_minute(),
            # Frames missing right now, while the signal is gone.
            "missing": 0 if self.locked else round((self.pos - self.last_frame_at) / self.frame_len),
            "jitter": round(self.jitter * 100, 1),
            "lastEvent": None if not self.last_event else {
                "ago": round(time.time() - self.last_event[0], 1), "text": self.last_event[1]},
        }
