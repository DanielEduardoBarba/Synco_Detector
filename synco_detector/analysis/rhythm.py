"""Tempo, meter, downbeat alignment, and 16th-note accent histograms."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from synco_detector.audio.dsp import autocorrelation, band_fluxes, rms
from synco_detector.config import ANALYSIS_SR

HOP = 256
N_FFT = 1024
MIN_BPM = 72.0
MAX_BPM = 148.0

# Lerdahl-Jackendoff-ish metrical weights for 16ths in 4/4.
WEIGHTS_4_16 = np.array([5, 1, 2, 1, 3, 1, 2, 1, 4, 1, 2, 1, 3, 1, 2, 1], dtype=np.float64)
WEIGHTS_3_12 = np.array([5, 1, 2, 1, 3, 1, 2, 1, 3, 1, 2, 1], dtype=np.float64)
WEIGHTS_2_8 = np.array([5, 1, 2, 1, 3, 1, 2, 1], dtype=np.float64)


@dataclass
class RhythmReport:
    tempo_bpm: float
    tempo_clarity: float
    meter: str
    beats_per_bar: int
    downbeat_source: str
    hist16: np.ndarray
    hist_low: np.ndarray
    hist_mid: np.ndarray
    hist_high: np.ndarray
    beat_energies: np.ndarray
    beat_low: np.ndarray
    beat_mid: np.ndarray
    rms: float
    hop: int = HOP
    sr: int = ANALYSIS_SR
    extras: dict = field(default_factory=dict)


def _bpm_to_lag(bpm: float, sr: int, hop: int) -> float:
    return (60.0 / bpm) * sr / hop


def estimate_tempo(
    onset: np.ndarray,
    sr: int = ANALYSIS_SR,
    hop: int = HOP,
    onset_kick: np.ndarray | None = None,
) -> tuple[float, float]:
    if onset.size < 32:
        return 0.0, 0.0
    min_lag = max(2, int(_bpm_to_lag(200.0, sr, hop)))
    max_lag = min(onset.size - 2, int(_bpm_to_lag(50.0, sr, hop)))
    if max_lag <= min_lag + 2:
        return 0.0, 0.0
    ac = autocorrelation(onset, max_lag)
    ac[:min_lag] = 0
    scored: list[tuple[float, float, float]] = []
    for lag in range(min_lag + 1, max_lag):
        if ac[lag] < 0.10:
            continue
        if ac[lag] >= ac[lag - 1] and ac[lag] >= ac[lag + 1]:
            bpm = 60.0 * sr / (lag * hop)
            folded = bpm
            while folded < MIN_BPM:
                folded *= 2.0
            while folded > MAX_BPM:
                folded /= 2.0
            if folded < MIN_BPM or folded > MAX_BPM:
                continue
            center = float(np.exp(-0.5 * ((folded - 100.0) / 30.0) ** 2))
            adj = float(ac[lag]) * (1.0 + 0.16 * center)
            scored.append((adj, folded, float(ac[lag])))
    if not scored:
        peak_i = int(np.argmax(ac[min_lag : max_lag + 1]) + min_lag)
        bpm = 60.0 * sr / (max(peak_i, 1) * hop)
        while bpm < MIN_BPM:
            bpm *= 2.0
        while bpm > MAX_BPM:
            bpm /= 2.0
        return float(np.clip(bpm, MIN_BPM, MAX_BPM)), float(np.clip(ac[peak_i], 0, 1))
    env = onset_kick if onset_kick is not None and onset_kick.size == onset.size else onset
    rescored: list[tuple[float, float, float]] = []
    seen: set[int] = set()
    for adj, bpm, acv in scored[:12]:
        key = int(round(bpm))
        if key in seen:
            continue
        seen.add(key)
        fpb = (60.0 / bpm) * sr / hop
        ph = _beat_phase(env, fpb)
        hist = _fold(env, fpb * 4.0, 16, ph)
        q = float(hist[0] + hist[4] + hist[8] + hist[12])
        rescored.append((adj * (0.50 + 0.50 * q), bpm, acv))
    rescored.sort(reverse=True)
    return float(rescored[0][1]), float(np.clip(rescored[0][2], 0, 1))


def _fold(env: np.ndarray, frames_per_bar: float, n_bins: int, phase: float = 0.0) -> np.ndarray:
    hist = np.zeros(n_bins, dtype=np.float64)
    if env.size == 0 or frames_per_bar < 4:
        return hist
    for i, v in enumerate(env):
        pos = ((i - phase) / frames_per_bar) * n_bins
        pos %= n_bins
        b0 = int(np.floor(pos)) % n_bins
        b1 = (b0 + 1) % n_bins
        f = pos - np.floor(pos)
        hist[b0] += float(v) * (1.0 - f)
        hist[b1] += float(v) * f
    s = hist.sum()
    if s > 0:
        hist /= s
    return hist


def _beat_phase(onset: np.ndarray, frames_per_beat: float) -> float:
    """Phase (in frames) that best aligns an impulse train with the onset envelope."""
    if onset.size < 16 or frames_per_beat < 2:
        return 0.0
    n = onset.size
    t = np.arange(n, dtype=np.float64)
    best_p, best = 0.0, -1.0
    steps = max(8, int(frames_per_beat))
    for k in range(steps):
        phase = (k / steps) * frames_per_beat
        pulse = np.exp(-0.5 * (((t - phase + frames_per_beat / 2) % frames_per_beat - frames_per_beat / 2) / (0.12 * frames_per_beat)) ** 2)
        score = float(np.dot(onset, pulse))
        if score > best:
            best, best_p = score, phase
    return best_p


def _rotate_to_kick(hist_low: np.ndarray, beats: int) -> tuple[int, str]:
    """Rotate so the strongest low-band quarter-note is beat 1, if it is peaked."""
    n = hist_low.size
    step = n // beats
    if step < 1:
        return 0, "none"
    quarters = np.array(
        [hist_low[i * step] + 0.45 * hist_low[(i * step + 1) % n] for i in range(beats)]
    )
    if quarters.sum() < 1e-9:
        return 0, "flat"
    peak = float(quarters.max())
    mean = float(quarters.mean() + 1e-12)
    if peak / mean < 1.28:
        return 0, "grid"
    rot_beats = int(np.argmax(quarters))
    return rot_beats * step, "low-band"


def _rotate_bins(hist: np.ndarray, rot: int) -> np.ndarray:
    return np.roll(hist, -rot)


def _meter_from_beats(beat_series: np.ndarray) -> tuple[int, str, float]:
    """Choose 2, 3, or 4-beat grouping from a sequence of beat energies."""
    if beat_series.size < 8:
        return 4, "4/4", 0.2
    x = beat_series - np.mean(beat_series)
    if np.allclose(x, 0):
        return 4, "4/4", 0.2
    scores = {}
    for m in (2, 3, 4):
        ac = 0.0
        count = 0
        for lag in range(m, min(len(x) - 1, m * 6), m):
            a = x[:-lag]
            b = x[lag:]
            den = float(np.linalg.norm(a) * np.linalg.norm(b)) + 1e-9
            ac += float(np.dot(a, b) / den)
            count += 1
        scores[m] = ac / max(count, 1)
    # Prefer 4 slightly — most popular music — unless 3 is clearly stronger.
    scores[4] *= 1.20
    scores[2] *= 0.78
    best = max(scores, key=scores.get)
    if scores[3] > scores[4] * 1.12 and scores[3] >= scores[2]:
        best = 3
    label = {2: "2/4", 3: "3/4", 4: "4/4"}[best]
    return best, label, float(scores[best])


def extract_rhythm(y: np.ndarray, sr: int = ANALYSIS_SR) -> RhythmReport:
    y = np.asarray(y, dtype=np.float32)
    level = rms(y)
    empty = RhythmReport(
        tempo_bpm=0.0,
        tempo_clarity=0.0,
        meter="n/a",
        beats_per_bar=4,
        downbeat_source="silence",
        hist16=np.zeros(16),
        hist_low=np.zeros(16),
        hist_mid=np.zeros(16),
        hist_high=np.zeros(16),
        beat_energies=np.zeros(4),
        beat_low=np.zeros(4),
        beat_mid=np.zeros(4),
        rms=level,
    )
    if y.size < sr * 1.5 or level < 1e-4 or not np.isfinite(level):
        return empty

    fluxes = band_fluxes(y, sr, hop=HOP, n_fft=N_FFT)
    onset = fluxes["full"]
    onset_low = fluxes["low"]
    onset_mid = fluxes["mid"]
    onset_high = fluxes["high"]
    onset_kick = fluxes.get("kick", onset_low)
    n = min(onset.size, onset_low.size, onset_mid.size, onset_high.size, onset_kick.size)
    onset, onset_low, onset_mid, onset_high, onset_kick = (
        onset[:n],
        onset_low[:n],
        onset_mid[:n],
        onset_high[:n],
        onset_kick[:n],
    )

    bpm, clarity = estimate_tempo(onset, sr, HOP, onset_kick=onset_kick)
    if bpm <= 0:
        bpm, clarity = 100.0, 0.05

    frames_per_beat = (60.0 / bpm) * sr / HOP
    phase = _beat_phase(onset_kick, frames_per_beat)

    # Beat energy series for meter
    n_beats = int(n / frames_per_beat)
    beat_series = np.zeros(max(n_beats, 1))
    for i in range(n_beats):
        a = int(phase + i * frames_per_beat)
        b = int(phase + (i + 1) * frames_per_beat)
        beat_series[i] = float(onset[a:b].sum()) if b <= n and b > a else 0.0
    beats_per_bar, meter, meter_score = _meter_from_beats(beat_series)
    # Classify groove on a 4-beat bar unless 3/4 is clearly the meter.
    hist_beats = 3 if beats_per_bar == 3 else 4
    n_bins = hist_beats * 4
    frames_per_bar = frames_per_beat * hist_beats
    hist_full = _fold(onset, frames_per_bar, n_bins, phase)
    hist_low = _fold(onset_low, frames_per_bar, n_bins, phase)
    hist_mid = _fold(onset_mid, frames_per_bar, n_bins, phase)
    hist_high = _fold(onset_high, frames_per_bar, n_bins, phase)
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    clipped = peak > 0.90
    kick_mean = float(onset_kick.mean())
    mid_mean = float(onset_mid.mean())
    vocal_mix = mid_mean > 2.2 * max(kick_mean, 1e-9)
    if vocal_mix:
        hist = 0.18 * hist_full + 0.07 * hist_low + 0.72 * hist_mid + 0.03 * hist_high
    else:
        hist = 0.22 * hist_full + 0.18 * hist_low + 0.32 * hist_mid + 0.28 * hist_high
    if hist.sum() > 0:
        hist = hist / hist.sum()

    rot, source = _rotate_to_kick(_fold(onset_kick, frames_per_bar, n_bins, phase), hist_beats)
    hist = _rotate_bins(hist, rot)
    hist_low = _rotate_bins(hist_low, rot)
    hist_mid = _rotate_bins(hist_mid, rot)
    hist_high = _rotate_bins(hist_high, rot)

    # Canonical 16-bin view (pad/trim) for the classifier.
    hist16 = np.zeros(16)
    hlow, hmid, hhigh = np.zeros(16), np.zeros(16), np.zeros(16)
    take = min(16, n_bins)
    hist16[:take] = hist[:take]
    hlow[:take] = hist_low[:take]
    hmid[:take] = hist_mid[:take]
    hhigh[:take] = hist_high[:take]

    step = max(1, n_bins // hist_beats)
    beat_e = np.zeros(hist_beats)
    beat_l = np.zeros(hist_beats)
    beat_m = np.zeros(hist_beats)
    for i in range(hist_beats):
        sl = slice(i * step, i * step + 2)
        beat_e[i] = float(hist[sl].sum())
        beat_l[i] = float(hist_low[sl].sum())
        beat_m[i] = float(hist_mid[sl].sum())

    peak = float(np.max(onset) + 1e-9)
    med = float(np.median(onset) + 1e-9)
    transient = float(np.clip(peak / med / 12.0, 0.0, 1.0))

    return RhythmReport(
        tempo_bpm=float(bpm),
        tempo_clarity=float(clarity),
        meter=meter,
        beats_per_bar=hist_beats,
        downbeat_source=source,
        hist16=hist16,
        hist_low=hlow,
        hist_mid=hmid,
        hist_high=hhigh,
        beat_energies=beat_e,
        beat_low=beat_l,
        beat_mid=beat_m,
        rms=level,
        extras={
            "meter_score": meter_score,
            "n_bins": n_bins,
            "phase": phase,
            "transient": transient,
            "onset_mean": float(onset.mean()),
            "kick_mean": float(onset_kick.mean()),
            "mid_mean": float(onset_mid.mean()),
            "high_mean": float(onset_high.mean()),
            "low_mean": float(onset_low.mean()),
            "clipped": bool(clipped),
            "peak": peak,
        },
    )
