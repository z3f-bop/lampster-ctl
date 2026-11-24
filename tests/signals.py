"""Synthetic audio for the analysis tests: deterministic, no recordings, nothing copyrighted."""
import numpy as np

RATE = 48000


def click_track(bpm, seconds, rate=RATE, hats=True):
    """A bass drum on every beat (a decaying 60 Hz burst) and, optionally, quiet hi-hats between them."""
    n, rng = int(seconds * rate), np.random.default_rng(1)
    x = np.zeros(n, dtype=np.float32)
    k = np.arange(int(0.08 * rate))
    kick = (0.8 * np.sin(2 * np.pi * 60 * k / rate) * np.exp(-k / (0.02 * rate))).astype(np.float32)
    hat = (np.diff(rng.standard_normal(int(0.03 * rate) + 1)) * 0.05 * np.exp(-np.arange(int(0.03 * rate)) / (0.006 * rate))).astype(np.float32)
    beat, t = 60 / bpm, 0.0
    while t < seconds:
        for sound, at in ((kick, t), (hat, t + beat / 2) if hats else (kick[:0], t)):
            i = int(at * rate)
            if i < n and len(sound):
                x[i:i + len(sound)] += sound[:n - i]
        t += beat
    return x


def silence(seconds, rate=RATE):
    return np.zeros(int(seconds * rate), dtype=np.float32)


def riser(seconds, rate=RATE, start=0.02, end=0.5):
    """Bright noise swelling from quiet to loud with no kick: what a build-up before a drop looks like."""
    n = int(seconds * rate)
    noise = np.diff(np.random.default_rng(2).standard_normal(n + 1)).astype(np.float32)
    return noise * np.linspace(start, end, n, dtype=np.float32)


def join(*parts):
    return np.concatenate(parts).astype(np.float32)
