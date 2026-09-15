#!/usr/bin/env python3
"""BlueZ Agent1 that shows the iPhone passkey and auto-accepts pairing.

Run with system Python (needs dbus + gi). Capability: DisplayYesNo so iOS
numeric-comparison pairing works. Prints PASSKEY lines to stdout for Synco.
"""

from __future__ import annotations

import sys

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

BUS_NAME = "org.bluez"
AGENT_IFACE = "org.bluez.Agent1"
AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"
AGENT_PATH = "/org/bluez/synco_detector/agent"
CAPABILITY = "DisplayYesNo"


class Rejected(dbus.DBusException):
    _dbus_error_name = "org.bluez.Error.Rejected"


class Agent(dbus.service.Object):
    def __init__(self, bus: dbus.SystemBus, path: str) -> None:
        super().__init__(bus, path)

    def _log(self, msg: str) -> None:
        print(msg, flush=True)

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Release(self) -> None:
        self._log("AGENT release")

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device: str, uuid: str) -> None:
        self._log(f"AGENT authorize {device} uuid={uuid}")
        # Always allow A2DP / audio profiles.
        return

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device: str) -> str:
        self._log(f"AGENT RequestPinCode {device} -> 0000")
        return "0000"

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device: str) -> int:
        self._log(f"AGENT RequestPasskey {device} -> 0")
        return dbus.UInt32(0)

    @dbus.service.method(AGENT_IFACE, in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device: str, passkey: int, entered: int) -> None:
        self._log(f"PASSKEY {int(passkey):06d}  (entered={int(entered)}) device={device}")
        self._log(f">>> Type this code on the iPhone if asked: {int(passkey):06d}")

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def DisplayPinCode(self, device: str, pincode: str) -> None:
        self._log(f"PINCODE {pincode} device={device}")
        self._log(f">>> Type this PIN on the iPhone if asked: {pincode}")

    @dbus.service.method(AGENT_IFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device: str, passkey: int) -> None:
        # iPhone numeric comparison — show and auto-accept.
        self._log(f"PASSKEY {int(passkey):06d} device={device}")
        self._log(
            f">>> Confirm on iPhone: both sides should show {int(passkey):06d} — auto-accepting on laptop"
        )
        return

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device: str) -> None:
        self._log(f"AGENT RequestAuthorization {device} — accepting")
        return

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Cancel(self) -> None:
        self._log("AGENT cancel")


def main() -> int:
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    agent = Agent(bus, AGENT_PATH)
    manager = dbus.Interface(bus.get_object(BUS_NAME, "/org/bluez"), AGENT_MANAGER_IFACE)
    try:
        manager.UnregisterAgent(AGENT_PATH)
    except Exception:
        pass
    manager.RegisterAgent(AGENT_PATH, CAPABILITY)
    manager.RequestDefaultAgent(AGENT_PATH)
    agent._log(f"AGENT ready capability={CAPABILITY} path={AGENT_PATH}")
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    try:
        manager.UnregisterAgent(AGENT_PATH)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
