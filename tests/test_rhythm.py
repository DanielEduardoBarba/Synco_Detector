from synco_detector.analysis.patterns import GROOVES, render_groove
from synco_detector.analysis.rhythm import extract_rhythm
from synco_detector.config import ANALYSIS_SR


def test_tempo_in_ballpark_for_hymn():
    spec = next(g for g in GROOVES if g.name == "hymn_1_emphasis")
    r = extract_rhythm(render_groove(spec), sr=ANALYSIS_SR)
    assert r.tempo_bpm > 60
    assert abs(r.tempo_bpm - spec.bpm) < 50  # allow octave error
    assert r.rms > 0.01


def test_histograms_normalized():
    spec = next(g for g in GROOVES if g.name == "rock_backbeat")
    r = extract_rhythm(render_groove(spec), sr=ANALYSIS_SR)
    assert abs(r.hist16.sum() - 1.0) < 0.15 or r.hist16.sum() == 0
