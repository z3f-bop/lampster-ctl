#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["bleak", "numpy", "sounddevice"]
# ///
"""Control The Lampster over Bluetooth LE.

  lampster.py version          # print the version (also --version)
  lampster.py status
  lampster.py color R G B     # 0-255 each, or a single #rrggbb
  lampster.py white WARM COOL  # 0-100 each
  lampster.py mode off|white|color|both   # which LED banks are lit
  lampster.py fade SECONDS R G B|#rrggbb  # crossfade whatever is lit into that color
  lampster.py reactive [SECONDS]          # pulse to the bass heard on the audio input, whites on risers
                                          # LAMPSTER_AUDIO_INPUT=name part of an input device, or "system" for what the Mac plays
  lampster.py monitor [BPM]               # live browser view of the audio analysis, with keys to mark what you hear
  lampster.py disco [BPM [SECONDS]]       # a new colour every beat, an occasional 3 Hz strobe burst
  lampster.py sun [SPEED [HH:MM]]         # follow the sun and moon; SPEED>1 plays one day from HH:MM (default now)
                                          # needs LAMPSTER_LAT and LAMPSTER_LON (degrees, east positive)
"""
import asyncio
import colorsys
import json
import math
import os
import random
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime, timedelta

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError, BleakGATTProtocolError

VERSION = "0.1.0"
NAME = "Lampster"
BASE = "-ba5e-f4ee-5ca1-eb1e5e4b1ce0"
MODE, WHITE, COLOR = (f"01ff{h}{BASE}" for h in ("5554", "5556", "5559"))
MODES = {"off": 0x40, "white": 0xC0, "color": 0xA0, "both": 0xE0}
LEAVING_ROOM = 0x10  # app's "turn off when leaving room" flag, kept across mode writes
WHITE_BANK, COLOR_BANK = 0x40, 0x20  # bank bits of the mode byte
FADE_STEP = 0.25  # seconds between fade writes
LAT, LON = (float(os.environ.get(k, "nan")) for k in ("LAMPSTER_LAT", "LAMPSTER_LON"))  # degrees, east positive
STROBE_HZ = 3  # flashes per second, at the photosensitivity guideline limit; never raise this
STROBE_CHANCE = 0.3  # per 8-beat phrase
AUDIO_FPS = 40  # lamp updates per second in reactive mode
BASS_HZ = (30, 150)  # band whose energy marks a beat
BEAT_RATIO = 1.4  # a beat is bass this many times above its running average
MIN_BEAT_GAP = 0.15  # seconds between raw kick detections
DANCE_BPM = (82, 165)  # the range people move to (hard techno stays at full speed, drum and bass halves): faster is danced at half speed, slower at double
PRIOR_BPM = 110  # tempo people find most natural, used to break ties between octaves
TEMPO_WINDOW = 10  # seconds of bass history the tempo estimate looks at
TEMPO_SMOOTH = 0.08  # share of each new estimate taken per second, so the tempo drifts instead of jumping
TEMPO_JUMP = 0.12  # an estimate this far from the smoothed tempo...
TEMPO_SNAP = 5  # ...for this many seconds in a row is a new track, and the tempo snaps to it
HIGH_HZ = (2000, 8000)  # band whose climb marks a riser
KICK_SHARE = 0.3  # a kick must also reach this share of the recent peak, so volume does not matter
PEAK_DECAY = 0.999  # per audio block; the recent peak halves in about 16 s
RISER_RATIO = 1.3  # highs this far above their slow average, with no kick for a second, start a riser
RISER_SECONDS = 8  # how long the whites take to swell to full
RISER_TIMEOUT = 24
WHITE_EVERY = 4 # every Nth kick is white instead of a colour; 0 for colours only
MONITOR_PORT = 8787
CHANGE_NOVELTY = 0.2  # the last bar this far from the last 8 bars' average counts as a change in the groove
QUIET_LEVEL = 0.05  # brightness the lamp settles to once the music stops
QUIET_FADE = 5  # seconds it takes to get there, starting half a second after the last sound
DROP_FLASH = 0.15  # seconds of full light when the kick returns after a riser
SILENCE = 0.3  # energy below this is not a beat
DECAY = 0.7  # brightness kept per update after a beat; lower is spikier
FLOOR = 0.06  # dimmest the lamp gets between beats
SUN_TICK = 30  # seconds between sun updates at real-time speed
# (solar elevation deg, R, G, B, warm, cool): red at the horizon, white warming up and cooling by noon, dark by -12
MOON_LEVELS = (0, 0, 8, 0, 12)  # [R, G, B, warm, cool] a full moon at the zenith adds: faint cool blue-white
SUN_KEYS = [
    (-12, 0, 0, 0, 0, 0),
    (-8, 0, 0, 12, 0, 0),
    (-4, 20, 0, 30, 0, 0),
    (0, 100, 20, 0, 0, 0),
    (4, 60, 12, 0, 35, 0),
    (10, 20, 4, 0, 70, 0),
    (25, 0, 0, 0, 100, 35),
    (50, 0, 0, 0, 80, 100),
]
MODE_NAMES ={v: k for k, v in MODES.items()}


def pct(v255):
    return round(max(0, min(255, v255)) * 100 / 255)


def parse_color(args):
    if len(args) == 1:
        h = args[0].lstrip("#")
        return [pct(int(h[i:i + 2], 16)) for i in (0, 2, 4)]
    return [pct(int(a)) for a in args]


async def status(c):
    mode = (await c.read_gatt_char(MODE))[0]
    warm, cool = await c.read_gatt_char(WHITE)
    r, g, b = await c.read_gatt_char(COLOR)
    print(f"mode={MODE_NAMES.get(mode & ~LEAVING_ROOM, hex(mode))}{' +leaving-room' if mode & LEAVING_ROOM else ''} color(R,G,B %)={r},{g},{b} white(warm,cool %)={warm},{cool}")


async def fade(c, seconds, target):
    """Crossfade from what is lit now to a pure color, then switch to the RGB bank."""
    mode = (await c.read_gatt_char(MODE))[0]
    warm, cool = await c.read_gatt_char(WHITE)
    rgb = list(await c.read_gatt_char(COLOR))
    start_white = [warm, cool] if mode & WHITE_BANK else [0, 0]
    start_rgb = rgb if mode & COLOR_BANK else [0, 0, 0]
    keep = mode & LEAVING_ROOM
    await c.write_gatt_char(COLOR, bytes(start_rgb), response=False)
    await c.write_gatt_char(MODE, bytes([MODES["both"] | keep]), response=True)
    t0 = time.monotonic()
    while (t := min(1, (time.monotonic() - t0) / seconds)) < 1:
        await c.write_gatt_char(COLOR, bytes(round(a + (b - a) * t) for a, b in zip(start_rgb, target)), response=False)
        await c.write_gatt_char(WHITE, bytes(round(a * (1 - t)) for a in start_white), response=True)
        await asyncio.sleep(FADE_STEP)
    await c.write_gatt_char(COLOR, bytes(target), response=False)
    await c.write_gatt_char(WHITE, bytes([0, 0]), response=True)
    await c.write_gatt_char(MODE, bytes([MODES["color"] | keep]), response=True)


def _days(when):
    return when.timestamp() / 86400 + 2440587.5 - 2451545.0  # days since J2000


def _altitude(n, ra, dec):
    """Elevation in degrees at LAT/LON of a body at right ascension ra, declination dec (radians)."""
    hour_angle = math.radians((280.46061837 + 360.98564736629 * n + LON) % 360) - ra
    lat = math.radians(LAT)
    return math.degrees(math.asin(math.sin(lat) * math.sin(dec) + math.cos(lat) * math.cos(dec) * math.cos(hour_angle)))


def _sun_radec(n):
    """Low-precision almanac formulae."""
    g = math.radians((357.528 + 0.9856003 * n) % 360)
    lam = math.radians((280.46 + 0.9856474 * n) % 360 + 1.915 * math.sin(g) + 0.02 * math.sin(2 * g))
    eps = math.radians(23.439 - 4e-7 * n)
    return math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam)), math.asin(math.sin(eps) * math.sin(lam))


def _moon_radec(n):
    """Principal terms only: good to about a degree, plenty for a lamp."""
    lon = math.radians(218.316 + 13.176396 * n) + math.radians(6.289) * math.sin(math.radians(134.963 + 13.064993 * n))
    lat = math.radians(5.128) * math.sin(math.radians(93.272 + 13.229350 * n))
    eps = math.radians(23.439 - 4e-7 * n)
    ra = math.atan2(math.sin(lon) * math.cos(eps) - math.tan(lat) * math.sin(eps), math.cos(lon))
    return ra, math.asin(math.sin(lat) * math.cos(eps) + math.cos(lat) * math.sin(eps) * math.sin(lon))


def sun_elevation(when):
    n = _days(when)
    return _altitude(n, *_sun_radec(n))


def moon_light(when):
    """0..1: lit fraction of the moon, scaled by how high it stands."""
    n = _days(when)
    (sra, sdec), (mra, mdec) = _sun_radec(n), _moon_radec(n)
    elongation = math.acos(math.sin(sdec) * math.sin(mdec) + math.cos(sdec) * math.cos(mdec) * math.cos(sra - mra))
    return (1 - math.cos(elongation)) / 2 * max(0, math.sin(math.radians(_altitude(n, mra, mdec))))


def sun_levels(elevation):
    """[R, G, B, warm, cool] percents for a solar elevation, interpolated between SUN_KEYS."""
    if elevation <= SUN_KEYS[0][0]:
        return list(SUN_KEYS[0][1:])
    for (e0, *v0), (e1, *v1) in zip(SUN_KEYS, SUN_KEYS[1:]):
        if elevation <= e1:
            t = (elevation - e0) / (e1 - e0)
            return [round(a + (b - a) * t) for a, b in zip(v0, v1)]
    return list(SUN_KEYS[-1][1:])


async def sun(c, args):
    """Follow the sun's color and brightness. SPEED 1 is real time; above 1 it plays one day and stops."""
    speed = float(args[0]) if args else 1.0
    start = datetime.now().astimezone()
    if len(args) > 1:
        hour, minute = map(int, args[1].split(":"))
        start = start.replace(hour=hour, minute=minute, second=0, microsecond=0)
    keep = (await c.read_gatt_char(MODE))[0] & LEAVING_ROOM
    t0 = time.monotonic()
    last = None
    while True:
        sim = start + timedelta(seconds=(time.monotonic() - t0) * speed)
        if speed > 1 and sim - start > timedelta(hours=24):
            break
        elevation = sun_elevation(sim)
        moon = moon_light(sim) * max(0, min(1, -elevation / 6))  # the sun drowns the moon until it is 6 degrees down
        levels = [min(100, a + round(b * moon)) for a, b in zip(sun_levels(elevation), MOON_LEVELS)]
        if levels != last:
            await c.write_gatt_char(COLOR, bytes(levels[:3]), response=False)
            await c.write_gatt_char(WHITE, bytes(levels[3:]), response=True)
            if last is None:
                await c.write_gatt_char(MODE, bytes([MODES["both"] | keep]), response=True)
            last = levels
            print(f"{sim:%H:%M} sun {elevation:+5.1f}° moon {moon:.2f} rgb={levels[:3]} white={levels[3:]}", flush=True)
        await asyncio.sleep(max(0.5, SUN_TICK / speed))


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


SYSTEM_AUDIO = "system"  # LAMPSTER_AUDIO_INPUT value that taps the Mac's own output instead of an input device
TAP_SOURCE = os.path.join(os.path.dirname(os.path.realpath(__file__)), "systap.swift")
TAP_BINARY = os.path.expanduser("~/.cache/lampster-ctl/systap")


class SystemTap:
    """What the Mac is playing, tapped by systap.swift (Core Audio process tap, macOS 14.2+), with the same
    `with` and callback shape as sd.InputStream so the analysis does not care where its samples come from."""

    def __init__(self):
        import numpy as np

        self.np = np
        if not os.path.exists(TAP_BINARY) or os.path.getmtime(TAP_BINARY) < os.path.getmtime(TAP_SOURCE):
            os.makedirs(os.path.dirname(TAP_BINARY), exist_ok=True)
            subprocess.run(["swiftc", "-swift-version", "5", "-O", TAP_SOURCE, "-o", TAP_BINARY], check=True)
        self.proc = subprocess.Popen([TAP_BINARY], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        line = self.proc.stderr.readline().decode()
        if not line.startswith("rate="):
            self.proc.kill()
            sys.exit(f"system audio tap failed: {(line + self.proc.stderr.read().decode()).strip()}")
        self.rate = float(line.split()[0].removeprefix("rate="))

    def stream(self, blocksize, callback):
        self.blocksize, self.callback = blocksize, callback
        return self

    def __enter__(self):
        threading.Thread(target=self.pump, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.proc.terminate()
        self.proc.wait()

    def pump(self):
        while True:
            data = self.proc.stdout.read(self.blocksize * 4)  # float32 mono
            if len(data) < self.blocksize * 4:
                return
            block = self.np.frombuffer(data, dtype=self.np.float32).reshape(-1, 1)
            self.callback(block, self.blocksize, None, None)


def open_audio(device):
    """(sample rate, stream factory taking blocksize and callback) for LAMPSTER_AUDIO_INPUT."""
    if device == SYSTEM_AUDIO:
        tap = SystemTap()
        return tap.rate, tap.stream
    import sounddevice as sd

    rate = sd.query_devices(device, "input")["default_samplerate"]
    return rate, lambda blocksize, callback: sd.InputStream(
        device=device, channels=1, samplerate=rate, blocksize=blocksize, callback=callback
    )


async def reactive(c, args):
    """Flash a new colour on each bass beat. When the kick drops out and the highs climb (a riser) the whites
    swell and pulse, and the kick coming back is answered by one full flash."""
    import numpy as np

    seconds = float(args[0]) if args else None
    rate, listen = open_audio(os.environ.get("LAMPSTER_AUDIO_INPUT"))
    size = 2048
    window = np.hanning(size)
    bins = {name: slice(*(int(hz * size / rate) for hz in span)) for name, span in (("bass", BASS_HZ), ("high", HIGH_HZ))}
    ring = np.zeros(size, dtype=np.float32)
    heard = []

    def hear(indata, frames, time_info, status):
        nonlocal ring
        ring = np.concatenate([ring[frames:], indata[:, 0]])
        spectrum = np.abs(np.fft.rfft(ring * window))
        heard.append(tuple(float(np.sqrt(np.mean(spectrum[bins[n]] ** 2))) for n in ("bass", "high")))

    keep = (await c.read_gatt_char(MODE))[0] & LEAVING_ROOM
    await c.write_gatt_char(WHITE, bytes([0, 0]), response=True)
    await c.write_gatt_char(COLOR, bytes([0, 0, 0]), response=False)
    await c.write_gatt_char(MODE, bytes([MODES["both"] | keep]), response=True)  # RGB and white banks both live
    hue, level, last_beat, flash_until = random.random(), FLOOR, 0.0, 0.0
    bass_avg = high_slow = high_fast = None
    peak, riser, last_sent, quiet_since, white_hit = 0.0, None, None, None, False
    kicks, risers, drops, refused, hits = 0, 0, 0, 0, 0
    block_rate = rate / 1024
    history = deque(maxlen=int(block_rate * TEMPO_WINDOW))
    period, last_hit, last_tempo, tempo_known, disagreed = 60 / PRIOR_BPM, 0.0, 0.0, False, 0

    def hit(when):
        """One beat of the lamp: a spike, in a new colour or (every WHITE_EVERY-th) white."""
        nonlocal level, hue, white_hit, hits, last_hit
        last_hit, hits, level = when, hits + 1, 1.0
        white_hit = bool(WHITE_EVERY) and hits % WHITE_EVERY == 0
        if not white_hit:
            hue = (hue + random.uniform(0.2, 0.6)) % 1

    t0 = time.monotonic()
    with listen(1024, hear):
        while seconds is None or time.monotonic() - t0 < seconds:
            blocks, heard[:] = heard[:], []
            now = time.monotonic()
            for bass, high in blocks:
                peak = max(bass, peak * PEAK_DECAY)  # follows the track's own loudness, so volume does not matter
                if bass_avg is None:
                    bass_avg, high_slow, high_fast = bass, high, high
                if bass < SILENCE and high < SILENCE:
                    quiet_since = quiet_since or now
                else:
                    quiet_since = None
                history.append(bass)
                bass_avg += (bass - bass_avg) / 43
                high_slow += (high - high_slow) / 130
                high_fast += (high - high_fast) / 20
                if bass > SILENCE and bass > BEAT_RATIO * bass_avg and bass > KICK_SHARE * peak and now - last_beat > MIN_BEAT_GAP:
                    last_beat, kicks = now, kicks + 1
                    if riser and now - riser > 1.5:
                        flash_until, drops = now + DROP_FLASH, drops + 1
                    riser = None  # a kick ends any riser, with a flash only if it had built long enough
                    if now - last_hit >= 0.75 * period:  # faster kicks than the dance tempo are skipped
                        hit(now)
                elif riser is None and now - last_beat > 1.0 and high > SILENCE and high_fast > RISER_RATIO * high_slow:
                    riser, risers = now, risers + 1
            if now - last_tempo > 1.0 and len(history) > block_rate * 4:
                last_tempo, bpm = now, fold_tempo(history, block_rate)
                if bpm:
                    estimate = 60 / bpm
                    disagreed = disagreed + 1 if abs(estimate - period) > TEMPO_JUMP * period else 0
                    if not tempo_known or disagreed >= TEMPO_SNAP:
                        period, tempo_known, disagreed = estimate, True, 0
                    else:
                        period += (estimate - period) * TEMPO_SMOOTH
            if quiet_since and now - quiet_since > 2:
                history.clear()  # between tracks: start the tempo over
                tempo_known = False
            if last_beat and quiet_since is None and not riser and now - last_beat < 2.5 and now - last_hit >= 1.1 * period:
                hit(now)  # kicks slower than the dance tempo get the beats in between
            if riser and (now - riser > RISER_TIMEOUT or high_fast < 0.7 * high_slow):
                riser = None  # the build fizzled out
            fade = 0 if quiet_since is None else min(1, max(0, (now - quiet_since - 0.5) / QUIET_FADE))
            level = max(FLOOR + (QUIET_LEVEL - FLOOR) * fade, level * DECAY)
            if now < flash_until:
                out = (bytes([100] * 3), bytes([100, 100]))
            elif riser:
                swell = min(1.0, (now - riser) / RISER_SECONDS)
                beat_hz = 1 / period
                hz = min(STROBE_HZ, beat_hz * (0.5 + 3.5 * swell))  # beat-synced, ramping up, never past the cap
                lit = int((now - riser) * hz * 2) % 2 == 0
                out = (bytes(3), bytes([round(60 * (1 - swell)), round(100 * swell)]) if lit else bytes(2))
            elif white_hit:
                out = (bytes(3), bytes([round(30 * level), round(100 * level)]))  # crisp white: a little warm under the cool
            else:
                out = (bytes(round(v * 100) for v in colorsys.hsv_to_rgb(hue, 1, level)), bytes(2))
            if out != last_sent:
                await c.write_gatt_char(COLOR, out[0], response=False)
                if last_sent is None or out[1] != last_sent[1]:
                    try:
                        await c.write_gatt_char(WHITE, out[1], response=True)
                    except BleakGATTProtocolError:
                        out, refused = (out[0], None), refused + 1  # lamp was busy: whites go out again next tick
                last_sent = out
            await asyncio.sleep(1 / AUDIO_FPS)
    print(f"{kicks} kicks, {hits} lamp beats in {time.monotonic() - t0:.0f}s at ~{60 / period:.0f} BPM, "
          f"{risers} risers, {drops} drops, {refused} white writes refused, peak bass energy {peak:.1f}")


async def disco(c, args):
    """A new vivid colour on every beat; at the end of some 8-beat phrases a short white strobe burst."""
    beat = 60 / (float(args[0]) if args else 128.0)
    seconds = float(args[1]) if len(args) > 1 else None
    keep = (await c.read_gatt_char(MODE))[0] & LEAVING_ROOM
    await c.write_gatt_char(WHITE, bytes([0, 0]), response=True)
    await c.write_gatt_char(COLOR, bytes([0, 0, 0]), response=False)
    await c.write_gatt_char(MODE, bytes([MODES["color"] | keep]), response=True)
    hue, n, t0 = random.random(), 0, time.monotonic()
    while seconds is None or time.monotonic() - t0 < seconds:
        if n % 8 == 7 and random.random() < STROBE_CHANCE:
            for _ in range(4):
                for level in (100, 0):
                    await c.write_gatt_char(COLOR, bytes([level] * 3), response=False)
                    await asyncio.sleep(1 / (2 * STROBE_HZ))
            n = max(n + 1, math.ceil((time.monotonic() - t0) / beat))
        else:
            hue = (hue + random.uniform(0.2, 0.6)) % 1
            await c.write_gatt_char(COLOR, bytes(round(v * 100) for v in colorsys.hsv_to_rgb(hue, 1, 1)), response=False)
            n += 1
        await asyncio.sleep(max(0, t0 + n * beat - time.monotonic()))


MONITOR_HTML = """<!doctype html><meta charset=utf-8><title>Lampster monitor</title>
<style>
:root{--bg:#0d0d12;--fg:#e8e8f0;--dim:#8a8aa0;--grid:#23232e}
@media (prefers-color-scheme:light){:root{--bg:#f6f6fa;--fg:#14141c;--dim:#6a6a80;--grid:#dcdce6}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif}
main{max-width:1240px;margin:0 auto;padding:16px}
.row{display:flex;gap:24px;align-items:baseline;flex-wrap:wrap;margin-bottom:8px}
.big{font-size:40px;font-weight:700;font-variant-numeric:tabular-nums}
.dim{color:var(--dim)}
canvas{width:100%;height:420px;display:block;background:var(--bg);border:1px solid var(--grid);border-radius:8px}
button{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid var(--grid);background:transparent;color:var(--fg);cursor:pointer}
#state{font-weight:700;padding:2px 10px;border-radius:6px}
.key{color:var(--dim);font-size:13px}
</style>
<main>
<div class=row><div><div class=dim>tempo (folded)</div><div class=big id=bpm>--</div></div>
<div><div class=dim>your tap <span class=key id=diff>tap the beat</span></div><div class=big id=tap>--</div></div>
<div><div class=dim>novelty</div><div class=big id=nov>--</div></div>
<div><div class=dim>groove</div><div class=big id=state>--</div></div></div>
<canvas id=c width=2400 height=840></canvas>
<div class=row style="margin-top:10px"><button onclick="tap()">Tap tempo <span class=key>[space]</span></button>
<button onclick="mark('change')">Change <span class=key>[c]</span></button>
<button onclick="mark('build')">Build <span class=key>[b]</span></button>
<button onclick="mark('drop')">Drop <span class=key>[d]</span></button>
<span class=dim>Tap the beat with space (x clears). Mark what you hear. Marks are saved to lampster-marks.jsonl. White: novelty. Blue: low band. Orange: high band. Ticks: kicks.</span></div>
</main>
<script>
const SPAN=120,frames=[],marks=[],c=document.getElementById('c'),x=c.getContext('2d');
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
function mark(label){fetch('/mark?label='+label,{method:'POST'})}
let taps=[],tapBpm=null,detected=null;
function tap(){const n=performance.now();if(taps.length&&n-taps[taps.length-1]>2500)taps=[];taps.push(n);taps=taps.slice(-9);
 if(taps.length>1){tapBpm=60000/((taps[taps.length-1]-taps[0])/(taps.length-1));fetch('/tempo?bpm='+tapBpm.toFixed(2),{method:'POST'})}show()}
function show(){document.getElementById('tap').textContent=tapBpm?tapBpm.toFixed(1):'--';
 document.getElementById('diff').textContent=tapBpm&&detected?'detector '+(detected>tapBpm?'+':'')+(detected-tapBpm).toFixed(1)+' BPM off':'tap the beat'}
addEventListener('keydown',e=>{if(e.repeat)return;if(e.key===' '){e.preventDefault();tap()}else if(e.key==='c')mark('change');else if(e.key==='b')mark('build');else if(e.key==='d')mark('drop');else if(e.key==='x'){taps=[];tapBpm=null;fetch('/tempo?bpm=0',{method:'POST'});show()}});
const es=new EventSource('/events');
es.onmessage=m=>{const f=JSON.parse(m.data);frames.push(f);if(f.mark)marks.push({t:f.t,label:f.mark});
 while(frames.length&&frames[0].t<f.t-SPAN)frames.shift();
 detected=f.bpm;document.getElementById('bpm').textContent=f.bpm?f.bpm.toFixed(1):'--';show();
 document.getElementById('nov').textContent=f.novelty.toFixed(2);
 const st=document.getElementById('state'),ch=f.novelty>f.threshold;
 st.textContent=ch?'CHANGE':'steady';st.style.background=ch?'#d4358c':'transparent';st.style.color=ch?'#fff':css('--dim');draw()};
function draw(){const W=c.width,H=c.height,t1=frames.length?frames[frames.length-1].t:0,t0=t1-SPAN;
 x.clearRect(0,0,W,H);x.strokeStyle=css('--grid');x.lineWidth=2;
 for(let i=1;i<4;i++){x.beginPath();x.moveTo(0,H*i/4);x.lineTo(W,H*i/4);x.stroke()}
 const X=t=>(t-t0)/SPAN*W,Y=(v,max)=>H-18-Math.min(v/max,1)*(H-40);
 const line=(key,max,color)=>{x.strokeStyle=color;x.lineWidth=3;x.beginPath();frames.forEach((f,i)=>{const px=X(f.t),py=Y(f[key],max);i?x.lineTo(px,py):x.moveTo(px,py)});x.stroke()};
 line('low',2.6,'#3b82f6');line('high',0.6,'#f59e0b');line('novelty',1,css('--fg'));
 if(frames.length){x.strokeStyle='#d4358c';x.setLineDash([10,10]);x.lineWidth=2;const y=Y(frames[frames.length-1].threshold,1);x.beginPath();x.moveTo(0,y);x.lineTo(W,y);x.stroke();x.setLineDash([])}
 x.fillStyle=css('--dim');frames.forEach(f=>{if(f.kick)x.fillRect(X(f.t),H-12,3,12)});
 marks.forEach(m=>{if(m.t<t0)return;x.strokeStyle='#d4358c';x.lineWidth=3;x.beginPath();x.moveTo(X(m.t),0);x.lineTo(X(m.t),H);x.stroke();x.fillStyle='#d4358c';x.font='28px system-ui';x.fillText(m.label,X(m.t)+8,34)})}
</script>"""


async def monitor(args):
    """Serve a live browser view of the audio analysis: spectrum bands, novelty against the last 8 bars,
    kicks and tempo. Keys in the page drop timestamped marks into lampster-marks.jsonl."""
    import numpy as np

    bpm_hint = float(args[0]) if args else PRIOR_BPM
    rate, listen = open_audio(os.environ.get("LAMPSTER_AUDIO_INPUT"))
    size, hop = 2048, 1024
    window = np.hanning(size)
    freqs = np.fft.rfftfreq(size, 1 / rate)
    edges = np.geomspace(40, 12000, 11)
    groups = [(freqs >= lo) & (freqs < hi) for lo, hi in zip(edges, edges[1:])]
    bass_bins = (freqs >= BASS_HZ[0]) & (freqs <= BASS_HZ[1])
    block_rate = rate / hop
    ring = np.zeros(size, dtype=np.float32)
    heard, clients, pending_marks, state = [], set(), [], {"tap": None}
    history = deque(maxlen=int(block_rate * TEMPO_WINDOW))

    def hear(indata, frames, time_info, status):
        nonlocal ring
        ring = np.concatenate([ring[frames:], indata[:, 0]])
        spectrum = np.abs(np.fft.rfft(ring * window))
        heard.append(([float(np.log1p(spectrum[g].mean())) for g in groups], float(np.sqrt(np.mean(spectrum[bass_bins] ** 2)))))

    async def serve(reader, writer):
        request = (await reader.readline()).decode().split()
        while (await reader.readline()).strip():
            pass
        path = request[1] if len(request) > 1 else "/"
        if path.startswith("/events"):
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\n\r\n")
            await writer.drain()
            clients.add(writer)
            return
        if path.startswith("/tempo"):
            state["tap"] = float(path.partition("bpm=")[2] or 0) or None
            body, kind = b"ok", b"text/plain"
        elif path.startswith("/mark"):
            pending_marks.append(path.partition("label=")[2] or "mark")
            body, kind = b"ok", b"text/plain"
        else:
            body, kind = MONITOR_HTML.encode(), b"text/html; charset=utf-8"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: " + kind + b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", MONITOR_PORT)
    url = f"http://localhost:{MONITOR_PORT}"
    print(f"monitor at {url} (Ctrl-C to stop)", flush=True)
    webbrowser.open(url)
    short = long = None
    bar = 4 * 60 / bpm_hint
    bass_avg, peak, last_kick, last_tempo, bpm = None, 0.0, 0.0, 0.0, None
    t0 = time.monotonic()
    with listen(hop, hear), open("lampster-marks.jsonl", "a") as marks_file:
        while True:
            await asyncio.sleep(0.1)
            now = time.monotonic()
            blocks, heard[:] = heard[:], []
            kick = False
            for vector, bass in blocks:
                v = np.array(vector)
                if short is None:
                    short, long = v.copy(), v.copy()
                short += (v - short) / (bar * block_rate)
                long += (v - long) / (8 * bar * block_rate)
                history.append(bass)
                peak = max(bass, peak * PEAK_DECAY)
                bass_avg = bass if bass_avg is None else bass_avg + (bass - bass_avg) / 43
                if bass > SILENCE and bass > BEAT_RATIO * bass_avg and bass > KICK_SHARE * peak and now - last_kick > MIN_BEAT_GAP:
                    last_kick, kick = now, True
            if short is None:
                continue
            if now - last_tempo > 1.0 and len(history) > block_rate * 4:
                last_tempo, bpm = now, fold_tempo(history, block_rate)
            bar = 4 * 60 / (state["tap"] or bpm or bpm_hint)  # a tapped tempo overrides the detector
            label = pending_marks.pop(0) if pending_marks else None
            frame = {"t": now - t0, "novelty": float(np.abs(short - long).sum() / max(long.sum(), 1e-9)),
                     "low": float(short[:3].mean()), "high": float(short[7:].mean()), "kick": kick, "bpm": bpm,
                     "threshold": CHANGE_NOVELTY, "mark": label}
            if label:
                marks_file.write(json.dumps({"t": round(frame["t"], 2), "label": label, "novelty": round(frame["novelty"], 3)}) + "\n")
                marks_file.flush()
            message = f"data: {json.dumps(frame)}\n\n".encode()
            for client in list(clients):
                try:
                    client.write(message)
                    await client.drain()
                except (ConnectionError, OSError):
                    clients.discard(client)


async def run(c, argv):
    if argv[0] == "fade":
        await fade(c, float(argv[1]), parse_color(argv[2:]))
    elif argv[0] == "reactive":
        await reactive(c, argv[1:])
    elif argv[0] == "disco":
        await disco(c, argv[1:])
    elif argv[0] == "sun":
        await sun(c, argv[1:])
    elif argv[0] == "color":
        await c.write_gatt_char(COLOR, bytes(parse_color(argv[1:])), response=False)
    elif argv[0] == "mode":
        keep = (await c.read_gatt_char(MODE))[0] & LEAVING_ROOM
        await c.write_gatt_char(MODE, bytes([MODES[argv[1]] | keep]), response=True)
    elif argv[0] == "white":
        await c.write_gatt_char(WHITE, bytes(int(a) for a in argv[1:]), response=True)
    await asyncio.sleep(0.5)
    await status(c)


async def main(argv):
    if argv in (["version"], ["--version"]):
        return print(f"lampster-ctl {VERSION}")
    if not argv or argv[0] not in ("status", "color", "white", "mode", "fade", "sun", "disco", "reactive", "monitor"):
        sys.exit(__doc__)
    if argv[0] == "mode" and argv[1:] not in ([m] for m in MODES):
        sys.exit(__doc__)
    if argv[0] == "sun" and (math.isnan(LAT) or math.isnan(LON)):
        sys.exit("sun needs LAMPSTER_LAT and LAMPSTER_LON in the environment (degrees, east positive)")
    if argv[0] == "monitor":
        return await monitor(argv[1:])
    long_running, drops = argv[0] in ("sun", "disco", "reactive"), 0
    while True:
        lamp = await BleakScanner.find_device_by_name(NAME, timeout=15)
        if lamp is None and not drops:
            sys.exit(f"{NAME} not found: is it powered and is the phone app force-quit?")
        if lamp:
            try:
                async with BleakClient(lamp, timeout=20) as c:
                    return await run(c, argv)
            except (BleakError, asyncio.TimeoutError) as error:
                if not long_running:
                    raise
                drops += 1
                print(f"lost the lamp ({error}), reconnecting", flush=True)
        await asyncio.sleep(2)  # a long-running mode keeps trying until stopped


asyncio.run(main(sys.argv[1:]))
