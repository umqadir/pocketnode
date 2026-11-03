"""Minimal usbmuxd client: list devices and open a TCP stream to a port on one.

Protocol: a 16-byte little-endian header (length, version=1, type=8 plist,
tag) followed by an XML plist. After a successful "Connect" the socket becomes
a raw byte stream to the device port. The port goes on the wire in network byte
order inside a little-endian integer, hence the byte swap.
"""

from __future__ import annotations

import os
import plistlib
import socket
import struct
from dataclasses import dataclass

SOCKET = os.environ.get("USBMUXD_SOCKET_ADDRESS", "/var/run/usbmuxd")
_HDR = struct.Struct("<IIII")
_PLIST = 8


@dataclass
class Device:
    id: int
    serial: str  # UDID
    connection: str  # "USB" or "Network"
    product_id: int | None = None


class MuxError(OSError):
    pass


def _open() -> socket.socket:
    if SOCKET.startswith("/"):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(SOCKET)
    else:
        host, port = SOCKET.rsplit(":", 1)
        s = socket.create_connection((host, int(port)))
    return s


def _send(s: socket.socket, payload: dict, tag: int = 1) -> None:
    body = plistlib.dumps({"ClientVersionString": "pocketnode", "ProgName": "pocketnode",
                           "kLibUSBMuxVersion": 3, **payload})
    s.sendall(_HDR.pack(_HDR.size + len(body), 1, _PLIST, tag) + body)


def _recv(s: socket.socket) -> dict:
    hdr = _exact(s, _HDR.size)
    length, _, _, _ = _HDR.unpack(hdr)
    return plistlib.loads(_exact(s, length - _HDR.size))


def _exact(s: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise MuxError("usbmuxd closed the connection")
        buf += chunk
    return bytes(buf)


def list_devices() -> list[Device]:
    s = _open()
    try:
        _send(s, {"MessageType": "ListDevices"})
        reply = _recv(s)
    finally:
        s.close()
    out = []
    for d in reply.get("DeviceList", []):
        p = d.get("Properties", {})
        out.append(Device(d["DeviceID"], p.get("SerialNumber", ""), p.get("ConnectionType", "USB"),
                          p.get("ProductID")))
    return out


def connect(device_id: int, port: int) -> socket.socket:
    s = _open()
    _send(s, {"MessageType": "Connect", "DeviceID": device_id,
              "PortNumber": socket.htons(port)})
    reply = _recv(s)
    if reply.get("MessageType") != "Result" or reply.get("Number", 1) != 0:
        s.close()
        raise MuxError(f"usbmuxd refused connection to port {port}: {reply.get('Number')}")
    return s
