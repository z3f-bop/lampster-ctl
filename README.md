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

`./lampster.py disco [BPM [SECONDS]]` (default 128 BPM, runs until stopped) changes to a new vivid colour on every beat. At the end of some 8-beat phrases it fires a short white strobe burst.

**Photosensitivity:** strobing can trigger seizures in people with photosensitive epilepsy. The burst is held to 3 flashes per second, the common guideline ceiling, and `STROBE_HZ` is a constant you should leave there, especially with guests around. Set `STROBE_CHANCE` to 0 for no strobes at all.

## Reactive mode

`./lampster.py reactive [SECONDS]` listens to an audio input and flashes a new colour on every bass beat, then lets the light decay until the next one. It uses the system's default input (the microphone), so it works for any room with music in it. For a clean digital signal with nothing to reroute, tap what the Mac is playing (macOS 14.2+, needs Xcode's command line tools for `swiftc`):

```bash
LAMPSTER_AUDIO_INPUT=system ./lampster.py reactive
```

`systap.swift` is a small Core Audio process tap that writes the system output as mono samples; `lampster.py` compiles it once into `~/.cache/lampster-ctl/` and the microphone stays free for dictation. Your output device and volume are untouched, and the first run may ask the terminal for system audio permission. `monitor` accepts it too. The older route also works: send the output to a loopback device such as [BlackHole](https://existential.audio/blackhole/) through a Multi-Output Device in Audio MIDI Setup, and name it with `LAMPSTER_AUDIO_INPUT=BlackHole`.

The beat detector looks at 30-150 Hz energy against its own running average (`BASS_HZ`, `BEAT_RATIO`, `SILENCE` at the top of the script). A kick must reach a share of the track's own recent peak, so the system volume does not matter. When the kick drops out and the highs start climbing (a riser), the whites swell and pulse, capped at 3 flashes per second, then a single full flash lands when the kick returns. The lamp's beat is not every kick: the tempo is estimated from the bass (autocorrelation over the last 10 seconds) and folded into a dance range (`DANCE_BPM`, 82-165), the way people hear it. Music that reads faster is danced at half speed, so extra kicks are skipped; music that reads slower is danced at double speed, so beats are added in between. Every `WHITE_EVERY`-th beat (default 4, one bar) is white and the rest are new colours, each a spike that decays at `DECAY` per update. When the music stops, the lamp fades to 5% over 5 seconds. Laptop speakers have little bass, so with a microphone you may need to lower `SILENCE`. The terminal needs microphone permission the first time. Like disco, it needs `uv` to fetch `numpy` and `sounddevice`.

## Monitor

`./lampster.py monitor [BPM]` opens a live page at `http://localhost:8787` (it never touches the lamp) showing the audio analysis as the track plays: low and high band levels, kicks, the folded tempo, and **novelty**, how far the last bar's spectrum is from the average of the last 8 bars. Steady four-on-the-floor sits near 0.05; a breakdown, a new element or a build lifts it above 0.2, which is where the page shows CHANGE.

Keys in the page: **space** taps tempo (the page shows how far the detector is from your tap, and your tap sets the bar length for the novelty measure; **x** clears it), and **c**, **b**, **d** mark a change, a build or a drop at that moment. Marks go to `lampster-marks.jsonl` in the directory you ran it from, so you can compare what you heard with what the numbers did. The novelty measure is not used by `reactive` yet.

## Good to know

- The lamp talks to one device at a time. Force-quit the phone app before running anything here, and note that a running `fade` or `sun` holds the connection, so the app cannot connect until you stop it.
- The lamp has no pairing or authentication: anyone within Bluetooth range can control it.
- The wake-up and off timers in the app are not exposed over the registers read here; they appear to live in the app.
- Everything here was worked out on one lamp. If yours behaves differently (other firmware, other UUIDs), open an issue with the output of `status` and your lamp's GATT table.

How the protocol was mapped is in [PROTOCOL.md](PROTOCOL.md).

## Licence

MIT. Not affiliated with the makers of The Lampster.

Built by Zeph, with oO supplying the lamp, the ideas and the validation.
