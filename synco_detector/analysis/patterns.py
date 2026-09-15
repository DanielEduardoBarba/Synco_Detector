"""Deterministic synthetic grooves for tests, demos, and self-calibration."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class GrooveSpec:
    name: str
    bpm: float
    beats_per_bar: int
    bars: int
    # sixteenth-note positions within a bar
    kicks: tuple[int, ...]
    snares: tuple[int, ...]
    hats: tuple[int, ...]
    melody: tuple[int, ...]
    expected_side: str  # "simple" | "syncopated"
    expected_emphasis: str  # "1" | "2" | "3" | "4" | "2+4" | "offbeat"
    notes: str


def _tone(freq: float, n: int, sr: int, amp: float, decay: float) -> np.ndarray:
    t = np.arange(n) / sr
    env = np.exp(-t * decay)
    return (amp * env * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _noise(n: int, sr: int, amp: float, decay: float, hp: bool = True) -> np.ndarray:
    rng = np.random.default_rng(7)
    x = rng.standard_normal(n).astype(np.float32)
    t = np.arange(n) / sr
    env = np.exp(-t * decay)
    if hp and n > 8:
        # crude first-difference highpass for snare/hat brightness
        x = np.diff(x, prepend=x[:1])
    return (amp * env * x).astype(np.float32)


def _place(buf: np.ndarray, start: int, sample: np.ndarray) -> None:
    end = min(buf.size, start + sample.size)
    if start >= buf.size or start < 0:
        return
    sl = slice(start, end)
    buf[sl] += sample[: end - start]


def render_groove(spec: GrooveSpec, sr: int = 22050) -> np.ndarray:
    beat = 60.0 / spec.bpm
    step = beat / 4.0  # 16th
    bar_s = spec.beats_per_bar * beat
    total_s = spec.bars * bar_s + 0.25
    n = int(total_s * sr)
    y = np.zeros(n, dtype=np.float32)
    steps_per_bar = spec.beats_per_bar * 4

    kick_n = int(0.18 * sr)
    snare_n = int(0.14 * sr)
    hat_n = int(0.06 * sr)
    mel_n = int(0.22 * sr)

    kick = _tone(55.0, kick_n, sr, 1.0, 18.0) + 0.45 * _tone(90.0, kick_n, sr, 1.0, 22.0)
    snare = 0.35 * _tone(180.0, snare_n, sr, 1.0, 20.0) + _noise(snare_n, sr, 0.55, 28.0)
    hat = _noise(hat_n, sr, 0.22, 70.0)
    melody_freqs = [261.63, 293.66, 329.63, 392.00, 329.63, 293.66, 261.63, 246.94]

    for bar in range(spec.bars):
        bar_t = bar * bar_s
        for pos in spec.kicks:
            if pos >= steps_per_bar:
                continue
            _place(y, int((bar_t + pos * step) * sr), kick)
        for pos in spec.snares:
            if pos >= steps_per_bar:
                continue
            _place(y, int((bar_t + pos * step) * sr), snare * 1.05)
        for pos in spec.hats:
            if pos >= steps_per_bar:
                continue
            _place(y, int((bar_t + pos * step) * sr), hat)
        for i, pos in enumerate(spec.melody):
            if pos >= steps_per_bar:
                continue
            freq = melody_freqs[i % len(melody_freqs)]
            accent = 1.35 if pos == 0 else 0.85
            _place(
                y,
                int((bar_t + pos * step) * sr),
                _tone(freq, mel_n, sr, 0.42 * accent, 9.0),
            )
        # Quiet on-beat grid so emphasis is relative to a known pulse (not rotated away).
        grid = _tone(55.0, int(0.09 * sr), sr, 0.22, 20.0)
        for b in range(spec.beats_per_bar):
            _place(y, int((bar_t + b * beat) * sr), grid)
    peak = float(np.max(np.abs(y))) or 1.0
    return (0.92 * y / peak).astype(np.float32)


def jesus_loves_me_like(sr: int = 22050, bars: int = 8) -> np.ndarray:
    """On-beat hymn-like melody: strong 1, even quarters, no backbeat."""
    beat = 60.0 / 92.0
    note_n = int(0.42 * sr)
    # Two-bar phrase in 16ths approximating "Jesus loves me this I know".
    phrase = [
        (0, 261.63, 1.15),
        (4, 261.63, 0.8),
        (8, 293.66, 0.85),
        (12, 329.63, 0.9),
        (16, 261.63, 1.1),
        (20, 329.63, 0.85),
        (24, 293.66, 0.85),
        (32, 261.63, 1.15),
        (36, 261.63, 0.8),
        (40, 293.66, 0.85),
        (44, 329.63, 0.9),
        (48, 392.00, 1.1),
        (52, 329.63, 0.85),
        (56, 293.66, 0.8),
        (60, 261.63, 0.95),
    ]
    sixteenth = beat / 4.0
    two_bar = 8 * beat
    n_phrases = max(1, int(bars // 2))
    total = n_phrases * two_bar + 0.3
    y = np.zeros(int(total * sr), dtype=np.float32)
    soft_pulse = _tone(65.0, int(0.12 * sr), sr, 0.28, 16.0)
    for p in range(n_phrases):
        base = p * two_bar
        for pos, freq, amp in phrase:
            start = int((base + pos * sixteenth) * sr)
            _place(y, start, _tone(freq, note_n, sr, 0.55 * amp, 7.5))
        # gentle downbeat pulse only (1 and, weakly, 3) — hymn accompaniment
        for bar in range(2):
            _place(y, int((base + bar * 4 * beat) * sr), soft_pulse)
            _place(y, int((base + bar * 4 * beat + 2 * beat) * sr), soft_pulse * 0.45)
    peak = float(np.max(np.abs(y))) or 1.0
    return (0.92 * y / peak).astype(np.float32)


GROOVES: tuple[GrooveSpec, ...] = (
    GrooveSpec(
        name="hymn_1_emphasis",
        bpm=96,
        beats_per_bar=4,
        bars=8,
        kicks=(0,),
        snares=(),
        hats=(),
        melody=(0, 4, 8, 12),
        expected_side="simple",
        expected_emphasis="1",
        notes="Kids-church / hymn pulse: melody on quarters, accent on 1, no backbeat.",
    ),
    GrooveSpec(
        name="hymn_1_and_3",
        bpm=88,
        beats_per_bar=4,
        bars=8,
        kicks=(0, 8),
        snares=(),
        hats=(),
        melody=(0, 4, 8, 12),
        expected_side="simple",
        expected_emphasis="1",
        notes="Marching hymn: pulses on 1 and 3 only.",
    ),
    GrooveSpec(
        name="march_2_4_meter",
        bpm=110,
        beats_per_bar=2,
        bars=12,
        kicks=(0,),
        snares=(),
        hats=(0, 4),
        melody=(0, 4),
        expected_side="simple",
        expected_emphasis="1",
        notes="Simple 2/4 march, downbeat emphasis.",
    ),
    GrooveSpec(
        name="waltz_3_4_downbeat",
        bpm=138,
        beats_per_bar=3,
        bars=12,
        kicks=(0,),
        snares=(),
        hats=(4, 8),
        melody=(0, 4, 8),
        expected_side="simple",
        expected_emphasis="1",
        notes="Waltz 3/4 with oom-pah-pah still anchored on 1.",
    ),
    GrooveSpec(
        name="rock_backbeat",
        bpm=118,
        beats_per_bar=4,
        bars=8,
        kicks=(0, 8),
        snares=(4, 12),
        hats=(0, 2, 4, 6, 8, 10, 12, 14),
        melody=(),
        expected_side="syncopated",
        expected_emphasis="2+4",
        notes="Classic rock backbeat: snare on 2 and 4.",
    ),
    GrooveSpec(
        name="hiphop_boom_bap",
        bpm=92,
        beats_per_bar=4,
        bars=8,
        kicks=(0, 10),
        snares=(4, 12),
        hats=(0, 2, 4, 6, 8, 10, 12, 14),
        melody=(),
        expected_side="syncopated",
        expected_emphasis="2+4",
        notes="Boom-bap: snare 2/4 plus kick on the 'and' of 2 (syncopated).",
    ),
    GrooveSpec(
        name="hiphop_trap",
        bpm=80,
        beats_per_bar=4,
        bars=8,
        kicks=(0, 3, 6, 10),
        snares=(8,),
        hats=tuple(range(16)),
        melody=(),
        expected_side="syncopated",
        expected_emphasis="3",
        notes="Trap-like: rapid hats, displaced kicks, snare on 3.",
    ),
    GrooveSpec(
        name="funk_16th_syncopation",
        bpm=104,
        beats_per_bar=4,
        bars=8,
        kicks=(0, 3, 6, 10, 11),
        snares=(4, 12, 13),
        hats=(0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15),
        melody=(),
        expected_side="syncopated",
        expected_emphasis="2+4",
        notes="Funk: 16th-grid displacements and ghost snares.",
    ),
    GrooveSpec(
        name="dark_rap_half_time",
        bpm=72,
        beats_per_bar=4,
        bars=8,
        kicks=(0, 6, 11),
        snares=(8,),
        hats=tuple(range(0, 16, 1)),
        melody=(),
        expected_side="syncopated",
        expected_emphasis="3",
        notes="NF-like dark rap: sparse kicks, half-time snare on 3, busy hats.",
    ),
    GrooveSpec(
        name="reggae_skank",
        bpm=76,
        beats_per_bar=4,
        bars=8,
        kicks=(0, 8),
        snares=(4, 12),
        hats=(2, 6, 10, 14),
        melody=(),
        expected_side="syncopated",
        expected_emphasis="2+4",
        notes="Off-beat skank (the 'and') plus backbeat snare.",
    ),
    GrooveSpec(
        name="emphasis_beat_2",
        bpm=100,
        beats_per_bar=4,
        bars=8,
        kicks=(0,),
        snares=(4,),
        hats=(),
        melody=(),
        expected_side="syncopated",
        expected_emphasis="2",
        notes="Isolated beat-2 emphasis.",
    ),
    GrooveSpec(
        name="emphasis_beat_3",
        bpm=100,
        beats_per_bar=4,
        bars=8,
        kicks=(8,),
        snares=(),
        hats=(),
        melody=(8,),
        expected_side="simple",
        expected_emphasis="3",
        notes="Isolated beat-3 emphasis (still a strong metrical beat, not a backbeat).",
    ),
    GrooveSpec(
        name="emphasis_beat_4",
        bpm=100,
        beats_per_bar=4,
        bars=8,
        kicks=(0,),
        snares=(12,),
        hats=(),
        melody=(),
        expected_side="syncopated",
        expected_emphasis="4",
        notes="Isolated beat-4 emphasis (backbeat side).",
    ),
    GrooveSpec(
        name="offbeat_eighths",
        bpm=112,
        beats_per_bar=4,
        bars=8,
        kicks=(),
        snares=(),
        hats=(2, 6, 10, 14),
        melody=(2, 6, 10, 14),
        expected_side="syncopated",
        expected_emphasis="offbeat",
        notes="Pure off-beat (the 'and') accents.",
    ),
)


def all_named_fixtures(sr: int = 22050) -> dict[str, tuple[np.ndarray, GrooveSpec | None]]:
    out: dict[str, tuple[np.ndarray, GrooveSpec | None]] = {}
    for spec in GROOVES:
        out[spec.name] = (render_groove(spec, sr=sr), spec)
    hymn = jesus_loves_me_like(sr=sr, bars=8)
    out["jesus_loves_me_like"] = (
        hymn,
        GrooveSpec(
            name="jesus_loves_me_like",
            bpm=92,
            beats_per_bar=4,
            bars=8,
            kicks=(0, 8),
            snares=(),
            hats=(),
            melody=(0, 4, 8, 12),
            expected_side="simple",
            expected_emphasis="1",
            notes="Melodic stand-in for Jesus Loves Me: on-beat, 1-emphasis.",
        ),
    )
    return out
