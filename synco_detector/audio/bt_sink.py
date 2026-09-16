"""Advertise this machine as a Bluetooth A2DP speaker and capture that stream.

iPhone → laptop A2DP appears in PipeWire as a *sink-input* (bluez_input / a2dp-source)
that normally plays out the laptop speakers. We create a dedicated null sink
(`synco_detector_bt`) with no hardware output, move the phone stream onto it, and
capture from its `.monitor` — analysis-only, speakers stay quiet.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

from synco_detector import log
from synco_detector.config import SAMPLE_RATE

_BLUEZ_SINK_RE = re.compile(r"^(bluez_output\.|bluez_sink\.)", re.I)
_PASSKEY_RE = re.compile(r"(?:PASSKEY|Confirm passkey|passkey)\s*[:=]?\s*(\d{4,6})", re.I)
_AGENT_SCRIPT = Path(__file__).resolve().parent / "bluez_agent.py"
_METADATA_SCRIPT = Path(__file__).resolve().parent / "bluez_metadata.py"

# App-private silent destination — not wired to ALSA / HDMI speakers.
SYNCO_SILENT_SINK = "synco_detector_bt"
_SINK_INPUT_SPLIT = re.compile(r"\n(?=Sink Input #)")


def _run(cmd: list[str], timeout: float = 8.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def list_bluez_sinks() -> list[str]:
    """Legacy: sinks named bluez_output.* (PC → BT speaker). Rare for phone→PC."""
    code, out, _err = _run(["pactl", "list", "short", "sinks"])
    if code != 0:
        return []
    sinks: list[str] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[1]
        if _BLUEZ_SINK_RE.search(name):
            sinks.append(name)
    return sinks


def list_bluez_phone_streams() -> list[dict[str, str]]:
    """Phone→PC A2DP streams show up as Pulse sink-inputs (not sinks)."""
    code, out, _err = _run(["pactl", "list", "sink-inputs"])
    if code != 0 or not out.strip():
        return []
    found: list[dict[str, str]] = []
    for block in _SINK_INPUT_SPLIT.split(out):
        if not block.strip():
            continue
        low = block.lower()
        is_phone = (
            "a2dp-source" in low
            or "api.bluez5.a2dp.source" in low
            or "bluez_input." in low
            or ('device.api = "bluez5"' in low and "a2dp" in low)
        )
        if not is_phone:
            continue
        mid = re.search(r"Sink Input #(\d+)", block)
        sink_id = re.search(r"Sink:\s*(\d+)", block)
        node = re.search(r'node\.name = "([^"]+)"', block)
        media = re.search(r'media\.name = "([^"]+)"', block)
        if not mid:
            continue
        found.append(
            {
                "id": mid.group(1),
                "sink_id": sink_id.group(1) if sink_id else "",
                "node": node.group(1) if node else "",
                "media": media.group(1) if media else "",
            }
        )
    return found


def monitor_for_sink(sink: str) -> str:
    if sink.endswith(".monitor"):
        return sink
    return f"{sink}.monitor"


def _sink_exists(name: str) -> bool:
    code, out, _ = _run(["pactl", "list", "short", "sinks"])
    if code != 0:
        return False
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == name:
            return True
    return False


def _sink_index(name: str) -> str | None:
    code, out, _ = _run(["pactl", "list", "short", "sinks"])
    if code != 0:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == name:
            return parts[0]
    return None


class BluetoothSinkManager:
    """Advertise as a speaker, park phone audio on a silent null sink, expose monitor."""

    def __init__(self, alias: str = "Synco Detector") -> None:
        self.alias = alias
        self.adapter_name = ""
        self.sink_name: str | None = None
        self.monitor_name: str | None = None
        self.status = "idle"
        self.error: str | None = None
        self.last_passkey: str | None = None
        self.pair_hint = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_sink: Callable[[str | None], None] | None = None
        self._btctl: subprocess.Popen | None = None
        self._agent: subprocess.Popen | None = None
        self._bt_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._null_module_id: str | None = None
        self._phone_connected = False
        self._saved_default_sink: str | None = None
        self._last_route_log = 0.0
        self._last_meta_poll = 0.0
        self._last_meta_sig = ""
        self._silent_since: float | None = None
        self._last_a2dp_nudge = 0.0
        self.now_playing: dict[str, Any] = {
            "available": False,
            "title": "",
            "artist": "",
            "album": "",
            "genre": "",
            "status": "",
            "device": "",
            "position_ms": 0,
            "duration_ms": 0,
            "player_path": "",
        }
        self.audio_alive = False
        self._live_peak_ref: Callable[[], float] | None = None

    def _note_passkey(self, key: str, source: str = "") -> None:
        key = key.zfill(6) if key.isdigit() and len(key) <= 6 else key
        self.last_passkey = key
        self.pair_hint = f"Pairing code {key} — confirm the same number on the iPhone"
        log.bt(f"PAIRING CODE  >>>  {key}  <<<  {source}".rstrip())
        log.bt("On the iPhone: confirm that code (or type it). Laptop auto-accepts.")

    def _bt_send(self, *lines: str) -> None:
        if self._btctl is None or self._btctl.stdin is None:
            return
        with self._bt_lock:
            for line in lines:
                try:
                    self._btctl.stdin.write(line + "\n")
                    self._btctl.stdin.flush()
                except Exception:
                    break

    def _poll_now_playing(self) -> None:
        if not _METADATA_SCRIPT.is_file():
            return
        now = time.time()
        # Keep Position fresh for the UI scrubber (~1 Hz).
        if now - self._last_meta_poll < 0.85:
            return
        self._last_meta_poll = now
        py = shutil.which("python3") or "/usr/bin/python3"
        code, out, _err = _run([py, str(_METADATA_SCRIPT), "meta"], timeout=4.0)
        if code != 0 or not out.strip():
            return
        try:
            meta = json.loads(out)
        except json.JSONDecodeError:
            return
        if not isinstance(meta, dict):
            return
        try:
            position_ms = max(0, int(meta.get("position_ms") or 0))
        except (TypeError, ValueError):
            position_ms = 0
        try:
            duration_ms = max(0, int(meta.get("duration_ms") or 0))
        except (TypeError, ValueError):
            duration_ms = 0
        self.now_playing = {
            "available": bool(meta.get("available")),
            "title": str(meta.get("title") or ""),
            "artist": str(meta.get("artist") or ""),
            "album": str(meta.get("album") or ""),
            "genre": str(meta.get("genre") or ""),
            "status": str(meta.get("status") or ""),
            "device": str(meta.get("device") or ""),
            "position_ms": position_ms,
            "duration_ms": duration_ms,
            "player_path": str(meta.get("player_path") or ""),
            "transport_state": str(meta.get("transport_state") or ""),
            "transport_uuid": str(meta.get("transport_uuid") or ""),
            "transport_volume": meta.get("transport_volume"),
        }
        sig = "|".join(
            str(self.now_playing.get(k, ""))
            for k in ("title", "artist", "album", "status", "transport_state")
        )
        if sig and sig != self._last_meta_sig:
            self._last_meta_sig = sig
            title = self.now_playing["title"]
            artist = self.now_playing["artist"]
            album = self.now_playing["album"]
            if title or artist:
                bits = [b for b in (artist, title) if b]
                extra = f" · {album}" if album else ""
                tstate = self.now_playing.get("transport_state") or "?"
                dur = self.now_playing.get("duration_ms") or 0
                dur_s = f"  {dur // 60000}:{(dur // 1000) % 60:02d}" if dur else ""
                log.bt(f"now playing  {' — '.join(bits)}{extra}{dur_s}  (a2dp={tstate})")

    def media_command(self, action: str, position_ms: int | None = None) -> dict[str, Any]:
        """AVRCP transport / seek via BlueZ helper script."""
        if not _METADATA_SCRIPT.is_file():
            return {"ok": False, "error": "metadata helper missing"}
        py = shutil.which("python3") or "/usr/bin/python3"
        if action == "seek":
            if position_ms is None:
                return {"ok": False, "error": "position_ms required"}
            cmd = [py, str(_METADATA_SCRIPT), "seek", str(int(position_ms))]
        elif action in {"play", "pause", "next", "prev"}:
            cmd = [py, str(_METADATA_SCRIPT), action]
        else:
            return {"ok": False, "error": f"unknown action {action}"}
        code, out, err = _run(cmd, timeout=6.0)
        if not out.strip():
            return {"ok": False, "error": err.strip() or f"exit {code}"}
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {"ok": False, "error": out.strip() or err.strip() or "bad json"}

    def _connected_phone_mac(self) -> str | None:
        code, out, _ = _run(["bluetoothctl", "devices", "Connected"], timeout=4.0)
        if code != 0:
            return None
        for line in out.splitlines():
            parts = line.split(maxsplit=2)
            if len(parts) >= 2 and parts[0] == "Device":
                return parts[1]
        return None

    def _nudge_a2dp(self, reason: str) -> None:
        """Soft-reconnect A2DP when AVRCP says playing but PCM is silent."""
        now = time.time()
        if now - self._last_a2dp_nudge < 25.0:
            return
        self._last_a2dp_nudge = now
        mac = self._connected_phone_mac()
        if not mac:
            log.change("a2dp-nudge", f"a2dp silent ({reason}) — no connected phone", tag="warn")
            return
        log.bt(f"A2DP audio stuck ({reason}) — soft-reconnecting {mac}")
        # Keep desktop speakers as default so browser hear-through stays audible.
        self._ensure_desktop_speakers()
        _run(["bluetoothctl", "trust", mac], timeout=4.0)
        code, _o, _e = _run(["bluetoothctl", "connect", mac], timeout=12.0)
        if code != 0:
            _run(["bluetoothctl", "disconnect", mac], timeout=6.0)
            time.sleep(0.8)
            _run(["bluetoothctl", "connect", mac], timeout=12.0)
        # Re-assert card profile for PipeWire.
        _c, cards, _ = _run(["pactl", "list", "short", "cards"])
        for line in cards.splitlines():
            parts = line.split()
            if len(parts) >= 2 and "bluez_card" in parts[1]:
                _run(["pactl", "set-card-profile", parts[1], "audio-gateway"], timeout=5.0)
        time.sleep(0.6)
        self.route_phone_to_silent_sink()
        self._ensure_desktop_speakers()
        restart = getattr(self, "_restart_capture_ref", None)
        if callable(restart):
            try:
                restart()
            except Exception as exc:
                log.warn(f"capture restart after a2dp nudge: {exc}")
        log.bt("A2DP nudge done — press Play once on iPhone if still silent")

    def _check_audio_health(self) -> None:
        """If metadata says playing but capture is silent, recover A2DP."""
        status = (self.now_playing.get("status") or "").lower()
        tstate = (self.now_playing.get("transport_state") or "").lower()
        peak = 0.0
        if self._live_peak_ref:
            try:
                peak = float(self._live_peak_ref() or 0.0)
            except Exception:
                peak = 0.0
        playing = status == "playing" or bool(self.now_playing.get("title"))
        silent = peak < 0.015
        broken_transport = tstate in {"", "idle", "pending"} or (
            playing and silent and self._phone_connected
        )
        if playing and silent:
            if self._silent_since is None:
                self._silent_since = time.time()
                log.change(
                    "a2dp-health",
                    f"phone playing but capture silent (a2dp={tstate or 'missing'})",
                    tag="warn",
                )
            elif time.time() - self._silent_since >= 6.0 and broken_transport:
                self._nudge_a2dp(f"playing+silent peak={peak:.3f} a2dp={tstate or 'missing'}")
                self._silent_since = time.time()
        else:
            if self._silent_since is not None and not silent:
                log.change("a2dp-health", "Bluetooth PCM flowing", tag="bt")
            self._silent_since = None
            self.audio_alive = not silent and peak >= 0.015

    def _start_dbus_agent(self) -> bool:
        """System-python BlueZ agent that can display/confirm iPhone passkeys."""
        if not _AGENT_SCRIPT.is_file():
            return False
        py = shutil.which("python3") or "/usr/bin/python3"
        # Only one DefaultAgent — drop stale Synco agents from prior runs.
        try:
            subprocess.run(
                ["pkill", "-f", str(_AGENT_SCRIPT)],
                capture_output=True,
                timeout=2.0,
                check=False,
            )
            time.sleep(0.15)
        except Exception:
            pass
        try:
            self._agent = subprocess.Popen(
                [py, str(_AGENT_SCRIPT)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            log.warn(f"could not start BlueZ agent: {exc}")
            self._agent = None
            return False
        threading.Thread(target=self._agent_stdout_loop, name="synco-bt-agent", daemon=True).start()
        time.sleep(0.35)
        if self._agent.poll() is not None:
            log.warn("BlueZ agent exited early — falling back to bluetoothctl agent")
            self._agent = None
            return False
        log.bt("pairing agent ready (DisplayYesNo) — iPhone passkeys will print here")
        return True

    def _agent_stdout_loop(self) -> None:
        assert self._agent is not None and self._agent.stdout is not None
        for line in self._agent.stdout:
            line = line.rstrip()
            if not line:
                continue
            m = _PASSKEY_RE.search(line)
            if m:
                self._note_passkey(m.group(1), "agent")
            elif line.startswith("AGENT") or line.startswith(">>>"):
                log.bt(line)

    def _btctl_stdout_loop(self) -> None:
        assert self._btctl is not None and self._btctl.stdout is not None
        for line in self._btctl.stdout:
            raw = line.rstrip()
            if not raw:
                continue
            low = raw.lower()
            m = _PASSKEY_RE.search(raw)
            if m:
                self._note_passkey(m.group(1), "bluetoothctl")
            if "confirm passkey" in low or "(yes/no)" in low or "authorize service" in low:
                log.bt(f"auto-confirm: {raw}")
                self._bt_send("yes")
            elif "request confirmation" in low:
                self._bt_send("yes")

    def _remove_stale_phone_bond(self) -> None:
        """Only remove a half-paired phone that matches our speaker alias / iPhone."""
        code, out, _ = _run(["bluetoothctl", "devices"])
        if code != 0:
            return
        alias_l = self.alias.lower()
        for line in out.splitlines():
            parts = line.split(maxsplit=2)
            if len(parts) < 2 or parts[0] != "Device":
                continue
            mac = parts[1]
            name = parts[2] if len(parts) > 2 else ""
            name_l = name.lower()
            _c, info, _e = _run(["bluetoothctl", "info", mac], timeout=4.0)
            low = info.lower()
            if "paired: yes" in low:
                continue
            interesting = (
                "iphone" in name_l
                or "ipad" in name_l
                or alias_l in name_l
                or ("paired: no" in low and "connected: yes" in low)
            )
            if not interesting:
                continue
            _run(["bluetoothctl", "remove", mac], timeout=5.0)
            log.bt(f"cleared stale phone bond {name or mac} ({mac})")

    def ensure_silent_sink(self) -> bool:
        """Create (or reuse) a null sink with no speaker output for phone audio."""
        if _sink_exists(SYNCO_SILENT_SINK):
            self.sink_name = SYNCO_SILENT_SINK
            self.monitor_name = monitor_for_sink(SYNCO_SILENT_SINK)
            _run(["pactl", "suspend-sink", SYNCO_SILENT_SINK, "0"], timeout=3.0)
            return True
        # session.suspend-timeout-seconds=0 keeps the null sink alive so A2DP
        # does not go idle just because nothing is playing to hardware.
        code, out, err = _run(
            [
                "pactl",
                "load-module",
                "module-null-sink",
                f"sink_name={SYNCO_SILENT_SINK}",
                "sink_properties="
                "device.description=\"Synco Detector (silent)\" "
                "device.class=\"sound\" "
                "session.suspend-timeout-seconds=0",
                "rate=44100",
                "channels=2",
            ],
            timeout=8.0,
        )
        mid = (out or "").strip() or (err or "").strip()
        if code != 0 or not mid.isdigit():
            # Fallback without extra properties (older Pulse compat).
            code, out, err = _run(
                [
                    "pactl",
                    "load-module",
                    "module-null-sink",
                    f"sink_name={SYNCO_SILENT_SINK}",
                    'sink_properties=device.description="Synco Detector (silent)"',
                    "rate=44100",
                    "channels=2",
                ],
                timeout=8.0,
            )
            mid = (out or "").strip() or (err or "").strip()
        if code != 0 or not mid.isdigit():
            self.error = f"could not create silent sink: {err or out or 'unknown'}"
            log.error(self.error)
            return False
        self._null_module_id = mid
        for _ in range(20):
            if _sink_exists(SYNCO_SILENT_SINK):
                break
            time.sleep(0.05)
        if not _sink_exists(SYNCO_SILENT_SINK):
            self.error = "silent sink created but not visible yet"
            log.error(self.error)
            return False
        _run(["pactl", "suspend-sink", SYNCO_SILENT_SINK, "0"], timeout=3.0)
        self.sink_name = SYNCO_SILENT_SINK
        self.monitor_name = monitor_for_sink(SYNCO_SILENT_SINK)
        log.bt(
            f"silent sink ready  name={SYNCO_SILENT_SINK}  "
            f"monitor={self.monitor_name}  (speakers bypassed)"
        )
        return True

    def _pick_hardware_sink(self) -> str | None:
        """Best real speaker/headphones sink (never the silent analysis sink)."""
        if self._saved_default_sink and self._saved_default_sink != SYNCO_SILENT_SINK:
            if _sink_exists(self._saved_default_sink):
                return self._saved_default_sink
        code, out, _ = _run(["pactl", "list", "short", "sinks"])
        if code != 0:
            return None
        scored: list[tuple[int, str]] = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            name = parts[1]
            low = name.lower()
            if name == SYNCO_SILENT_SINK or "null" in low or low.startswith("auto_null"):
                continue
            score = 1
            if "analog" in low:
                score += 40
            if "headphone" in low or "headset" in low:
                score += 35
            if "usb" in low:
                score += 20
            if "hdmi" in low or "displayport" in low:
                score += 10
            scored.append((score, name))
        if not scored:
            return None
        scored.sort(key=lambda x: (-x[0], x[1]))
        return scored[0][1]

    def _prefer_silent_for_new_streams(self) -> None:
        """Remember speakers. Do not leave default on silent — that mutes the browser.

        Phone A2DP is moved onto the silent sink by route_phone_to_silent_sink().
        """
        hw = self._pick_hardware_sink()
        if hw:
            self._saved_default_sink = hw
            _c, dout, _ = _run(["pactl", "get-default-sink"])
            current = dout.strip() if _c == 0 else ""
            if current != hw:
                _run(["pactl", "set-default-sink", hw], timeout=4.0)
                log.bt(f"default sink → {hw} (desktop/browser audible; phone routed separately)")
        # Ensure silent sink exists for phone parking.
        if not _sink_exists(SYNCO_SILENT_SINK):
            self.ensure_silent_sink()

    def _restore_default_sink(self) -> None:
        self._ensure_desktop_speakers(force_log=True)

    def _ensure_desktop_speakers(self, force_log: bool = False) -> None:
        """Keep default sink on real speakers so browser hear-through is audible.

        Phone A2DP stays on the silent sink via move-sink-input; only the *default*
        sink is restored so Chromium/WebAudio is not parked on the null sink.
        """
        hw = self._pick_hardware_sink()
        if not hw:
            return
        self._saved_default_sink = hw
        _c, dout, _ = _run(["pactl", "get-default-sink"])
        current = dout.strip() if _c == 0 else ""
        if current != hw:
            _run(["pactl", "set-default-sink", hw], timeout=4.0)
            log.bt(f"default sink → {hw} (desktop/browser audible; phone stays silent)")
        elif force_log:
            log.bt(f"default sink already {hw} (desktop/browser audible)")
        self._rescue_desktop_from_silent(hw)

    def _rescue_desktop_from_silent(self, hw: str) -> None:
        """Move non-phone streams (browser, etc.) off the silent sink onto speakers."""
        silent_idx = _sink_index(SYNCO_SILENT_SINK)
        if not silent_idx:
            return
        phone_ids = {s["id"] for s in list_bluez_phone_streams()}
        code, out, _ = _run(["pactl", "list", "sink-inputs"])
        if code != 0 or not out.strip():
            return
        for block in _SINK_INPUT_SPLIT.split(out):
            if not block.strip():
                continue
            mid = re.search(r"Sink Input #(\d+)", block)
            sink_id = re.search(r"Sink:\s*(\d+)", block)
            if not mid or not sink_id:
                continue
            sid = mid.group(1)
            if sid in phone_ids:
                continue
            if sink_id.group(1) != silent_idx:
                continue
            _run(["pactl", "move-sink-input", sid, hw], timeout=4.0)
            log.change(
                "bt-rescue-desktop",
                f"moved desktop stream #{sid} → {hw} (was on silent sink)",
                tag="bt",
            )

    def _unload_silent_sink(self) -> None:
        # Move phone streams off our sink first so PipeWire doesn't drop the transport.
        default = ""
        _c, dout, _ = _run(["pactl", "get-default-sink"])
        if _c == 0:
            default = dout.strip()
        silent_idx = _sink_index(SYNCO_SILENT_SINK)
        for stream in list_bluez_phone_streams():
            if silent_idx and stream.get("sink_id") == silent_idx and default:
                _run(["pactl", "move-sink-input", stream["id"], default], timeout=4.0)
        if self._null_module_id:
            _run(["pactl", "unload-module", self._null_module_id], timeout=5.0)
            self._null_module_id = None
        elif _sink_exists(SYNCO_SILENT_SINK):
            # Best-effort: find module by sink name if we didn't create it this session.
            _c, mods, _ = _run(["pactl", "list", "short", "modules"])
            for line in mods.splitlines():
                if "module-null-sink" in line and SYNCO_SILENT_SINK in line:
                    mid = line.split()[0]
                    _run(["pactl", "unload-module", mid], timeout=5.0)
                    break
        self.sink_name = None
        self.monitor_name = None

    def route_phone_to_silent_sink(self) -> list[dict[str, str]]:
        """Move every phone A2DP stream onto the silent sink (away from speakers)."""
        if not _sink_exists(SYNCO_SILENT_SINK):
            if not self.ensure_silent_sink():
                return []
        silent_idx = _sink_index(SYNCO_SILENT_SINK)
        streams = list_bluez_phone_streams()
        for stream in streams:
            if silent_idx and stream.get("sink_id") == silent_idx:
                continue
            code, _o, err = _run(
                ["pactl", "move-sink-input", stream["id"], SYNCO_SILENT_SINK],
                timeout=4.0,
            )
            label = stream.get("media") or stream.get("node") or stream["id"]
            if code == 0:
                log.change(
                    "bt-route",
                    f"routed phone audio → silent sink  ({label})",
                    tag="bt",
                )
            else:
                log.warn(f"move-sink-input failed for {label}: {err.strip() or code}")
        return list_bluez_phone_streams()

    def enable_speaker_mode(self) -> bool:
        if not shutil.which("bluetoothctl"):
            self.error = "bluetoothctl missing — install bluez"
            self.status = "error"
            log.error(self.error)
            return False
        if not shutil.which("pactl"):
            self.error = "pactl missing — install pulseaudio-utils / pipewire-pulse"
            self.status = "error"
            log.error(self.error)
            return False
        if not shutil.which("parec"):
            self.error = "parec missing — install pulseaudio-utils"
            self.status = "error"
            log.error(self.error)
            return False

        log.bt(f"advertising as Bluetooth speaker  alias={self.alias!r}")
        if not self.ensure_silent_sink():
            return False

        # Remember real speakers before we briefly arm the silent default.
        hw = self._pick_hardware_sink()
        if hw:
            self._saved_default_sink = hw

        dbus_ok = self._start_dbus_agent()

        try:
            self._btctl = subprocess.Popen(
                ["bluetoothctl"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            self.error = f"could not start bluetoothctl: {exc}"
            self.status = "error"
            log.error(self.error)
            return False

        self._reader = threading.Thread(
            target=self._btctl_stdout_loop, name="synco-btctl-out", daemon=True
        )
        self._reader.start()

        for cmd in (
            ["bluetoothctl", "power", "on"],
            ["bluetoothctl", "system-alias", self.alias],
            ["bluetoothctl", "pairable", "on"],
            ["bluetoothctl", "discoverable-timeout", "0"],
            ["bluetoothctl", "discoverable", "on"],
        ):
            _run(cmd, timeout=6.0)

        if dbus_ok:
            log.bt("using D-Bus DisplayYesNo agent (bluetoothctl agent left off)")
        else:
            self._bt_send("agent off", "agent DisplayYesNo", "default-agent")
            log.warn("D-Bus agent unavailable — bluetoothctl DisplayYesNo fallback")

        time.sleep(0.4)
        if os.environ.get("SYNCO_BT_CLEAR_PAIRS", "").strip().lower() in {"1", "true", "yes"}:
            self._remove_stale_phone_bond()
        _run(["bluetoothctl", "discoverable", "on"], timeout=4.0)
        _run(["bluetoothctl", "pairable", "on"], timeout=4.0)

        # Make silent the default so new A2DP playback never hits laptop speakers.
        self._prefer_silent_for_new_streams()
        self.route_phone_to_silent_sink()

        code, show_out, _ = _run(["bluetoothctl", "show"])
        text = show_out.lower()
        for line in show_out.splitlines():
            s = line.strip()
            if s.startswith("Alias:") or s.startswith("Name:"):
                self.adapter_name = s.split(":", 1)[-1].strip()
                break
        if "powered: yes" not in text and code != 0:
            self.error = "Bluetooth adapter did not power on"
            self.status = "error"
            log.error(self.error)
            return False
        if "discoverable: yes" not in text:
            log.warn("adapter not discoverable yet — retrying")
            _run(["bluetoothctl", "discoverable", "on"], timeout=4.0)
        self.status = "discoverable"
        log.bt(
            f"ready — on iPhone: Settings → Bluetooth → {self.alias!r}. "
            f"Audio is parked on silent sink '{SYNCO_SILENT_SINK}' (not speakers)."
        )
        log.bt("If pairing fails: on iPhone tap the (i) → Forget This Device, then connect again.")
        if "audio sink" not in text and "0000110b" not in text:
            log.warn("Audio Sink UUID not listed on adapter — check PipeWire bluez5")
        return True

    def disable_speaker_mode(self) -> None:
        self._restore_default_sink()
        self._bt_send("discoverable off", "pairable off", "quit")
        if self._btctl is not None:
            try:
                self._btctl.terminate()
                self._btctl.wait(timeout=2.0)
            except Exception:
                try:
                    self._btctl.kill()
                except Exception:
                    pass
            self._btctl = None
        if self._agent is not None:
            try:
                self._agent.terminate()
                self._agent.wait(timeout=2.0)
            except Exception:
                try:
                    self._agent.kill()
                except Exception:
                    pass
            self._agent = None
        self._unload_silent_sink()
        self.status = "idle"
        self.pair_hint = ""
        log.bt("Bluetooth speaker mode stopped")

    def start_watch(self, on_sink: Callable[[str | None], None] | None = None) -> None:
        self._on_sink = on_sink
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch_loop, name="synco-bt-watch", daemon=True)
        self._thread.start()

    def stop_watch(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _watch_loop(self) -> None:
        last_connected = False
        ticks = 0
        while not self._stop.wait(0.75):
            ticks += 1
            if ticks % 20 == 0 and not last_connected:
                _run(["bluetoothctl", "discoverable", "on"], timeout=3.0)
                _run(["bluetoothctl", "pairable", "on"], timeout=3.0)

            streams = self.route_phone_to_silent_sink()
            connected = bool(streams)
            self._phone_connected = connected
            if connected:
                self._poll_now_playing()
                self._check_audio_health()
                # Keep speakers as default even if a reconnect flipped us back to silent.
                if ticks % 2 == 0:
                    self._ensure_desktop_speakers()

            if connected != last_connected:
                last_connected = connected
                if connected:
                    self.status = "connected"
                    self.pair_hint = ""
                    self.last_passkey = None
                    label = streams[0].get("media") or streams[0].get("node") or "phone"
                    log.bt(f"A2DP phone stream connected  {label} → {SYNCO_SILENT_SINK} (silent)")
                    # Desktop apps can use speakers again; phone stream stays on silent.
                    self._ensure_desktop_speakers(force_log=True)
                    if self._on_sink:
                        try:
                            self._on_sink(SYNCO_SILENT_SINK)
                        except Exception as exc:
                            log.warn(f"bt sink callback: {exc}")
                else:
                    self.status = "discoverable"
                    self._prefer_silent_for_new_streams()
                    log.change(
                        "bt-wait-stream",
                        "waiting for iPhone to stream audio (silent sink armed)…",
                        tag="bt",
                    )
                    if self._on_sink:
                        try:
                            self._on_sink(None)
                        except Exception as exc:
                            log.warn(f"bt sink callback: {exc}")
            elif connected and ticks % 20 == 0:
                # Only remind when we have no now-playing and no PCM.
                if not self.now_playing.get("title") and not self.audio_alive:
                    log.change(
                        "bt-idle-hint",
                        "phone linked — press Play on iPhone if analysis is idle",
                        tag="bt",
                    )


class BluetoothSinkCapture:
    """Capture phone A2DP from the silent null-sink monitor (mic off, speakers off)."""

    def __init__(
        self,
        sr: int = SAMPLE_RATE,
        maxlen_s: float = 30.0,
        alias: str | None = None,
    ) -> None:
        self.sr = sr
        self.maxlen = int(sr * maxlen_s)
        self._buf = np.zeros(self.maxlen, dtype=np.float32)
        self._listen = np.zeros(self.maxlen, dtype=np.float32)
        self._write = 0
        self._filled = 0
        self._listen_read = 0  # absolute sample counter for consume
        self._listen_write = 0  # absolute sample counter
        self._lock = threading.Lock()
        self._err: str | None = None
        self.device_name = "bluetooth-sink (waiting…)"
        self._level = 0.08
        self.gain = 1.0
        self._listen_gain = 1.0
        self.mode = "bluetooth"
        alias = alias or os.environ.get("SYNCO_BT_ALIAS", "Synco Detector")
        self._mgr = BluetoothSinkManager(alias=alias)
        self._mgr._live_peak_ref = lambda: float(self.live_peak)
        self._mgr._restart_capture_ref = self.restart_monitor_capture
        self._parec: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._current_sink: str | None = None
        self.live_peak = 0.0

    @property
    def pair_hint(self) -> str:
        return self._mgr.pair_hint

    @property
    def last_passkey(self) -> str | None:
        return self._mgr.last_passkey

    @property
    def bt_status(self) -> str:
        return self._mgr.status

    @property
    def now_playing(self) -> dict[str, Any]:
        return dict(self._mgr.now_playing)

    def media_command(self, action: str, position_ms: int | None = None) -> dict[str, Any]:
        return self._mgr.media_command(action, position_ms=position_ms)

    def start(self) -> None:
        if not self._mgr.enable_speaker_mode():
            self._err = self._mgr.error
            return
        self._stop.clear()
        self._mgr.start_watch(on_sink=self._on_sink_change)
        # Start capturing the silent monitor immediately so the first samples after
        # the phone connects are not lost; engine still waits for real level.
        self._on_sink_change(SYNCO_SILENT_SINK)
        if list_bluez_phone_streams():
            log.bt("mic disabled — analyzing Bluetooth A2DP via silent sink")
        else:
            self.device_name = f"bluetooth · waiting for phone ({self._mgr.alias})"
            log.bt("mic disabled — silent sink armed; waiting for phone A2DP")

    def stop(self) -> None:
        self._stop.set()
        self._stop_parec()
        self._mgr.stop_watch()
        try:
            self._mgr.disable_speaker_mode()
        except Exception:
            pass

    def restart_monitor_capture(self) -> None:
        """Bounce parec after A2DP recoveries so the monitor is not stuck silent."""
        sink = self._current_sink or SYNCO_SILENT_SINK
        self._stop_parec()
        self._current_sink = None
        with self._lock:
            self._write = 0
            self._filled = 0
            self._listen_read = 0
            self._listen_write = 0
            self._buf[:] = 0
            self._listen[:] = 0
        self._on_sink_change(sink)
        log.bt(f"capture restarted on {sink}.monitor")

    def _on_sink_change(self, sink: str | None) -> None:
        if sink is None:
            # Keep silent-sink capture running; just update label.
            self.device_name = f"bluetooth · waiting for phone ({self._mgr.alias})"
            return
        if sink == self._current_sink and self.active:
            self.device_name = f"bluetooth · {sink} (silent)"
            return
        self._stop_parec()
        self._current_sink = sink
        self.device_name = f"bluetooth · {sink} (silent)"
        self._start_parec(monitor_for_sink(sink))

    def _stop_parec(self) -> None:
        proc = self._parec
        self._parec = None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if self._reader and self._reader.is_alive():
            self._reader.join(timeout=1.5)
        self._reader = None

    def _start_parec(self, monitor: str) -> None:
        log.bt(f"capturing monitor={monitor}  sr={self.sr} (speakers muted via null sink)")
        try:
            self._parec = subprocess.Popen(
                [
                    "parec",
                    f"--device={monitor}",
                    "--format=float32le",
                    f"--rate={self.sr}",
                    "--channels=1",
                    "--latency-msec=80",
                    "--process-time-msec=20",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except Exception as exc:
            self._err = f"parec open failed: {exc}"
            log.error(self._err)
            self._parec = None
            return
        self._err = None
        self._reader = threading.Thread(target=self._read_loop, name="synco-parec", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        proc = self._parec
        if proc is None or proc.stdout is None:
            return
        chunk = 1024 * 4
        while not self._stop.is_set():
            if self._parec is not proc:
                break
            if proc.poll() is not None:
                break
            try:
                data = proc.stdout.read(chunk)
            except Exception as exc:
                self._err = f"parec read: {exc}"
                break
            if not data:
                time.sleep(0.02)
                continue
            raw = np.frombuffer(data, dtype=np.float32).copy()
            if raw.size == 0:
                continue
            block_peak = float(np.max(np.abs(raw)))
            self._level = 0.97 * self._level + 0.03 * max(block_peak, 1e-4)
            self.live_peak = self._level
            # Listen path: smooth gain toward a healthy playback level (quiet A2DP
            # still needs lift; avoid the analysis path's extreme pumping).
            if block_peak > 1e-5:
                target = float(np.clip(0.38 / block_peak, 1.0, 14.0))
            else:
                target = max(1.0, self._listen_gain * 0.98)
            self._listen_gain = 0.88 * self._listen_gain + 0.12 * target
            dry = np.clip(raw * self._listen_gain, -0.95, 0.95)
            # Analysis path: stronger AGC for rhythm/lyrics stability.
            if self._level > 0.55:
                self.gain = 0.55 / self._level
            elif self._level > 1e-5:
                self.gain = min(10.0, 0.22 / self._level)
            else:
                self.gain = 1.0
            mono = np.clip(raw * self.gain, -0.98, 0.98)
            n = mono.size
            with self._lock:
                i = self._write
                end = i + n
                if end <= self.maxlen:
                    self._buf[i:end] = mono
                    self._listen[i:end] = dry
                else:
                    k = self.maxlen - i
                    self._buf[i:] = mono[:k]
                    self._buf[: n - k] = mono[k:]
                    self._listen[i:] = dry[:k]
                    self._listen[: n - k] = dry[k:]
                self._write = end % self.maxlen
                self._filled = min(self.maxlen, self._filled + n)
                self._listen_write += n
                # Drop unread listen samples that were overwritten.
                behind = self._listen_write - self._listen_read
                if behind > self.maxlen:
                    self._listen_read = self._listen_write - self.maxlen
        if proc.poll() not in (None, 0) and self._parec is proc:
            log.warn("parec exited — phone may have disconnected")

    @property
    def error(self) -> str | None:
        return self._err or self._mgr.error

    @property
    def active(self) -> bool:
        return self._parec is not None and self._parec.poll() is None

    def latest(self, seconds: float) -> np.ndarray:
        n = min(int(self.sr * seconds), self.maxlen)
        with self._lock:
            filled = self._filled
            write = self._write
            if filled == 0:
                return np.zeros(0, dtype=np.float32)
            n = min(n, filled)
            start = (write - n) % self.maxlen
            if start + n <= self.maxlen:
                return self._buf[start : start + n].copy()
            k = self.maxlen - start
            return np.concatenate([self._buf[start:], self._buf[: n - k]])

    def consume_listen(self, max_seconds: float = 0.12) -> np.ndarray:
        """Return new dry PCM since last consume (no overlaps — for browser playback)."""
        max_n = max(1, min(int(self.sr * max_seconds), self.maxlen))
        with self._lock:
            available = int(self._listen_write - self._listen_read)
            if available <= 0:
                return np.zeros(0, dtype=np.float32)
            n = min(available, max_n)
            start = int(self._listen_read % self.maxlen)
            if start + n <= self.maxlen:
                dry = self._listen[start : start + n].copy()
                ana = self._buf[start : start + n].copy()
            else:
                k = self.maxlen - start
                dry = np.concatenate([self._listen[start:], self._listen[: n - k]])
                ana = np.concatenate([self._buf[start:], self._buf[: n - k]])
            self._listen_read += n
        dry_peak = float(np.max(np.abs(dry))) if dry.size else 0.0
        ana_peak = float(np.max(np.abs(ana))) if ana.size else 0.0
        # Prefer musical dry path; fall back to analysis AGC if dry is still too quiet.
        if dry_peak >= 0.04:
            return dry
        if ana_peak > dry_peak * 1.5 and ana_peak >= 0.03:
            return ana
        return dry
