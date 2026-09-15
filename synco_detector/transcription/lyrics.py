"""Rolling Whisper transcription of microphone audio."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field

import numpy as np

from synco_detector import log
from synco_detector.audio.dsp import isolate_vocals, peak_normalize, speech_gate
from synco_detector.config import ANALYSIS_SR, MODEL_DIR, WHISPER_MODEL


@dataclass
class LyricsState:
    committed: str = ""
    partial: str = ""
    ready: bool = False
    loading: bool = False
    status: str = "idle"
    error: str | None = None
    model: str = WHISPER_MODEL
    note: str = ""
    hearing: bool = False
    segments: list[str] = field(default_factory=list)


_PROMPT_LEAK = (
    "english song",
    "song lyrics",
    "transcript",
    "thank you for watching",
    "thanks for watching",
    "please subscribe",
    "subscribe",
    "www.",
    "happy birthday",
    "oh my god",
    "oh no",
    "music playing",
    "applause",
)

_WORD_RE = re.compile(r"[a-z']+")
_REPEAT_WORD = re.compile(r"\b(\w+)\b(?:[\s,]+(?:\1\b)){3,}")
_SONG_HINT = ("jesus", "loves", "bible", "little", "weak", "strong", "belong")


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(text.lower()) if len(w) > 1]


def _overlap(a: str, b: str) -> float:
    ta, tb = set(_tokens(a)), set(_tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _repeated_ngram(words: list[str], n: int = 3) -> bool:
    if len(words) < n * 3:
        return False
    for i in range(len(words) - n * 2):
        gram = words[i : i + n]
        count = 1
        j = i + n
        while j + n <= len(words) and words[j : j + n] == gram:
            count += 1
            j += n
        if count >= 3:
            return True
    return False


def junk_lyrics(text: str) -> bool:
    low = text.lower().strip()
    if not low:
        return True
    if any(ch in text for ch in "♪🎵🎶🎼"):
        return True
    if any(p in low for p in _PROMPT_LEAK):
        return True
    compact = "".join(ch for ch in low if ch.isalnum())
    if len(text) > 24 and len(set(compact)) < 5:
        return True
    if _REPEAT_WORD.search(low):
        return True
    words = _tokens(low)
    if not words:
        return True
    if len(words) >= 8 and len(set(words)) <= 3:
        return True
    if len(words) >= 6 and max(words.count(w) for w in set(words)) >= len(words) * 0.5:
        return True
    if _repeated_ngram(words, 3) or _repeated_ngram(words, 2):
        return True
    return False


def _song_like(text: str) -> bool:
    low = text.lower()
    return any(h in low for h in _SONG_HINT)


class LyricsTranscriber:
    def __init__(self) -> None:
        self.state = LyricsState()
        self._model = None
        self._lock = threading.Lock()
        self._candidate = ""
        self._hits = 0

    def reset_for_new_track(self) -> None:
        """Clear rolling lyric state when Bluetooth metadata changes songs."""
        with self._lock:
            ready = self.state.ready
            loading = self.state.loading
            err = self.state.error
            model = self.state.model
            self.state = LyricsState(
                ready=ready,
                loading=loading,
                error=err,
                model=model,
                status="ready" if ready else ("loading" if loading else "idle"),
            )
            self._candidate = ""
            self._hits = 0

    def load(self) -> None:
        self.state.loading = True
        self.state.status = "loading"
        log.whisper(f"loading model {WHISPER_MODEL} from {MODEL_DIR}")
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:
            self.state.error = f"faster-whisper not available: {exc}"
            self.state.loading = False
            self.state.status = "error"
            log.error(self.state.error)
            return
        last_err: Exception | None = None
        for name in (WHISPER_MODEL, "base.en", "tiny.en"):
            try:
                MODEL_DIR.mkdir(parents=True, exist_ok=True)
                self._model = WhisperModel(
                    name,
                    device="cpu",
                    compute_type="int8",
                    download_root=str(MODEL_DIR),
                )
                self.state.model = name
                self.state.ready = True
                self.state.loading = False
                self.state.error = None
                self.state.status = "ready"
                log.whisper(f"ready ({name}, cpu/int8) — showing live guesses, committing only steady words")
                return
            except Exception as exc:
                last_err = exc
                log.warn(f"could not load {name}: {exc}")
        self.state.error = f"whisper model load failed: {last_err}"
        self.state.loading = False
        self.state.status = "error"
        self._model = None
        log.error(self.state.error)

    def _purge_junk_committed(self) -> None:
        cleaned = [s for s in self.state.segments if not junk_lyrics(s)]
        if cleaned != self.state.segments:
            self.state.segments = cleaned[-4:]
            self.state.committed = "  ·  ".join(self.state.segments[-3:])

    def transcribe(
        self,
        y: np.ndarray,
        sr: int = ANALYSIS_SR,
        *,
        source: str = "mic",
    ) -> LyricsState:
        if self._model is None:
            log.change("lyrics-skip", "lyrics: model not loaded yet", tag="skip")
            return self.state
        if y.size < sr * 1.5:
            log.change("lyrics-skip", "lyrics: buffer too short", tag="skip")
            return self.state

        bt = source == "bt"
        gate = speech_gate(
            y,
            sr,
            quiet_rms=0.0025 if bt else 0.007,
            min_vocal_share=0.04 if bt else 0.06,
        )
        self.state.hearing = bool(gate["hearing"])
        gate_msg = (
            f"gate {gate['reason']}  rms={gate['rms']:.3f}  peak={gate['peak']:.3f}  "
            f"clip={gate['clip']:.2f}  vocal={gate['vocal_share']:.2f}"
        )
        log.change("whisper-gate", gate_msg, tag="whisper")
        if gate["reason"] == "quiet":
            self.state.status = "quiet"
            self.state.note = ""
            log.change("lyrics-skip", "lyrics: too quiet", tag="skip")
            return self.state
        if gate["reason"] == "clipped":
            self.state.status = "clipped"
            self.state.note = ""
            log.change("lyrics-skip", "lyrics: clipped — skipping decode", tag="skip")
            return self.state
        if gate["reason"] in {"no_voice", "weak_voice"}:
            if bt and gate["peak"] >= 0.035:
                pass
            else:
                self.state.status = "no_voice"
                self.state.note = ""
                self.state.partial = ""
                log.change("lyrics-skip", "lyrics: no clear vocal energy", tag="skip")
                return self.state

        audio = isolate_vocals(y, sr)
        # Soft compression keeps consonants without reinventing clipping.
        audio = np.tanh(audio * 1.15).astype(np.float32)
        audio = peak_normalize(audio, 0.90)
        self.state.status = "decoding"
        self.state.note = ""
        log.whisper(f"decoding {audio.size / sr:.1f}s of vocal-band audio")
        n_raw = 0
        kept: list[tuple[float, float, str]] = []
        try:
            segments, _info = self._model.transcribe(
                audio,
                language="en",
                vad_filter=False,
                beam_size=3,
                best_of=3,
                temperature=[0.0, 0.2],
                condition_on_previous_text=False,
                compression_ratio_threshold=2.2,
                log_prob_threshold=-1.15,
                no_speech_threshold=0.62,
                without_timestamps=True,
            )
            for s in segments:
                n_raw += 1
                nsp = float(getattr(s, "no_speech_prob", 0.0) or 0.0)
                lp = float(getattr(s, "avg_logprob", 0.0) or 0.0)
                cr = float(getattr(s, "compression_ratio", 1.0) or 1.0)
                text = (s.text or "").strip()
                log.whisper(f"  seg nsp={nsp:.2f} logp={lp:.2f} cr={cr:.2f}  {text[:80]!r}")
                if junk_lyrics(text):
                    log.change("lyrics-drop", f"lyrics: dropped junk {text[:60]!r}", tag="skip")
                    continue
                songish = _song_like(text)
                nsp_lim = 0.58 if songish else 0.50
                lp_lim = -1.20 if songish else -1.05
                if nsp > nsp_lim:
                    log.change("lyrics-drop", f"lyrics: dropped (no-speech {nsp:.2f})", tag="skip")
                    continue
                if lp < lp_lim:
                    log.change("lyrics-drop", f"lyrics: dropped (low confidence {lp:.2f})", tag="skip")
                    continue
                if cr > 2.35:
                    log.change("lyrics-drop", f"lyrics: dropped (repetition cr={cr:.2f})", tag="skip")
                    continue
                if len(text) < 2:
                    continue
                kept.append((nsp, lp, text))
        except Exception as exc:
            self.state.error = str(exc)
            self.state.status = "error"
            log.error(f"lyrics decode failed: {exc}")
            return self.state

        self._purge_junk_committed()

        if not kept:
            self.state.status = "empty"
            self.state.note = ""
            self.state.partial = ""
            log.change("lyrics-skip", f"lyrics: no usable words ({n_raw} raw segs)", tag="skip")
            return self.state

        text = " ".join(t for _, _, t in kept).strip()
        if junk_lyrics(text):
            self.state.status = "empty"
            self.state.partial = ""
            self.state.note = ""
            log.change("lyrics-skip", f"lyrics: final junk {text[:60]!r}", tag="skip")
            return self.state

        best_nsp = min(k[0] for k in kept)
        best_lp = max(k[1] for k in kept)
        songish = _song_like(text)
        confident = (best_nsp < 0.32 and best_lp > -0.55) or (
            songish and best_nsp < 0.45 and best_lp > -0.95
        )

        with self._lock:
            # Always surface the live guess so the UI is not stuck on stale commits.
            self.state.partial = text
            if confident:
                self._hits = 2
                self._candidate = text
            elif _overlap(text, self._candidate) >= 0.35:
                self._hits += 1
            else:
                self._candidate = text
                self._hits = 1
            confirmed = self._hits >= 2 or (songish and self._hits >= 1 and best_lp > -0.85)
            if not confirmed:
                self.state.status = "checking"
                self.state.note = ""
                log.whisper(f"live guess {text[:80]!r}")
                return self.state
            if self.state.segments and _overlap(text, self.state.segments[-1]) >= 0.45:
                self.state.segments[-1] = text
            elif not self.state.segments or self.state.segments[-1] != text:
                self.state.segments.append(text)
            self.state.segments = [s for s in self.state.segments if not junk_lyrics(s)][-4:]
            self.state.committed = "  ·  ".join(self.state.segments[-3:])
            self.state.status = "ready"
            self.state.note = ""
        log.lyric(text[:140] + ("…" if len(text) > 140 else ""))
        return self.state
