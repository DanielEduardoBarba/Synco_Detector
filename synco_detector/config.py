"""Runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = Path(os.environ.get("SYNCO_MODEL_DIR", ROOT / ".models"))
SAMPLE_RATE = 22050
ANALYSIS_SR = 22050
CHANNELS = 1
BLOCK_SIZE = 1024
ANALYSIS_WINDOW_S = 10.0
ANALYSIS_HOP_S = 0.45
TRANSCRIBE_WINDOW_S = 8.0
TRANSCRIBE_HOP_S = 2.8
WHISPER_MODEL = os.environ.get("SYNCO_WHISPER_MODEL", "small.en")
HOST = os.environ.get("SYNCO_HOST", "127.0.0.1")
PORT = int(os.environ.get("SYNCO_PORT", "8080"))
SILENCE_RMS = 0.008
# When true, advertise as a Bluetooth A2DP speaker and analyze that stream (mic off).
AUDIO_SINK_MODE = os.environ.get("SYNCO_AUDIO_SINK", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
BT_ALIAS = os.environ.get("SYNCO_BT_ALIAS", "Synco Detector")


@dataclass(frozen=True)
class Band:
    name: str
    low_hz: float
    high_hz: float


BANDS = (
    Band("kick", 35.0, 90.0),     # downbeat / kick only
    Band("low", 30.0, 150.0),
    Band("mid", 150.0, 2500.0),
    Band("high", 5000.0, 10000.0),
)
