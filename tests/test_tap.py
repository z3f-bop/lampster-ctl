"""The system tap supervisor, against a fake tap process: no lamp and no audio hardware needed.
Run: uv run --with pytest --with numpy --with bleak --with sounddevice pytest tests"""
import os
import time

import pytest

from lampster_ctl import audio

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_tap.py")


def wait_until(condition, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def make_tap(tmp_path, monkeypatch):
    taps, counter = [], tmp_path / "spawns"
    monkeypatch.setattr(audio.SystemTap, "STALL", 0.6)
    monkeypatch.setattr(audio.SystemTap, "PAUSES", (0.1, 0.1, 0.1))
    monkeypatch.setenv("FAKE_TAP_COUNTER", str(counter))

    def make(modes="steady", rates="48000"):
        monkeypatch.setenv("FAKE_TAP_MODES", modes)
        monkeypatch.setenv("FAKE_TAP_RATES", rates)
        tap = audio.SystemTap(binary=FAKE)
        taps.append(tap)
        return tap

    make.spawns = lambda: sum(1 for _ in open(counter)) if counter.exists() else 0
    yield make
    for tap in taps:
        tap.close()


def collect(tap, blocksize=1024):
    blocks = []
    tap.stream(blocksize, lambda block, frames, t, s: blocks.append(block)).__enter__()
    return blocks


def test_delivers_blocks_of_the_requested_size(make_tap):
    tap = make_tap()
    blocks = collect(tap)
    assert wait_until(lambda: len(blocks) >= 30)
    assert all(b.shape == (1024, 1) and str(b.dtype) == "float32" for b in blocks)
    assert max(abs(b).max() for b in blocks) > 0.1  # real samples, not zeros


def test_restarts_a_tap_that_dies(make_tap):
    tap = make_tap(modes="die,steady")
    blocks = collect(tap)
    assert wait_until(lambda: tap.restarts == 1)
    seen = len(blocks)
    assert wait_until(lambda: len(blocks) > seen + 20)  # audio flows again
    assert make_tap.spawns() == 2


def test_restarts_a_tap_that_stalls_and_kills_the_stuck_one(make_tap):
    tap = make_tap(modes="stall,steady")
    blocks = collect(tap)
    stuck = tap.proc
    assert wait_until(lambda: tap.restarts == 1)
    assert wait_until(lambda: stuck.poll() is not None)  # the stuck child did not linger
    seen = len(blocks)
    assert wait_until(lambda: len(blocks) > seen + 20)


def test_a_tap_that_keeps_failing_is_retried_with_growing_pauses_not_in_a_hot_loop(make_tap):
    tap = make_tap(modes="die")
    collect(tap)
    time.sleep(2.5)
    assert 2 <= tap.restarts <= 8  # restarts continue, but not hundreds


def test_the_tap_outlives_one_analysis_and_serves_the_next(make_tap):
    tap = make_tap()
    first = collect(tap)
    assert wait_until(lambda: len(first) > 5)
    tap.__exit__(None, None, None)  # a lamp reconnect ends the analysis...
    seen = len(first)
    time.sleep(0.3)
    assert len(first) == seen  # ...which stops receiving blocks
    second = collect(tap)  # the next analysis reuses the same tap
    assert wait_until(lambda: len(second) > 5)
    assert make_tap.spawns() == 1 and tap.restarts == 0


def test_rate_change_after_a_restart_is_resampled_to_the_original_rate(make_tap):
    tap = make_tap(modes="stall,steady", rates="48000,44100")
    blocks = collect(tap)
    assert wait_until(lambda: tap.restarts == 1 and tap.source_rate == 44100)
    seen = len(blocks)
    assert wait_until(lambda: len(blocks) > seen + 20)
    assert tap.rate == 48000
    assert all(b.shape == (1024, 1) for b in blocks)  # the analysis still gets what it was built for


def test_a_failing_callback_does_not_end_the_audio(make_tap):
    tap = make_tap()
    calls = []

    def bad(block, frames, t, s):
        calls.append(1)
        raise ValueError("boom")

    tap.stream(1024, bad).__enter__()
    assert wait_until(lambda: len(calls) > 10)  # kept delivering after the first exception


def test_close_stops_the_child(make_tap):
    tap = make_tap()
    collect(tap)
    proc = tap.proc
    tap.close()
    assert proc.poll() is not None


def test_open_audio_reuses_one_system_tap(monkeypatch):
    class Stub:
        rate = 48000.0
        closed = type("E", (), {"is_set": staticmethod(lambda: False)})()

        def stream(self, *args):
            return self

    stub = Stub()
    monkeypatch.setattr(audio, "_system_tap", stub)
    assert audio.open_audio("system") == (48000.0, stub.stream)
    assert audio.open_audio("system") == (48000.0, stub.stream)  # a reconnect asks again...
    assert audio._system_tap is stub  # ...and never builds a second tap
