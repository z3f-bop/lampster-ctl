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

## Good to know

- The lamp talks to one device at a time. Force-quit the phone app before running anything here, and note that a running `fade` or `sun` holds the connection, so the app cannot connect until you stop it.
- The lamp has no pairing or authentication: anyone within Bluetooth range can control it.
- The wake-up and off timers in the app are not exposed over the registers read here; they appear to live in the app.
- Everything here was worked out on one lamp. If yours behaves differently (other firmware, other UUIDs), open an issue with the output of `status` and your lamp's GATT table.

How the protocol was mapped is in [PROTOCOL.md](PROTOCOL.md).

## Licence

MIT. Not affiliated with the makers of The Lampster.

Built by Zeph, with oO supplying the lamp, the ideas and the validation.
