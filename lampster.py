#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["bleak"]
# ///
"""Control The Lampster over Bluetooth LE.

  lampster.py status
  lampster.py color R G B      # 0-255 each, or a single #rrggbb
  lampster.py white WARM COOL  # 0-100 each
  lampster.py mode off|white|color|both   # which LED banks are lit
  lampster.py fade SECONDS R G B|#rrggbb  # crossfade whatever is lit into that color
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
from datetime import datetime, timedelta

from bleak import BleakClient, BleakScanner

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
    if not argv or argv[0] not in ("status", "color", "white", "mode", "fade", "sun", "disco"):
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
