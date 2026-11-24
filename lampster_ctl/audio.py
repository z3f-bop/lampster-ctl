"""Where the audio comes from: the Mac's own output through systap (a Core Audio process tap), or an input device.

Every source has the same shape: `.rate` and `.stream(blocksize, callback)`, a context manager that calls
callback(block, frames, None, None) with a float32 (frames, 1) array, like sd.InputStream."""
import atexit
import contextlib
import json
import os
import select
import subprocess
import sys
import threading
import time

SYSTEM_AUDIO = "system"  # LAMPSTER_AUDIO_INPUT value that taps the Mac's own output instead of an input device
ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
TAP_SOURCE = os.path.join(ROOT, "systap.swift")
TAP_BINARY = os.environ.get("LAMPSTER_TAP_BINARY") or os.path.expanduser("~/.cache/lampster-ctl/systap")


def log(message):
    print(f"lampster: {message}", file=sys.stderr, flush=True)


class SystemTap:
    """What the Mac is playing, tapped by systap.swift (Core Audio process tap, macOS 14.2+).

    One tap serves the whole process: a lamp reconnect re-enters the analysis but reuses this object, so a second
    tap is never opened. A supervisor thread restarts the child when it exits or stops delivering audio, pausing
    longer after each quick failure, and the child exits by itself when this process is gone.

    LAMPSTER_RECORD=path writes the mono float32 samples to path (and the rate to path.json) for `lampster.py replay`."""

    STALL = 3.0  # seconds without a byte before the tap counts as stuck
    PAUSES = (0.5, 1, 2, 5, 10, 30)  # seconds before the next restart, growing while restarts keep failing
    HEALTHY = 10.0  # a tap that ran this long starts the pauses over

    def __init__(self, binary=None):
        import numpy as np

        self.np, self.binary = np, binary or TAP_BINARY
        self.callback = self.pending = self.proc = self.thread = self.recording = None
        self.blocksize, self.restarts, self.closed = 1024, 0, threading.Event()
        if binary is None and (not os.path.exists(TAP_BINARY) or os.path.getmtime(TAP_BINARY) < os.path.getmtime(TAP_SOURCE)):
            os.makedirs(os.path.dirname(TAP_BINARY), exist_ok=True)
            subprocess.run(["swiftc", "-swift-version", "5", "-O", TAP_SOURCE, "-o", TAP_BINARY], check=True)
        try:
            self.rate = self.source_rate = self.spawn()
        except RuntimeError as error:
            sys.exit(str(error))
        if os.environ.get("LAMPSTER_RECORD"):
            path = os.environ["LAMPSTER_RECORD"]
            self.recording = open(path, "wb")
            with open(path + ".json", "w") as sidecar:
                json.dump({"rate": self.rate, "format": "float32 mono"}, sidecar)
            log(f"recording the tap to {path}")
        atexit.register(self.close)

    def spawn(self):
        """Start the child and return its sample rate. stdin stays open: its end of file is the child's cue to exit."""
        proc = subprocess.Popen([self.binary], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        line = proc.stderr.readline().decode() if select.select([proc.stderr], [], [], 15)[0] else ""
        if not line.startswith("rate="):
            proc.kill()
            proc.wait()
            raise RuntimeError(f"system audio tap failed: {(line + proc.stderr.read().decode()).strip()}")
        self.proc = proc
        threading.Thread(target=self.relay, args=(proc,), daemon=True).start()
        return float(line.split()[0].removeprefix("rate="))

    def relay(self, proc):
        for line in proc.stderr:
            log(f"tap: {line.decode().strip()}")

    def stream(self, blocksize, callback):
        self.pending = (blocksize, callback)
        return self

    def __enter__(self):
        self.blocksize, self.callback = self.pending
        if self.thread is None:
            self.thread = threading.Thread(target=self.supervise, daemon=True)
            self.thread.start()
        return self

    def __exit__(self, *exc):
        self.callback = None  # the tap keeps running for the next analysis

    def close(self):
        self.closed.set()
        self.stop(self.proc)
        if self.recording:
            self.recording.close()

    @staticmethod
    def stop(proc):
        if proc is None:
            return
        with contextlib.suppress(OSError):
            proc.stdin.close()  # end of file on stdin makes the child clean up and exit
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def supervise(self):
        failures = 0
        while not self.closed.is_set():
            started = time.monotonic()
            reason = self.pump(self.proc)
            if self.closed.is_set():
                return
            self.stop(self.proc)
            failures = 0 if time.monotonic() - started >= self.HEALTHY else failures + 1
            self.restarts += 1
            log(f"audio tap {reason} after {time.monotonic() - started:.0f}s, restart {self.restarts}")
            while not self.closed.wait(self.PAUSES[min(failures, len(self.PAUSES) - 1)]):
                try:
                    self.source_rate = self.spawn()
                    break
                except RuntimeError as error:
                    failures += 1
                    log(str(error))

    def need(self):
        """Bytes of float32 mono that make one block, at the child's rate."""
        return round(self.blocksize * self.source_rate / self.rate) * 4

    def pump(self, proc):
        """Deliver blocks until the child exits or goes quiet; returns why."""
        fd, buf, last = proc.stdout.fileno(), bytearray(), time.monotonic()
        while not self.closed.is_set():
            if select.select([fd], [], [], 0.5)[0]:
                chunk = os.read(fd, 65536)
                if not chunk:
                    return "exited"
                buf += chunk
                last = time.monotonic()
                while len(buf) >= self.need():
                    size = self.need()
                    self.deliver(bytes(buf[:size]))
                    del buf[:size]
            elif proc.poll() is not None:
                return "exited"
            elif time.monotonic() - last > self.STALL:
                return "stalled"
        return "closed"

    def deliver(self, data):
        callback = self.callback
        if callback is None:
            return
        block = self.np.frombuffer(data, dtype=self.np.float32)
        if self.source_rate != self.rate:  # the output device changed rate: resample to the rate the analysis was built for
            block = self.np.interp(self.np.linspace(0, len(block) - 1, self.blocksize), self.np.arange(len(block)), block).astype(self.np.float32)
        if self.recording:
            self.recording.write(block.tobytes())
        try:
            callback(block.reshape(-1, 1), self.blocksize, None, None)
        except Exception as error:  # a bad block must not end the audio
            log(f"analysis callback failed: {error!r}")


class DeviceAudio:
    """An input device (the microphone, BlackHole), through sounddevice."""

    def __init__(self, device):
        import sounddevice as sd

        self.sd, self.device = sd, device
        self.rate = sd.query_devices(device, "input")["default_samplerate"]

    def stream(self, blocksize, callback):
        return self.sd.InputStream(device=self.device, channels=1, samplerate=self.rate, blocksize=blocksize, callback=callback)


_system_tap = None


def open_audio(device):
    """(sample rate, stream factory taking blocksize and callback) for LAMPSTER_AUDIO_INPUT."""
    global _system_tap
    if device == SYSTEM_AUDIO:
        if _system_tap is None or _system_tap.closed.is_set():
            _system_tap = SystemTap()
        return _system_tap.rate, _system_tap.stream
    audio = DeviceAudio(device)
    return audio.rate, audio.stream
