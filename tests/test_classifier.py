import numpy as np

from synco_detector.analysis.classifier import analyze_audio
from synco_detector.analysis.patterns import GROOVES, render_groove
from synco_detector.config import ANALYSIS_SR


def test_hymn_is_simple_1_emphasis():
    spec = next(g for g in GROOVES if g.name == "hymn_1_emphasis")
    v = analyze_audio(render_groove(spec), sr=ANALYSIS_SR)
    assert v.spectrum < 45
    assert v.side in {"simple", "mixed"}
    assert v.primary_emphasis in {"1", "3"}


def test_boom_bap_is_syncopated():
    spec = next(g for g in GROOVES if g.name == "hiphop_boom_bap")
    v = analyze_audio(render_groove(spec), sr=ANALYSIS_SR)
    assert v.spectrum > 55
    assert v.side in {"syncopated", "mixed"}


def test_backbeat_not_confused_with_hymn():
    hymn = next(g for g in GROOVES if g.name == "hymn_1_emphasis")
    rock = next(g for g in GROOVES if g.name == "rock_backbeat")
    vh = analyze_audio(render_groove(hymn), sr=ANALYSIS_SR)
    vr = analyze_audio(render_groove(rock), sr=ANALYSIS_SR)
    assert vr.spectrum - vh.spectrum > 18


def test_dark_rap_half_time_is_syncopated():
    spec = next(g for g in GROOVES if g.name == "dark_rap_half_time")
    v = analyze_audio(render_groove(spec), sr=ANALYSIS_SR)
    assert v.spectrum > 65
    assert v.side == "syncopated"


def test_trap_stays_syncopated():
    spec = next(g for g in GROOVES if g.name == "hiphop_trap")
    v = analyze_audio(render_groove(spec), sr=ANALYSIS_SR)
    assert v.spectrum > 70
    assert v.side == "syncopated"
