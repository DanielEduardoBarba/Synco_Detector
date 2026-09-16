"""Live analysis engine: mic → rhythm verdict + lyrics."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from synco_detector import log
from synco_detector.analysis.classifier import Verdict, analyze_audio, _band
from synco_detector.audio.bt_sink import BluetoothSinkCapture
from synco_detector.audio.capture import MicCapture
from synco_detector.audio.dsp import clip_fraction, freq_map, rms
from synco_detector.config import (
    ANALYSIS_HOP_S,
    ANALYSIS_WINDOW_S,
    BT_ALIAS,
    SAMPLE_RATE,
    TRANSCRIBE_HOP_S,
    TRANSCRIBE_WINDOW_S,
)
from synco_detector.transcription.lyrics import LyricsState, LyricsTranscriber


def _audio_sink_enabled() -> bool:
    return os.environ.get("SYNCO_AUDIO_SINK", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _side_from_spectrum(spec: float) -> str:
    if spec < 35:
        return "simple"
    if spec > 65:
        return "syncopated"
    return "mixed"


def _summary_from_avg(v: Verdict, avg: float) -> str:
    """Keep tempo/emphasis from the live frame; judgment text tracks the average."""
    judgment = _band(avg)
    tail = ""
    if v.summary and ". " in v.summary:
        tail = v.summary.split(". ", 1)[1]
    if not tail:
        tail = f"Confidence {v.confidence:.0f}%. Tempo ~{v.tempo_bpm:.0f} BPM."
    return f"{judgment}. {tail}"


@dataclass
class LiveState:
    listening: bool = False
    mic_name: str = ""
    mic_error: str | None = None
    audio_mode: str = "mic"  # "mic" | "bluetooth"
    bt_status: str = ""
    pair_hint: str = ""
    pair_passkey: str | None = None
    bt_now_playing: dict = field(default_factory=dict)
    signal_level: float = 0.0
    freq_map: list[float] = field(default_factory=list)
    lyrics: LyricsState = field(default_factory=LyricsState)
    verdict: dict = field(default_factory=dict)
    song_history: list = field(default_factory=list)
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "listening": self.listening,
            "mic_name": self.mic_name,
            "mic_error": self.mic_error,
            "audio_mode": self.audio_mode,
            "bt_status": self.bt_status,
            "pair_hint": self.pair_hint,
            "pair_passkey": self.pair_passkey,
            "bt_now_playing": self.bt_now_playing,
            "signal_level": self.signal_level,
            "freq_map": self.freq_map,
            "lyrics": {
                "committed": self.lyrics.committed,
                "partial": self.lyrics.partial,
                "ready": self.lyrics.ready,
                "loading": self.lyrics.loading,
                "status": self.lyrics.status,
                "error": self.lyrics.error,
                "model": self.lyrics.model,
                "note": self.lyrics.note,
                "hearing": self.lyrics.hearing,
                "segments": self.lyrics.segments[-6:],
            },
            "verdict": self.verdict,
            "song_history": list(self.song_history),
            "updated_at": self.updated_at,
        }


class Engine:
    def __init__(self) -> None:
        self.audio_sink = _audio_sink_enabled()
        if self.audio_sink:
            alias = os.environ.get("SYNCO_BT_ALIAS", BT_ALIAS)
            self.capture: MicCapture | BluetoothSinkCapture = BluetoothSinkCapture(
                sr=SAMPLE_RATE, alias=alias
            )
        else:
            self.capture = MicCapture(sr=SAMPLE_RATE)
        self.lyrics = LyricsTranscriber()
        self.state = LiveState(audio_mode="bluetooth" if self.audio_sink else "mic")
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._listeners: list[Callable[[dict], None]] = []
        # RLock: spectrum/analysis may nest meta reset while holding the state lock.
        self._lock = threading.RLock()
        self._meta_lock = threading.Lock()
        self._ema: float | None = None
        self._dwell = np.zeros(21, dtype=np.float64)
        self._song_avg: float | None = None
        self._whisper_loading = False
        self._last_groove_log = 0.0
        self._bt_wait_announced = False
        self._track_sig = ""
        self._last_pos_ms: int | None = None
        self._history: dict[str, dict] = {}
        self._history_order: list[str] = []

    def subscribe(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        self._listeners.append(fn)

        def _unsub() -> None:
            try:
                self._listeners.remove(fn)
            except ValueError:
                pass

        return _unsub

    def snapshot(self) -> dict:
        with self._lock:
            return self.state.to_dict()

    def start(self) -> None:
        if self.state.listening:
            return
        self._stop.clear()
        self.capture.start()
        self.state.listening = True
        self.state.audio_mode = "bluetooth" if self.audio_sink else "mic"
        self.state.mic_name = self.capture.device_name
        self.state.mic_error = self.capture.error
        if self.capture.error:
            log.error(f"audio: {self.capture.error}")
        elif self.audio_sink:
            log.boot("AUDIO SINK mode — mic off; connect iPhone to this Bluetooth speaker")
            log.bt(f"input={self.capture.device_name!r}")
        else:
            log.mic(f"open  device={self.capture.device_name!r}  sr={SAMPLE_RATE}")
        self._threads = [
            threading.Thread(target=self._analysis_loop, name="synco-analyze", daemon=True),
            threading.Thread(target=self._spectrum_loop, name="synco-freqmap", daemon=True),
            threading.Thread(target=self._lyrics_loop, name="synco-lyrics", daemon=True),
        ]
        for t in self._threads:
            t.start()
        log.boot("analysis + lyrics threads running")

    def stop(self) -> None:
        self._stop.set()
        self.capture.stop()
        self.state.listening = False
        log.mic("stopped")

    def ensure_whisper(self) -> None:
        if self.lyrics.state.ready or self._whisper_loading:
            return
        self._whisper_loading = True
        self.lyrics.state.loading = True
        self.lyrics.state.status = "loading"

        def _load():
            self.lyrics.load()
            self._whisper_loading = False

        threading.Thread(target=_load, name="synco-whisper-load", daemon=True).start()

    def analyze_array(self, y: np.ndarray, sr: int = SAMPLE_RATE) -> Verdict:
        v = analyze_audio(y, sr=sr)
        v.spectrum_avg = v.spectrum
        log.groove(
            f"file  side={v.side:11}  spec={v.spectrum:5.1f}  conf={v.confidence:4.0f}%  "
            f"emp={v.primary_emphasis}  {v.meter}  {v.tempo_bpm:.0f}bpm",
            side=v.side,
        )
        with self._lock:
            self.state.verdict = v.to_dict()
            self.state.signal_level = float(rms(y))
            self.state.updated_at = time.time()
        return v

    def _broadcast(self) -> None:
        # Never call while holding _lock with a non-reentrant Lock — snapshot takes _lock.
        snap = self.snapshot()
        for fn in list(self._listeners):
            try:
                fn(snap)
            except Exception:
                pass

    def _smooth(self, v: Verdict) -> Verdict:
        raw = float(v.spectrum)
        feats = v.features or {}
        hymn = bool(feats.get("m_hymn_pulse"))
        snare = float(feats.get("m_snare_backbeat") or 0.0)
        perc = bool(feats.get("m_percussive"))
        if v.side == "silence" or v.confidence < 8:
            avg = float(self._song_avg if self._song_avg is not None else raw)
            v.spectrum_avg = round(avg, 2)
            if self._song_avg is not None:
                v.side = _side_from_spectrum(avg)
                v.judgment = _band(avg)
                v.summary = _summary_from_avg(v, avg)
            return v
        if self._ema is None:
            self._ema = raw
        else:
            if hymn or raw <= 12:
                alpha = 0.78
            elif perc and (snare > 0.28 or raw >= 74):
                alpha = 0.72
            elif abs(raw - self._ema) > 28 and v.confidence >= 22:
                alpha = 0.60
            elif v.confidence >= 25:
                alpha = 0.48
            else:
                alpha = 0.28
            self._ema = alpha * raw + (1.0 - alpha) * self._ema

        now_spec = round(float(np.clip(self._ema, 0, 100)), 2)
        v.spectrum = now_spec
        avg = round(self._accumulate_dwell(now_spec, v.confidence), 2)
        v.spectrum_avg = avg
        v.side = _side_from_spectrum(avg)
        v.judgment = _band(avg)
        v.summary = _summary_from_avg(v, avg)
        if not isinstance(v.features, dict):
            v.features = {}
        v.features["spectrum_now"] = now_spec
        v.features["spectrum_avg"] = avg
        v.features["side_now"] = _side_from_spectrum(now_spec)
        return v

    def _accumulate_dwell(self, spectrum: float, confidence: float) -> float:
        conf_w = float(np.clip(confidence / 100.0, 0.08, 1.0))
        # Soft locality — allow a new groove to migrate after a track change.
        if self._song_avg is not None:
            near = float(np.exp(-((spectrum - self._song_avg) / 28.0) ** 2))
            conf_w *= 0.55 + 0.45 * near
        x = float(np.clip(spectrum, 0.0, 100.0)) / 5.0
        deposit = np.zeros(21, dtype=np.float64)
        for i in range(21):
            d = abs(i - x)
            if d < 2.8:
                deposit[i] = max(0.0, 1.0 - d / 2.8) ** 2
        self._dwell *= 0.992
        self._dwell += conf_w * deposit
        mass = float(self._dwell.sum())
        if mass < 1e-6:
            self._song_avg = float(spectrum)
            return float(spectrum)
        centers = np.arange(21, dtype=np.float64) * 5.0
        avg = float(np.dot(self._dwell, centers) / mass)
        self._song_avg = float(np.clip(avg, 0.0, 100.0))
        return self._song_avg

    def reset_song_average(self) -> None:
        self._dwell[:] = 0.0
        self._song_avg = None
        self._ema = None

    def pcm_chunk(self, seconds: float = 0.08) -> np.ndarray:
        """New dry float32 mono PCM for browser listen-through (no overlapping slices)."""
        try:
            if hasattr(self.capture, "consume_listen"):
                y = self.capture.consume_listen(seconds)
            else:
                y = self.capture.latest(seconds)
        except Exception:
            return np.zeros(0, dtype=np.float32)
        if y is None or getattr(y, "size", 0) == 0:
            return np.zeros(0, dtype=np.float32)
        return np.asarray(y, dtype=np.float32)

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()
            self._history_order.clear()
            self.state.song_history = []
            self.state.updated_at = time.time()
        self._broadcast()

    def _publish_history(self) -> None:
        rows = [self._history[k] for k in self._history_order if k in self._history]
        self.state.song_history = rows

    def _upsert_history(
        self,
        *,
        artist: str,
        title: str,
        album: str = "",
        duration_ms: int = 0,
        spectrum_avg: float | None = None,
        side: str = "",
        judgment: str = "",
        confidence: float | None = None,
        reset: bool = False,
    ) -> None:
        if not title and not artist:
            return
        key = f"{artist.casefold()}|{title.casefold()}"
        now = time.time()
        with self._lock:
            prev = self._history.get(key)
            if reset or prev is None:
                plays = 1 if prev is None else int(prev.get("plays") or 1)
                if reset and prev is not None:
                    plays = int(prev.get("plays") or 0) + 1
                row = {
                    "key": key,
                    "title": title,
                    "artist": artist,
                    "album": album,
                    "duration_ms": int(duration_ms or 0),
                    "spectrum_avg": 50.0 if spectrum_avg is None else float(spectrum_avg),
                    "side": side or "—",
                    "judgment": judgment or "Listening…",
                    "confidence": float(confidence or 0.0),
                    "updated_at": now,
                    "plays": plays,
                }
            else:
                row = dict(prev)
                row["title"] = title or row.get("title") or ""
                row["artist"] = artist or row.get("artist") or ""
                if album:
                    row["album"] = album
                if duration_ms:
                    row["duration_ms"] = int(duration_ms)
                if spectrum_avg is not None:
                    row["spectrum_avg"] = float(spectrum_avg)
                if side:
                    row["side"] = side
                if judgment:
                    row["judgment"] = judgment
                if confidence is not None:
                    row["confidence"] = float(confidence)
                row["updated_at"] = now
            self._history[key] = row
            if key in self._history_order:
                self._history_order.remove(key)
            self._history_order.insert(0, key)
            # Cap history length.
            while len(self._history_order) > 40:
                old = self._history_order.pop()
                self._history.pop(old, None)
            self._publish_history()

    def media_command(self, action: str, position_ms: int | None = None) -> dict:
        if not self.audio_sink or not hasattr(self.capture, "media_command"):
            return {"ok": False, "error": "Bluetooth sink mode required"}
        result = self.capture.media_command(action, position_ms=position_ms)
        if not isinstance(result, dict) or not result.get("ok"):
            return result if isinstance(result, dict) else {"ok": False, "error": "media command failed"}
        # Prefer phone-confirmed position when the helper returns one.
        confirmed = result.get("position_ms")
        if action == "seek":
            try:
                position_ms = int(confirmed if confirmed is not None else position_ms)
            except (TypeError, ValueError):
                pass
        # Optimistic UI + flush so scrubber/audio don't sit on stale buffer.
        with self._meta_lock:
            np_meta = dict(self.state.bt_now_playing) if isinstance(self.state.bt_now_playing, dict) else {}
            if action == "seek" and position_ms is not None:
                try:
                    pos = max(0, int(position_ms))
                except (TypeError, ValueError):
                    pos = 0
                np_meta["position_ms"] = pos
                self._last_pos_ms = pos
                if not np_meta.get("status"):
                    np_meta["status"] = "playing"
            elif action == "pause":
                np_meta["status"] = "paused"
            elif action == "play":
                np_meta["status"] = "playing"
            elif action in {"next", "prev"}:
                np_meta["position_ms"] = 0
                self._last_pos_ms = 0
            try:
                live = getattr(self.capture, "now_playing", None)
                live = live() if callable(live) else live
                if isinstance(live, dict):
                    np_meta = {**np_meta, **live}
                    if action == "seek" and position_ms is not None:
                        np_meta["position_ms"] = max(0, int(position_ms))
            except Exception:
                pass
            self.state.bt_now_playing = np_meta
            self.state.updated_at = time.time()
        self._broadcast()
        return result

    def _reset_for_new_song(self, artist: str, title: str, reason: str) -> None:
        self.reset_song_average()
        try:
            self.lyrics.reset_for_new_track()
        except Exception:
            pass
        np_meta = self.state.bt_now_playing if isinstance(self.state.bt_now_playing, dict) else {}
        album = str(np_meta.get("album") or "").strip()
        try:
            duration_ms = int(np_meta.get("duration_ms") or 0)
        except (TypeError, ValueError):
            duration_ms = 0
        # Same song again → overwrite history row and rebuild avg from scratch.
        self._upsert_history(
            artist=artist,
            title=title,
            album=album,
            duration_ms=duration_ms,
            spectrum_avg=50.0,
            side="—",
            judgment="Listening…",
            confidence=0.0,
            reset=True,
        )
        with self._lock:
            self.state.verdict = {}
            self.state.lyrics = self.lyrics.state
            self.state.updated_at = time.time()
        label = f"{artist or '—'} — {title or '—'}"
        log.bt(f"{reason} → reset groove/lyrics  {label}")

    def _on_track_meta(self, meta: dict | None) -> None:
        """Reset groove + lyrics on track name change or on-repeat rewind."""
        if not isinstance(meta, dict):
            return
        title = str(meta.get("title") or "").strip()
        artist = str(meta.get("artist") or "").strip()
        album = str(meta.get("album") or "").strip()
        try:
            pos = max(0, int(meta.get("position_ms") or 0))
        except (TypeError, ValueError):
            pos = 0
        try:
            dur = max(0, int(meta.get("duration_ms") or 0))
        except (TypeError, ValueError):
            dur = 0

        reason: str | None = None
        with self._meta_lock:
            prev = self.state.bt_now_playing if isinstance(self.state.bt_now_playing, dict) else {}
            # Sticky merge: never flash empty title over a known track mid-skip.
            if not title and not artist:
                title = str(prev.get("title") or "").strip()
                artist = str(prev.get("artist") or "").strip()
                album = album or str(prev.get("album") or "").strip()
                if not dur:
                    try:
                        dur = max(0, int(prev.get("duration_ms") or 0))
                    except (TypeError, ValueError):
                        dur = 0
                merged = {**prev, **meta, "title": title, "artist": artist, "album": album}
                if dur:
                    merged["duration_ms"] = dur
                self.state.bt_now_playing = merged
                if not title and not artist:
                    return
            else:
                if not album:
                    album = str(prev.get("album") or "").strip()
                merged = {**prev, **meta, "title": title, "artist": artist, "album": album}
                if dur:
                    merged["duration_ms"] = dur
                self.state.bt_now_playing = merged

            sig = f"{artist.casefold()}|{title.casefold()}"
            if sig == self._track_sig:
                last = self._last_pos_ms
                self._last_pos_ms = pos
                if last is None:
                    return
                restarted = False
                if dur >= 30_000:
                    near_end = last >= max(int(dur * 0.72), dur - 45_000)
                    near_start = pos <= min(12_000, max(4_000, int(dur * 0.08)))
                    if near_end and near_start:
                        restarted = True
                if not restarted and last > 20_000 and pos < 8_000 and (last - pos) > 15_000:
                    restarted = True
                # Manual restart / scrub-to-start mid track.
                if not restarted and last - pos > 25_000 and pos < 15_000:
                    restarted = True
                if restarted:
                    reason = "repeat"
            else:
                prev_sig = self._track_sig
                self._track_sig = sig
                self._last_pos_ms = pos
                reason = "track change" if prev_sig else "now playing"

        # Release meta lock before reset (lyrics lock / state lock can block).
        if reason:
            self._reset_for_new_song(artist, title, reason)

    def _spectrum_loop(self) -> None:
        """Fast bass→treble map for the live UI (~12 fps)."""
        while not self._stop.wait(0.08):
            y = self.capture.latest(0.28)
            bands = freq_map(y, SAMPLE_RATE, n_bars=40)
            level = float(rms(y)) if y.size else 0.0
            meta = None
            bt_status = ""
            mic_name = self.capture.device_name
            if self.audio_sink and hasattr(self.capture, "now_playing"):
                np_meta = getattr(self.capture, "now_playing", None)
                meta = np_meta() if callable(np_meta) else np_meta
                bt_status = getattr(self.capture, "bt_status", "") or ""
            # Track-change reset must not run while holding _lock (nested acquire freeze).
            if isinstance(meta, dict):
                self._on_track_meta(meta)
            with self._lock:
                self.state.freq_map = bands
                # Keep the meter alive between groove hops.
                if level > 0.0:
                    self.state.signal_level = level
                self.state.updated_at = time.time()
                if bt_status:
                    self.state.bt_status = bt_status
                self.state.mic_name = mic_name
            self._broadcast()

    def _analysis_loop(self) -> None:
        while not self._stop.wait(ANALYSIS_HOP_S):
            y = self.capture.latest(ANALYSIS_WINDOW_S)
            self.state.mic_error = self.capture.error
            self.state.mic_name = self.capture.device_name
            if self.audio_sink and hasattr(self.capture, "pair_hint"):
                self.state.pair_hint = getattr(self.capture, "pair_hint", "") or ""
                self.state.pair_passkey = getattr(self.capture, "last_passkey", None)
                self.state.bt_status = getattr(self.capture, "bt_status", "") or ""
                np_meta = getattr(self.capture, "now_playing", None)
                meta = np_meta() if callable(np_meta) else np_meta
                if isinstance(meta, dict):
                    self._on_track_meta(meta)
            y_live = self.capture.latest(1.0) if self.audio_sink else y
            level = float(rms(y_live)) if y_live.size else 0.0
            peak_live = float(np.max(np.abs(y_live))) if y_live.size else 0.0
            bt_waiting = False
            if self.audio_sink:
                bt_st = getattr(self.capture, "bt_status", "") or self.state.bt_status
                live_peak = float(getattr(self.capture, "live_peak", 0.0) or 0.0)
                # Use the last ~1s (not the full 10s window) so stale buffer doesn't fake signal.
                has_signal = (
                    level >= 0.003
                    or peak_live >= 0.025
                    or live_peak >= 0.02
                )
                if bt_st != "connected" or not has_signal:
                    bt_waiting = True
            if y.size < SAMPLE_RATE or bt_waiting:
                self.state.signal_level = 0.0
                if self.audio_sink:
                    if not self._bt_wait_announced:
                        self._bt_wait_announced = True
                        log.bt("waiting for phone audio — press Play on iPhone (silent sink armed)")
                    log.change(
                        "bt-groove-wait",
                        "groove: waiting for Bluetooth audio from phone",
                        tag="skip",
                    )
                else:
                    log.change("mic-groove-wait", "groove: waiting for 1s of audio", tag="skip")
                self._broadcast()
                continue
            if self._bt_wait_announced:
                log.change("bt-groove-wait", "groove: Bluetooth audio flowing", tag="bt")
            self._bt_wait_announced = False
            v = analyze_audio(y, sr=SAMPLE_RATE)
            raw_spec = float(v.spectrum)
            v = self._smooth(v)
            feats = v.features or {}
            now = time.time()
            if now - self._last_groove_log >= 0.9:
                self._last_groove_log = now
                log.groove(
                    f"rms={level:.3f}  raw={raw_spec:5.1f}  now={v.spectrum:5.1f}  "
                    f"avg={v.spectrum_avg:5.1f}  conf={v.confidence:4.0f}%  "
                    f"side={v.side:11}  emp={v.primary_emphasis:<7}  {v.meter} {v.tempo_bpm:.0f}bpm  "
                    f"onbeat={float(feats.get('m_onbeat_tolerant', 0)):.2f}  "
                    f"hymn={feats.get('m_hymn_pulse')}  perc={feats.get('m_percussive')}  "
                    f"snare={float(feats.get('m_snare_backbeat', 0)):.2f}",
                    side=v.side,
                )
                peak = float(np.max(np.abs(y))) if y.size else 0.0
                clip = clip_fraction(y)
                src = "bt" if self.audio_sink else "mic"
                log.mic(
                    f"{src}  buffer={y.size / SAMPLE_RATE:.1f}s  peak={peak:.3f}  "
                    f"clip={clip:.2f}  agc={self.capture.gain:.2f}"
                )
                if clip > 0.08 and not self.audio_sink:
                    log.warn("mic is clipping — turn the speaker down a bit for a cleaner read")
            with self._lock:
                self.state.verdict = v.to_dict()
                self.state.signal_level = level
                self.state.lyrics = self.lyrics.state
                self.state.updated_at = time.time()
            np_meta = self.state.bt_now_playing if isinstance(self.state.bt_now_playing, dict) else {}
            title = str(np_meta.get("title") or "").strip()
            artist = str(np_meta.get("artist") or "").strip()
            if title or artist:
                try:
                    duration_ms = int(np_meta.get("duration_ms") or 0)
                except (TypeError, ValueError):
                    duration_ms = 0
                self._upsert_history(
                    artist=artist,
                    title=title,
                    album=str(np_meta.get("album") or "").strip(),
                    duration_ms=duration_ms,
                    spectrum_avg=float(v.spectrum_avg),
                    side=str(v.side or ""),
                    judgment=str(v.judgment or ""),
                    confidence=float(v.confidence or 0.0),
                    reset=False,
                )
            self._broadcast()

    def _lyrics_loop(self) -> None:
        self.ensure_whisper()
        while not self._stop.wait(TRANSCRIBE_HOP_S):
            if self.lyrics.state.loading:
                continue
            if not self.lyrics.state.ready:
                if self.lyrics.state.error:
                    log.error(f"lyrics disabled: {self.lyrics.state.error}")
                continue
            y = self.capture.latest(TRANSCRIBE_WINDOW_S)
            if y.size < SAMPLE_RATE * 1.5:
                log.change("lyrics-buffer", "lyrics: need ~1.5s of audio", tag="skip")
                continue
            src = "bt" if self.audio_sink else "mic"
            self.lyrics.transcribe(y, sr=SAMPLE_RATE, source=src)
            with self._lock:
                self.state.lyrics = self.lyrics.state
            self._broadcast()


ENGINE = Engine()
