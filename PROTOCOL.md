# The Lampster BLE protocol

Observed on one unit. No pairing, no encryption, no authentication. The lamp advertises the name `Lampster`.

## Method

Clean-room, from the outside: no firmware dump, no app decompilation. A person set a state in the phone app, force-quit the app, and every readable GATT characteristic was snapshotted; the snapshots were diffed against each other. Candidate writes were then tried from a computer and confirmed by eye and by reading the value back.

## Characteristics

Base UUID `01ffXXXX-ba5e-f4ee-5ca1-eb1e5e4b1ce0`.

| XXXX | use | format |
|---|---|---|
| `5559` | RGB levels | `[R, G, B]`, each 0-100. Write without response (write with response is rejected) |
| `5556` | white levels | `[warm, cool]`, each 0-100. Write with response |
| `5554` | mode | one byte, write with response (see below) |
| `5560`, `2a6e` | telemetry | notify about every 5 s; look like ADC readings, meaning unknown |
| `5561`, `5562`, `5563` | unknown | always read zero |
| `5551`, `5552` | unknown | a command pair, never written |

Levels are percents. The app's own sliders bottom out at 5, but the lamp accepts 0 (it may read back 1 for a written 0). The app's hue / saturation / brightness controls are converted to RGB percent before sending.

## Mode byte (`5554`)

| bit | meaning |
|---|---|
| `0x80` | power |
| `0x40` | white bank lit |
| `0x20` | RGB bank lit |
| `0x10` | the app's "turn off when leaving room" flag |

So `0x40` is off, `0xC0` white, `0xA0` colour, `0xE0` both (white with RGB blended in). Writes should preserve bit `0x10`. Levels are stored independently of the mode, so set levels first, then the mode.

## Not found

- Wake-up and off timers: set in the app, they did not fire with the app closed and showed up in no readable register.
- The app's demo mode runs in the app, which writes `5559` itself.
- What `5551`/`5552` and `5561`-`5563` do, and what the telemetry measures.
