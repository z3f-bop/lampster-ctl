"""What the lamp hears and what it does about it: spectrum, beat and riser detection, tempo, and the light each
moment calls for. No I/O and no clock: the caller passes audio and the time, so the same code drives the lamp, the
monitor page and an offline replay of a recording."""
import colorsys
import random
from collections import deque, namedtuple

from .config import (
    AUDIO_FPS, BASS_HZ, BEAT_RATIO, BLOCK, DANCE_BPM, DECAY, DROP_FLASH, FLASH_JUMP, FLOOR, HIGH_HZ, KICK_SHARE, MIN_BEAT_GAP,
    PEAK_DECAY, PRIOR_BPM, QUIET_FADE, QUIET_LEVEL, RISER_RATIO, RISER_SECONDS, RISER_STROBE_HZ, RISER_TIMEOUT, SILENCE, STROBE_HZ,
    TEMPO_JUMP, TEMPO_SMOOTH, TEMPO_SNAP, TEMPO_WINDOW, WHITE_EVERY, WINDOW,
)

Features = namedtuple("Features", "bass high bands")
Frame = namedtuple("Frame", "rgb white kicked hit riser flash level bpm lamp_bpm")


class Spectrum:
    """A sliding window over the audio, reporting the energy in the bands the lamp listens to.
    With bands=True it also reports ten log-spaced band levels, which the monitor draws."""

    def __init__(self, rate, bands=False):
        import numpy as np

        self.np = np
        self.window = np.hanning(WINDOW)
        self.ring = np.zeros(WINDOW, dtype=np.float32)
        self.slices = {name: slice(*(int(hz * WINDOW / rate) for hz in span)) for name, span in (("bass", BASS_HZ), ("high", HIGH_HZ))}
        self.groups = None
        if bands:
            freqs = np.fft.rfftfreq(WINDOW, 1 / rate)
            edges = np.geomspace(40, 12000, 11)
            self.groups = [(freqs >= lo) & (freqs < hi) for lo, hi in zip(edges, edges[1:])]

    def __call__(self, block):
        np = self.np
        self.ring = np.concatenate([self.ring[len(block):], block])
        spectrum = np.abs(np.fft.rfft(self.ring * self.window))
        bass, high = (float(np.sqrt(np.mean(spectrum[self.slices[n]] ** 2))) for n in ("bass", "high"))
        bands = [float(np.log1p(spectrum[g].mean())) for g in self.groups] if self.groups else None
        return Features(bass, high, bands)


def fold_tempo(energies, block_rate):
    """Tempo from the autocorrelation of the bass onsets, folded into DANCE_BPM."""
    import numpy as np

    flux = np.maximum(0, np.diff(np.asarray(energies)))
    flux = flux - flux.mean()
    ac = np.maximum(0, np.correlate(flux, flux, "full")[len(flux) - 1:])
    lags = np.arange(int(block_rate * 60 / 200), min(len(ac), int(block_rate * 60 / 55)))
    if len(lags) == 0 or ac[0] <= 0:
        return None
    bpms = 60 * block_rate / lags
    bpm = float(bpms[np.argmax(ac[lags] * np.exp(-0.5 * (np.log2(bpms / PRIOR_BPM) / 0.6) ** 2))])
    while bpm < DANCE_BPM[0]:
        bpm *= 2
    while bpm >= DANCE_BPM[1]:
        bpm /= 2
    return bpm


class Brain:
    """Feed it (bass, high) energies per audio block and the time; it returns the Frame the lamp should show.

    Kicks: bass well above its running average and a share of the track's own recent peak, so volume does not
    matter. Tempo: autocorrelation of the last TEMPO_WINDOW seconds of bass, folded into DANCE_BPM and smoothed. A
    lamp beat (a hit) lands on a kick that is not faster than the dance tempo, or fills in between slow kicks.
    Risers: the highs climb with no kick; the whites then swell and strobe, and the kick's return flashes once."""

    def __init__(self, rate, block=BLOCK, rng=None):
        self.rng = rng or random.Random()
        self.block_rate = rate / block
        self.history = deque(maxlen=int(self.block_rate * TEMPO_WINDOW))
        self.hue, self.level, self.last_beat, self.flash_until = self.rng.random(), FLOOR, 0.0, 0.0
        self.bass_avg = self.high_slow = self.high_fast = None
        self.peak, self.riser, self.quiet_since, self.white_hit = 0.0, None, None, False
        self.kicks, self.risers, self.drops, self.hits = 0, 0, 0, 0
        self.period, self.last_hit, self.last_tempo, self.tempo_known, self.disagreed = 60 / PRIOR_BPM, 0.0, 0.0, False, 0
        self.detected = None  # the last raw tempo from the detector, before smoothing
        self.phase, self.tick_at = 0.0, None  # the riser strobe's phase in cycles, integrated tick by tick
        self.shown, self.held, self.edges = 0.0, (bytes(3), bytes(2)), deque()  # what the flash limiter remembers

    def hit(self, when):
        """One beat of the lamp: a spike, in a new colour or (every WHITE_EVERY-th) white."""
        self.last_hit, self.hits, self.level = when, self.hits + 1, 1.0
        self.white_hit = bool(WHITE_EVERY) and self.hits % WHITE_EVERY == 0
        if not self.white_hit:
            self.hue = (self.hue + self.rng.uniform(0.2, 0.6)) % 1

    def step(self, blocks, now):
        kicks, hits = self.kicks, self.hits
        for bass, high in blocks:
            self.peak = max(bass, self.peak * PEAK_DECAY)  # follows the track's own loudness, so volume does not matter
            if self.bass_avg is None:
                self.bass_avg, self.high_slow, self.high_fast = bass, high, high
            if bass < SILENCE and high < SILENCE:
                self.quiet_since = now if self.quiet_since is None else self.quiet_since
            else:
                self.quiet_since = None
            self.history.append(bass)
            self.bass_avg += (bass - self.bass_avg) / 43
            self.high_slow += (high - self.high_slow) / 130
            self.high_fast += (high - self.high_fast) / 20
            if bass > SILENCE and bass > BEAT_RATIO * self.bass_avg and bass > KICK_SHARE * self.peak and now - self.last_beat > MIN_BEAT_GAP:
                self.last_beat, self.kicks = now, self.kicks + 1
                if self.riser is not None and now - self.riser > 1.5:
                    self.flash_until, self.drops = now + DROP_FLASH, self.drops + 1
                self.riser = None  # a kick ends any riser, with a flash only if it had built long enough
                if now - self.last_hit >= 0.75 * self.period:  # faster kicks than the dance tempo are skipped
                    self.hit(now)
            elif self.riser is None and now - self.last_beat > 1.0 and high > SILENCE and self.high_fast > RISER_RATIO * self.high_slow:
                self.riser, self.risers, self.phase = now, self.risers + 1, 0.0
        if now - self.last_tempo > 1.0 and len(self.history) > self.block_rate * 4:
            self.last_tempo, self.detected = now, fold_tempo(self.history, self.block_rate)
            if self.detected:
                estimate = 60 / self.detected
                self.disagreed = self.disagreed + 1 if abs(estimate - self.period) > TEMPO_JUMP * self.period else 0
                if not self.tempo_known or self.disagreed >= TEMPO_SNAP:
                    self.period, self.tempo_known, self.disagreed = estimate, True, 0
                else:
                    self.period += (estimate - self.period) * TEMPO_SMOOTH
        if self.quiet_since is not None and now - self.quiet_since > 2:
            self.history.clear()  # between tracks: start the tempo over
            self.tempo_known = False
        if self.last_beat and self.quiet_since is None and self.riser is None and now - self.last_beat < 2.5 and now - self.last_hit >= 1.1 * self.period:
            self.hit(now)  # kicks slower than the dance tempo get the beats in between
        if self.riser is not None and (now - self.riser > RISER_TIMEOUT or self.high_fast < 0.7 * self.high_slow):
            self.riser = None  # the build fizzled out
        fade = 0 if self.quiet_since is None else min(1, max(0, (now - self.quiet_since - 0.5) / QUIET_FADE))
        self.level = max(FLOOR + (QUIET_LEVEL - FLOOR) * fade, self.level * DECAY)
        dt, self.tick_at = (now - self.tick_at if self.tick_at is not None else 0.0), now
        rgb, white = self.render(now, dt)
        rgb, white, skipped = self.limit(now, rgb, white)
        return Frame(rgb, white, self.kicks > kicks, self.hits > hits, self.riser is not None,
                     now < self.flash_until and not skipped, self.level, self.detected,
                     60 / self.period if self.tempo_known else None)

    def limit(self, now, rgb, white):
        """Photosensitivity: never more than STROBE_HZ flashes in any second, whatever asked for them (a riser's
        strobe, the drop flash, a hail of fast kicks). A flash that would be one too many is skipped: the light
        stays as it was."""
        light = max(max(rgb), max(white)) / 100
        skipped = False
        if light - self.shown >= FLASH_JUMP:
            while self.edges and now - self.edges[0] >= 1.0:
                self.edges.popleft()
            if len(self.edges) >= STROBE_HZ:
                (rgb, white), light, skipped = self.held, self.shown, True
            else:
                self.edges.append(now)
        self.shown, self.held = light, (rgb, white)
        return rgb, white, skipped

    def render(self, now, dt=0.0):
        """The light for this moment, as 0-100 levels (rgb bytes, white warm and cool bytes), before brightness scaling."""
        if now < self.flash_until:
            return bytes([100] * 3), bytes([100, 100])
        if self.riser is not None:
            swell = min(1.0, (now - self.riser) / RISER_SECONDS)
            hz = min(RISER_STROBE_HZ, (1 / self.period) * (0.5 + 3.5 * swell))  # beat-synced, ramping up, never past the cap
            self.phase += hz * dt  # integrated: elapsed * hz would flash faster than hz while hz is still rising
            lit = int(self.phase * 2) % 2 == 0
            return bytes(3), bytes([round(60 * (1 - swell)), round(100 * swell)]) if lit else bytes(2)
        if self.white_hit:
            return bytes(3), bytes([round(30 * self.level), round(100 * self.level)])  # crisp white: a little warm under the cool
        return bytes(round(v * 100) for v in colorsys.hsv_to_rgb(self.hue, 1, self.level)), bytes(2)


def simulate(samples, rate, fps=AUDIO_FPS, rng=None):
    """Run recorded mono samples through Spectrum and Brain the way the lamp loop does, one tick per 1/fps
    seconds; yields (seconds into the recording, Frame). The clock starts at 100 s: the state treats 0.0 as 'never'."""
    spectrum, brain = Spectrum(rate), Brain(rate, rng=rng)
    blocks = len(samples) // BLOCK
    heard = [spectrum(samples[i * BLOCK:(i + 1) * BLOCK]) for i in range(blocks)]
    done = 0
    for tick in range(int(blocks * BLOCK / rate * fps)):
        t = tick / fps
        batch = []
        while done < blocks and (done + 1) * BLOCK / rate <= t:
            batch.append((heard[done].bass, heard[done].high))
            done += 1
        yield t, brain.step(batch, 100.0 + t)
