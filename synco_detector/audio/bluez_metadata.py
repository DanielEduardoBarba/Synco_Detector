#!/usr/bin/env python3
"""BlueZ AVRCP helpers: now-playing JSON, optional seek/play/pause.

Run with system Python (needs dbus). Prints JSON on stdout for read commands.
"""

from __future__ import annotations

import json
import sys


def _track_field(track: dict, *keys: str):
    for key in keys:
        if key in track and track[key] is not None and track[key] != "":
            val = track[key]
            if isinstance(val, bytes):
                val = val.decode("utf-8", errors="replace")
            return val
    return None


def _as_ms(val) -> int | None:
    if val is None:
        return None
    try:
        n = int(val)
    except (TypeError, ValueError):
        return None
    # BlueZ Position / Duration are typically milliseconds.
    # Guard absurd values (treat > 24h as microseconds).
    if n > 86_400_000:
        n = n // 1000
    return max(0, n)


def _collect() -> dict:
    out: dict = {
        "available": False,
        "title": "",
        "artist": "",
        "album": "",
        "genre": "",
        "status": "",
        "device": "",
        "player_path": "",
        "position_ms": 0,
        "duration_ms": 0,
        "transport_state": "",
        "transport_uuid": "",
        "transport_volume": None,
    }
    import dbus

    bus = dbus.SystemBus()
    om = dbus.Interface(
        bus.get_object("org.bluez", "/"),
        "org.freedesktop.DBus.ObjectManager",
    )
    objects = om.GetManagedObjects()

    for path, ifaces in objects.items():
        if "org.bluez.MediaTransport1" in ifaces:
            tr = ifaces["org.bluez.MediaTransport1"]
            out["transport_state"] = str(tr.get("State", "") or "")
            out["transport_uuid"] = str(tr.get("UUID", "") or "")
            try:
                out["transport_volume"] = int(tr.get("Volume"))
            except Exception:
                out["transport_volume"] = None

    best: tuple[int, dict] | None = None
    for path, ifaces in objects.items():
        if "org.bluez.MediaPlayer1" not in ifaces:
            continue
        player = ifaces["org.bluez.MediaPlayer1"]
        track = player.get("Track") or {}
        if hasattr(track, "items"):
            track = dict(track)
        title = str(_track_field(track, "Title") or "").strip()
        artist = str(_track_field(track, "Artist", "AlbumArtist") or "").strip()
        album = str(_track_field(track, "Album") or "").strip()
        genre = str(_track_field(track, "Genre") or "").strip()
        status = str(player.get("Status", "") or "")
        device = str(player.get("Device", "") or "")
        name = str(player.get("Name", "") or "")
        position_ms = _as_ms(player.get("Position")) or 0
        duration_ms = _as_ms(_track_field(track, "Duration")) or 0
        score = 0
        if title:
            score += 4
        if artist:
            score += 2
        if status.lower() in {"playing", "paused"}:
            score += 1
        row = {
            **out,
            "available": bool(title or artist or album),
            "title": title,
            "artist": artist,
            "album": album,
            "genre": genre,
            "status": status,
            "device": device or name,
            "player_path": str(path),
            "position_ms": position_ms,
            "duration_ms": duration_ms,
        }
        if best is None or score > best[0]:
            best = (score, row)

    if best:
        out = best[1]
    return out


def _player_iface(path: str):
    import dbus

    bus = dbus.SystemBus()
    return dbus.Interface(bus.get_object("org.bluez", path), "org.bluez.MediaPlayer1")


def cmd_seek(position_ms: int) -> dict:
    import time

    import dbus

    meta = _collect()
    path = meta.get("player_path") or ""
    if not path:
        return {"ok": False, "error": "no MediaPlayer"}
    player = _player_iface(path)
    cur = int(meta.get("position_ms") or 0)
    target = max(0, int(position_ms))
    # Prefer absolute Position write when supported; else relative Seek.
    try:
        props = dbus.Interface(
            dbus.SystemBus().get_object("org.bluez", path),
            "org.freedesktop.DBus.Properties",
        )
        props.Set("org.bluez.MediaPlayer1", "Position", dbus.UInt32(target))
        return {"ok": True, "method": "SetPosition", "position_ms": target}
    except Exception:
        pass
    try:
        delta = target - cur
        player.Seek(dbus.Int64(delta))
        return {"ok": True, "method": "Seek", "delta_ms": delta}
    except Exception as exc:
        # Best-effort FF/RW pulses for players without Seek.
        try:
            steps = max(1, min(40, abs(target - cur) // 2000))
            meth = player.FastForward if target > cur else player.Rewind
            for _ in range(steps):
                meth()
                time.sleep(0.05)
            player.Play()
            return {"ok": True, "method": "FastForward/Rewind", "steps": steps}
        except Exception as exc2:
            return {"ok": False, "error": f"{exc}; {exc2}"}


def cmd_transport(action: str) -> dict:
    meta = _collect()
    path = meta.get("player_path") or ""
    if not path:
        return {"ok": False, "error": "no MediaPlayer"}
    player = _player_iface(path)
    try:
        if action == "play":
            player.Play()
        elif action == "pause":
            player.Pause()
        elif action == "next":
            player.Next()
        elif action == "prev":
            player.Previous()
        else:
            return {"ok": False, "error": f"unknown action {action}"}
        return {"ok": True, "action": action}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def main(argv: list[str]) -> int:
    try:
        if len(argv) <= 1 or argv[1] == "meta":
            print(json.dumps(_collect(), ensure_ascii=False))
            return 0
        if argv[1] == "seek" and len(argv) >= 3:
            print(json.dumps(cmd_seek(int(float(argv[2]))), ensure_ascii=False))
            return 0
        if argv[1] in {"play", "pause", "next", "prev"}:
            print(json.dumps(cmd_transport(argv[1]), ensure_ascii=False))
            return 0
        print(json.dumps({"ok": False, "error": "usage: meta|seek <ms>|play|pause|next|prev"}))
        return 2
    except Exception as exc:
        print(json.dumps({"available": False, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
