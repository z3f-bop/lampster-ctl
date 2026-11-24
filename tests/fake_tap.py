#!/usr/bin/env python3
"""Stand-in for systap in tests: float32 mono sine blocks on stdout, "rate=..." on stderr, exit on stdin EOF.

Behaviour comes from the environment, one entry per spawn (the last entry repeats):
  FAKE_TAP_COUNTER  file that gets one line per spawn (required)
  FAKE_TAP_MODES    steady | die (exits after 0.5 s) | stall (alive but silent after 0.5 s) | silent
  FAKE_TAP_RATES    sample rate each spawn reports"""
import math
import os
import struct
import sys
import threading
import time

counter = os.environ["FAKE_TAP_COUNTER"]
with open(counter, "a") as f:
    f.write("spawn\n")
spawn = sum(1 for _ in open(counter))


def nth(name, default):
    values = os.environ.get(name, default).split(",")
    return values[min(spawn, len(values)) - 1]


mode, rate = nth("FAKE_TAP_MODES", "steady"), int(nth("FAKE_TAP_RATES", "48000"))
sys.stderr.write(f"rate={rate} channels=2 interleaved=true\n")
sys.stderr.flush()


def watch_stdin():
    while sys.stdin.buffer.read(1024):
        pass
    os._exit(0)


threading.Thread(target=watch_stdin, daemon=True).start()

block, sent, started = 480, 0, time.monotonic()
while True:
    age = time.monotonic() - started
    if mode == "silent" or (mode == "stall" and age > 0.5):
        time.sleep(60)
    if mode == "die" and age > 0.5:
        os._exit(3)  # sys.exit would hang at shutdown: the stdin watcher thread is blocked in read()
    samples = [0.5 * math.sin(2 * math.pi * 440 * (sent + i) / rate) for i in range(block)]
    sys.stdout.buffer.write(struct.pack(f"{block}f", *samples))
    sys.stdout.buffer.flush()
    sent += block
    time.sleep(block / rate)
