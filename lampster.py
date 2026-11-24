#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["bleak", "numpy", "sounddevice"]
# ///
"""Control The Lampster over Bluetooth LE.

  lampster.py                  # no command = reactive: follow the music the Mac is playing
  lampster.py version          # print the version (also --version)
  lampster.py status
  lampster.py color R G B     # 0-255 each, or a single #rrggbb
  lampster.py white WARM COOL  # 0-100 each
  lampster.py mode off|white|color|both   # which LED banks are lit
  lampster.py fade SECONDS R G B|#rrggbb  # crossfade whatever is lit into that color
  lampster.py reactive [SECONDS]          # pulse to the bass heard on the audio input, whites on risers
                                          # LAMPSTER_AUDIO_INPUT=name part of an input device; the default is "system", what the Mac plays
  lampster.py monitor [BPM]               # live browser view of what the lamp hears and does, with keys to mark what you hear
                                          # LAMPSTER_RECORD=file also records the tapped audio, for replay
  lampster.py replay FILE [TAPPED_BPM]    # run a recording through the lamp's analysis offline, no lamp needed
  lampster.py disco [BPM [SECONDS]]       # a new colour every beat, an occasional 3 Hz strobe burst
  lampster.py hue [PERIOD [SECONDS]]      # rotate the hue of the lit color through 360 degrees every PERIOD seconds
                                          # (default 60), whites untouched; runs until SECONDS (default: stopped)
  lampster.py sun [SPEED [HH:MM]]         # follow the sun and moon; SPEED>1 plays one day from HH:MM (default now)
                                          # needs LAMPSTER_LAT and LAMPSTER_LON (degrees, east positive)
"""
import asyncio
import colorsys
import contextlib
import json
import math
import os
import random
import signal
import sys
import time
import webbrowser
from collections import deque
from datetime import datetime, timedelta

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError, BleakGATTProtocolError

from lampster_ctl.analysis import Brain, Spectrum, simulate
from lampster_ctl.audio import SYSTEM_AUDIO, open_audio
from lampster_ctl.config import (
    AUDIO_BACKLOG, AUDIO_FPS, BLOCK, BRIGHTNESS, CHANGE_NOVELTY, PRIOR_BPM, STROBE_CHANCE, STROBE_HZ,
)

VERSION = "0.8.0"
NAME = "Lampster"
BASE = "-ba5e-f4ee-5ca1-eb1e5e4b1ce0"
MODE, WHITE, COLOR = (f"01ff{h}{BASE}" for h in ("5554", "5556", "5559"))
MODES = {"off": 0x40, "white": 0xC0, "color": 0xA0, "both": 0xE0}
LEAVING_ROOM = 0x10  # app's "turn off when leaving room" flag, kept across mode writes
WHITE_BANK, COLOR_BANK = 0x40, 0x20  # bank bits of the mode byte
FADE_STEP = 0.25  # seconds between fade writes
LAT, LON = (float(os.environ.get(k, "nan")) for k in ("LAMPSTER_LAT", "LAMPSTER_LON"))  # degrees, east positive
MONITOR_PORT = int(os.environ.get("LAMPSTER_MONITOR_PORT", "8787"))  # override when 8787 is taken by another local service
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


async def hue(c, args):
    """Rotate the hue of the color now lit through a full circle every PERIOD seconds; saturation, value and whites stay."""
    period = float(args[0]) if args else 60.0
    seconds = float(args[1]) if len(args) > 1 else math.inf
    mode = (await c.read_gatt_char(MODE))[0]
    rgb = list(await c.read_gatt_char(COLOR))
    h0, s, v = colorsys.rgb_to_hsv(*(x / 100 for x in rgb)) if any(rgb) else (0.0, 1.0, 1.0)
    await c.write_gatt_char(MODE, bytes([mode | COLOR_BANK]), response=True)
    t0 = time.monotonic()
    while (elapsed := time.monotonic() - t0) < seconds:
        levels = [round(x * 100) for x in colorsys.hsv_to_rgb((h0 + elapsed / period) % 1, s, v)]
        await c.write_gatt_char(COLOR, bytes(levels), response=False)
        await asyncio.sleep(FADE_STEP)


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


def dimmed(levels):
    """Scale 0-100 channel levels by BRIGHTNESS."""
    return bytes(round(v * BRIGHTNESS) for v in levels)


async def reactive(c, args):
    """Flash a new colour on each bass beat. When the kick drops out and the highs climb (a riser) the whites
    swell and pulse, and the kick coming back is answered by one full flash. What it hears and decides lives in
    lampster_ctl.analysis; this only writes the result to the lamp."""
    seconds = float(args[0]) if args else None
    rate, listen = open_audio(os.environ.get("LAMPSTER_AUDIO_INPUT", SYSTEM_AUDIO))
    spectrum, brain = Spectrum(rate), Brain(rate)
    heard = deque(maxlen=int(rate / BLOCK * AUDIO_BACKLOG))  # if the lamp stalls, the oldest blocks fall off

    def hear(indata, frames, time_info, status):
        features = spectrum(indata[:, 0])
        heard.append((features.bass, features.high))

    keep = (await c.read_gatt_char(MODE))[0] & LEAVING_ROOM
    await c.write_gatt_char(WHITE, bytes([0, 0]), response=True)
    await c.write_gatt_char(COLOR, bytes([0, 0, 0]), response=False)
    await c.write_gatt_char(MODE, bytes([MODES["both"] | keep]), response=True)  # RGB and white banks both live
    last_sent, refused = None, 0
    t0 = time.monotonic()
    with listen(BLOCK, hear):
        while seconds is None or time.monotonic() - t0 < seconds:
            blocks = [heard.popleft() for _ in range(len(heard))]
            frame = brain.step(blocks, time.monotonic())
            out = (dimmed(frame.rgb), dimmed(frame.white))
            if out != last_sent:
                await c.write_gatt_char(COLOR, out[0], response=False)
                if last_sent is None or out[1] != last_sent[1]:
                    try:
                        await c.write_gatt_char(WHITE, out[1], response=True)
                    except BleakGATTProtocolError:
                        out, refused = (out[0], None), refused + 1  # lamp was busy: whites go out again next tick
                last_sent = out
            await asyncio.sleep(1 / AUDIO_FPS)
    print(f"{brain.kicks} kicks, {brain.hits} lamp beats in {time.monotonic() - t0:.0f}s at ~{60 / brain.period:.0f} BPM, "
          f"{brain.risers} risers, {brain.drops} drops, {refused} white writes refused, peak bass energy {brain.peak:.1f}")


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
                    await c.write_gatt_char(COLOR, dimmed([level] * 3), response=False)
                    await asyncio.sleep(1 / (2 * STROBE_HZ))
            n = max(n + 1, math.ceil((time.monotonic() - t0) / beat))
        else:
            hue = (hue + random.uniform(0.2, 0.6)) % 1
            await c.write_gatt_char(COLOR, dimmed(round(v * 100) for v in colorsys.hsv_to_rgb(hue, 1, 1)), response=False)
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
<div class=row><div><div class=dim>lamp tempo</div><div class=big id=lbpm>--</div></div>
<div><div class=dim>detector (raw)</div><div class=big id=bpm>--</div></div>
<div><div class=dim>your tap <span class=key id=diff>tap the beat</span></div><div class=big id=tap>--</div></div>
<div><div class=dim>novelty</div><div class=big id=nov>--</div></div>
<div><div class=dim>groove</div><div class=big id=state>--</div></div></div>
<canvas id=c width=2400 height=840></canvas>
<div class=row style="margin-top:10px"><button onclick="tap()">Tap tempo <span class=key>[space]</span></button>
<button onclick="mark('change')">Change <span class=key>[c]</span></button>
<button onclick="mark('build')">Build <span class=key>[b]</span></button>
<button onclick="mark('drop')">Drop <span class=key>[d]</span></button>
<span class=dim>Tap the beat with space (x clears). Mark what you hear. Marks are saved to lampster-marks.jsonl. White: novelty. Blue: low band. Orange: high band. Grey ticks: kicks. Green line: how bright the lamp is. Green ticks: lamp beats. Orange shading: a riser. The lamp tempo and every tick come from the same code the lamp runs.</span></div>
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
 detected=f.bpm;document.getElementById('bpm').textContent=f.bpm?f.bpm.toFixed(1):'--';document.getElementById('lbpm').textContent=f.lamp_bpm?f.lamp_bpm.toFixed(1):'--';show();
 document.getElementById('nov').textContent=f.novelty.toFixed(2);
 const st=document.getElementById('state'),ch=f.novelty>f.threshold;
 st.textContent=ch?'CHANGE':'steady';st.style.background=ch?'#d4358c':'transparent';st.style.color=ch?'#fff':css('--dim');draw()};
function draw(){const W=c.width,H=c.height,t1=frames.length?frames[frames.length-1].t:0,t0=t1-SPAN;
 x.clearRect(0,0,W,H);x.strokeStyle=css('--grid');x.lineWidth=2;
 for(let i=1;i<4;i++){x.beginPath();x.moveTo(0,H*i/4);x.lineTo(W,H*i/4);x.stroke()}
 const X=t=>(t-t0)/SPAN*W,Y=(v,max)=>H-18-Math.min(v/max,1)*(H-40);
 const line=(key,max,color)=>{x.strokeStyle=color;x.lineWidth=3;x.beginPath();frames.forEach((f,i)=>{const px=X(f.t),py=Y(f[key],max);i?x.lineTo(px,py):x.moveTo(px,py)});x.stroke()};
 x.fillStyle='rgba(245,158,11,0.14)';frames.forEach(f=>{if(f.riser)x.fillRect(X(f.t),0,Math.max(3,W/SPAN*0.1),H)});
 line('low',2.6,'#3b82f6');line('high',0.6,'#f59e0b');line('novelty',1,css('--fg'));line('level',1,'#22c55e');
 if(frames.length){x.strokeStyle='#d4358c';x.setLineDash([10,10]);x.lineWidth=2;const y=Y(frames[frames.length-1].threshold,1);x.beginPath();x.moveTo(0,y);x.lineTo(W,y);x.stroke();x.setLineDash([])}
 x.fillStyle=css('--dim');frames.forEach(f=>{if(f.kick)x.fillRect(X(f.t),H-12,3,12)});
 x.fillStyle='#22c55e';frames.forEach(f=>{if(f.hit)x.fillRect(X(f.t),H-30,5,16)});
 marks.forEach(m=>{if(m.t<t0)return;x.strokeStyle='#d4358c';x.lineWidth=3;x.beginPath();x.moveTo(X(m.t),0);x.lineTo(X(m.t),H);x.stroke();x.fillStyle='#d4358c';x.font='28px system-ui';x.fillText(m.label,X(m.t)+8,34)})}
</script>"""


async def monitor(args):
    """Serve a live browser view of what the lamp hears: spectrum bands, novelty against the last 8 bars, and the
    same kicks, tempo, lamp beats and risers the lamp acts on (they come from the same Brain as `reactive`). Keys in
    the page drop timestamped marks into lampster-marks.jsonl."""
    import numpy as np

    bpm_hint = float(args[0]) if args else PRIOR_BPM
    rate, listen = open_audio(os.environ.get("LAMPSTER_AUDIO_INPUT", SYSTEM_AUDIO))
    spectrum, brain = Spectrum(rate, bands=True), Brain(rate)
    block_rate = rate / BLOCK
    heard, clients, pending_marks, state = deque(maxlen=int(block_rate * AUDIO_BACKLOG)), set(), [], {"tap": None}

    def hear(indata, frames, time_info, status):
        heard.append(spectrum(indata[:, 0]))

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
    seen = dict.fromkeys(("kick", "hit"), False)  # events since the last frame sent to the page
    t0 = last_sent = time.monotonic()
    with listen(BLOCK, hear), open("lampster-marks.jsonl", "a") as marks_file:
        while True:
            await asyncio.sleep(1 / AUDIO_FPS)  # the lamp's own tick, so the Brain behaves exactly as it does on the lamp
            now = time.monotonic()
            blocks = [heard.popleft() for _ in range(len(heard))]
            lamp = brain.step([(f.bass, f.high) for f in blocks], now)
            seen["kick"], seen["hit"] = seen["kick"] or lamp.kicked, seen["hit"] or lamp.hit
            for f in blocks:
                v = np.array(f.bands)
                if short is None:
                    short, long = v.copy(), v.copy()
                short += (v - short) / (bar * block_rate)
                long += (v - long) / (8 * bar * block_rate)
            if short is None or now - last_sent < 0.1:
                continue
            last_sent = now
            bar = 4 * 60 / (state["tap"] or brain.detected or bpm_hint)  # a tapped tempo overrides the detector
            label = pending_marks.pop(0) if pending_marks else None
            frame = {"t": now - t0, "novelty": float(np.abs(short - long).sum() / max(long.sum(), 1e-9)),
                     "low": float(short[:3].mean()), "high": float(short[7:].mean()), "kick": seen["kick"], "hit": seen["hit"],
                     "riser": lamp.riser, "level": round(lamp.level, 3), "bpm": brain.detected, "lamp_bpm": lamp.lamp_bpm,
                     "threshold": CHANGE_NOVELTY, "mark": label, "tap": state["tap"]}
            seen["kick"] = seen["hit"] = False
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


def replay(args):
    """Run a recording (LAMPSTER_RECORD=file lampster.py monitor) through the same analysis the lamp uses, with no lamp:
    one line per second, then the summary `reactive` prints. An optional second argument is the tempo you tapped."""
    import numpy as np

    if not args:
        sys.exit("usage: lampster.py replay RECORDING [TAPPED_BPM]")
    rate = json.load(open(args[0] + ".json"))["rate"] if os.path.exists(args[0] + ".json") else 48000
    samples = np.fromfile(args[0], dtype=np.float32)
    kicks = hits = 0
    last, frames = None, 0
    for t, frame in simulate(samples, rate, rng=random.Random(0)):
        kicks, hits, frames, last = kicks + frame.kicked, hits + frame.hit, frames + 1, frame
        if frames % AUDIO_FPS == 0:
            tempo = f"{frame.lamp_bpm:6.1f}" if frame.lamp_bpm else "   -  "
            raw = f"{frame.bpm:6.1f}" if frame.bpm else "   -  "
            print(f"{t:6.1f}s  lamp {tempo} BPM  detector {raw}  kicks {kicks:4d}  lamp beats {hits:4d}  "
                  f"{'RISER' if frame.riser else '     '}  level {frame.level:.2f}")
    if last and last.lamp_bpm:
        extra = f"; you tapped {float(args[1]):.1f}, off by {100 * (last.lamp_bpm / float(args[1]) - 1):+.1f}%" if len(args) > 1 else ""
        print(f"{kicks} kicks, {hits} lamp beats in {len(samples) / rate:.0f}s, final lamp tempo {last.lamp_bpm:.1f} BPM{extra}")


async def run(c, argv):
    if argv[0] == "fade":
        await fade(c, float(argv[1]), parse_color(argv[2:]))
    elif argv[0] == "reactive":
        await reactive(c, argv[1:])
    elif argv[0] == "disco":
        await disco(c, argv[1:])
    elif argv[0] == "hue":
        await hue(c, argv[1:])
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
    if not argv:
        argv = ["reactive"]  # the default: follow the music the Mac is playing
    if argv[0] not in ("status", "color", "white", "mode", "fade", "hue", "sun", "disco", "reactive", "monitor", "replay"):
        sys.exit(__doc__)
    if argv[0] == "mode" and argv[1:] not in ([m] for m in MODES):
        sys.exit(__doc__)
    if argv[0] == "sun" and (math.isnan(LAT) or math.isnan(LON)):
        sys.exit("sun needs LAMPSTER_LAT and LAMPSTER_LON in the environment (degrees, east positive)")
    if argv[0] == "monitor":
        return await monitor(argv[1:])
    if argv[0] == "replay":
        return replay(argv[1:])
    long_running, drops = argv[0] in ("hue", "sun", "disco", "reactive"), 0
    while True:
        lamp = await BleakScanner.find_device_by_name(NAME, timeout=15)
        if lamp is None and not drops:
            sys.exit(f"{NAME} not found: is it powered and is the phone app force-quit?")
        if lamp:
            try:
                async with BleakClient(lamp, timeout=20) as c:
                    try:
                        return await run(c, argv)
                    except asyncio.CancelledError:  # stopped from outside: leave the lamp dark, not on its last colour
                        if long_running and c.is_connected:
                            with contextlib.suppress(Exception):
                                await c.write_gatt_char(COLOR, bytes(3), response=False)
                                await c.write_gatt_char(WHITE, bytes(2), response=True)
                        raise
            except (BleakError, asyncio.TimeoutError) as error:
                if not long_running:
                    raise
                drops += 1
                print(f"lost the lamp ({error}), reconnecting", flush=True)
        await asyncio.sleep(2)  # a long-running mode keeps trying until stopped


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal.default_int_handler)  # a plain kill ends a run like Ctrl-C, so the lamp is released cleanly
    asyncio.run(main(sys.argv[1:]))
