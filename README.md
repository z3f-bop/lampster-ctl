# lampster-ctl

Control [The Lampster](https://thelampster.com) (an RGB + warm/cool white LED lamp) over Bluetooth LE from your computer, no phone app needed.

One file, no install: needs [`uv`](https://docs.astral.sh/uv/) and Bluetooth permission for your terminal.

```bash
./lampster.py status
./lampster.py color '#ff0080'          # or: color 255 0 128
./lampster.py white 80 20              # warm cool, 0-100
./lampster.py mode off                 # off | white | color | both
./lampster.py fade 60 '#ff0000'        # crossfade whatever is lit into a color over 60 s
LAMPSTER_LAT=48.86 LAMPSTER_LON=2.35 ./lampster.py sun
```

`color` and `white` set levels; `mode` chooses which LED banks are lit (`both` blends RGB into the white, the app's "overdrive").

## Sun mode

`sun` makes the lamp follow the sky at your location: dark at night, deep blue and purple at dusk, red at the horizon, warm white climbing and cooling toward noon, and a faint cool glow from the moon when it is up (scaled by its lit fraction and height). The position maths is plain almanac formulae, no dependencies.

- `sun` alone runs in real time, updating every 30 s until you stop it.
- `sun 1440 05:00` plays one day from 05:00 in a minute, to preview it.
- Latitude and longitude come from `LAMPSTER_LAT` and `LAMPSTER_LON` (degrees, east positive).
- The colours are the `SUN_KEYS` and `MOON_LEVELS` tables at the top of the script. Edit to taste.

## Disco mode

`./lampster.py hue [PERIOD [SECONDS]]` rotates the hue of the lit colour through a full circle every PERIOD seconds (default 60), starting from the colour on the lamp, at the same saturation and brightness. The whites are not touched, so a warm white keeps its level underneath. It runs until stopped, or for SECONDS.

`./lampster.py disco [BPM [SECONDS]]` (default 128 BPM, runs until stopped) changes to a new vivid colour on every beat. At the end of some 8-beat phrases it fires a short white strobe burst.

**Photosensitivity:** strobing can trigger seizures in people with photosensitive epilepsy. The burst is held to 3 flashes per second, the common guideline ceiling, and `STROBE_HZ` is a constant you should leave there, especially with guests around. Set `STROBE_CHANCE` to 0 for no strobes at all.

## Reactive mode

`./lampster.py reactive [SECONDS]` flashes a new colour on every bass beat, then lets the light decay until the next one. It is the default: `./lampster.py` with no command runs it. By default it taps what the Mac is playing, a clean digital signal with nothing to reroute (macOS 14.2+, needs Xcode's command line tools for `swiftc`). To listen to a room with a microphone instead, name the input device:

```bash
LAMPSTER_AUDIO_INPUT=MacBook ./lampster.py reactive   # any part of an input device's name
```

`systap.swift` is a small Core Audio process tap that writes the system output as mono samples; `lampster.py` compiles it once into `~/.cache/lampster-ctl/` and the microphone stays free for dictation. Your output device and volume are untouched, and the first run may ask the terminal for system audio permission. `monitor` accepts it too. The older route also works: send the output to a loopback device such as [BlackHole](https://existential.audio/blackhole/) through a Multi-Output Device in Audio MIDI Setup, and name it with `LAMPSTER_AUDIO_INPUT=BlackHole`.

The beat detector looks at 30-150 Hz energy against its own running average (`BASS_HZ`, `BEAT_RATIO`, `SILENCE` at the top of the script). A kick must reach a share of the track's own recent peak, so the system volume does not matter. When the kick drops out and the highs start climbing (a riser), the whites swell and pulse, strobing at no more than 2 flashes per second, then a single full flash lands when the kick returns. The lamp's beat is not every kick: the tempo is estimated from the bass (autocorrelation over the last 10 seconds) and folded into a dance range (`DANCE_BPM`, 82-165), the way people hear it. Music that reads faster is danced at half speed, so extra kicks are skipped; music that reads slower is danced at double speed, so beats are added in between. Every `WHITE_EVERY`-th beat (default 4, one bar) is white and the rest are new colours, each a spike that decays at `DECAY` per update. When the music stops, the lamp fades to 5% over 5 seconds. Laptop speakers have little bass, so with a microphone you may need to lower `SILENCE`. The terminal needs microphone permission the first time. Like disco, it needs `uv` to fetch `numpy` and `sounddevice`. `LAMPSTER_BRIGHTNESS` (5 to 100, default 100) scales every light level of `reactive` and `disco`, the strobes and risers included; about 30 suits a dark room.

### Photosensitivity

The lamp never flashes more than 3 times in any second (`STROBE_HZ`), whatever asks for the flashes. A flash is a rise in light of at least `FLASH_JUMP` (a quarter of full). The riser strobe is capped at one flash below that limit so the flash when the kick returns always fits, and a limiter in `Brain` skips any flash that would be a fourth in the second (a hail of fast kicks, say). Tests drive it with ordinary and adversarial audio. An earlier version computed the strobe phase as elapsed time times a frequency that was still ramping up, which made the strobe faster than its cap while the riser swelled; the phase is now integrated tick by tick.

### Reliability of the system tap

- One tap serves the whole run: a lamp reconnect re-enters the analysis but reuses it, so a second tap is never opened.
- A supervisor restarts `systap` when it exits or delivers nothing for 3 seconds (a stuck one is killed), pausing 0.5 s, 1 s, 2 s... up to 30 s while restarts keep failing, and logs each restart to stderr. If the output device changes sample rate, blocks are resampled to the rate the analysis started with.
- `systap` cleans up and exits by itself on SIGTERM, SIGINT, or end of file on stdin (its parent is gone, even if killed). Its audio thread never blocks on the pipe: after about 1.4 s of backlog blocks are dropped and counted (`drops=N` on stderr).
- The analysis keeps at most `AUDIO_BACKLOG` seconds of blocks if the lamp loop stalls.
- A plain `kill` (SIGTERM) ends a run like Ctrl-C, and the lamp is left dark instead of on its last colour.
- Tests need no lamp and no audio hardware, only a fake tap process: `uv run --with pytest --with numpy --with bleak --with sounddevice pytest tests`. `LAMPSTER_TAP_BINARY` points the script at another tap binary, so a test build never replaces the one a live run is executing.

## Structure

- `lampster.py`: the command line and the Bluetooth side (what to write to the lamp, reconnecting, the other modes).
- `lampster_ctl/audio.py`: where audio comes from (the system tap with its supervisor, or an input device).
- `lampster_ctl/analysis.py`: `Spectrum` (energy in the bands the lamp listens to) and `Brain` (kicks, tempo, risers, and the light each moment calls for). It does no I/O and has no clock: it is given audio blocks and the time, so the lamp, the monitor and an offline replay run exactly the same code.
- `lampster_ctl/config.py`: every tunable.
- `systap.swift`: the Core Audio tap.

## Monitor

`./lampster.py monitor [BPM]` opens a live page at `http://localhost:8787` (set `LAMPSTER_MONITOR_PORT` if another service holds that port; it never touches the lamp) showing what the lamp hears and does as the track plays: low and high band levels, kicks, **the lamp's own tempo** next to the raw detector reading, **lamp beats**, risers, how bright the lamp is, and **novelty**, how far the last bar's spectrum is from the average of the last 8 bars. Everything but novelty comes from the same `Brain` that drives the lamp, so the page cannot disagree with what you see on it. Steady four-on-the-floor sits near 0.05; a breakdown, a new element or a build lifts it above 0.2, which is where the page shows CHANGE.

Keys in the page: **space** taps tempo (the page shows how far the detector is from your tap, and your tap sets the bar length for the novelty measure; **x** clears it), **1** marks a downbeat (the one of the bar: press it on every bar), and **c**, **b**, **d** mark a change, a build or a drop at that moment. Marks go to `lampster-marks.jsonl` in the directory you ran it from, timed in seconds from the first audio block, so they line up with a recording made in the same run (`LAMPSTER_RECORD=song.f32`): your taps and `1` marks are the ground truth to score a tempo or downbeat tracker against. The novelty measure is not used by `reactive` yet.

## Recording and replay

`LAMPSTER_RECORD=song.f32 ./lampster.py monitor` (or `reactive`) also writes the tapped audio to `song.f32` (mono float32) and its sample rate to `song.f32.json`. `./lampster.py replay song.f32 [TAPPED_BPM]` then runs the recording through the lamp's analysis with no lamp and prints one line per second (lamp tempo, raw detector tempo, kicks, lamp beats, risers, brightness) and a summary, with your tapped tempo compared if you give it. That is the way to tune a threshold or test another beat tracker against tracks the detector gets wrong, without the lamp or the music playing. Recordings are the music itself: keep them local, never commit them.

## Good to know

- The lamp talks to one device at a time. Force-quit the phone app before running anything here, and note that a running `fade`, `hue` or `sun` holds the connection, so the app cannot connect until you stop it.
- The lamp has no pairing or authentication: anyone within Bluetooth range can control it.
- The wake-up and off timers in the app are not exposed over the registers read here; they appear to live in the app.
- Everything here was worked out on one lamp. If yours behaves differently (other firmware, other UUIDs), open an issue with the output of `status` and your lamp's GATT table.

How the protocol was mapped is in [PROTOCOL.md](PROTOCOL.md).

## Licence

MIT. Not affiliated with the makers of The Lampster.

Built by Zeph, with oO supplying the lamp, the ideas and the validation.
