"""What this machine is and how it's doing: cores, memory, thermal state.

Pure ctypes against libSystem and the Objective-C runtime, so it works inside
the iOS app without a native helper and on macOS for the local stand-in.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
import sys

IS_IOS = sys.platform == "ios"
THERMAL = ("nominal", "fair", "serious", "critical")

_libc = ctypes.CDLL(None)


def sysctl(name: str, kind=ctypes.c_int64):
    try:
        if kind is str:
            size = ctypes.c_size_t(0)
            if _libc.sysctlbyname(name.encode(), None, ctypes.byref(size), None, 0):
                return None
            buf = ctypes.create_string_buffer(size.value)
            _libc.sysctlbyname(name.encode(), buf, ctypes.byref(size), None, 0)
            return buf.value.decode()
        v = kind(0)
        size = ctypes.c_size_t(ctypes.sizeof(v))
        if _libc.sysctlbyname(name.encode(), ctypes.byref(v), ctypes.byref(size), None, 0):
            return None
        return v.value
    except Exception:
        return None


def core_layout() -> dict:
    levels = sysctl("hw.nperflevels", ctypes.c_int32) or 1
    out = {"logical": os.cpu_count() or 1}
    for i in range(levels):
        n = sysctl(f"hw.perflevel{i}.physicalcpu", ctypes.c_int32)
        name = sysctl(f"hw.perflevel{i}.name", str)
        if n:
            out[(name or f"level{i}").lower()] = n
    return out


def model() -> str:
    if IS_IOS:
        return sysctl("hw.machine", str) or platform.machine()
    return sysctl("hw.model", str) or platform.machine()


def total_memory() -> int:
    return sysctl("hw.memsize") or 0


def available_memory() -> int | None:
    """Bytes this process may still allocate before jetsam (iOS) or a rough proxy."""
    if IS_IOS:
        try:
            f = _libc.os_proc_available_memory
            f.restype = ctypes.c_size_t
            return int(f()) or None  # 0 means no limit applies (the simulator)
        except AttributeError:
            return None
    return None


# --------------------------------------------------------- Objective-C bits

_objc = None


def _objc_lib():
    global _objc
    if _objc is None:
        _objc = ctypes.CDLL(ctypes.util.find_library("objc") or "/usr/lib/libobjc.A.dylib")
        _objc.objc_getClass.restype = ctypes.c_void_p
        _objc.objc_getClass.argtypes = [ctypes.c_char_p]
        _objc.sel_registerName.restype = ctypes.c_void_p
        _objc.sel_registerName.argtypes = [ctypes.c_char_p]
        ctypes.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
    return _objc


def _send(obj, sel: str, restype=ctypes.c_void_p):
    lib = _objc_lib()
    f = ctypes.cast(lib.objc_msgSend, ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p))
    return f(obj, lib.sel_registerName(sel.encode()))


def thermal_state() -> str:
    try:
        info = _send(_objc_lib().objc_getClass(b"NSProcessInfo"), "processInfo")
        return THERMAL[_send(info, "thermalState", ctypes.c_long)]
    except Exception:
        return "nominal"


def low_power_mode() -> bool:
    try:
        info = _send(_objc_lib().objc_getClass(b"NSProcessInfo"), "processInfo")
        return bool(_send(info, "isLowPowerModeEnabled", ctypes.c_bool))
    except Exception:
        return False


def snapshot() -> dict:
    return {
        "thermal": thermal_state(),
        "low_power": low_power_mode(),
        "avail_mem": available_memory(),
    }
