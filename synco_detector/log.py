"""Color terminal logs so the live session is readable."""

from __future__ import annotations

import sys
import time

_last_change: dict[str, str] = {}


class _C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GOLD = "\033[38;5;178m"
    GREEN = "\033[38;5;114m"
    ROSE = "\033[38;5;168m"
    CYAN = "\033[38;5;81m"
    BLUE = "\033[38;5;75m"
    YELLOW = "\033[38;5;221m"
    RED = "\033[38;5;203m"
    GRAY = "\033[38;5;246m"
    MAGENTA = "\033[38;5;176m"


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _emit(tag: str, color: str, msg: str) -> None:
    line = f"{_C.DIM}{_ts()}{_C.RESET} {color}{_C.BOLD}{tag:<9}{_C.RESET} {msg}"
    print(line, file=sys.stdout, flush=True)


def change(key: str, msg: str, *, tag: str = "skip", color: str | None = None) -> bool:
    """Emit only when msg for this key changes. Returns True if logged."""
    prev = _last_change.get(key)
    if prev == msg:
        return False
    _last_change[key] = msg
    palette = {
        "skip": _C.GRAY,
        "whisper": _C.MAGENTA,
        "bt": _C.CYAN,
        "mic": _C.BLUE,
        "warn": _C.YELLOW,
        "lyrics": _C.GREEN,
    }
    _emit(tag, color or palette.get(tag, _C.GRAY), msg)
    return True


def boot(msg: str) -> None:
    _emit("boot", _C.CYAN, msg)


def mic(msg: str) -> None:
    _emit("mic", _C.BLUE, msg)


def bt(msg: str) -> None:
    _emit("bt", _C.CYAN, msg)


def groove(msg: str, side: str = "") -> None:
    color = _C.GOLD if side == "simple" else _C.ROSE if side == "syncopated" else _C.YELLOW
    _emit("groove", color, msg)


def lyric(msg: str) -> None:
    _emit("lyrics", _C.GREEN, msg)


def whisper(msg: str) -> None:
    _emit("whisper", _C.MAGENTA, msg)


def warn(msg: str) -> None:
    _emit("warn", _C.YELLOW, msg)


def error(msg: str) -> None:
    _emit("error", _C.RED, msg)


def skip(msg: str) -> None:
    _emit("skip", _C.GRAY, msg)
