#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["bleak", "numpy", "sounddevice"]
# ///
"""Control The Lampster over Bluetooth LE.

  lampster.py status
  lampster.py color R G B      # 0-255 each, or a single #rrggbb
  lampster.py white WARM COOL  # 0-100 each
  lampster.py mode off|white|color|both   # which LED banks are lit
  lampster.py fade SECONDS R G B|#rrggbb  # crossfade whatever is lit into that color
  lampster.py reactive [SECONDS]          # pulse to the bass heard on the audio input, whites on risers (LAMPSTER_AUDIO_INPUT=name part)
  lampster.py disco [BPM [SECONDS]]       # a new colour every beat, an occasional 3 Hz strobe burst
  lampster.py sun [SPEED [HH:MM]]         # follow the sun and moon; SPEED>1 plays one day from HH:MM (default now)
                                          # needs LAMPSTER_LAT and LAMPSTER_LON (degrees, east positive)
"""
import asyncio
import colorsys
import math
import os
import random
import sys
import time
from collections import deque
from datetime import datetime, timedelta

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakGATTProtocolError

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
DANCE_BPM = (70, 140)  # the range people move to: faster is danced at half speed, slower at double
PRIOR_BPM = 110  # tempo people find most natural, used to break ties between octaves
TEMPO_WINDOW = 10  # seconds of bass history the tempo estimate looks at
HIGH_HZ = (2000, 8000)  # band whose climb marks a riser
KICK_SHARE = 0.3  # a kick must also reach this share of the recent peak, so volume does not matter
PEAK_DECAY = 0.999  # per audio block; the recent peak halves in about 16 s
RISER_RATIO = 1.3  # highs this far above their slow average, with no kick for a second, start a riser
RISER_SECONDS = 8  # how long the whites take to swell to full
RISER_TIMEOUT = 24
WHITE_EVERY = 4 # every Nth kick is white instead of a colour; 0 for colours only
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


async def reactive(c, args):
    """Flash a new colour on each bass beat. When the kick drops out and the highs climb (a riser) the whites
    swell and pulse, and the kick coming back is answered by one full flash."""
    import numpy as np
    import sounddevice as sd

    seconds = float(args[0]) if args else None
    device = os.environ.get("LAMPSTER_AUDIO_INPUT")
    rate = sd.query_devices(device, "input")["default_samplerate"]
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
    period, last_hit, last_tempo = 60 / PRIOR_BPM, 0.0, 0.0

    def hit(when):
        """One beat of the lamp: a spike, in a new colour or (every WHITE_EVERY-th) white."""
        nonlocal level, hue, white_hit, hits, last_hit
        last_hit, hits, level = when, hits + 1, 1.0
        white_hit = bool(WHITE_EVERY) and hits % WHITE_EVERY == 0
        if not white_hit:
            hue = (hue + random.uniform(0.2, 0.6)) % 1

    t0 = time.monotonic()
    with sd.InputStream(device=device, channels=1, samplerate=rate, blocksize=1024, callback=hear):
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
                    period += (60 / bpm - period) / 2
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


async def main(argv):
    if not argv or argv[0] not in ("status", "color", "white", "mode", "fade", "sun", "disco", "reactive"):
        sys.exit(__doc__)
    if argv[0] == "mode" and argv[1:] not in ([m] for m in MODES):
        sys.exit(__doc__)
    if argv[0] == "sun" and (math.isnan(LAT) or math.isnan(LON)):
        sys.exit("sun needs LAMPSTER_LAT and LAMPSTER_LON in the environment (degrees, east positive)")
    lamp = await BleakScanner.find_device_by_name(NAME, timeout=15)
    if lamp is None:
        sys.exit(f"{NAME} not found: is it powered and is the phone app force-quit?")
    async with BleakClient(lamp, timeout=20) as c:
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


asyncio.run(main(sys.argv[1:]))
