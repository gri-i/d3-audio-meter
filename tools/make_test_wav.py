"""Generates a test WAV for the Audio Meter plugin, plus a cue sheet.

Every level below is the PEAK level in dBFS, i.e. what the plugin's peak
readout should show for that section.

    python tools/make_test_wav.py            -> test/audio_meter_test.wav
"""

import wave
from pathlib import Path

import numpy as np

SR = 48000
OUT = Path(__file__).resolve().parent.parent / "test"
rng = np.random.default_rng(1)


def amp(db):
    return 10 ** (db / 20)


def t(sec):
    return np.arange(int(sec * SR)) / SR


def fade(x, ms=10):
    # Short fades so section edges don't click (clicks would spike the spectrum).
    n = int(SR * ms / 1000)
    ramp = np.linspace(0, 1, n)
    x = x.copy()
    x[:n] *= ramp[:, None]
    x[-n:] *= ramp[::-1, None]
    return x


def stereo(l, r=None):
    return np.stack([l, l if r is None else r], axis=1)


def sine(sec, db, f=1000):
    return stereo(amp(db) * np.sin(2 * np.pi * f * t(sec)))


def silence(sec):
    return np.zeros((int(sec * SR), 2))


def normalize_peak(x, db):
    return x / np.abs(x).max() * amp(db)


def pink(n):
    # Voss-style approximation via 1/f filtering in the frequency domain.
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / SR)
    f[0] = f[1]
    return np.fft.irfft(spec / np.sqrt(f), n)


sections = []  # (title, what to look for, samples)


def add(title, expect, x, fades=True):
    sections.append((title, expect, fade(x) if fades else x))


add("Тишина", "метры пустые, пики -∞", silence(2), fades=False)
add("1 кГц, -20 dBFS", "зелёный столбик, цифры -20.0 белые, черта зелёная", sine(5, -20))
add("1 кГц, -9 dBFS", "жёлтый выше -12, цифры -9.0 белые, черта жёлтая", sine(5, -9))
add("1 кГц, -2 dBFS", "черта удержания КРАСНАЯ, цифры -2.0 жёлтые", sine(5, -2))
add("1 кГц, 0 dBFS", "цифры 0.0 красные, кнопка «Сброс» краснеет (клип)", sine(3, 0))
add("Пауза", "столбик падает, цифры держат максимум до «Сброс»", silence(3), fades=False)

ramp_t = t(12)
ramp_db = -60 + 60 * ramp_t / ramp_t[-1]
add("Нарастание -60 → 0 dBFS за 12 с",
    "столбик растёт, зелёный→жёлтый на -12, черта жёлтая→красная на -3",
    stereo(amp(ramp_db) * np.sin(2 * np.pi * 1000 * ramp_t)))

add("Только левый, -12 dBFS", "двигается только L", stereo(sine(3, -12)[:, 0], np.zeros(3 * SR)))
add("Только правый, -12 dBFS", "двигается только R", stereo(np.zeros(3 * SR), sine(3, -12)[:, 0]))

sweep_t = t(15)
k = np.log(20000 / 20) / sweep_t[-1]
phase = 2 * np.pi * 20 * (np.exp(k * sweep_t) - 1) / k
add("Свип 20 Гц → 20 кГц, -12 dBFS", "пик спектра проходит слева направо за 15 с",
    stereo(amp(-12) * np.sin(phase)))

for f in (50, 100, 1000, 10000):
    add(f"{f} Гц, -12 dBFS", f"один пик спектра на отметке {f if f < 1000 else str(f // 1000) + 'k'}",
        sine(2, -12, f))

add("Розовый шум, -6 dBFS пик", "спектр заполнен по всей ширине", stereo(normalize_peak(pink(5 * SR), -6)))

mono = normalize_peak(rng.standard_normal(4 * SR), -12)
add("Моно (L = R)", "корреляция +1.00, зелёная", stereo(mono))
wide = normalize_peak(rng.standard_normal((4 * SR, 2)), -12)
add("Независимые L и R", "корреляция около 0, жёлтая", wide)
add("Противофаза (R = -L)", "корреляция -1.00, красная", stereo(mono, -mono))

add("Тишина", "всё падает к нулю", silence(3), fades=False)

OUT.mkdir(exist_ok=True)
audio = np.concatenate([s for _, _, s in sections])
pcm = np.round(np.clip(audio, -1, 32767 / 32768) * 32768).astype("<i2")
with wave.open(str(OUT / "audio_meter_test.wav"), "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(pcm.tobytes())

lines = ["Audio Meter — тестовый файл audio_meter_test.wav (48 кГц, 16 бит, стерео)", ""]
pos = 0
for title, expect, s in sections:
    lines.append(f"{pos // SR // 60:02d}:{pos // SR % 60:02d}  {title:<34} {expect}")
    pos += len(s)
lines.append(f"{pos // SR // 60:02d}:{pos // SR % 60:02d}  конец")
(OUT / "audio_meter_test.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n".join(lines))
