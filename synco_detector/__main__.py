"""CLI: run the live app, self-test, or classify a file."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Synco Detector — live lyrics + groove emphasis")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--test", action="store_true", help="Run exhaustive synthetic self-test")
    p.add_argument("--file", type=str, default=None, help="Classify a WAV/FLAC/OGG file and exit")
    p.add_argument(
        "--audiosink",
        action="store_true",
        help=(
            "Advertise this machine as a Bluetooth A2DP speaker (iPhone sees a Bluetooth "
            "speaker). Analyze that stream only — microphone is disabled."
        ),
    )
    p.add_argument(
        "--bt-alias",
        default=None,
        help="Bluetooth name shown to phones (default: Synco Detector)",
    )
    args = p.parse_args(argv)

    if args.audiosink:
        os.environ["SYNCO_AUDIO_SINK"] = "1"
    if args.bt_alias:
        os.environ["SYNCO_BT_ALIAS"] = args.bt_alias

    if args.test:
        from synco_detector.selftest import format_report, run_selftest

        result = run_selftest()
        print(format_report(result))
        return 0 if result["ok"] else 1

    if args.file:
        import soundfile as sf

        from synco_detector.analysis.classifier import analyze_audio

        path = Path(args.file)
        y, sr = sf.read(str(path), always_2d=False)
        if y.ndim > 1:
            y = np.mean(y, axis=1)
        v = analyze_audio(y.astype(np.float32), sr=int(sr))
        print(json.dumps(v.to_dict(), indent=2))
        print()
        print(v.summary)
        return 0

    from synco_detector import log
    from synco_detector.config import HOST, PORT

    host = args.host or HOST
    port = args.port or PORT
    import uvicorn

    # Import webapp AFTER env flags so Engine picks up AUDIO_SINK mode.
    from synco_detector.webapp import app

    log.boot(f"Synco Detector  →  http://{host}:{port}")
    log.boot("green = simple 1-emphasis   red = syncopation (bad)")
    if args.audiosink:
        log.boot("mode = Bluetooth audio sink (mic off)")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
