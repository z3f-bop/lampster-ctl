"""What the lamp hears and does, on synthetic audio: no lamp, no audio hardware, no recordings.
Run: uv run --with pytest --with numpy --with bleak --with sounddevice pytest tests"""
import random

import numpy as np
import pytest

import signals
from lampster_ctl import analysis
from lampster_ctl.config import AUDIO_FPS, STROBE_HZ

RATE = signals.RATE


def run(samples, seed=7):
    """[(seconds, Frame)] for the whole signal, with a fixed random seed so the colours repeat."""
    return list(analysis.simulate(samples, RATE, rng=random.Random(seed)))


def brightness(frame):
    return max(max(frame.rgb), max(frame.white)) / 100


def flash_peak(frames, window=1.0, jump=0.25):
    """The most flashes in any `window` seconds, where a flash is a jump up in light of at least `jump` of full."""
    edges, previous = [], 0.0
    for t, frame in frames:
        light = brightness(frame)
        if light - previous >= jump:
            edges.append(t)
        previous = light
    return max((sum(1 for e in edges if t <= e < t + window) for t in edges), default=0)


@pytest.mark.parametrize("bpm", [100, 124, 145])
def test_tempo_of_a_steady_kick(bpm):
    frames = run(signals.click_track(bpm, 30))
    tempo = frames[-1][1].lamp_bpm
    assert tempo and abs(tempo / bpm - 1) < 0.03, f"{tempo} for {bpm}"


def test_every_kick_is_heard_and_most_become_lamp_beats():
    frames = run(signals.click_track(124, 30))
    kicks, hits = sum(f.kicked for _, f in frames), sum(f.hit for _, f in frames)
    assert 55 <= kicks <= 65  # 124 BPM for 30 s is 62 beats
    assert hits >= 0.9 * kicks


def test_silence_fades_the_lamp_and_forgets_the_tempo():
    frames = run(signals.join(signals.click_track(128, 15), signals.silence(12)))
    assert frames[14 * AUDIO_FPS][1].lamp_bpm  # tempo known while the music plays
    final = frames[-1][1]
    assert final.lamp_bpm is None  # between tracks the tempo starts over
    assert final.level < 0.1 and brightness(final) < 0.12


def test_a_riser_swells_and_the_return_of_the_kick_flashes_once():
    frames = run(signals.join(signals.click_track(124, 14), signals.riser(5), signals.click_track(124, 8)))
    assert any(f.riser for _, f in frames)
    flash = [t for t, f in frames if f.flash]
    assert flash and max(flash) - min(flash) < 0.5  # one flash, 0.15 s long
    assert next(f for _, f in frames if f.flash).rgb == bytes([100] * 3)


def test_a_new_track_at_another_tempo_snaps_within_a_few_seconds():
    frames = run(signals.join(signals.click_track(128, 20), signals.silence(4), signals.click_track(100, 20)))
    tempo = frames[-1][1].lamp_bpm
    assert abs(tempo / 100 - 1) < 0.04, tempo


def test_the_lamp_never_flashes_more_than_three_times_a_second_on_ordinary_music():
    for bpm in (100, 124, 145, 160):
        assert flash_peak(run(signals.click_track(bpm, 30))) <= STROBE_HZ, bpm


@pytest.mark.parametrize("bpm", [290, 330, 400])
def test_the_lamp_never_flashes_more_than_three_times_a_second_when_bass_notes_double_the_kicks(bpm):
    """A rolling bassline puts bass energy between the kicks, so the detector sees kicks at twice the beat."""
    peak = flash_peak(run(signals.click_track(bpm, 30, hats=False)))
    assert peak <= STROBE_HZ, f"{peak} flashes in one second at {bpm} detected kicks per minute"


def test_the_lamp_never_flashes_more_than_three_times_a_second_through_a_riser_and_its_drop():
    for bpm in (124, 145, 160):
        frames = run(signals.join(signals.click_track(bpm, 14), signals.riser(24), signals.click_track(bpm, 8)))
        assert flash_peak(frames) <= STROBE_HZ, bpm


def test_the_same_audio_and_seed_give_the_same_lamp():
    samples = signals.click_track(124, 12)
    assert [(f.rgb, f.white) for _, f in run(samples)] == [(f.rgb, f.white) for _, f in run(samples)]
