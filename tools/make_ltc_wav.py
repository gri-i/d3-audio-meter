"""Generates an LTC test WAV (LTC on the left channel) with deliberate faults.

    python tools/make_ltc_wav.py            -> test/ltc_test_25fps.wav + cue sheet
    python tools/make_ltc_wav.py --fps 29.97 --df
"""

import argparse
import wave
from pathlib import Path

import numpy as np

SR = 48000
OUT = Path(__file__).resolve().parent.parent / "test"
rng = np.random.default_rng(2)


def frame_bits(tc, df):
    h, m, s, f = tc
    bits = [0] * 80

    def put(start, n, v):
        for i in range(n):
            bits[start + i] = (v >> i) & 1

    put(0, 4, f % 10); put(8, 2, f // 10)
    bits[10] = int(df)
    put(16, 4, s % 10); put(24, 3, s // 10)
    put(32, 4, m % 10); put(40, 3, m // 10)
    put(48, 4, h % 10); put(56, 2, h // 10)
    for i, b in enumerate("0011111111111101"):
        bits[64 + i] = int(b)
    return bits


def next_tc(tc, base, df):
    h, m, s, f = tc
    f += 1
    if f >= base:
        f, s = 0, s + 1
        if s >= 60:
            s, m = 0, m + 1
            if m >= 60:
                m, h = 0, (h + 1) % 24
            if df and m % 10 != 0:
                f = 2
    return (h, m, s, f)


def tc_from_seconds(sec, base):
    sec = int(sec)
    return (sec // 3600 % 24, sec // 60 % 60, sec % 60, 0)


class Encoder:
    """Continuous biphase-mark stream; timing in exact fractional samples."""

    def __init__(self, fps, df):
        self.fps, self.df = fps, df
        self.base = 24 if fps < 24.5 else 25 if fps < 27.5 else 30
        self.half = SR / (fps * 160)  # samples per half bit cell
        self.t = 0.0  # fractional sample position of the next half-cell edge
        self.level = 1.0

    def frames(self, tc, n_frames):
        """Levels for n frames starting at tc; returns (samples, next tc)."""
        halves = []
        for _ in range(n_frames):
            for b in frame_bits(tc, self.df):
                self.level = -self.level  # transition at every cell start
                halves.append(self.level)
                if b:
                    self.level = -self.level  # extra mid-cell transition for "1"
                halves.append(self.level)
            tc = next_tc(tc, self.base, self.df)
        edges = self.t + self.half * np.arange(len(halves) + 1)
        self.t = edges[-1] - np.floor(edges[-1])
        idx = np.floor(edges).astype(int) - int(np.floor(edges[0]))
        out = np.empty(idx[-1])
        for i, v in enumerate(halves):
            out[idx[i]:idx[i + 1]] = v
        # ~50 us rise time like real LTC generators instead of perfect squares.
        k = np.ones(3) / 3
        return np.convolve(out, k, mode="same"), tc


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fps", type=float, default=25.0)
    p.add_argument("--df", action="store_true")
    args = p.parse_args()
    enc = Encoder(args.fps, args.df)
    fr = lambda sec: int(round(sec * args.fps))  # noqa: E731
    amp = lambda db: 10 ** (db / 20)  # noqa: E731

    sections = []
    tc = (10, 0, 0, 0)

    def add(title, expect, x):
        sections.append((title, expect, x))

    x, tc = enc.frames(tc, fr(20))
    add("Норма, -12 dBFS, с 10:00:00:00", "LTC OK, таймкод идёт ровно", amp(-12) * x)

    tc = (10, 0, 30, 0)
    x, tc = enc.frames(tc, fr(10))
    add("Скачок на 10:00:30:00", "событие «скачок 10:00:19:24 → 10:00:30:00»", amp(-12) * x)

    add("Пропадание 1 с", "событие «пропал сигнал», статус красный", np.zeros(SR))
    tc = (10, 0, 41, 0)

    x, tc = enc.frames(tc, fr(10))
    add("Перегруз: клип 0 dBFS", "метр в красное, клип; таймкод при этом читается",
        np.clip(amp(+6) * x, -1, 1))

    x, tc = enc.frames(tc, fr(10))
    add("Слабый сигнал, -40 dBFS", "таймкод читается, метр низко", amp(-40) * x)

    x, tc = enc.frames(tc, fr(10))
    noisy = amp(-12) * x + amp(-24) * rng.standard_normal(len(x))
    add("Шум -24 dBFS поверх", "дрожание фронтов растёт, ошибок нет или мало", noisy)

    add("Конец: тишина", "пропал сигнал", np.zeros(2 * SR))

    left = np.concatenate([s for _, _, s in sections])
    audio = np.stack([left, np.zeros_like(left)], axis=1)
    pcm = np.round(np.clip(audio, -1, 32767 / 32768) * 32768).astype("<i2")
    name = f"ltc_test_{args.fps:g}fps{'_df' if args.df else ''}"
    OUT.mkdir(exist_ok=True)
    with wave.open(str(OUT / f"{name}.wav"), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())

    lines = [f"{name}.wav — LTC {args.fps:g} fps{' DF' if args.df else ''} на левом канале, правый пустой", ""]
    pos = 0
    for title, expect, s in sections:
        lines.append(f"{pos // SR // 60:02d}:{pos // SR % 60:02d}  {title:<32} {expect}")
        pos += len(s)
    lines.append(f"{pos // SR // 60:02d}:{pos // SR % 60:02d}  конец")
    (OUT / f"{name}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
