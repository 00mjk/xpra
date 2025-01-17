# This file is part of Xpra.
# Copyright (C) 2017-2022 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import ctypes
from ctypes import WinDLL, WINFUNCTYPE, oledll, c_int  # @UnresolvedImport
from ctypes.wintypes import BOOL, LPVOID, LPCWSTR, LPCVOID, LPOLESTR


dsound = WinDLL("dsound", use_last_error=True)
DirectSoundEnumerateW = dsound.DirectSoundEnumerateW
DirectSoundCaptureEnumerate = dsound.DirectSoundCaptureEnumerateW
GetDeviceID = dsound.GetDeviceID
#DEFINE_GUID(DSDEVID_DefaultPlayback,     0xDEF00000,0x9C6D,0x47Ed,0xAA,0xF1,0x4D,0xDA,0x8F,0x2B,0x5C,0x03);
#DEFINE_GUID(DSDEVID_DefaultCapture,      0xDEF00001,0x9C6D,0x47Ed,0xAA,0xF1,0x4D,0xDA,0x8F,0x2B,0x5C,0x03);
#DEFINE_GUID(DSDEVID_DefaultVoicePlayback,0xDEF00002,0x9C6D,0x47Ed,0xAA,0xF1,0x4D,0xDA,0x8F,0x2B,0x5C,0x03);
#DEFINE_GUID(DSDEVID_DefaultVoiceCapture, 0xDEF00003,0x9C6D,0x47ED,0xAA,0xF1,0x4D,0xDA,0x8F,0x2B,0x5C,0x03);

LPDSENUMCALLBACK = WINFUNCTYPE(BOOL, LPVOID, LPCWSTR, LPCWSTR, LPCVOID)
StringFromGUID2 = oledll.ole32.StringFromGUID2
StringFromGUID2.restype = c_int
StringFromGUID2.argtypes = [LPVOID, LPOLESTR, c_int]

def _enum_devices(fn):
    devices = []
    def cb_enum(lpGUID, lpszDesc, _lpszDrvName, _):
        dev = ""
        if lpGUID is not None:
            buf = ctypes.create_unicode_buffer(256)
            pbuf = ctypes.byref(buf)
            if StringFromGUID2(lpGUID, ctypes.cast(pbuf, LPOLESTR), 256):
                dev = buf.value
        devices.append((dev, lpszDesc))
        return True
    fn(LPDSENUMCALLBACK(cb_enum), None)
    return devices

def get_devices():
    return _enum_devices(DirectSoundEnumerateW)

def get_capture_devices():
    return _enum_devices(DirectSoundCaptureEnumerate)


def main():
    from xpra.platform import program_context
    from xpra.log import Logger, enable_color
    with program_context("Audio Device Info", "Audio Device Info"):
        enable_color()
        log = Logger("win32", "audio")
        import sys
        verbose = "-v" in sys.argv or "--verbose" in sys.argv
        if verbose:
            log.enable_debug()
        log.info("")
        log.info("Capture Devices:")
        for k,v in get_capture_devices():
            log.info("* %-40s : %s", v, k)
        log.info("")
        log.info("All Devices:")
        for guid,name in get_devices():
            log.info("* %-40s : %s", name, guid)

if __name__ == "__main__":
    main()
