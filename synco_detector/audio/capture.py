"""Microphone capture into a rolling buffer."""

from __future__ import annotations

import threading

import numpy as np

from synco_detector.config import BLOCK_SIZE, CHANNELS, SAMPLE_RATE


class MicCapture:
    def __init__(self, sr: int = SAMPLE_RATE, maxlen_s: float = 30.0) -> None:
        self.sr = sr
        self.maxlen = int(sr * maxlen_s)
        self._buf = np.zeros(self.maxlen, dtype=np.float32)
        self._write = 0
        self._filled = 0
        self._lock = threading.Lock()
        self._stream = None
        self._err: str | None = None
        self.device_name = "none"
        self._level = 0.12
        self.gain = 1.0
        self.mode = "mic"

    def start(self) -> None:
        try:
            import sounddevice as sd
        except Exception as exc:  # pragma: no cover
            self._err = f"sounddevice unavailable: {exc}"
            return
        try:
            info = sd.query_devices(kind="input")
            self.device_name = str(info.get("name", "default"))
        except Exception:
            self.device_name = "default"

        def callback(indata, frames, time_info, status):  # noqa: ARG001
            if status:
                self._err = str(status)
            mono = np.asarray(indata[:, 0] if indata.ndim > 1 else indata, dtype=np.float32)
            n = mono.size
            block_peak = float(np.max(np.abs(mono))) if n else 0.0
            self._level = 0.97 * self._level + 0.03 * max(block_peak, 1e-4)
            if self._level > 0.50:
                self.gain = 0.50 / self._level
            else:
                self.gain = 1.0
            mono = np.clip(mono * self.gain, -0.98, 0.98)
            with self._lock:
                i = self._write
                end = i + n
                if end <= self.maxlen:
                    self._buf[i:end] = mono
                else:
                    k = self.maxlen - i
                    self._buf[i:] = mono[:k]
                    self._buf[: n - k] = mono[k:]
                self._write = end % self.maxlen
                self._filled = min(self.maxlen, self._filled + n)

        try:
            self._stream = sd.InputStream(
                samplerate=self.sr,
                channels=CHANNELS,
                blocksize=BLOCK_SIZE,
                dtype="float32",
                callback=callback,
            )
            self._stream.start()
        except Exception as exc:
            self._err = f"microphone open failed: {exc}"
            self._stream = None

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    @property
    def error(self) -> str | None:
        return self._err

    @property
    def active(self) -> bool:
        return self._stream is not None

    def latest(self, seconds: float) -> np.ndarray:
        n = min(int(self.sr * seconds), self.maxlen)
        with self._lock:
            filled = self._filled
            write = self._write
            if filled == 0:
                return np.zeros(0, dtype=np.float32)
            n = min(n, filled)
            start = (write - n) % self.maxlen
            if start + n <= self.maxlen:
                return self._buf[start : start + n].copy()
            k = self.maxlen - start
            return np.concatenate([self._buf[start:], self._buf[: n - k]])
