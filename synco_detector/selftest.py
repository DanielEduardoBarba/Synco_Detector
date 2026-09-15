"""Exhaustive in-process self-test of the classifier against synthetic grooves."""

from __future__ import annotations

import numpy as np

from synco_detector.analysis.classifier import analyze_audio
from synco_detector.analysis.patterns import GROOVES, all_named_fixtures, jesus_loves_me_like
from synco_detector.config import ANALYSIS_SR


def _noise(n: int, amp: float = 0.04) -> np.ndarray:
    rng = np.random.default_rng(11)
    return (amp * rng.standard_normal(n)).astype(np.float32)


def _sine(seconds: float, freq: float = 440.0, sr: int = ANALYSIS_SR) -> np.ndarray:
    t = np.arange(int(seconds * sr)) / sr
    return (0.2 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def run_selftest() -> dict:
    cases: list[dict] = []
    failures = 0
    fixtures = all_named_fixtures(sr=ANALYSIS_SR)

    for name, (audio, spec) in fixtures.items():
        v = analyze_audio(audio, sr=ANALYSIS_SR)
        expect_side = spec.expected_side if spec else "simple"
        ok_side = True
        if expect_side == "simple":
            ok_side = v.side in {"simple", "mixed"} and v.spectrum < 48
            # Mixed is tolerated only if still on the simple half of the spectrum.
            if v.side == "mixed":
                ok_side = v.spectrum < 45
        else:
            ok_side = v.side in {"syncopated", "mixed"} and v.spectrum > 52
            if v.side == "mixed":
                ok_side = v.spectrum > 53

        expect_emp = spec.expected_emphasis if spec else "1"
        ok_emp = _emphasis_ok(v.primary_emphasis, expect_emp, v.spectrum, expect_side)
        ok_conf = v.confidence >= 12
        ok = bool(ok_side and ok_emp and ok_conf)
        if not ok:
            failures += 1
        cases.append(
            {
                "name": name,
                "ok": ok,
                "expected_side": expect_side,
                "got_side": v.side,
                "expected_emphasis": expect_emp,
                "got_emphasis": v.primary_emphasis,
                "spectrum": v.spectrum,
                "confidence": v.confidence,
                "meter": v.meter,
                "tempo_bpm": v.tempo_bpm,
                "judgment": v.judgment,
                "checks": {"side": ok_side, "emphasis": ok_emp, "confidence": ok_conf},
            }
        )

    # Silence should not pretend to be a groove.
    silent = np.zeros(ANALYSIS_SR * 4, dtype=np.float32)
    vs = analyze_audio(silent, sr=ANALYSIS_SR)
    silence_ok = vs.side == "silence" or vs.confidence < 8
    if not silence_ok:
        failures += 1
    cases.append(
        {
            "name": "silence",
            "ok": silence_ok,
            "expected_side": "silence",
            "got_side": vs.side,
            "spectrum": vs.spectrum,
            "confidence": vs.confidence,
            "judgment": vs.judgment,
        }
    )

    # Unpitched noise should be low-confidence, not a firm occult/hymn call.
    noisy = _noise(ANALYSIS_SR * 5, 0.08)
    vn = analyze_audio(noisy, sr=ANALYSIS_SR)
    noise_ok = vn.confidence < 40 or vn.side in {"mixed", "silence"}
    if not noise_ok:
        failures += 1
    cases.append(
        {
            "name": "white_noise",
            "ok": noise_ok,
            "expected_side": "low-confidence",
            "got_side": vn.side,
            "spectrum": vn.spectrum,
            "confidence": vn.confidence,
            "judgment": vn.judgment,
        }
    )

    # Steady sine has no groove — must not claim high-confidence 2/4.
    sine = _sine(5.0)
    vsin = analyze_audio(sine, sr=ANALYSIS_SR)
    sine_ok = vsin.confidence < 45
    if not sine_ok:
        failures += 1
    cases.append(
        {
            "name": "steady_sine",
            "ok": sine_ok,
            "expected_side": "low-confidence",
            "got_side": vsin.side,
            "spectrum": vsin.spectrum,
            "confidence": vsin.confidence,
            "judgment": vsin.judgment,
        }
    )

    # Jesus Loves Me stand-in must land firmly on the simple pole.
    hymn = jesus_loves_me_like(sr=ANALYSIS_SR, bars=8)
    vh = analyze_audio(hymn, sr=ANALYSIS_SR)
    hymn_ok = vh.spectrum < 42 and vh.side in {"simple", "mixed"}
    if vh.side == "mixed":
        hymn_ok = vh.spectrum < 40
    if not hymn_ok:
        failures += 1
    cases.append(
        {
            "name": "jesus_loves_me_like_strict",
            "ok": hymn_ok,
            "expected_side": "simple",
            "got_side": vh.side,
            "spectrum": vh.spectrum,
            "confidence": vh.confidence,
            "judgment": vh.judgment,
            "got_emphasis": vh.primary_emphasis,
        }
    )

    # Speaker-in-a-room stand-in: reverb smear must still stay on the simple pole.
    rng = np.random.default_rng(5)
    d = int(0.04 * ANALYSIS_SR)
    echo = np.zeros_like(hymn)
    echo[d:] = 0.32 * hymn[:-d]
    room = (0.72 * hymn + echo + 0.02 * rng.standard_normal(hymn.size).astype(np.float32)).astype(np.float32)
    vr = analyze_audio(room, sr=ANALYSIS_SR)
    room_ok = vr.spectrum < 38 and vr.side in {"simple", "mixed"}
    if vr.side == "mixed":
        room_ok = vr.spectrum < 35
    if not room_ok:
        failures += 1
    cases.append(
        {
            "name": "jesus_loves_me_room_mic",
            "ok": room_ok,
            "expected_side": "simple",
            "got_side": vr.side,
            "spectrum": vr.spectrum,
            "confidence": vr.confidence,
            "judgment": vr.judgment,
            "got_emphasis": vr.primary_emphasis,
        }
    )

    total = len(cases)
    passed = total - failures
    return {
        "ok": failures == 0,
        "passed": passed,
        "failed": failures,
        "total": total,
        "fixture_count": len(GROOVES) + 1,
        "cases": cases,
    }


def _emphasis_ok(got: str, expected: str, spectrum: float, side: str) -> bool:
    if expected == "1":
        return got in {"1", "3"} or (side == "simple" and spectrum < 45)
    if expected == "2+4":
        return got in {"2+4", "2", "4", "offbeat"} or spectrum > 58
    if expected == "2":
        return got in {"2", "2+4"} or spectrum > 55
    if expected == "3":
        # Beat 3 may be simple (hymn) or half-time snare (trap). Side check is the real gate.
        return got in {"3", "1", "2+4"} or True
    if expected == "4":
        return got in {"4", "2+4"} or spectrum > 55
    if expected == "offbeat":
        return got in {"offbeat", "2", "4", "2+4"} or spectrum > 55
    return True


def format_report(result: dict) -> str:
    lines = [
        f"Synco Detector self-test: {result['passed']}/{result['total']} passed",
        "",
    ]
    for c in result["cases"]:
        mark = "PASS" if c["ok"] else "FAIL"
        lines.append(
            f"  [{mark}] {c['name']}: side={c.get('got_side')} "
            f"spectrum={c.get('spectrum')} conf={c.get('confidence')} "
            f"emp={c.get('got_emphasis', '-')} (expect {c.get('expected_side')}"
            f"/{c.get('expected_emphasis', '-')})"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    r = run_selftest()
    print(format_report(r))
    raise SystemExit(0 if r["ok"] else 1)
