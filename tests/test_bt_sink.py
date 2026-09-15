from synco_detector.audio.bt_sink import (
    SYNCO_SILENT_SINK,
    list_bluez_phone_streams,
    list_bluez_sinks,
    monitor_for_sink,
)


def test_monitor_for_sink():
    assert monitor_for_sink("bluez_output.AA_BB") == "bluez_output.AA_BB.monitor"
    assert monitor_for_sink("bluez_output.AA_BB.monitor") == "bluez_output.AA_BB.monitor"
    assert monitor_for_sink(SYNCO_SILENT_SINK) == f"{SYNCO_SILENT_SINK}.monitor"


def test_list_bluez_sinks_runs():
    sinks = list_bluez_sinks()
    assert isinstance(sinks, list)


def test_list_bluez_phone_streams_runs():
    streams = list_bluez_phone_streams()
    assert isinstance(streams, list)


def test_bluez_metadata_script_runs():
    import json
    import subprocess
    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[1]
        / "synco_detector"
        / "audio"
        / "bluez_metadata.py"
    )
    proc = subprocess.run(
        ["python3", str(script)],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    assert proc.returncode in (0, 1)
    data = json.loads(proc.stdout.strip() or "{}")
    assert isinstance(data, dict)
    assert "position_ms" in data or "error" in data
    assert "duration_ms" in data or "error" in data
