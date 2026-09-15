"""Map rhythm metrics onto a simple (1-emphasis) ↔ syncopated (2/4 / off-beat) spectrum."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np

from synco_detector.analysis.rhythm import RhythmReport, extract_rhythm
from synco_detector.analysis.syncopation import GrooveMetrics, compute_metrics
from synco_detector.audio.dsp import rms
from synco_detector.config import ANALYSIS_SR, SILENCE_RMS

SIMPLE_POLE = "Simple 1-emphasis"
SYNCOPATED_POLE = "Syncopated 2/4 emphasis"


@dataclass
class Verdict:
    side: str  # "simple" | "syncopated" | "mixed" | "silence"
    spectrum: float  # 0 = hymn-like 1-emphasis, 100 = syncopated / backbeat
    confidence: float  # 0-100
    judgment: str
    summary: str
    primary_emphasis: str
    secondary_emphasis: str
    meter: str
    tempo_bpm: float
    spectrum_avg: float = 50.0  # dwell-weighted song average (breadth)
    features: dict = field(default_factory=dict)
    beat_energies: list[float] = field(default_factory=list)
    hist16: list[float] = field(default_factory=list)
    hist_low: list[float] = field(default_factory=list)
    hist_mid: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _band(spectrum: float) -> str:
    if spectrum < 22:
        return f"{SIMPLE_POLE} (hymn / kids-church pulse, downbeat 1)"
    if spectrum < 38:
        return f"Mostly {SIMPLE_POLE.lower()} — light displacement only"
    if spectrum < 62:
        return "Mixed / transitional groove (neither clearly 1 nor 2/4)"
    if spectrum < 78:
        return f"Leaning {SYNCOPATED_POLE.lower()} (backbeat or off-beat)"
    return f"{SYNCOPATED_POLE} (hip-hop / rap / funk-style displacement)"


def _emphasis_sentence(m: GrooveMetrics, meter: str) -> str:
    p = m.primary_emphasis
    if p == "1":
        return f"Primary weight sits on beat 1 of {meter} (downbeat / 'the 1')."
    if p == "2+4":
        return f"Primary weight sits on the backbeat (beats 2 and 4) of {meter}."
    if p == "2":
        return f"Primary weight sits on beat 2 of {meter} (backbeat side)."
    if p == "3":
        return f"Primary weight sits on beat 3 of {meter}."
    if p == "4":
        return f"Primary weight sits on beat 4 of {meter} (backbeat side)."
    if p == "offbeat":
        return f"Primary weight sits off the beat (the 'and') in {meter}."
    return f"Emphasis is distributed across {meter}."


def classify_metrics(r: RhythmReport, m: GrooveMetrics) -> Verdict:
    if r.rms < SILENCE_RMS or r.tempo_bpm <= 0:
        return Verdict(
            side="silence",
            spectrum=50.0,
            confidence=0.0,
            judgment="No usable pulse",
            summary="Signal is too quiet or too unstructured to judge emphasis.",
            primary_emphasis="n/a",
            secondary_emphasis="n/a",
            meter=r.meter,
            tempo_bpm=r.tempo_bpm,
            features={"rms": r.rms},
        )

    # Evidence in [0, 1] pointing toward the SYNCOPATED pole.
    half_time_trap = 0.0
    if r.beats_per_bar >= 4:
        # Half-time trap: snare on 3 + displaced kicks + busy hats.
        half_time_trap = float(
            np.clip(
                0.5 * m.half_time_snare
                + 0.35 * m.kick_syncopation
                + 0.15 * m.hat_density
                - 0.25 * m.downbeat_share,
                0.0,
                1.0,
            )
        )
        if m.kick_syncopation > 0.35 and m.half_time_snare > 0.18 and m.snare_backbeat < 0.35:
            half_time_trap = max(half_time_trap, 0.72)
        if m.half_time_snare > 0.22 and m.hat_density > 0.7 and m.kick_syncopation > 0.3:
            half_time_trap = max(half_time_trap, 0.78)

    sync_components = {
        "backbeat_share": m.backbeat_share,
        "snare_backbeat": m.snare_backbeat,
        "offbeat_share": m.offbeat_share,
        "sixteenth_off_share": m.sixteenth_off_share * 0.85,
        "lhl_syncopation": m.lhl_syncopation,
        "kick_syncopation": m.kick_syncopation,
        "half_time_trap": half_time_trap,
        "high_offbeat": m.high_offbeat,
        "entropy": m.accent_entropy * 0.55,
    }
    simple_components = {
        "downbeat_share": m.downbeat_share,
        "odd_beat_share": m.odd_beat_share,
        "onbeat_concentration": m.onbeat_concentration,
        "not_backbeat": float(np.clip(1.0 - m.backbeat_share, 0, 1)),
        "not_offbeat": float(np.clip(1.0 - m.offbeat_share, 0, 1)),
        "not_lhl": float(np.clip(1.0 - m.lhl_syncopation, 0, 1)),
        "not_snare_back": float(np.clip(1.0 - m.snare_backbeat, 0, 1)),
    }

    # Weights tuned so hymn-like fixtures stay low and backbeat/hip-hop stay high.
    w_sync = {
        "backbeat_share": 1.35,
        "snare_backbeat": 1.45,
        "offbeat_share": 1.20,
        "sixteenth_off_share": 0.55,
        "lhl_syncopation": 1.10,
        "kick_syncopation": 0.90,
        "half_time_trap": 1.15,
        "high_offbeat": 1.25,
        "entropy": 0.25,
    }
    w_simple = {
        "downbeat_share": 1.40,
        "odd_beat_share": 1.10,
        "onbeat_concentration": 1.15,
        "not_backbeat": 0.80,
        "not_offbeat": 0.70,
        "not_lhl": 0.55,
        "not_snare_back": 0.85,
    }

    # Percussion-only cues must not fire on a cappella / piano-vocal hymns.
    if not m.percussive and m.offbeat_share < 0.28:
        sync_components["kick_syncopation"] = 0.0
        sync_components["high_offbeat"] *= 0.15
        sync_components["snare_backbeat"] = 0.0
        sync_components["half_time_trap"] *= 0.15
        simple_components["onbeat_concentration"] = max(
            m.onbeat_concentration, m.onbeat_tolerant
        )

    sync_score = sum(sync_components[k] * w_sync[k] for k in w_sync)
    sync_score /= sum(w_sync.values())
    simple_score = sum(simple_components[k] * w_simple[k] for k in w_simple)
    simple_score /= sum(w_simple.values())

    # Beat-3-only hymns should remain simple; beat-3 snare with syncopated kick should not.
    if m.primary_emphasis == "3" and m.kick_syncopation < 0.28 and m.snare_backbeat < 0.22 and m.offbeat_share < 0.22:
        simple_score = max(simple_score, 0.62)
        sync_score *= 0.75

    # Trap/rap groove even without a classic 2/4 snare (half-time / sparse kits).
    trap_pulse = bool(
        not m.hymn_pulse
        and m.onbeat_tolerant < 0.88
        and (
            (m.half_time_snare > 0.18 and m.hat_density > 0.62 and m.kick_syncopation > 0.28)
            or (m.kick_syncopation > 0.38 and m.hat_density > 0.70)
            or (m.kick_syncopation > 0.48 and m.offbeat_share > 0.24)
            or (m.sixteenth_off_share > 0.38 and m.kick_present and m.hat_density > 0.55)
            or (
                m.kick_present
                and m.kick_syncopation > 0.40
                and m.hat_density > 0.82
                and m.snare_backbeat < 0.20
                and m.offbeat_share > 0.18
            )
        )
    )
    if trap_pulse:
        sync_score = max(sync_score, 0.76)
        simple_score *= 0.72
        # Treat as percussive for downstream clamps even if snare is sparse.
        if not m.percussive:
            sync_score = max(sync_score, 0.80)

    if m.percussive and m.primary_emphasis in {"2", "4", "2+4", "offbeat"}:
        sync_score = max(sync_score, 0.68)
    if m.percussive and m.primary_emphasis == "offbeat":
        sync_score = max(sync_score, 0.74)
        simple_score *= 0.78

    if m.percussive and m.accent_entropy > 0.78 and m.kick_syncopation > 0.40:
        sync_score = max(sync_score, 0.72)
    if m.percussive and m.sixteenth_off_share > 0.42 and m.kick_syncopation > 0.40:
        sync_score = max(sync_score, 0.70)

    if m.percussive and m.high_offbeat > 0.35 and m.snare_backbeat > 0.15:
        sync_score = max(sync_score, 0.76)
    if m.percussive and m.snare_backbeat > 0.38:
        sync_score = max(sync_score, 0.80)
    if m.percussive and m.snare_backbeat > 0.28 and m.primary_emphasis in {"2", "4", "2+4"}:
        sync_score = max(sync_score, 0.78)
        simple_score *= 0.82

    if r.beats_per_bar == 3 and m.downbeat_share > 0.28 and m.offbeat_share < 0.22:
        simple_score = max(simple_score, 0.68)
        sync_score *= 0.72

    # Vocal-led simple hymns only — do not pull trap/rap toward the simple pole.
    vocal_simple = (m.vocal_dominant or not m.percussive) and not trap_pulse and not m.kick_present
    if m.hymn_pulse:
        simple_score = max(simple_score, 0.90)
        sync_score *= 0.18
    elif (
        vocal_simple
        and m.onbeat_tolerant > 0.68
        and m.offbeat_share < 0.26
        and m.snare_backbeat < 0.14
        and m.half_time_snare < 0.20
    ):
        simple_score = max(simple_score, 0.80)
        sync_score *= 0.40
    elif (
        vocal_simple
        and m.onbeat_concentration > 0.30
        and m.offbeat_share < 0.24
        and m.snare_backbeat < 0.14
        and m.accent_entropy < 0.82
        and m.hat_density < 0.85
    ):
        simple_score = max(simple_score, 0.74)
        sync_score *= 0.55

    denom = simple_score + sync_score + 1e-9
    raw_ratio = float(sync_score / denom)
    # Contrast curve: expand away from 50 so live songs reach the poles.
    centered = (raw_ratio - 0.5) * 2.0  # [-1, 1]
    expanded = float(np.sign(centered) * (abs(centered) ** 0.62))
    spectrum = 50.0 + 50.0 * expanded
    spectrum = float(np.nan_to_num(spectrum, nan=50.0))
    spectrum = float(np.clip(spectrum, 0.0, 100.0))

    if m.hymn_pulse:
        spectrum = min(spectrum, 8.0)
    elif (
        vocal_simple
        and m.onbeat_tolerant > 0.68
        and m.snare_backbeat < 0.14
        and m.offbeat_share < 0.26
        and m.half_time_snare < 0.20
    ):
        spectrum = min(spectrum, 18.0)
    elif (
        vocal_simple
        and m.onbeat_concentration > 0.30
        and m.offbeat_share < 0.24
        and m.snare_backbeat < 0.14
        and m.accent_entropy < 0.82
        and m.hat_density < 0.85
    ):
        spectrum = min(spectrum, 28.0)

    if trap_pulse:
        spectrum = max(spectrum, 78.0)
    if m.percussive and m.snare_backbeat > 0.35:
        spectrum = max(spectrum, 82.0)
    elif m.percussive and m.primary_emphasis in {"2", "4", "2+4"} and m.snare_backbeat > 0.22:
        spectrum = max(spectrum, 78.0)
    elif m.percussive and m.primary_emphasis == "offbeat" and m.offbeat_share > 0.32:
        spectrum = max(spectrum, 80.0)
    elif m.percussive and (m.kick_syncopation > 0.45 or half_time_trap > 0.55):
        spectrum = max(spectrum, 74.0)

    spectrum = float(np.clip(spectrum, 0.0, 100.0))

    if spectrum < 40:
        side = "simple"
    elif spectrum > 60:
        side = "syncopated"
    else:
        side = "mixed"

    agreement = float(abs(sync_score - simple_score))
    peakiness = float(1.0 - m.accent_entropy)
    conf = 100.0 * float(
        np.clip(
            0.28 * r.tempo_clarity
            + 0.32 * agreement
            + 0.22 * peakiness
            + 0.18 * min(r.rms / 0.08, 1.0),
            0.0,
            1.0,
        )
    )
    if r.tempo_clarity < 0.12:
        conf *= 0.55
    if r.rms < SILENCE_RMS * 3:
        conf *= 0.5
    transient = float(r.extras.get("transient", 1.0))
    onset_mean = float(r.extras.get("onset_mean", 1.0))
    if onset_mean < 0.003 and not m.hymn_pulse:
        conf = min(conf, 18.0)
        side = "mixed"
        spectrum = 50.0
    elif transient < 0.18 and onset_mean < 0.006 and not m.hymn_pulse:
        conf *= 0.7
    # Flat/noisy non-hymn signals: don't invent a strong call.
    # Live Jesus Loves Me has high entropy + smeared onbeat_strict — do NOT yank it to 40.
    if m.accent_entropy > 0.82 and not m.percussive and not m.hymn_pulse:
        conf = min(conf, 28.0)
        if m.onbeat_concentration < 0.34 and m.onbeat_tolerant < 0.62:
            side = "mixed"
            spectrum = 50.0 + 0.2 * (spectrum - 50.0)
    if m.hymn_pulse:
        spectrum = min(spectrum, 8.0)
        conf = max(conf, 35.0)
    conf = float(np.clip(np.nan_to_num(conf, nan=0.0), 3.0, 99.0))
    if spectrum < 35:
        side = "simple"
    elif spectrum > 65:
        side = "syncopated"
    else:
        side = "mixed"

    judgment = _band(spectrum)
    summary = (
        f"{judgment}. {_emphasis_sentence(m, r.meter)} "
        f"Confidence {conf:.0f}%. Tempo ~{r.tempo_bpm:.0f} BPM."
    )

    features = {
        "rms": r.rms,
        "tempo_clarity": r.tempo_clarity,
        "downbeat_source": r.downbeat_source,
        "simple_score": simple_score,
        "sync_score": sync_score,
        "half_time_trap": half_time_trap,
        "trap_pulse": trap_pulse,
        "onset_mean": float(r.extras.get("onset_mean", 0.0) or 0.0),
        "transient": float(r.extras.get("transient", 0.0) or 0.0),
        **{f"m_{k}": v for k, v in asdict(m).items()},
        **{f"sync_{k}": v for k, v in sync_components.items()},
        **{f"simple_{k}": v for k, v in simple_components.items()},
    }

    be = [float(x) for x in r.beat_energies.tolist()]
    return Verdict(
        side=side,
        spectrum=round(spectrum, 2),
        confidence=round(conf, 1),
        judgment=judgment,
        summary=summary,
        primary_emphasis=m.primary_emphasis,
        secondary_emphasis=m.secondary_emphasis,
        meter=r.meter,
        tempo_bpm=round(float(r.tempo_bpm), 1),
        spectrum_avg=round(spectrum, 2),
        features=features,
        beat_energies=be,
        hist16=[float(x) for x in r.hist16.tolist()],
        hist_low=[float(x) for x in r.hist_low.tolist()],
        hist_mid=[float(x) for x in r.hist_mid.tolist()],
    )


def analyze_audio(y: np.ndarray, sr: int = ANALYSIS_SR) -> Verdict:
    y = np.asarray(y, dtype=np.float32)
    if sr != ANALYSIS_SR:
        # Linear resample without requiring librosa at import-time for tests.
        n = int(round(y.size * ANALYSIS_SR / sr))
        if n > 1 and y.size > 1:
            t_old = np.linspace(0.0, 1.0, y.size, endpoint=False)
            t_new = np.linspace(0.0, 1.0, n, endpoint=False)
            y = np.interp(t_new, t_old, y).astype(np.float32)
        sr = ANALYSIS_SR
    if rms(y) < SILENCE_RMS * 0.5:
        r = extract_rhythm(y, sr)
        m = compute_metrics(r)
        v = classify_metrics(r, m)
        v.side = "silence"
        v.confidence = 0.0
        v.judgment = "No usable pulse"
        return v
    r = extract_rhythm(y, sr)
    m = compute_metrics(r)
    return classify_metrics(r, m)
