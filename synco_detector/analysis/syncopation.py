"""Syncopation and emphasis metrics from a rhythm report."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from synco_detector.analysis.rhythm import WEIGHTS_2_8, WEIGHTS_3_12, WEIGHTS_4_16, RhythmReport


@dataclass
class GrooveMetrics:
    downbeat_share: float
    odd_beat_share: float  # beats 1 and 3 in 4/4; beat 1 in 3/4
    backbeat_share: float  # beats 2 and 4
    beat2_share: float
    beat3_share: float
    beat4_share: float
    offbeat_share: float
    sixteenth_off_share: float
    snare_backbeat: float
    kick_syncopation: float
    lhl_syncopation: float
    accent_entropy: float
    onbeat_concentration: float
    hat_density: float
    half_time_snare: float
    high_offbeat: float
    onbeat_tolerant: float
    kick_present: bool
    vocal_dominant: bool
    percussive: bool
    hymn_pulse: bool
    primary_emphasis: str
    secondary_emphasis: str


def _weights(beats: int) -> np.ndarray:
    if beats == 3:
        return WEIGHTS_3_12
    if beats == 2:
        return WEIGHTS_2_8
    return WEIGHTS_4_16


def _lhl(hist: np.ndarray, beats: int) -> float:
    n = hist.size if beats != 4 else 16
    w = _weights(beats)
    n = min(n, w.size, hist.size)
    h = hist[:n]
    w = w[:n]
    if h.sum() < 1e-12:
        return 0.0
    score = 0.0
    for i in range(n):
        if h[i] < 0.04:
            continue
        for j in range(1, n):
            k = (i + j) % n
            if w[k] > w[i]:
                if h[k] < h[i] * 0.55:
                    score += (w[k] - w[i]) * h[i]
                break
    return float(np.clip(score / (h.sum() * 4.0), 0.0, 1.0))


def _entropy(p: np.ndarray) -> float:
    p = p[p > 1e-12]
    if p.size == 0:
        return 0.0
    p = p / p.sum()
    h = -np.sum(p * np.log2(p))
    return float(h / np.log2(max(p.size, 2)))


def is_hymn_pulse(
    onbeat_tolerant: float,
    snare_backbeat: float,
    kick_syncopation: float,
    *,
    offbeat_share: float = 0.0,
    onbeat_strict: float = 0.0,
    tempo_clarity: float = 1.0,
    onset_mean: float = 1.0,
    half_time_snare: float = 0.0,
    hat_density: float = 0.0,
    kick_present: bool = False,
) -> bool:
    """On-beat hymn / kids-church pulse (live Jesus Loves Me signature).

    Room-mic bass often looks like 'kick syncopation' even when the groove is
    squarely on the beat — so a high tolerant on-beat + no snare is enough.
    Reject trap/rap signatures (busy hats, half-time snare, displaced kicks).
    """
    if snare_backbeat >= 0.22:
        return False
    if tempo_clarity < 0.16 or onset_mean < 0.0025:
        return False
    # Trap/rap: half-time + hats only counts when the kick is also displaced
    # (hymn melodies often put energy on beat 3 without being trap).
    if half_time_snare >= 0.22 and hat_density >= 0.62 and kick_syncopation >= 0.30:
        return False
    if kick_present and kick_syncopation >= 0.38 and hat_density >= 0.55:
        return False
    if kick_syncopation >= 0.50 and offbeat_share >= 0.22:
        return False
    if onbeat_tolerant >= 0.62 and kick_syncopation < 0.38:
        return True
    if (
        onbeat_tolerant >= 0.70
        and snare_backbeat < 0.14
        and offbeat_share < 0.28
        and onbeat_strict >= 0.20
        and tempo_clarity >= 0.12
        and not (kick_present and kick_syncopation >= 0.42)
    ):
        return True
    return False


def compute_metrics(r: RhythmReport) -> GrooveMetrics:
    beats = r.beats_per_bar
    n_bins = beats * 4
    hist = r.hist16[:n_bins] if n_bins <= 16 else r.hist16
    if hist.sum() <= 0:
        hist = np.ones(n_bins) / n_bins
    else:
        hist = hist / hist.sum()

    step = 4

    def beat_share(i: int, h: np.ndarray) -> float:
        if i >= beats:
            return 0.0
        a = i * step
        return float(h[a : a + 2].sum()) if a < h.size else 0.0

    b1 = beat_share(0, hist)
    b2 = beat_share(1, hist)
    b3 = beat_share(2, hist)
    b4 = beat_share(3, hist)
    onbeat_strict = float(hist[0::4].sum()) if hist.size else 0.0
    onbeat_tol = 0.0
    if hist.size:
        for q in range(0, n_bins, 4):
            for d in (-1, 0, 1):
                onbeat_tol += float(hist[(q + d) % n_bins])
        onbeat_tol = float(np.clip(onbeat_tol, 0.0, 1.0))
    off_eighth = float(hist[2::4].sum()) if hist.size else 0.0
    off_16 = float(np.clip(1.0 - onbeat_tol, 0.0, 1.0))

    mid = r.hist_mid[:n_bins]
    low = r.hist_low[:n_bins]
    if mid.sum() > 0:
        mid = mid / mid.sum()
    if low.sum() > 0:
        low = low / low.sum()

    snare_back = 0.0
    snare_odd = 0.0
    if beats >= 4 and mid.size >= 14:
        snare_back = float(mid[4:6].sum() + mid[12:14].sum())
        snare_odd = float(mid[0:2].sum() + mid[8:10].sum())
    elif beats == 2 and mid.size >= 6:
        snare_back = float(mid[4:6].sum())
        snare_odd = float(mid[0:2].sum())
    snare_back = float(np.clip(snare_back - snare_odd, 0.0, 1.0))

    mid_mean = float(r.extras.get("mid_mean", 0.0))
    kick_mean = float(r.extras.get("kick_mean", 0.0))
    high_mean = float(r.extras.get("high_mean", 0.0))

    kick_sync = 0.0
    kick_present = False
    if low.size:
        quarters = np.array(
            [
                float(low[i * 4] + (low[i * 4 + 1] if i * 4 + 1 < low.size else 0))
                for i in range(max(beats, 1))
            ]
        )
        kick_on = float(low[0::4].sum())
        peak = float(quarters.max()) if quarters.size else 0.0
        mean_q = float(quarters.mean() + 1e-12) if quarters.size else 1.0
        kick_present = bool(
            kick_mean > 0.012
            and peak / mean_q >= 1.32
            and kick_mean >= 0.45 * max(mid_mean, 1e-9)
        )
        if kick_present:
            kick_sync = float(np.clip(1.0 - kick_on, 0.0, 1.0))

    high = r.hist_high[:n_bins]
    if high.sum() > 0:
        high = high / high.sum()
        hat_density = float(np.clip(_entropy(high), 0.0, 1.0))
    else:
        hat_density = 0.0
    half_time = float(mid[8:10].sum()) if beats >= 4 and mid.size >= 10 else 0.0
    high_off = float(high[2::4].sum()) if high.size else 0.0

    vocal_dominant = bool(mid_mean > 1.6 * max(kick_mean, 1e-9) and snare_back < 0.16)
    hat_perc = (
        high_mean > 0.025
        and hat_density > 0.78
        and off_eighth > 0.26
        and onbeat_strict < 0.34
        and onbeat_tol < 0.62
        and snare_back > 0.06
    )
    kick_groove = (
        kick_present
        and kick_sync > 0.42
        and (off_eighth > 0.28 or hat_density > 0.88)
        and not (
            onbeat_tol >= 0.74
            and snare_back < 0.10
            and off_eighth < 0.27
            and onbeat_strict >= 0.22
        )
    )
    percussive = bool(
        snare_back > 0.18
        or (kick_present and kick_sync > 0.32 and snare_back > 0.10)
        or kick_groove
        or hat_perc
        or (off_eighth > 0.40 and onbeat_strict < 0.26 and onbeat_tol < 0.55)
        # Sparse dark-rap kits: kick displacement + hats without classic snare 2/4.
        or (kick_present and kick_sync > 0.40 and hat_density > 0.70 and half_time > 0.12)
        or (half_time > 0.22 and hat_density > 0.72 and onbeat_strict < 0.40)
    )
    hymn_pulse = is_hymn_pulse(
        onbeat_tol,
        snare_back,
        kick_sync,
        offbeat_share=off_eighth,
        onbeat_strict=onbeat_strict,
        tempo_clarity=float(getattr(r, "tempo_clarity", 1.0) or 1.0),
        onset_mean=float(r.extras.get("onset_mean", 1.0) or 1.0),
        half_time_snare=half_time,
        hat_density=hat_density,
        kick_present=kick_present,
    )
    if (
        onbeat_strict > 0.48
        and snare_back < 0.16
        and half_time < 0.20
        and kick_sync < 0.35
        and hat_density < 0.80
    ):
        percussive = False
    if hymn_pulse:
        percussive = False
        kick_sync = min(kick_sync, 0.20)

    odd = b1 + (b3 if beats >= 3 else 0.0)
    back = b2 + (b4 if beats >= 4 else 0.0)

    shares = {"1": b1, "2": b2, "3": b3, "4": b4, "offbeat": off_eighth}
    if beats == 3:
        shares["4"] = 0.0
    if beats == 2:
        shares["3"] = 0.0
        shares["4"] = 0.0
    ranked = sorted(shares.items(), key=lambda kv: kv[1], reverse=True)
    primary = ranked[0][0]
    secondary = ranked[1][0] if len(ranked) > 1 else "none"
    if beats >= 4 and back > odd * 1.05 and back > 0.28 and percussive:
        primary = "2+4"
        secondary = "1" if b1 >= b3 else "3"
    if vocal_dominant and not percussive and onbeat_tol > 0.42:
        primary = "1" if b1 >= max(b2, b3, b4) else primary
    if hymn_pulse:
        primary = "1"

    return GrooveMetrics(
        downbeat_share=b1,
        odd_beat_share=odd,
        backbeat_share=back,
        beat2_share=b2,
        beat3_share=b3,
        beat4_share=b4,
        offbeat_share=off_eighth,
        sixteenth_off_share=off_16,
        snare_backbeat=snare_back,
        kick_syncopation=kick_sync,
        lhl_syncopation=_lhl(r.hist16, beats),
        accent_entropy=_entropy(hist),
        onbeat_concentration=onbeat_strict,
        hat_density=hat_density,
        half_time_snare=half_time,
        high_offbeat=high_off,
        onbeat_tolerant=onbeat_tol,
        kick_present=kick_present,
        vocal_dominant=vocal_dominant,
        percussive=percussive,
        hymn_pulse=hymn_pulse,
        primary_emphasis=primary,
        secondary_emphasis=secondary,
    )
