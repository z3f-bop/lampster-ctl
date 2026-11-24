"""Tunables of what the lamp hears and shows. Each can be changed here; none needs the lamp to try."""
import os

STROBE_HZ = 3  # flashes per second, at the photosensitivity guideline limit; never raise this
STROBE_CHANCE = 0.3  # per 8-beat phrase
RISER_STROBE_HZ = STROBE_HZ - 1  # the riser strobes slower than the cap, so the flash when the kick returns always fits in the same second
FLASH_JUMP = 0.25  # a rise in light of at least this share of full counts as a flash; at most STROBE_HZ of them in any second
AUDIO_FPS = 40  # lamp updates per second in reactive mode
AUDIO_BACKLOG = 5  # seconds of analysed audio kept if the lamp loop stalls; older blocks are dropped
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
WHITE_EVERY = 4  # every Nth kick is white instead of a colour; 0 for colours only
CHANGE_NOVELTY = 0.2  # the last bar this far from the last 8 bars' average counts as a change in the groove
QUIET_LEVEL = 0.05  # brightness the lamp settles to once the music stops
QUIET_FADE = 5  # seconds it takes to get there, starting half a second after the last sound
DROP_FLASH = 0.15  # seconds of full light when the kick returns after a riser
SILENCE = 0.3  # energy below this is not a beat
DECAY = 0.7  # brightness kept per update after a beat; lower is spikier
FLOOR = 0.06  # dimmest the lamp gets between beats
BRIGHTNESS = min(100.0, max(5.0, float(os.environ.get("LAMPSTER_BRIGHTNESS", "100")))) / 100  # share of full light that disco and reactive may use
BLOCK = 1024  # samples per analysed block
WINDOW = 2048  # samples the spectrum looks at
