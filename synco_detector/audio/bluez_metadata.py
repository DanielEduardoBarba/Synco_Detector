#!/usr/bin/env python3
"""BlueZ AVRCP helpers: now-playing JSON, optional seek/play/pause.

Run with system Python (needs dbus). Prints JSON on stdout for read commands.
"""

from __future__ import annotations

import json
import sys


def _as_text(val) -> str:
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace").strip()
    if isinstance(val, (list, tuple)):
        parts = [_as_text(x) for x in val]
        return ", ".join(p for p in parts if p)
    # dbus Array / String etc. stringify poorly — prefer str after unwrap
    try:
        if hasattr(val, "strip"):
            return str(val).strip()
    except Exception:
        pass
    text = str(val).strip()
    return text


def _track_field(track: dict, *keys: str):
    # BlueZ may use dbus.String keys; normalize to plain str.
    normalized = {}
    for key, val in dict(track or {}).items():
        normalized[str(key)] = val
    for key in keys:
        if key in normalized and normalized[key] is not None and normalized[key] != "":
            return normalized[key]
    # Case-insensitive fallback
    lower_map = {str(k).lower(): v for k, v in normalized.items()}
    for key in keys:
        if key.lower() in lower_map and lower_map[key.lower()] not in (None, ""):
            return lower_map[key.lower()]
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


def _empty() -> dict:
    return {
        "available": False,
        "title": "",
        "artist": "",
        "album": "",
        "genre": "",
        "status": "",
        "device": "",
        "player_name": "",
        "player_path": "",
        "position_ms": 0,
        "duration_ms": 0,
        "transport_state": "",
        "transport_uuid": "",
        "transport_volume": None,
    }


def _collect() -> dict:
    out = _empty()
    import dbus

    bus = dbus.SystemBus()
    om = dbus.Interface(
        bus.get_object("org.bluez", "/"),
        "org.freedesktop.DBus.ObjectManager",
    )
    objects = om.GetManagedObjects()

    preferred_player = ""
    for _path, ifaces in objects.items():
        if "org.bluez.MediaControl1" in ifaces:
            ctrl = ifaces["org.bluez.MediaControl1"]
            player = ctrl.get("Player")
            if player:
                preferred_player = str(player)
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
        props = None
        try:
            props = dbus.Interface(
                bus.get_object("org.bluez", path),
                "org.freedesktop.DBus.Properties",
            )
        except Exception:
            props = None
        # Prefer live Properties.Get for Track — cached snapshot can lag.
        track = player.get("Track") or {}
        if props is not None:
            try:
                live = props.Get("org.bluez.MediaPlayer1", "Track")
                if live:
                    track = live
            except Exception:
                pass
        if hasattr(track, "items"):
            track = dict(track)

        title = _as_text(_track_field(track, "Title", "Name"))
        artist = _as_text(_track_field(track, "Artist", "AlbumArtist", "Composer"))
        album = _as_text(_track_field(track, "Album"))
        genre = _as_text(_track_field(track, "Genre"))
        status = _as_text(player.get("Status", ""))
        device = _as_text(player.get("Device", ""))
        player_name = _as_text(player.get("Name", ""))
        position_ms = _as_ms(player.get("Position")) or 0
        if props is not None:
            try:
                live_pos = props.Get("org.bluez.MediaPlayer1", "Position")
                position_ms = _as_ms(live_pos) or position_ms
            except Exception:
                pass
        duration_ms = _as_ms(_track_field(track, "Duration")) or 0

        score = 0
        if str(path) == preferred_player:
            score += 8
        if title:
            score += 4
        if artist:
            score += 2
        if album:
            score += 1
        if status.lower() in {"playing", "paused"}:
            score += 3
        if status.lower() == "playing":
            score += 2
        row = {
            **out,
            "available": bool(title or artist or album),
            "title": title,
            "artist": artist,
            "album": album,
            "genre": genre,
            "status": status,
            "device": device or player_name,
            "player_name": player_name,
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
    method = None
    # Prefer absolute Position write when supported; else relative Seek.
    try:
        props = dbus.Interface(
            dbus.SystemBus().get_object("org.bluez", path),
            "org.freedesktop.DBus.Properties",
        )
        props.Set("org.bluez.MediaPlayer1", "Position", dbus.UInt32(target))
        method = "SetPosition"
    except Exception:
        props = None
    if method is None:
        try:
            delta = target - cur
            player.Seek(dbus.Int64(delta))
            method = "Seek"
        except Exception as exc:
            # Best-effort FF/RW pulses for players without Seek.
            try:
                steps = max(1, min(40, abs(target - cur) // 2000))
                meth = player.FastForward if target > cur else player.Rewind
                for _ in range(steps):
                    meth()
                    time.sleep(0.05)
                player.Play()
                method = "FastForward/Rewind"
            except Exception as exc2:
                return {"ok": False, "error": f"{exc}; {exc2}"}
    # Give AVRCP a beat, then read back Position (iPhone often lags a little).
    time.sleep(0.12)
    confirmed = target
    try:
        props = dbus.Interface(
            dbus.SystemBus().get_object("org.bluez", path),
            "org.freedesktop.DBus.Properties",
        )
        confirmed = _as_ms(props.Get("org.bluez.MediaPlayer1", "Position")) or target
    except Exception:
        confirmed = target
    return {"ok": True, "method": method, "position_ms": int(confirmed), "requested_ms": target}


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
