"""Enumerate MSMF capture devices without activating video or opening a stream.

Use the same MFEnumDeviceSources order as OpenCV's MSMF backend, with the
opaque symbolic link as identity (friendly names need not be unique).
"""

import ctypes as ct
import os
import uuid


class GUID(ct.Structure):
    _fields_ = [("data1", ct.c_uint32), ("data2", ct.c_uint16),
                ("data3", ct.c_uint16), ("data4", ct.c_ubyte * 8)]


def _guid(value):
    return GUID.from_buffer_copy(uuid.UUID(value).bytes_le)


_SOURCE_TYPE = _guid("c60ac5fe-252a-478f-a0ef-bc8fa5f7cad3")
_VIDEO_SOURCE = _guid("8ac3587a-4ae7-42d8-99e0-0a6013eef90f")
_FRIENDLY_NAME = _guid("60d0e559-52f8-4fa2-bbce-acdb34a8ec01")
_SYMBOLIC_LINK = _guid("58f0aad8-22bf-4f8a-bb3d-d2c4978c6e2f")


def _check(hr, operation):
    if hr < 0:
        raise OSError(f"{operation} failed: HRESULT 0x{hr & 0xffffffff:08X}")


def _method(pointer, index, restype, *argtypes):
    table = ct.cast(pointer, ct.POINTER(ct.POINTER(ct.c_void_p))).contents
    return ct.WINFUNCTYPE(restype, ct.c_void_p, *argtypes)(table[index])


def enumerate_video_devices() -> list[dict]:
    if os.name != "nt":
        raise OSError("Media Foundation enumeration requires Windows")
    ole = ct.WinDLL("ole32")
    mfplat = ct.WinDLL("mfplat")
    mf = ct.WinDLL("mf")
    ole.CoInitializeEx.argtypes = [ct.c_void_p, ct.c_uint32]
    ole.CoInitializeEx.restype = ct.c_long
    ole.CoTaskMemFree.argtypes = [ct.c_void_p]
    mfplat.MFStartup.argtypes = [ct.c_uint32, ct.c_uint32]
    mfplat.MFStartup.restype = ct.c_long
    mfplat.MFShutdown.restype = ct.c_long
    mfplat.MFCreateAttributes.argtypes = [ct.POINTER(ct.c_void_p), ct.c_uint32]
    mfplat.MFCreateAttributes.restype = ct.c_long
    mf.MFEnumDeviceSources.argtypes = [ct.c_void_p,
                                      ct.POINTER(ct.POINTER(ct.c_void_p)),
                                      ct.POINTER(ct.c_uint32)]
    mf.MFEnumDeviceSources.restype = ct.c_long
    co_hr = ole.CoInitializeEx(None, 2)
    # RPC_E_CHANGED_MODE means COM is already initialised on this thread.
    if co_hr not in (0, 1, -2147417850):
        _check(co_hr, "CoInitializeEx")
    attributes = ct.c_void_p()
    devices = ct.POINTER(ct.c_void_p)()
    count = ct.c_uint32()
    started = False

    def string_attr(pointer, key):
        value = ct.c_void_p()
        length = ct.c_uint32()
        try:
            _check(_method(pointer, 13, ct.c_long, ct.POINTER(GUID),
                           ct.POINTER(ct.c_void_p), ct.POINTER(ct.c_uint32))(
                pointer, ct.byref(key), ct.byref(value), ct.byref(length)),
                "IMFAttributes.GetAllocatedString")
            return ct.wstring_at(value.value, length.value)
        finally:
            if value.value:
                ole.CoTaskMemFree(value)

    try:
        _check(mfplat.MFStartup(0x20070, 0), "MFStartup")
        started = True
        _check(mfplat.MFCreateAttributes(ct.byref(attributes), 1), "MFCreateAttributes")
        _check(_method(attributes, 24, ct.c_long, ct.POINTER(GUID), ct.POINTER(GUID))(
            attributes, ct.byref(_SOURCE_TYPE), ct.byref(_VIDEO_SOURCE)),
            "IMFAttributes.SetGUID")
        _check(mf.MFEnumDeviceSources(attributes, ct.byref(devices), ct.byref(count)),
               "MFEnumDeviceSources")
        return [{"device": str(index),
                 "name": string_attr(devices[index], _FRIENDLY_NAME),
                 "device_id": string_attr(devices[index], _SYMBOLIC_LINK),
                 "backend": "MSMF"}
                for index in range(count.value)]
    finally:
        if devices:
            for index in range(count.value):
                if devices[index]:
                    _method(devices[index], 2, ct.c_uint32)(devices[index])
            ole.CoTaskMemFree(ct.cast(devices, ct.c_void_p))
        if attributes.value:
            _method(attributes, 2, ct.c_uint32)(attributes)
        if started:
            mfplat.MFShutdown()
        if co_hr in (0, 1):
            ole.CoUninitialize()
