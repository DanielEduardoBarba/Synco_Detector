"""DSP helpers: envelopes, bands, onset strength, VAD."""

from __future__ import annotations

import numpy as np
from scipy.signal import medfilt

from synco_detector.config import ANALYSIS_SR, BANDS


def rms(y: np.ndarray) -> float:
    if y.size == 0:
        return 0.0
    x = np.nan_to_num(y.astype(np.float64), nan=0.0, posinf=0.0, neginf=0.0)
    return float(np.sqrt(np.mean(np.square(x))))


def peak_normalize(y: np.ndarray, peak: float = 0.95) -> np.ndarray:
    x = np.nan_to_num(y.astype(np.float32), nan=0.0, posinf=0.0, neginf=0.0)
    m = float(np.max(np.abs(x))) if x.size else 0.0
    if m < 1e-9:
        return x
    return (x * (peak / m)).astype(np.float32)


def frame_signal(y: np.ndarray, frame: int, hop: int) -> np.ndarray:
    y = np.ascontiguousarray(y, dtype=np.float32)
    if y.size < frame:
        pad = np.zeros(frame, dtype=np.float32)
        pad[: y.size] = y
        return pad.reshape(1, -1)
    n = 1 + (y.size - frame) // hop
    out = np.empty((n, frame), dtype=np.float32)
    for i in range(n):
        a = i * hop
        out[i] = y[a : a + frame]
    return out


def stft_mag(y: np.ndarray, hop: int = 256, n_fft: int = 1024) -> np.ndarray:
    window = np.hanning(n_fft).astype(np.float32)
    frames = frame_signal(y, n_fft, hop)
    return np.abs(np.fft.rfft(frames * window, n=n_fft, axis=1)).astype(np.float32)


def spectral_flux_from_mag(mag: np.ndarray) -> np.ndarray:
    if mag.size == 0:
        return np.zeros(0, dtype=np.float32)
    diff = np.diff(mag, axis=0, prepend=mag[:1])
    flux = np.maximum(diff, 0.0).mean(axis=1)
    if flux.size >= 3:
        flux = medfilt(flux, kernel_size=3)
    flux = flux - np.median(flux)
    flux = np.maximum(flux, 0.0)
    return np.nan_to_num(flux, nan=0.0).astype(np.float32)


def spectral_flux(y: np.ndarray, sr: int, hop: int = 256, n_fft: int = 1024) -> np.ndarray:
    mag = stft_mag(y, hop=hop, n_fft=n_fft)
    return spectral_flux_from_mag(mag)


def band_fluxes(
    y: np.ndarray, sr: int = ANALYSIS_SR, hop: int = 256, n_fft: int = 1024
) -> dict[str, np.ndarray]:
    mag = stft_mag(y, hop=hop, n_fft=n_fft)
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sr)
    out: dict[str, np.ndarray] = {"full": spectral_flux_from_mag(mag)}
    for band in BANDS:
        mask = (freqs >= band.low_hz) & (freqs < band.high_hz)
        if not np.any(mask):
            out[band.name] = np.zeros(mag.shape[0], dtype=np.float32)
            continue
        out[band.name] = spectral_flux_from_mag(mag[:, mask])
    return out


def rms_envelope(y: np.ndarray, hop: int = 256, frame: int = 1024) -> np.ndarray:
    frames = frame_signal(y.astype(np.float32), frame, hop)
    env = np.sqrt(np.mean(np.square(frames), axis=1) + 1e-12)
    if env.size >= 3:
        env = medfilt(env, kernel_size=3)
    return env.astype(np.float32)


def clip_fraction(y: np.ndarray, thresh: float = 0.97) -> float:
    if y.size == 0:
        return 0.0
    x = np.abs(np.nan_to_num(y.astype(np.float32), nan=0.0))
    return float(np.mean(x >= thresh))


def band_energy(y: np.ndarray, sr: int, lo: float, hi: float) -> float:
    if y.size < 32:
        return 0.0
    n = int(y.size)
    spec = np.abs(np.fft.rfft(y.astype(np.float32) * np.hanning(n)))
    freqs = np.fft.rfftfreq(n, d=1.0 / sr)
    mask = (freqs >= lo) & (freqs < hi)
    if not np.any(mask):
        return 0.0
    return float(np.mean(np.square(spec[mask])))


def isolate_vocals(y: np.ndarray, sr: int, lo: float = 140.0, hi: float = 4500.0) -> np.ndarray:
    """FFT band-limit to the singing range. Stable (no IIR)."""
    n = int(y.size)
    if n < 32:
        return y.astype(np.float32)
    spec = np.fft.rfft(y.astype(np.float32) * np.hanning(n))
    freqs = np.fft.rfftfreq(n, d=1.0 / sr)
    spec[(freqs < lo) | (freqs > hi)] = 0
    out = np.fft.irfft(spec, n=n).astype(np.float32)
    return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)


def freq_map(y: np.ndarray, sr: int, n_bars: int = 40) -> list[float]:
    """Log-spaced bass→treble magnitudes in 0..1 for a live audio map."""
    if n_bars < 4:
        n_bars = 4
    if y.size < 256:
        return [0.0] * n_bars
    n_fft = 2048
    if y.size >= n_fft:
        chunk = y[-n_fft:].astype(np.float32, copy=False)
    else:
        chunk = np.zeros(n_fft, dtype=np.float32)
        chunk[-y.size :] = y.astype(np.float32, copy=False)
    window = np.hanning(n_fft).astype(np.float32)
    mag = np.abs(np.fft.rfft(chunk * window))
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sr)
    f_lo = 40.0
    f_hi = min(sr * 0.48, 12000.0)
    edges = np.geomspace(f_lo, f_hi, n_bars + 1)
    out = np.zeros(n_bars, dtype=np.float64)
    for i in range(n_bars):
        mask = (freqs >= edges[i]) & (freqs < edges[i + 1])
        if np.any(mask):
            out[i] = float(np.sqrt(np.mean(np.square(mag[mask]))))
    peak = float(np.max(out)) + 1e-12
    out = out / peak
    # Mild compression so mid/high bands stay readable.
    out = np.power(np.clip(out, 0.0, 1.0), 0.55)
    return [float(x) for x in out]


def speech_gate(
    y: np.ndarray,
    sr: int,
    *,
    quiet_rms: float = 0.007,
    min_vocal_share: float = 0.06,
) -> dict:
    """Decide whether this buffer could contain words, vs bass/clip/noise."""
    level = rms(y)
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    clip = clip_fraction(y)
    vocal = band_energy(y, sr, 180.0, 3800.0)
    bass = band_energy(y, sr, 30.0, 160.0)
    hiss = band_energy(y, sr, 6000.0, 11000.0)
    tot = vocal + bass + hiss + 1e-12
    vocal_share = vocal / tot
    # Kids-church speakers often bury vocals under piano/bass — keep a soft floor.
    hearing = level >= quiet_rms * 1.4 and vocal_share >= min_vocal_share and clip < 0.28
    reason = "ok"
    if level < quiet_rms and peak < quiet_rms * 4:
        reason = "quiet"
    elif clip >= 0.28:
        reason = "clipped"
    elif vocal_share < min_vocal_share:
        reason = "no_voice"
    elif not hearing:
        reason = "weak_voice"
    return {
        "rms": level,
        "peak": peak,
        "clip": clip,
        "vocal_share": vocal_share,
        "hearing": hearing,
        "reason": reason,
    }


def energy_vad(y: np.ndarray, hop: int = 256, frame: int = 1024, thresh: float = 0.012) -> bool:
    env = rms_envelope(y, hop=hop, frame=frame)
    if env.size == 0:
        return False
    return float(np.percentile(env, 75)) > thresh


def autocorrelation(x: np.ndarray, max_lag: int) -> np.ndarray:
    x = np.nan_to_num(x.astype(np.float64), nan=0.0)
    x = x - np.mean(x)
    n = x.size
    if n < 8:
        return np.zeros(max_lag + 1, dtype=np.float32)
    spec = np.fft.rfft(x, n=1 << int(np.ceil(np.log2(2 * n))))
    ac = np.fft.irfft(spec * np.conj(spec))[:n]
    ac = ac[: max_lag + 1]
    if ac[0] > 1e-12:
        ac = ac / ac[0]
    return np.nan_to_num(ac, nan=0.0).astype(np.float32)
