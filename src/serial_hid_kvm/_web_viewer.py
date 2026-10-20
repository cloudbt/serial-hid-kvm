"""Web-based remote desktop viewer served over HTTP + WebSocket.

Provides a browser-based KVM interface: JPEG video stream over WebSocket
(binary frames) and keyboard/mouse input as JSON text messages.  HTML/JS
is embedded directly — no npm or build step required.

Usage (integrated into server.py):
    serial-hid-kvm --headless --web
    serial-hid-kvm --web --web-port 9330 --web-fps 20 --web-quality 60
"""

import asyncio
import hashlib
import hmac
import importlib.util
import ipaddress
import json
import logging
import os
import queue
import re
import secrets
import shutil
import socket
import ssl
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, quote

import websockets
from websockets.http11 import Response

from ._audio import AudioCapture
from ._captions import CaptionService
from ._minutes import MinutesService
from ._live_meeting import LiveMeeting
from ._web_audio import RECORDING_AUDIO_MIXER_JS
from ._web_recording_audio import RECORDING_AUDIO_CONTROLS_JS
from ._recording import convert_recording
from ._recording_session import RecordingSessions
from ._recording_library import RecordingLibrary
from ._server_recorder import FPS_CHOICES, SOURCES as SERVER_REC_SOURCES, ServerRecorder
from ._sop_http import SopService, local_request
from ._sop_viewer import install_sop_viewer
from .hid_protocol import (
    build_keyboard_report,
    build_mouse_abs_packet,
    build_mouse_rel_packet,
)

logger = logging.getLogger(__name__)

# WebRTC (H.264) streaming is optional: pip install serial-hid-kvm[webrtc].
# find_spec only checks installability — the actual import happens lazily on
# the first webrtc_offer, so startup cost is unaffected.
_WEBRTC_AVAILABLE = importlib.util.find_spec("aiortc") is not None


def _is_private_address(ip: str) -> bool:
    """Whether *ip* is loopback / LAN / link-local (vs. a public address).

    Drives the per-connection LAN/WAN tuning split: LAN clients keep the
    original loopback-tuned behaviour (fixed fps, max quality, zero jitter
    buffer, always-on audio), WAN clients get the adaptive treatment.
    Unparseable addresses are treated as WAN — the safe direction.
    """
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local


# WAN stream tuning (per-connection; LAN connections are unaffected).
_WAN_CREDITS = 2            # max unacked JPEG frames in flight
_WAN_FPS_CAP = 30           # WAN JPEG target fps (never above web_fps)
_WAN_QUALITY_MIN = 25       # adaptive JPEG quality floor
_WAN_QUALITY_CAP = 80       # adaptive JPEG quality ceiling (≤ web_quality)
_WAN_ACK_RESET_S = 5.0      # assume acks lost after this long at full credit
_WAN_RTC_FPS_CAP = 30       # WAN H264 default fps (never above webrtc_fps)
_WAN_RTC_START = 4_000_000  # WAN H264 starting bitrate (bits/s)


# ---------------------------------------------------------------------------
# W3C KeyboardEvent.code → HID keycode mapping
# ---------------------------------------------------------------------------

_JS_CODE_TO_HID: dict[str, int] = {
    # Row 0: Escape + Function keys
    "Escape": 0x29,
    "F1": 0x3A, "F2": 0x3B, "F3": 0x3C, "F4": 0x3D,
    "F5": 0x3E, "F6": 0x3F, "F7": 0x40, "F8": 0x41,
    "F9": 0x42, "F10": 0x43, "F11": 0x44, "F12": 0x45,
    "PrintScreen": 0x46, "ScrollLock": 0x47, "Pause": 0x48,

    # Row 1: Digits
    "Backquote": 0x35,
    "Digit1": 0x1E, "Digit2": 0x1F, "Digit3": 0x20, "Digit4": 0x21,
    "Digit5": 0x22, "Digit6": 0x23, "Digit7": 0x24, "Digit8": 0x25,
    "Digit9": 0x26, "Digit0": 0x27,
    "Minus": 0x2D, "Equal": 0x2E, "Backspace": 0x2A,

    # Row 2: QWERTY
    "Tab": 0x2B,
    "KeyQ": 0x14, "KeyW": 0x1A, "KeyE": 0x08, "KeyR": 0x15,
    "KeyT": 0x17, "KeyY": 0x1C, "KeyU": 0x18, "KeyI": 0x0C,
    "KeyO": 0x12, "KeyP": 0x13,
    "BracketLeft": 0x2F, "BracketRight": 0x30, "Backslash": 0x31,

    # Row 3: ASDF
    "CapsLock": 0x39,
    "KeyA": 0x04, "KeyS": 0x16, "KeyD": 0x07, "KeyF": 0x09,
    "KeyG": 0x0A, "KeyH": 0x0B, "KeyJ": 0x0D, "KeyK": 0x0E,
    "KeyL": 0x0F,
    "Semicolon": 0x33, "Quote": 0x34, "Enter": 0x28,

    # Row 4: ZXCV
    "KeyZ": 0x1D, "KeyX": 0x1B, "KeyC": 0x06, "KeyV": 0x19,
    "KeyB": 0x05, "KeyN": 0x11, "KeyM": 0x10,
    "Comma": 0x36, "Period": 0x37, "Slash": 0x38,

    # Row 5
    "Space": 0x2C,

    # Navigation cluster
    "Insert": 0x49, "Home": 0x4A, "PageUp": 0x4B,
    "Delete": 0x4C, "End": 0x4D, "PageDown": 0x4E,

    # Arrow keys
    "ArrowUp": 0x52, "ArrowDown": 0x51,
    "ArrowLeft": 0x50, "ArrowRight": 0x4F,

    # Numpad
    "NumLock": 0x53,
    "NumpadDivide": 0x54, "NumpadMultiply": 0x55,
    "NumpadSubtract": 0x56, "NumpadAdd": 0x57,
    "NumpadEnter": 0x58, "NumpadDecimal": 0x63,
    "Numpad0": 0x62, "Numpad1": 0x59, "Numpad2": 0x5A,
    "Numpad3": 0x5B, "Numpad4": 0x5C, "Numpad5": 0x5D,
    "Numpad6": 0x5E, "Numpad7": 0x5F, "Numpad8": 0x60,
    "Numpad9": 0x61,

    # ISO extra key (left of Z on non-US keyboards)
    "IntlBackslash": 0x64,

    # JIS-specific
    "IntlRo": 0x87,       # International1 (ろ / _\)
    "IntlYen": 0x89,      # International3 (¥|)
    "KanaMode": 0x88,     # Katakana/Hiragana
    "Convert": 0x8A,      # 変換
    "NonConvert": 0x8B,    # 無変換
    "Lang1": 0x90,
    "Lang2": 0x91,

    # Context menu
    "ContextMenu": 0x65,
}

# Modifier codes → bitmask
_JS_MOD_BITS: dict[str, int] = {
    "ShiftLeft": 0x02, "ShiftRight": 0x20,
    "ControlLeft": 0x01, "ControlRight": 0x10,
    "AltLeft": 0x04, "AltRight": 0x40,
    "MetaLeft": 0x08, "MetaRight": 0x80,
}


# ---------------------------------------------------------------------------
# PWA assets (manifest, service worker, icon)
# ---------------------------------------------------------------------------

_MANIFEST_JSON = json.dumps({
    "name": "serial-hid-kvm",
    "short_name": "KVM",
    "description": "Remote KVM via CH9329 USB HID + HDMI capture",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#1a1a2e",
    "theme_color": "#16213e",
    "icons": [
        {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"},
    ],
}, separators=(",", ":"))

# Minimal service worker — just enough for PWA installability, no caching.
_SW_JS = """\
self.addEventListener("fetch", (e) => e.respondWith(fetch(e.request)));
"""

# Simple SVG icon: monitor with "KVM" label
_ICON_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="64" fill="#16213e"/>
<rect x="56" y="80" width="400" height="260" rx="16" fill="#0a0a1a" stroke="#0f3460" stroke-width="12"/>
<rect x="200" y="360" width="112" height="24" rx="4" fill="#0f3460"/>
<rect x="160" y="392" width="192" height="16" rx="8" fill="#0f3460"/>
<text x="256" y="240" text-anchor="middle" font-family="system-ui,sans-serif" font-weight="bold" font-size="96" fill="#e0e0e0">KVM</text>
</svg>
"""

# ---------------------------------------------------------------------------
# Embedded HTML/JS viewer
# ---------------------------------------------------------------------------

_VIEWER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#16213e">
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<title>serial-hid-kvm</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100%;overflow:hidden;background:#1a1a2e;font-family:system-ui,sans-serif;color:#e0e0e0}
#toolbar{display:flex;align-items:center;gap:8px;padding:4px 12px;background:#16213e;height:40px;user-select:none;position:relative;z-index:10;flex-shrink:0}
#toolbar button{background:#0f3460;border:1px solid #1a1a5e;color:#e0e0e0;padding:4px 12px;border-radius:4px;cursor:pointer;font-size:13px}
#toolbar button:hover{background:#1a4a8a}
#toolbar button:active{background:#0a2a50}
#toolbar select{background:#0f3460;border:1px solid #1a1a5e;color:#e0e0e0;padding:4px 6px;border-radius:4px;cursor:pointer;font-size:13px}
#status{margin-left:auto;font-size:12px;color:#8888aa}
#fps{font-size:12px;color:#8888aa;min-width:70px;text-align:right}
#container{display:flex;align-items:center;justify-content:center;width:100%;height:calc(100% - 40px);background:#0a0a1a;overflow:auto;cursor:none;outline:none}
#screen,#video{display:block;image-rendering:auto;background:#0a0a1a}
#video{object-fit:fill}
#resolution{font-size:12px;white-space:nowrap}
#resolution.mismatch{color:#ffcf70}
#toolbar button.active{background:#2a6a2a;border-color:#3a8a3a}
#btnRec.recording{background:#7a1a1a;border-color:#c03030;animation:recpulse 1.5s ease-in-out infinite}
@keyframes recpulse{0%,100%{opacity:1}50%{opacity:.55}}
body.fs-autohide #toolbar{position:fixed;top:0;z-index:10;transform:translateY(-100%);transition:transform .3s ease;pointer-events:none;border-radius:0 0 8px 8px;box-shadow:0 2px 12px rgba(0,0,0,.5);cursor:grab}
body.fs-autohide #toolbar.visible{transform:translateY(0);pointer-events:auto}
body.fs-autohide #toolbar.dragging{cursor:grabbing;transition:none}
body.fs-autohide #container{height:100%}
body.tb-hidden #toolbar{display:none}
body.tb-hidden #container{height:100%}
#hint{position:fixed;top:8px;left:50%;transform:translateX(-50%);background:rgba(22,33,62,.92);border:1px solid #0f3460;color:#e0e0e0;padding:4px 12px;border-radius:6px;font-size:12px;z-index:20;opacity:0;transition:opacity .4s;pointer-events:none;white-space:nowrap}
#hint.show{opacity:1}
#login{position:fixed;inset:0;background:rgba(10,10,26,.9);display:none;align-items:center;justify-content:center;z-index:30}
#login.show{display:flex}
#login form{background:#16213e;border:1px solid #0f3460;border-radius:10px;padding:28px 32px;display:flex;flex-direction:column;gap:12px;min-width:280px}
#login h1{font-size:16px;font-weight:600;text-align:center}
#loginPw{background:#0a0a1a;border:1px solid #0f3460;border-radius:4px;color:#e0e0e0;padding:8px 10px;font-size:14px;outline:none}
#loginPw:focus{border-color:#1a4a8a}
#login button{background:#0f3460;border:1px solid #1a1a5e;color:#e0e0e0;padding:8px 12px;border-radius:4px;cursor:pointer;font-size:14px}
#login button:hover{background:#1a4a8a}
#loginErr{color:#e07070;font-size:12px;min-height:14px}
#login label{font-size:12px;color:#8888aa;display:flex;align-items:center;gap:6px;user-select:none}

[hidden]{display:none!important}
#toolbar{gap:6px;padding:6px 10px}
#toolbar button,#toolbar select,.tool-group>summary{font:13px system-ui,sans-serif;height:28px;border-radius:5px;white-space:nowrap}
#toolbar button{padding:3px 9px;flex-shrink:0}
#toolbar button:disabled{opacity:.45;cursor:default}
.tool-group{position:relative;flex-shrink:0}
.tool-group>summary{display:flex;align-items:center;gap:8px;padding:3px 9px;cursor:pointer;list-style:none;color:#d7deeb}
.tool-group>summary::-webkit-details-marker{display:none}
.tool-group>summary:hover,.tool-group[open]>summary{background:#263b5c}
.tool-group>summary:focus-visible,button:focus-visible,select:focus-visible,a:focus-visible{outline:2px solid #8fb9ff;outline-offset:2px}
.tool-panel{position:absolute;top:34px;left:0;width:280px;padding:14px;background:#18263b;border:1px solid #384b66;border-radius:9px;box-shadow:0 10px 30px #0008;cursor:default;max-height:calc(100dvh - 64px);overflow:auto}
.tool-panel h3{font-size:12px;font-weight:600;color:#b7c6dc;margin:0 0 10px}
.tool-panel h3:not(:first-child){margin-top:18px;border-top:1px solid #384b66;padding-top:12px}
.tool-panel label{display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:12px;margin:10px 0}
#toolbar .tool-panel select{min-width:0;max-width:190px;width:65%}
.tool-panel p{font-size:12px;color:#aebcd0;line-height:1.5;margin:8px 0;overflow-wrap:anywhere}
.panel-row{display:flex;align-items:center;flex-wrap:wrap;gap:6px}
.tool-divider{height:18px;width:1px;background:#384b66;flex-shrink:0;margin:0 3px}
.record-controls{display:flex;align-items:center;gap:1px;flex-shrink:0}
#btnRec{min-width:100px;font-variant-numeric:tabular-nums}
.record-controls>button{border-radius:5px 0 0 5px!important}
#recordMenu>summary{background:#0f3460;border:1px solid #1a1a5e;border-radius:0 5px 5px 0;padding:3px 7px}
.record-panel{width:min(420px,calc(100vw - 24px));left:auto;right:0}
.record-panel.server-mode .browser-only{display:none!important}
.record-panel .rec-server-note{font-size:12px;opacity:.8;margin:4px 0 8px}
.rec-channel{border-top:1px solid #384b66;padding-top:10px;margin-top:12px}
.rec-channel[hidden]{display:none}
.rec-channel-title{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:12px}
.rec-channel-state{color:#aebcd0;font-size:11px;overflow-wrap:anywhere}
.rec-channel-state.interrupted{color:#ffb48c}
.record-panel input[type=range]{width:100%;accent-color:#80b7ff}
.record-panel input[type=checkbox]{accent-color:#80b7ff}
.record-panel .rec-mute{display:flex;gap:5px;margin:0;white-space:nowrap}
.rec-level{display:flex;align-items:center;gap:10px;color:#aebcd0;font-size:11px}
.rec-level meter{flex:1;min-width:40px;height:12px}
.hdmi-monitor-controls{display:flex;align-items:center;gap:7px;flex-shrink:0}
.hdmi-monitor-volume{display:flex;align-items:center;gap:7px;position:relative}
#recMonitorGain{width:85px;display:block;accent-color:#80b7ff}
#recMonitorGainValue{font-size:12px;min-width:32px;color:#b7c6dc;font-variant-numeric:tabular-nums}
#btnMonitorVolume{display:none}
#btnHdmiMonitor[aria-pressed=false]{color:#9daec5}
#btnHdmiMonitor.monitor-error{border-color:#e2a96d;color:#ffd3a0}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
@media(max-width:900px){#btnMonitorVolume{display:block}#recMonitorGainValue{display:none}#hdmiVolumePanel{display:none;position:absolute;top:34px;right:0;padding:14px;background:#18263b;border:1px solid #384b66;border-radius:7px;box-shadow:0 10px 30px #0008;z-index:12}#hdmiVolumePanel.open{display:block}#recMonitorGain{width:140px}}
.status-group{margin-left:auto;min-width:0;flex-shrink:1;overflow:visible}
.status-group>summary{min-width:0;max-width:300px}
#status{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin:0}
#targetName{font-size:12px;color:#bccbe0;white-space:nowrap}
.status-panel{left:auto;right:0;width:330px;display:flex;flex-direction:column;gap:8px}
#fps{text-align:left;min-width:0}
#resolution{white-space:normal;overflow-wrap:anywhere}
#toolbar button.active{background:#235277;border-color:#5598ce}
#btnRec.recording{background:#8b2632!important;border-color:#d65c69!important;animation:none}
#recordingResult{position:fixed;z-index:21;top:50px;right:12px;width:min(380px,calc(100vw - 24px));padding:16px;background:#18263b;border:1px solid #45617d;border-radius:10px;box-shadow:0 10px 30px #0008;font-size:13px}
body:has(#toolbar details[open]) #recordingResult{display:none}
#recordingResult strong{display:block;padding-right:20px;color:#a6dfb8}
#recordingResultName{margin:10px 0 6px;overflow-wrap:anywhere}
#recordingResult p{font-size:12px;line-height:1.5;color:#b7c6dc;margin-bottom:12px}
.card-close{position:absolute;right:8px;top:8px;border:0;background:none;color:#bccbe0;font-size:20px;cursor:pointer}
.file-actions{display:flex;flex-wrap:wrap;gap:6px}
.file-actions a,.file-actions button{font:12px system-ui,sans-serif;color:#e0e9f7;background:#28466a;border:1px solid #45617d;border-radius:5px;padding:5px 8px;text-decoration:none;cursor:pointer}
.recent-file{padding:10px 0;border-bottom:1px solid #384b66}
.recent-file strong{display:block;font-size:12px;overflow-wrap:anywhere;margin-bottom:5px}
.recent-file small{display:block;color:#aebcd0;margin-bottom:8px}
body.fs-autohide #toolbar{max-width:100vw;overflow:visible}
@media(max-width:850px){#targetName{display:none}.status-group>summary{max-width:160px}.record-panel{position:fixed;left:auto;right:8px;top:46px}}
@media(max-width:600px){#toolbar{padding:6px;gap:3px}.status-group{display:none}.tool-divider{margin:0 1px}.tool-group>summary[data-compact]{font-size:0;padding:3px 7px}.tool-group>summary[data-compact]::after{content:attr(data-compact);font-size:16px}#toolbar #btnFs{font-size:0;padding:3px 8px}#btnFs::after{content:"⛶";font-size:17px}#toolbar #btnAudio{font-size:0;padding:3px 7px}#btnAudio::after{content:"♫";font-size:16px}.tool-panel,.record-panel{position:fixed;top:46px;left:8px;right:8px;width:auto;max-width:400px}}
@media(max-width:600px){body{display:flex;flex-direction:column}#toolbar{flex-wrap:wrap;height:auto;min-height:40px}#container{flex:1;min-height:0}.hdmi-monitor-name{display:none}.hdmi-monitor-controls,.hdmi-monitor-volume{gap:3px}#hdmiVolumePanel{position:fixed;top:82px;left:8px;right:auto}#recordingResult{top:auto;bottom:12px}}
.cc-controls{display:flex;align-items:center;gap:1px;flex-shrink:0}
.cc-controls>button{border-radius:5px 0 0 5px!important;min-width:38px}
#ccMenu>summary{background:#0f3460;border:1px solid #1a1a5e;border-radius:0 5px 5px 0;padding:3px 7px}
#toolbar #btnCC{display:inline-flex;align-items:center;gap:5px;font-weight:600;background:transparent;border:1px solid #4a5a72;color:#8796ad}
#toolbar #btnCC .cc-dot{width:8px;height:8px;border-radius:50%;background:currentColor;opacity:.45}
#toolbar #btnCC.cc-on{background:#1d9e75!important;border-color:#5dcaa5!important;color:#fff}
#toolbar #btnCC.cc-on .cc-dot{opacity:1;background:#fff;animation:ccPulse 1.6s infinite}
#toolbar #btnCC.cc-wait{background:#633806!important;border-color:#ef9f27!important;color:#fac775}
#toolbar #btnCC.cc-wait .cc-dot{opacity:1;background:#ef9f27;animation:ccPulse .8s infinite}
#toolbar #btnCC:disabled{border-style:dashed;color:#5f6b7d;opacity:1}
@keyframes ccPulse{0%,100%{opacity:1}50%{opacity:.3}}
.cc-pill{font-size:11px;padding:1px 8px;border-radius:10px;background:#2a3446;color:#8fa6c4;white-space:nowrap;margin-right:auto}
.cc-pill.on{background:#085041;color:#9fe1cb}
.cc-pill.wait{background:#633806;color:#fac775}
#livePoints header strong{margin-right:0}
.cc-panel{width:290px}
#ccOverlay{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);width:max-content;max-width:min(92vw,1100px);z-index:15;pointer-events:none;display:flex;flex-direction:column;align-items:center;gap:4px;transition:opacity .5s}
#ccOverlay.idle{opacity:0}
#ccLines{display:flex;flex-direction:column;justify-content:flex-end;overflow:hidden;max-height:calc(2.9em + 8px);background:rgba(0,0,0,.74);color:#fff;font-size:var(--cc-size,24px);line-height:1.45;padding:4px 14px;border-radius:6px;text-align:center;text-shadow:0 1px 2px #000}
#ccLines:empty{display:none}
#ccLines .interim{opacity:.7}
#ccNotice{font-size:12px;background:rgba(70,36,0,.88);color:#ffd3a0;padding:2px 10px;border-radius:5px}
#ccNotice:empty{display:none}
:root{--side-w:0px;--tb-h:40px;--bar-h:0px}
#container{width:calc(100% - var(--side-w));height:calc(100% - var(--tb-h) - var(--bar-h));align-items:flex-start;justify-content:flex-start}
body.fs-autohide #container,body.tb-hidden #container{height:calc(100% - var(--bar-h))}
#ccOverlay{left:calc((100vw - var(--side-w)) / 2);max-width:min(calc((100vw - var(--side-w)) * .92),1100px)}
#recordingResult{right:calc(var(--side-w) + 12px)}
#meetingBar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;width:calc(100% - var(--side-w));padding:5px 12px;background:#3a2c10;border-bottom:1px solid #8a6a2a;color:#ffe2a8;font-size:13px}
#meetingBar[hidden]{display:none}
#meetingBar button{font:12px system-ui,sans-serif;color:#fff;background:#8b2632;border:1px solid #d65c69;border-radius:5px;padding:3px 9px;cursor:pointer}
#meetingBar button+button{background:transparent;border-color:#8a6a2a;color:#ffe2a8}
#sidePanel{position:fixed;top:var(--tb-h);right:0;bottom:0;width:var(--side-w);z-index:9;display:flex;flex-direction:column;background:#141f31;border-left:1px solid #384b66;font-size:13px;min-width:0}
#sidePanel[hidden]{display:none}
#sideResize{position:absolute;left:-4px;top:0;bottom:0;width:8px;cursor:ew-resize;z-index:2;touch-action:none}
#sideResize:hover,#sideResize.dragging{background:#5598ce55}
.side-section{display:flex;flex-direction:column;min-height:0;flex:1 1 0;overflow:hidden}
.side-section.folded{flex:0 0 auto!important}
.side-section.folded>.side-body{display:none}
.side-section header{display:flex;align-items:center;gap:6px;padding:7px 10px;border-bottom:1px solid #384b66;flex-wrap:wrap;background:#18263b}
.side-section header strong{margin-right:auto;font-size:13px}
.side-section button,.side-section select{font:12px system-ui,sans-serif;color:#e0e9f7;background:#28466a;border:1px solid #45617d;border-radius:5px;padding:3px 7px;cursor:pointer}
.side-section .side-fold{background:transparent;border:0;padding:0 2px;font-size:13px;width:18px}
.side-section.folded .side-fold{transform:rotate(-90deg)}
.side-body{flex:1;overflow:auto;min-height:0}
#sideSplit{flex:0 0 7px;cursor:ns-resize;background:#22324a;border-top:1px solid #384b66;border-bottom:1px solid #384b66;touch-action:none}
#sideSplit:hover,#sideSplit.dragging{background:#2f5578}
#sidePanel.split-off #sideSplit{display:none}
#ccHistoryList{padding:8px 12px;user-select:text;cursor:text;line-height:1.55}
#ccHistoryList p{margin:0 0 6px}
#ccHistoryList time{color:#8fa6c4;font-variant-numeric:tabular-nums;margin-right:8px;font-size:12px}
#livePoints .side-body{padding:8px 12px;user-select:text;line-height:1.55}
.ls-actions{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:6px}
#lsMeta{color:#8fa6c4;font-size:12px;margin:2px 0 8px}
#lsMeta.failed{color:#ffb48c}
#lsMeta.final{color:#a6dfb8}
#lsRetry{margin:-4px 0 8px}
#lsContent h4{font-size:12px;color:#b7c6dc;margin:10px 0 4px;font-weight:600}
#lsContent ul{margin:0 0 4px 18px;padding:0}
#lsContent li{margin:2px 0}
#lsContent .ls-topic{font-size:14px;color:#fff}
#lsContent .ls-new{background:rgba(255,200,97,.14);border-left:3px solid #ffc861;padding-left:5px;margin-left:-8px;list-style-position:inside}
#lsContent .ls-new-list li{color:#ffe2a8}
#btnCC{position:relative}
#btnCC.has-badge::after{content:"";position:absolute;top:2px;right:2px;width:8px;height:8px;border-radius:50%;background:#ffb020;box-shadow:0 0 0 2px #16213e}
.minutes-line{display:flex;flex-wrap:wrap;align-items:center;gap:6px;font-size:12px;color:#b7c6dc;margin:6px 0 8px}
.minutes-line a,.minutes-line button{font:12px system-ui,sans-serif;color:#e0e9f7;background:#28466a;border:1px solid #45617d;border-radius:5px;padding:3px 7px;text-decoration:none;cursor:pointer}
.minutes-line .minutes-failed{color:#ffb48c;overflow-wrap:anywhere}
#recentMinutes .recent-file strong{font-weight:500}
@media(max-width:600px){#toolbar #btnCC{padding:3px 6px}#toolbar #btnCC .cc-word{display:none}#ccOverlay{bottom:12px}#sidePanel{top:auto;height:60vh;width:100%;border-left:0;border-top:1px solid #384b66;z-index:22}#sideResize{display:none}}
</style>
</head>
<body>
<div id="toolbar">
  <button id="btnFs" title="Toggle fullscreen">Fullscreen</button>
  <button id="btnScale" aria-pressed="false" title="Toggle 1:1 / Fit scaling">1:1</button>
  <details class="tool-group" id="keyboardMenu">
    <summary title="Keyboard shortcuts" data-compact="⌨">Keyboard</summary>
    <div class="tool-panel"><h3>Send to target</h3>
      <button id="btnCad" title="Send Ctrl+Alt+Delete to target">Ctrl+Alt+Del</button>
      <button id="btnAltTab" title="Send Alt+Tab to target">Alt+Tab</button>
    </div>
  </details>
  <details class="tool-group">
    <summary title="Display settings" data-compact="▣">Display</summary>
    <div class="tool-panel"><h3>Display</h3>
      <div class="panel-row"><button id="btnCursor" title="Toggle local cursor visibility">Cursor</button>
      </div>
      <label>Proportions <select id="displayAspect" aria-label="Display aspect ratio" title="Display proportion correction; does not change capture resolution">
        <option value="source">Source ratio</option><option value="3:2">Correct to 3:2</option>
      </select></label>
      <label>Size <select id="displayZoom" aria-label="Display size" title="KVM picture size; the 1:1 / Fit button toggles between Fit and the chosen size">
        <option value="fit">Fit to available area</option><option value="0.5">50%</option><option value="0.67">67%</option><option value="0.75">75%</option><option value="0.9">90%</option><option value="1">100% (1:1)</option><option value="1.1">110%</option><option value="1.25">125%</option><option value="1.5">150%</option>
      </select></label>
    </div>
  </details>
  <details class="tool-group">
    <summary title="Video mode and quality" data-compact="▶">Video</summary>
    <div class="tool-panel"><h3>Video mode</h3>
      <p>Default: H264 stream. Toggle it off for the JPEG stream.</p>
      <div class="panel-row"><button id="btnRtc" title="H264 streaming; capture stays available to OCR/MCP">H264</button>
      <button id="btnDirect" title="Browser opens the local capture card directly">Direct</button></div>
      <label>H264 quality <select id="rtcQuality" aria-label="H264 quality">
        <option value="auto">Auto</option><option value="16000000/60">16M/60</option>
        <option value="8000000/30">8M/30</option><option value="4000000/30">4M/30</option><option value="2000000/30">2M/30</option>
      </select></label>
    </div>
  </details>
  <span class="tool-divider"></span>
  <button id="btnAudio" title="Toggle audio playback" style="display:none">&#x1f507; Audio</button>
  <div class="cc-controls">
    <button id="btnCC" type="button" aria-pressed="false" title="Live captions" disabled><span class="cc-dot" aria-hidden="true"></span>CC<span class="cc-word">关</span></button>
    <details class="tool-group" id="ccMenu">
      <summary title="Caption settings and history" aria-label="Caption settings and history">▾</summary>
      <div class="tool-panel cc-panel"><h3>实时字幕</h3>
        <p id="ccState" role="status">字幕关闭</p>
        <label>字号 <select id="ccSize" aria-label="Caption font size">
          <option value="16">16 px</option><option value="20">20 px</option><option value="24">24 px</option><option value="32">32 px</option><option value="40">40 px</option>
        </select></label>
        <div class="panel-row"><button id="btnCcHistory" type="button" title="右侧面板：字幕历史 + 会议要点">面板</button><button id="btnCcCopy" type="button">复制</button><button id="btnCcDownload" type="button">下载 .txt</button></div>
        <p>只识别 Target 的 HDMI 声音，由局域网内的 kvm-ai-hub 生成，仅供参考。录制时同时保存为 .live.vtt。</p>
      </div>
    </details>
  </div>
  <div class="record-controls">
    <button id="btnRec" title="Start or stop recording">&#x23fa; Record</button>
    <details class="tool-group" id="recordMenu">
      <summary title="Recording settings and recent files" aria-label="Recording settings and recent files">▾</summary>
      <div class="tool-panel record-panel"><h3>Recording</h3>
        <label>方式 <select id="recMode" aria-label="Recording method" title="录制中不能切换">
          <option value="server">服务端录制（推荐）</option><option value="browser">浏览器录制（备用）</option>
        </select></label>
        <label id="recFpsRow">帧率 <select id="recFps" aria-label="Recording frame rate">
          <option value="15">15 fps</option><option value="30">30 fps</option>
        </select></label>
        <p class="rec-server-note" id="recServerNote">服务端录制：服务器直接录 Host 的麦克风（EarPods）和 Target 的 HDMI 声音。关闭或刷新页面不会停止录制，最长 3 小时。</p>
        <h3>Recording audio</h3>
        <label>Source <select id="recAudioSource" aria-label="Recording audio source" title="Stop recording before changing audio source">
          <option value="mic">Host Mic</option><option value="hdmi">Target HDMI</option><option value="mix">Host Mic + Target HDMI</option><option value="none">No audio</option>
        </select></label>
        <section class="rec-channel browser-only" id="recMicChannel" aria-label="Host Mic recording input">
          <div class="rec-channel-title"><strong>Host Mic · My voice</strong><span id="recMicState" class="rec-channel-state">Not checked</span></div>
          <label>Device <select id="recAudioDevice" aria-label="Host microphone device"></select></label>
          <div class="rec-channel-title"><label for="recMicGain">Record volume <output id="recMicGainValue">100%</output></label><label class="rec-mute"><input id="recMicMute" type="checkbox">Mute</label></div>
          <input id="recMicGain" type="range" min="0" max="200" value="100" aria-label="Host Mic recording volume">
          <div class="rec-level">Input level <meter id="recAudioLevel" aria-label="Host Mic input level" min="0" max="1" value="0"></meter></div>
        </section>
        <section class="rec-channel browser-only" id="recHdmiChannel" aria-label="Target HDMI recording input" hidden>
          <div class="rec-channel-title"><strong>Target HDMI · Meeting audio</strong><span id="recHdmiState" class="rec-channel-state">Not checked</span></div>
          <label>Device <select id="recHdmiDevice" aria-label="Target HDMI audio device"></select></label>
          <div class="rec-channel-title"><label for="recHdmiGain">Record volume <output id="recHdmiGainValue">100%</output></label><label class="rec-mute"><input id="recHdmiMute" type="checkbox">Mute</label></div>
          <input id="recHdmiGain" type="range" min="0" max="200" value="100" aria-label="Target HDMI recording volume">
          <div class="rec-level">Input level <meter id="recHdmiLevel" aria-label="Target HDMI input level" min="0" max="1" value="0"></meter></div>
        </section>
        <div class="panel-row browser-only"><button id="btnRecInputs" title="Allow audio access and refresh devices">Inputs</button>
        <button id="btnRecCheck" title="Check audio without recording">Check audio</button></div>
        <p id="recAudioStatus" class="browser-only" role="status"></p>
        <label id="recMinutesRow">会议记录 <select id="recMinutes" aria-label="Meeting minutes summarizer" title="录制结束后自动转写并生成会议记录（录制开始时确定），也用于会议要点。服务端设置，所有观看者共用。敏感会议请选 Mac Qwen 或关闭：内容不出局域网。">
          <option value="agy">agy（默认）</option><option value="codex">codex</option><option value="mac-qwen">Mac Qwen（局域网内）</option><option value="off">关闭</option>
        </select></label>
        <h3>Recent recordings <button id="refreshRecordings" title="Refresh recent recordings">↻</button></h3>
        <div id="recentRecordings" aria-live="polite">Connect to view recordings.</div>
        <h3 id="minutesHeading" hidden>会议记录</h3>
        <div id="recentMinutes" hidden></div>
        <h3 id="notesHeading" hidden>会议记录（字幕生成·无录像）</h3>
        <div id="meetingNotes" hidden></div>
        <p id="recordingActionStatus" role="status"></p>
      </div>
    </details>
  </div>
  <div class="hdmi-monitor-controls" id="hdmiMonitorControls" role="group" aria-label="HDMI listening">
    <button id="btnHdmiMonitor" type="button" aria-pressed="false" aria-label="Listen to Target HDMI" title="Listen to Target HDMI through Host default playback device. Does not change recorded audio.">🎧<span class="hdmi-monitor-name"> HDMI</span></button>
    <div class="hdmi-monitor-volume">
      <button id="btnMonitorVolume" type="button" aria-expanded="false" aria-controls="hdmiVolumePanel" aria-label="Show HDMI listen volume">50%</button>
      <div id="hdmiVolumePanel"><label class="sr-only" for="recMonitorGain">Listen volume</label><input id="recMonitorGain" type="range" min="0" max="100" value="50" aria-label="HDMI headphone monitoring volume" title="HDMI listen volume — does not change recorded audio"></div>
      <output id="recMonitorGainValue" for="recMonitorGain">50%</output>
    </div>
    <span id="hdmiMonitorStatus" class="sr-only" role="status" aria-live="polite">HDMI listening off</span>
  </div>
  <details class="tool-group status-group">
    <summary title="Connection and capture details"><span id="status">Connecting…</span><span id="targetName"></span></summary>
    <div class="tool-panel status-panel"><h3>Stream details</h3><span id="fps"></span><span id="resolution"></span></div>
  </details>
</div>
<aside id="recordingResult" hidden aria-label="Recording saved" aria-live="polite">
  <button id="closeRecordingResult" class="card-close" aria-label="Close recording result">×</button>
  <strong id="recordingResultTitle">Recording saved</strong>
  <div id="recordingResultName"></div><p id="recordingResultState"></p>
  <div id="recordingResultMinutes" hidden></div>
  <div id="recordingResultActions" class="file-actions"></div>
</aside>
<div id="meetingBar" hidden role="status"><span>检测到会议进行中，要开始录制吗？</span><button id="meetingBarRecord" type="button">开始录制</button><button id="meetingBarIgnore" type="button">本次忽略</button></div>
<div id="container" tabindex="0"><canvas id="screen"></canvas><video id="video" playsinline muted style="display:none"></video></div>
<div id="ccOverlay" class="idle" aria-live="polite"><div id="ccNotice"></div><div id="ccLines"></div></div>
<aside id="sidePanel" hidden aria-label="Captions and meeting points">
  <div id="sideResize" role="separator" aria-orientation="vertical" aria-label="Resize panel" title="拖动调整面板宽度"></div>
  <section id="ccHistory" class="side-section" aria-label="Caption history">
    <header><button class="side-fold" type="button" data-section="ccHistory" aria-expanded="true" title="折叠/展开">▾</button><strong>字幕历史</strong><button id="ccHistoryCopy" type="button">复制</button><button id="ccHistoryDownload" type="button">下载 .txt</button><button id="ccHistoryClose" type="button" aria-label="Close side panel" title="关闭面板">×</button></header>
    <div id="ccHistoryList" class="side-body"><p>还没有字幕。</p></div>
  </section>
  <div id="sideSplit" role="separator" aria-orientation="horizontal" aria-label="Resize sections" title="拖动调整上下比例"></div>
  <section id="livePoints" class="side-section" aria-label="Meeting points">
    <header><button class="side-fold" type="button" data-section="livePoints" aria-expanded="true" title="折叠/展开">▾</button><strong>会议要点</strong><span id="lsCcPill" class="cc-pill" role="status">字幕关闭</span>
      <select id="lsInterval" aria-label="Automatic summary interval" title="自动总结间隔（服务端设置，所有观看者共用；每次新会议重置为关闭）"><option value="0">自动：关闭</option><option value="3">每 3 分钟</option><option value="5">每 5 分钟</option><option value="10">每 10 分钟</option></select>
      <button id="lsRun" type="button" title="用「会议记录」里选择的方式立即总结">立即总结</button></header>
    <div class="side-body">
      <div class="ls-actions"><button id="lsNewMeeting" type="button" title="结束这次会议，下次字幕开始新的会议">新会议</button><button id="lsMinutes" type="button" title="用这次会议的字幕生成会议记录（只包含对方的声音）">生成会议记录</button><button id="lsCopy" type="button">复制</button><button id="lsExport" type="button">导出 .md</button></div>
      <p id="lsMeta">打开 CC 后，用「立即总结」或「自动」生成会议要点。</p>
      <button id="lsRetry" type="button" hidden title="重新生成这次的要点">重试</button>
      <div id="lsMinutesLine"></div>
      <div id="lsContent"></div>
    </div>
  </section>
</aside>
<div id="hint"></div>
<div id="login"><form id="loginForm">
  <h1>serial-hid-kvm</h1>
  <input id="loginPw" type="password" placeholder="Password" autocomplete="current-password">
  <label><input id="loginRemember" type="checkbox"> Remember on this device</label>
  <div id="loginErr"></div>
  <button type="submit">Connect</button>
</form></div>
<script>
"use strict";
const canvas = document.getElementById("screen");
const ctx = canvas.getContext("2d");
const statusEl = document.getElementById("status");
const fpsEl = document.getElementById("fps");
const container = document.getElementById("container");
const toolbar = document.getElementById("toolbar");
const video = document.getElementById("video");
const hint = document.getElementById("hint");
document.getElementById("btnFs").hidden =
  new URLSearchParams(location.search).has("kiosk");

let toolbarHidden = false;   // toolbar starts visible; Ctrl+Alt+Enter toggles it
let hintTimer = null;

let ws = null;
let frameCount = 0;
let lastFpsTime = performance.now();

// LAN = this page reaches the server via a private/loopback address.
// Several behaviours differ for remote (WAN) viewers — jitter buffer,
// frame acks, audio default — and the server makes the same LAN/WAN call
// from its side using the connection's source address.
const IS_LAN = isPrivateHost(location.hostname);
let adaptiveAck = false;   // server asked us to ack frames (hello.adaptive)

// Direct (native) video mode: the browser opens the capture card itself via
// getUserMedia and renders a real <video> element — same path the official app
// uses — instead of decoding server JPEG frames onto the canvas.
let videoMode = false;
let directStream = null;       // active getUserMedia MediaStream in direct mode
let serverCaptureLabel = "";   // capture-device name reported by the server

// WebRTC (H264) mode: the server encodes the capture as H.264 and streams it
// over a local RTCPeerConnection; the browser renders a native <video>.
// Unlike Direct mode the server KEEPS the capture device, so server-side
// OCR / MCP / capture_frame keep working while the viewer streams.
let rtcMode = false;
// H264 is the default mode; the first hello starts it (?rtc=0 keeps JPEG).
let autoRtcPending = new URLSearchParams(location.search).get("rtc") !== "0";
let rtcPc = null;              // active RTCPeerConnection
let _rtcWait = null;           // {res, rej} while an offer awaits its answer
let _rtcGen = 0;               // negotiation generation: pairs answers with
                               // offers so a late answer for an abandoned
                               // offer can't corrupt a newer connection

function wsSend(obj) {  // control messages, not gated by view-only
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

// --- Authentication (server started with --web-password) ---
// The server gates everything behind an "auth" message on the WebSocket.
// A successful password login yields a reconnect token, stored per-tab in
// sessionStorage (plus localStorage with "Remember") so reconnects and tab
// reloads re-authenticate silently.  Tokens die with the server process;
// when one expires the login overlay reappears.
const login = document.getElementById("login");
const loginPw = document.getElementById("loginPw");
const loginErr = document.getElementById("loginErr");
const loginRemember = document.getElementById("loginRemember");
let triedToken = false;   // one silent token attempt per connection

function storedToken() {
  return sessionStorage.getItem("kvmToken") || localStorage.getItem("kvmToken");
}
function storeToken(t) {
  sessionStorage.setItem("kvmToken", t);
  if (loginRemember.checked) localStorage.setItem("kvmToken", t);
}
function clearToken() {
  sessionStorage.removeItem("kvmToken");
  localStorage.removeItem("kvmToken");
}
function onAuthRequired() {
  statusEl.textContent = "Authentication required";
  const t = storedToken();
  if (t && !triedToken) {
    triedToken = true;
    wsSend({type: "auth", token: t});
  } else {
    login.classList.add("show");
    loginPw.focus();
  }
}
function onAuthOk(msg) {
  if (msg.token) storeToken(msg.token);
  login.classList.remove("show");
  loginErr.textContent = "";
  loginPw.value = "";
}
function onAuthFailed(msg) {
  if (msg.expired) clearToken();   // stale token → fall back to password
  else loginErr.textContent = msg.error || "Authentication failed";
  login.classList.add("show");
  loginPw.focus();
}
document.getElementById("loginForm").addEventListener("submit", (e) => {
  e.preventDefault();
  loginErr.textContent = "";
  wsSend({type: "auth", password: loginPw.value});
});

// --- Auto-reload when the server's frontend changed ---
// The server sends a fingerprint of its embedded HTML/JS on connect. We record
// the first one; if a later (reconnected) server reports a different build, the
// process was restarted with new code, so reload to pick it up. A plain restart
// keeps the same fingerprint and never reloads.
let _build = null;
function checkBuild(b) {
  if (!b) return;
  if (_build === null) { _build = b; return; }
  if (b !== _build) {
    if (recording || recFinishing || recStarting || recUpload) {
      showHint("Viewer update available — finish saving recording before refreshing");
      return;
    }
    location.reload();
  }
}

// --- WebSocket ---
function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(proto + "//" + location.host + "/ws");
  ws.binaryType = "arraybuffer";

  ws.onopen = () => {
    // Session setup (mode restore) happens on the server's "hello", which
    // arrives after authentication when a password is configured — anything
    // sent before that would be dropped by the server's auth gate.
    triedToken = false;
    statusEl.textContent = "Connecting…";
  };
  ws.onclose = (ev) => {
    const interrupted = recording || recFinishing;
    recordingConnectionLost();
    if (ccOn) ccSetState("connecting", "");
    statusEl.textContent = recording
      ? "Picture disconnected — recording continues locally; upload will resume"
      : recFinishing ? "Disconnected — recording stopped; waiting to upload"
      : "Disconnected — reconnecting…";
    console.warn("Viewer connection closed", ev.code, ev.reason);
    if (interrupted) showHint(statusEl.textContent);
    setTimeout(connect, 2000);
  };
  ws.onerror = () => { ws.close(); };
  ws.onmessage = (ev) => {
    if (ev.data instanceof ArrayBuffer) {
      const view = new Uint8Array(ev.data);
      const type = view[0];
      const payload = ev.data.slice(1);
      if (type === 1) {
        // Video frame (JPEG) — hand to the drop-stale renderer.  On the
        // WAN-adaptive stream, ack receipt so the server's credit pacing
        // knows what the link actually delivered.
        queueFrame(payload);
        if (adaptiveAck) wsSend({type: "frame_ack"});
      } else if (type === 2) {
        // Audio chunk (PCM int16 LE)
        feedAudio(payload);
      }
    } else {
      // Text message (JSON)
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "auth_required") { onAuthRequired(); }
        else if (msg.type === "auth_ok") { onAuthOk(msg); }
        else if (msg.type === "auth_failed") { onAuthFailed(msg); }
        else if (msg.type === "hello") {
          recConnectionReady = true;
          refreshRecordingLibrary();
          attachRecordingUpload();
          checkBuild(msg.build);
          if (msg.target_name) {
            document.title = msg.target_name + " · NanoKVM";
            document.getElementById("targetName").textContent = msg.target_name;
            setRecordingAudioTarget(msg.target_name);
          }
          if (msg.direct === false) {
            const directBtn = document.getElementById("btnDirect");
            directBtn.disabled = true;
            directBtn.title = "Direct is disabled for this device binding; use H264";
          }
          captureRequest = msg.capture_request || null;
          restoreDisplayAspect();
          applyCaptureStatus(msg.capture_status || null);
          updateResolutionStatus();
          adaptiveAck = !!msg.adaptive;
          minutesConfigure(msg.minutes || null);
          ccConfigure(msg.captions || null);
          srecConfigure(msg.server_recording || null);
          if (msg.webrtc === false) {
            const b = document.getElementById("btnRtc");
            b.disabled = true;
            b.title = "Server lacks aiortc (pip install serial-hid-kvm[webrtc])";
          }
          if (directStream) {
            statusEl.textContent = "Direct video (native)";
            // A reconnected server starts in stream mode; if we're showing
            // native video, tell it to release the capture device again.
            wsSend({type: "stream", on: false});
            recordingMediaReady();
          } else if (rtcMode) {
            // The server-side peer connection died with the old socket or
            // server — renegotiate a fresh one.
            restartRtc();
          } else {
            statusEl.textContent = "Connected";
          }
          if (autoRtcPending) { autoRtcPending = false; autoRtc(); }
          if (autoHdmiMonitorPending) { autoHdmiMonitorPending = false; autoStartHdmiMonitoring(); }
          // Direct video defaults OFF so the server keeps the capture device
          // and can share frames with server-side OCR / MCP / capture_frame
          // while the viewer is open.
        }
        else if (msg.type === "capture_status") { applyCaptureStatus(msg.info); }
        else if (msg.type === "audio_config") {
          setupAudioConfig(msg);
          updateRecordingDevices();
        }
        else if (msg.type === "capture_device") { serverCaptureLabel = msg.label || ""; }
        else if (msg.type === "webrtc_answer") {
          // Only apply the answer that matches the current offer; a late
          // answer for an abandoned offer must be dropped.
          if (rtcPc && msg.gen === _rtcGen &&
              rtcPc.signalingState === "have-local-offer") {
            rtcPc.setRemoteDescription({type: "answer", sdp: msg.sdp})
              .catch((e) => { if (_rtcWait) _rtcWait.rej(e); });
          }
        }
        else if (msg.type === "webrtc_error") {
          if (msg.gen !== undefined && msg.gen !== _rtcGen) { /* stale */ }
          else if (_rtcWait) _rtcWait.rej(new Error(msg.error));
          else {
            statusEl.textContent = "WebRTC error: " + msg.error;
            if (toolbarHidden) showHint("WebRTC error: " + msg.error);
          }
        }
        else if (msg.type === "rec_session_saved" && msg.server) {
          recordingFileSaved(msg);
          statusEl.textContent = "Recording saved · MP4 processing";
        }
        else if (["rec_session_ready", "rec_ack", "rec_session_saved", "rec_session_error"].includes(msg.type)) {
          recordingUploadEvent(msg);
        }
        else if (msg.type === "srec_state") { srecApply(msg); }
        else if (msg.type === "srec_error") {
          srecBusy = false;
          statusEl.textContent = "录制没有开始：" + msg.error;
          showHint(statusEl.textContent);
        }
        else if (msg.type === "srec_notice") { statusEl.textContent = msg.message; showHint(msg.message); }
        else if (msg.type === "caption") { ccCaption(msg); }
        else if (msg.type === "captions_state") { ccSetState(msg.state, msg.detail || "", msg.on, msg.since); }
        else if (msg.type === "live_summary") { onLiveSummary(msg); }
        else if (msg.type === "minutes_action") {
          if (msg.error) showHint("会议记录：" + msg.error); else showHint(msg.message || "已加入会议记录队列");
          refreshRecordingLibrary();
        }
        else if (msg.type === "recordings_list") { renderRecordingLibrary(msg); }
        else if (msg.type === "recordings_action") {
          document.getElementById("recordingActionStatus").textContent = msg.error || msg.message;
          if (msg.error) showHint(msg.error);
        }
        else if (msg.type === "rec_saved") {
          recordingFileSaved(msg);
          statusEl.textContent = "Recording saved · MP4 processing";
        }
        else if (msg.type === "rec_exported") {
          recordingFileUpdated(msg);
          statusEl.textContent = "MP4 ready";
        }
        else if (msg.type === "rec_export_error") {
          recordingFileUpdated(msg);
          statusEl.textContent = "MP4 failed; WebM kept: " + msg.error;
          showHint(statusEl.textContent);
        }
        else if (msg.type === "rec_error") {
          if (recording) stopRecording();
          statusEl.textContent = "Recording error: " + msg.error;
          if (toolbarHidden) showHint("Recording error: " + msg.error);
        }
      } catch(e) {}
    }
  };
}

// --- FPS counter ---
setInterval(() => {
  const now = performance.now();
  const elapsed = (now - lastFpsTime) / 1000;
  const fps = frameCount / elapsed;
  frameCount = 0;
  lastFpsTime = now;
  // <video> renders itself in direct/webrtc mode, so there's no per-frame count.
  fpsEl.textContent = videoMode ? (rtcMode ? "h264" : "native")
                                : fps.toFixed(1) + " fps";
}, 2000);

// --- Video frame rendering (decode off-thread, always draw the freshest) ---
let _pendingFrame = null;   // newest received-but-undrawn JPEG ArrayBuffer
let _decoding = false;
function queueFrame(buf) {
  _pendingFrame = buf;       // keep only the most recent frame
  if (!_decoding) renderLoop();
}
async function renderLoop() {
  _decoding = true;
  while (_pendingFrame) {
    const buf = _pendingFrame;
    _pendingFrame = null;    // anything that arrives during decode replaces this
    try {
      const bmp = await createImageBitmap(new Blob([buf], {type: "image/jpeg"}));
      if (canvas.width !== bmp.width || canvas.height !== bmp.height) {
        canvas.width = bmp.width;
        canvas.height = bmp.height;
        updateCanvasSize();
      }
      ctx.drawImage(bmp, 0, 0);
      bmp.close();
      frameCount++;
      if (!videoMode) recordingMediaReady();
    } catch (e) { /* skip undecodable frame */ }
  }
  _decoding = false;
}

// --- Display sizing (works for both the canvas and the direct <video>) ---
let scaleMode = "native";  // "native" = 1:1 pixel (default), "fit" = fit to window
let displayZoom = 1;       // size factor in "native" mode (Display > Size); 1 = 1:1
let captureRequest = null;
let captureStatus = null;
let aspectManuallySelected = false;
const displayAspect = document.getElementById("displayAspect");

function applyCaptureStatus(info) {
  captureStatus = info;
  // Use negotiated device dimensions, not auto-cropped frame dimensions.
  if (!aspectManuallySelected && captureRequest &&
      captureRequest.width === 1920 && captureRequest.height === 1280 &&
      info && info.status === "ready" && info.width > 0 && info.height > 0 &&
      (info.width !== 1920 || info.height !== 1280)) {
    displayAspect.value = "3:2";
  }
  updateCanvasSize();
}

function displayAspectKey() {
  if (!captureRequest) return null;
  return "serial-hid-kvm-display-aspect:" + captureRequest.width + "x" + captureRequest.height;
}

function restoreDisplayAspect() {
  const key = displayAspectKey();
  if (!key) return;
  try {
    const saved = localStorage.getItem(key);
    displayAspect.value = saved === "3:2" ? "3:2" : "source";
  } catch (_) { /* Storage may be unavailable in a restricted browser. */ }
  updateCanvasSize();
}

function updateResolutionStatus() {
  const el = document.getElementById("resolution");
  const w = mediaW(), h = mediaH();
  const requested = captureRequest && captureRequest.width && captureRequest.height;
  const mismatch = !!(requested && w && h &&
    (w !== captureRequest.width || h !== captureRequest.height));
  const parts = [];
  if (requested) parts.push("Request " + captureRequest.width + "×" + captureRequest.height);
  if (captureStatus && captureStatus.status === "ready") {
    parts.push("Capture " + captureStatus.width + "×" + captureStatus.height);
  }
  if (w && h && (!captureStatus || captureStatus.status !== "ready" ||
      w !== captureStatus.width || h !== captureStatus.height)) {
    parts.push("Frame " + w + "×" + h);
  }
  el.textContent = parts.join(" · ") + (mismatch ? " ⚠" : "");
  el.classList.toggle("mismatch", mismatch);
  el.title = "Requested capture size, negotiated capture size and received frame size (after any auto-crop)." +
    (mismatch ? " Dimensions differ; check device support and auto-crop." : "");
}

function activeEl() { return videoMode ? video : canvas; }
function mediaW() { return videoMode ? video.videoWidth : canvas.width; }
function mediaH() { return videoMode ? video.videoHeight : canvas.height; }

function updateCanvasSize() {
  const el = activeEl();
  const mw = mediaW(), mh = mediaH();
  updateResolutionStatus();
  const btn = document.getElementById("btnScale");
  btn.classList.toggle("active", scaleMode === "fit");
  btn.setAttribute("aria-pressed", String(scaleMode === "fit"));
  if (!mw || !mh) return;
  const ar = displayAspect.value === "3:2" ? 3 / 2 : mw / mh;
  btn.textContent = scaleMode === "fit" ? "Fit" : displayZoom !== 1 ? Math.round(displayZoom * 100) + "%" :
    (displayAspect.value === "source" ? "1:1" : "Width 1:1");
  // The element box IS the rendered picture (canvas stretches, video uses
  // object-fit: fill), so mouseCoords() stays exact for any size or position.
  if (scaleMode === "fit") {
    const cw = container.clientWidth;
    const ch = container.clientHeight;
    if (!cw || !ch) return;
    let dw, dh;
    if (cw / ch > ar) { dh = ch; dw = ch * ar; }
    else { dw = cw; dh = cw / ar; }
    el.style.width = dw + "px";
    el.style.height = dh + "px";
  } else {
    el.style.width = (mw * displayZoom) + "px";
    el.style.height = (mw / ar * displayZoom) + "px";
  }
}
window.addEventListener("resize", updateCanvasSize);
video.addEventListener("loadedmetadata", () => { if (videoMode) updateCanvasSize(); });
video.addEventListener("resize", () => { if (videoMode) updateCanvasSize(); });
displayAspect.addEventListener("change", () => {
  aspectManuallySelected = true;
  try {
    const key = displayAspectKey();
    if (key) localStorage.setItem(key, displayAspect.value);
  } catch (_) { /* Correction still works without persistent storage. */ }
  updateCanvasSize();
  container.focus();
});

// --- Mouse coordinate normalisation (0-4095) ---
function mouseCoords(e) {
  const rect = activeEl().getBoundingClientRect();
  if (!rect.width || !rect.height) return {x: 0, y: 0};
  const fx = (e.clientX - rect.left) / rect.width;
  const fy = (e.clientY - rect.top) / rect.height;
  const x = Math.max(0, Math.min(4095, Math.round(fx * 4095)));
  const y = Math.max(0, Math.min(4095, Math.round(fy * 4095)));
  return {x, y};
}

let viewOnly = false;
let showCursor = true;   // local cursor visible by default

function send(obj) {
  if (viewOnly) return;
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

// --- Mouse events ---
// Throttle moves to ~60Hz, but with a trailing send so the *final* position is
// always delivered — otherwise the target cursor settles a few pixels off from
// the host cursor whenever the last move falls inside the throttle window.
let lastMouseSend = 0;
let pendingMouse = null;
let mouseTrailTimer = null;
function flushMouse() {
  if (!pendingMouse) return;
  send({type:"mousemove", x: pendingMouse.x, y: pendingMouse.y,
        buttons: pendingMouse.buttons});
  pendingMouse = null;
  lastMouseSend = performance.now();
}
container.addEventListener("mousemove", (e) => {
  const {x, y} = mouseCoords(e);
  pendingMouse = {x, y, buttons: e.buttons};
  const dt = performance.now() - lastMouseSend;
  if (dt >= 16) {
    if (mouseTrailTimer) { clearTimeout(mouseTrailTimer); mouseTrailTimer = null; }
    flushMouse();
  } else if (!mouseTrailTimer) {
    mouseTrailTimer = setTimeout(() => { mouseTrailTimer = null; flushMouse(); },
                                 16 - dt);
  }
});
container.addEventListener("mousedown", (e) => {
  e.preventDefault();
  container.focus();
  const {x, y} = mouseCoords(e);
  send({type:"mousedown", x, y, buttons: e.buttons});
});
container.addEventListener("mouseup", (e) => {
  e.preventDefault();
  const {x, y} = mouseCoords(e);
  send({type:"mouseup", x, y, buttons: e.buttons});
});
container.addEventListener("wheel", (e) => {
  e.preventDefault();
  const dy = e.deltaY > 0 ? -3 : 3;
  send({type:"scroll", deltaY: dy});
}, {passive: false});
container.addEventListener("contextmenu", (e) => e.preventDefault());

// --- Keyboard events ---
container.setAttribute("tabindex", "0");
container.addEventListener("keydown", (e) => {
  if (viewOnly) return;
  e.preventDefault();
  e.stopPropagation();
  if (e.repeat) return;
  send({type:"keydown", code: e.code});
});
container.addEventListener("keyup", (e) => {
  if (viewOnly) return;
  e.preventDefault();
  e.stopPropagation();
  send({type:"keyup", code: e.code});
});
container.addEventListener("blur", () => { send({type:"release_all"}); });

// --- Toolbar ---
document.getElementById("btnCad").addEventListener("click", () => {
  send({type:"keydown", code:"ControlLeft"});
  send({type:"keydown", code:"AltLeft"});
  send({type:"keydown", code:"Delete"});
  setTimeout(() => {
    send({type:"keyup", code:"Delete"});
    send({type:"keyup", code:"AltLeft"});
    send({type:"keyup", code:"ControlLeft"});
  }, 100);
  container.focus();
});
// document.getElementById("btnViewOnly").addEventListener("click", () => {
//   const btn = document.getElementById("btnViewOnly");
//   viewOnly = !viewOnly;
//   btn.classList.toggle("active", viewOnly);
//   if (viewOnly) showCursor = true;
//   updateCursor();
// });
document.getElementById("btnCursor").addEventListener("click", () => {
  const btn = document.getElementById("btnCursor");
  showCursor = !showCursor;
  btn.classList.toggle("active", showCursor);
  updateCursor();
  container.focus();
});
function updateCursor() {
  container.style.cursor = showCursor ? "default" : "none";
  document.getElementById("btnCursor").classList.toggle("active", showCursor);
}
// Display size: Fit, or a fixed factor of the source width (1 = 1:1). The
// 1:1/Fit button toggles between Fit and the last fixed size. Remembered.
const displayZoomSelect = document.getElementById("displayZoom");
let lastFixedZoom = 1;
function applyDisplaySize(value, remember) {
  if (value === "fit") scaleMode = "fit";
  else {
    const zoom = parseFloat(value);
    displayZoom = zoom > 0.2 && zoom <= 4 ? zoom : 1;
    lastFixedZoom = displayZoom;
    scaleMode = "native";
  }
  displayZoomSelect.value = scaleMode === "fit" ? "fit" : String(displayZoom);
  if (remember) { try { localStorage.setItem("kvmDisplaySize", displayZoomSelect.value); } catch (e) {} }
  updateCanvasSize();
}
try {
  const saved = localStorage.getItem("kvmDisplaySize");
  if (saved && [...displayZoomSelect.options].some(o => o.value === saved)) applyDisplaySize(saved, false);
} catch (e) {}
displayZoomSelect.addEventListener("change", () => { applyDisplaySize(displayZoomSelect.value, true); container.focus(); });
document.getElementById("btnScale").addEventListener("click", () => {
  applyDisplaySize(scaleMode === "native" ? "fit" : String(lastFixedZoom), true);
  container.focus();
});
// Fit follows the available area: side panel, reminder bar, toolbar wrap.
if (window.ResizeObserver) new ResizeObserver(() => updateCanvasSize()).observe(container);
document.getElementById("btnAltTab").addEventListener("click", () => {
  send({type:"keydown", code:"AltLeft"});
  send({type:"keydown", code:"Tab"});
  setTimeout(() => {
    send({type:"keyup", code:"Tab"});
    send({type:"keyup", code:"AltLeft"});
  }, 100);
  container.focus();
});
// document.getElementById("btnIme").addEventListener("click", () => {
//   // Toggle target IME via 半角/全角 (Backquote, HID 0x35). Synthesised in JS,
//   // so it bypasses the controller's own IME, which swallows the physical key.
//   send({type:"keydown", code:"Backquote"});
//   setTimeout(() => { send({type:"keyup", code:"Backquote"}); }, 100);
//   container.focus();
// });
// document.getElementById("btnCaps").addEventListener("click", () => {
//   // Japanese (JIS) keyboards toggle Caps Lock with Shift+CapsLock(英数).
//   send({type:"keydown", code:"ShiftLeft"});
//   send({type:"keydown", code:"CapsLock"});
//   setTimeout(() => {
//     send({type:"keyup", code:"CapsLock"});
//     send({type:"keyup", code:"ShiftLeft"});
//   }, 100);
//   container.focus();
// });
document.getElementById("btnFs").addEventListener("click", () => {
  if (!document.fullscreenElement) document.documentElement.requestFullscreen();
  else document.exitFullscreen();
  container.focus();
});
// Fullscreen: keyboard lock + toolbar auto-hide
let _tbLeft = 0;
let _tbWidth = 0;
function _tbCenter() {
  _tbWidth = toolbar.offsetWidth;
  _tbLeft = (window.innerWidth - _tbWidth) / 2;
  toolbar.style.left = _tbLeft + "px";
}
document.addEventListener("fullscreenchange", () => {
  const isFs = !!document.fullscreenElement;
  document.body.classList.toggle("fs-autohide", isFs);
  if (isFs) {
    if (navigator.keyboard && navigator.keyboard.lock)
      navigator.keyboard.lock(["AltLeft","AltRight","Tab","MetaLeft","MetaRight","Escape"]).catch(() => {});
    requestAnimationFrame(_tbCenter);
  } else {
    toolbar.classList.remove("visible");
    toolbar.style.left = "";
  }
  updateCanvasSize();
});
window.addEventListener("resize", () => { if (document.fullscreenElement) _tbCenter(); });

// Auto-show/hide toolbar in fullscreen when mouse nears center-top
let _tbTimer = null;
function _tbShow() { toolbar.classList.add("visible"); clearTimeout(_tbTimer); }
function _tbScheduleHide() {
  clearTimeout(_tbTimer);
  _tbTimer = setTimeout(() => (toolbar.querySelector("details[open]") ? _tbScheduleHide() : toolbar.classList.remove("visible")), 1500);
}
document.addEventListener("mousemove", (e) => {
  if (!document.body.classList.contains("fs-autohide")) return;
  if (_tbDragging) return;
  const pad = 80;
  const inX = e.clientX >= _tbLeft - pad && e.clientX <= _tbLeft + _tbWidth + pad;
  if (inX && e.clientY < 50) _tbShow();
  else if (toolbar.classList.contains("visible")) _tbScheduleHide();
});
toolbar.addEventListener("mouseenter", () => { if (document.body.classList.contains("fs-autohide")) _tbShow(); });
toolbar.addEventListener("mouseleave", () => { if (document.body.classList.contains("fs-autohide") && !_tbDragging) _tbScheduleHide(); });

// Drag toolbar horizontally
let _tbDragging = false, _tbDragX0 = 0, _tbDragL0 = 0;
toolbar.addEventListener("mousedown", (e) => {
  if (!document.body.classList.contains("fs-autohide")) return;
  if (e.target !== toolbar) return;
  _tbDragging = true;
  _tbDragX0 = e.clientX;
  _tbDragL0 = _tbLeft;
  toolbar.classList.add("dragging");
  e.preventDefault();
});
document.addEventListener("mousemove", (e) => {
  if (!_tbDragging) return;
  _tbLeft = Math.max(0, Math.min(window.innerWidth - _tbWidth, _tbDragL0 + e.clientX - _tbDragX0));
  toolbar.style.left = _tbLeft + "px";
});
document.addEventListener("mouseup", () => {
  if (!_tbDragging) return;
  _tbDragging = false;
  toolbar.classList.remove("dragging");
});

// --- Audio playback ---
let audioCtx = null;
let audioNode = null;
let audioCfg = null;
let playGain = null;   // controls speaker playback volume (mute = 0)
let recDest = null;    // MediaStreamDestination feeding the recorder
let audioOn = false;   // listening starts only from the toolbar HDMI button

const WORKLET_SRC = `
class P extends AudioWorkletProcessor {
  constructor() {
    super();
    this.b = new Float32Array(0);
    this.port.onmessage = e => {
      const o = this.b, n = e.data;
      const m = new Float32Array(o.length + n.length);
      m.set(o); m.set(n, o.length);
      this.b = m;
    };
  }
  process(ins, outs) {
    const out = outs[0], ch = out.length, fr = out[0].length;
    const need = fr * ch;
    if (this.b.length >= need) {
      for (let i = 0; i < fr; i++)
        for (let c = 0; c < ch; c++)
          out[c][i] = this.b[i * ch + c];
      this.b = this.b.slice(need);
    }
    return true;
  }
}
registerProcessor("p", P);
`;

function setupAudioConfig(cfg) {
  audioCfg = cfg;
  const btn = document.getElementById("btnAudio");
  // Keep the legacy control hidden: the toolbar HDMI listener is the only
  // playback owner. A recording subscription must not implicitly play audio.
  btn.style.display = "none";
  audioOn = false;
  syncLegacyAudioPlayback();
}

// Start speaker playback and reflect the on-state on the button. Browsers block
// AudioContext until a user gesture, so if it's still suspended we resume on the
// first interaction (this is best-effort; recording is unaffected either way).
let _audioArmed = false;
async function enableAudioPlayback() {
  if (!audioCtx) await startAudio();
  if (audioCtx && audioCtx.state === "suspended") {
    try { await audioCtx.resume(); } catch (e) {}
  }
  if (audioCtx && audioCtx.state === "suspended" && !_audioArmed) {
    _audioArmed = true;
    const resume = () => {
      if (audioCtx && audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
      window.removeEventListener("pointerdown", resume, true);
      window.removeEventListener("keydown", resume, true);
      _audioArmed = false;
    };
    window.addEventListener("pointerdown", resume, true);
    window.addEventListener("keydown", resume, true);
  }
  audioOn = true;
  if (playGain) playGain.gain.value = 1;
  syncLegacyAudioPlayback();
  const btn = document.getElementById("btnAudio");
  btn.textContent = "\u{1f50a} Audio";
  btn.classList.add("active");
}

// Build the audio graph once.  audioNode → playGain → speakers (gain gates
// playback) and audioNode → recDest (always full-volume, for the recorder),
// so recording captures audio even when speaker playback is muted.
async function startAudio() {
  if (!audioCfg || audioCtx) return;
  audioCtx = new AudioContext({sampleRate: audioCfg.sampleRate});
  const blob = new Blob([WORKLET_SRC], {type: "application/javascript"});
  const url = URL.createObjectURL(blob);
  await audioCtx.audioWorklet.addModule(url);
  URL.revokeObjectURL(url);
  audioNode = new AudioWorkletNode(audioCtx, "p",
    {outputChannelCount: [audioCfg.channels]});
  playGain = audioCtx.createGain();
  playGain.gain.value = audioOn ? 1 : 0;
  audioNode.connect(playGain);
  playGain.connect(audioCtx.destination);
}

function feedAudio(buf) {
  if (!audioNode) return;
  const i16 = new Int16Array(buf);
  const f32 = new Float32Array(i16.length);
  for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768.0;
  audioNode.port.postMessage(f32, [f32.buffer]);
}

document.getElementById("btnAudio").addEventListener("click", async () => {
  const btn = document.getElementById("btnAudio");
  if (!audioCtx) await startAudio();
  if (audioCtx.state === "suspended") await audioCtx.resume();
  audioOn = !audioOn;
  if (playGain) playGain.gain.value = audioOn ? 1 : 0;
  // Remote viewers toggle the PCM feed itself so a muted stream costs no
  // uplink; while recording, the feed must keep flowing for the recorder
  // (LAN keeps the always-on feed, mute is just a local gain).
  if (!IS_LAN) {
    if (audioOn) wsSend({type: "audio", on: true});
    else if (!(recAudioSession?.hasHdmi || recAudioPreview?.hasHdmi))
      wsSend({type: "audio", on: false});
  }
  btn.textContent = (audioOn ? "\u{1f50a}" : "\u{1f507}") + " Audio";
  btn.classList.toggle("active", audioOn);
  container.focus();
});

// --- Toolbar panels and recording library ---
let recentRecordingItems = [];
let resultRecordingId = null;
let resultRecordingPending = false;
let localRecordingActions = false;
let recordingLibraryTimer = null;
function refreshRecordingLibrary() { wsSend({type:"recordings_list"}); }
function closeToolbarPanels() {
  toolbar.querySelectorAll("details[open]").forEach(d => d.open = false);
}
toolbar.querySelectorAll("details").forEach(detail => {
  detail.addEventListener("toggle", () => {
    if (!detail.open) return;
    toolbar.querySelectorAll("details[open]").forEach(other => { if (other !== detail) other.open = false; });
    if (detail.id === "recordMenu") refreshRecordingLibrary();
    if (document.fullscreenElement) _tbShow();
  });
});
document.addEventListener("pointerdown", e => { if (!toolbar.contains(e.target)) closeToolbarPanels(); });
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && toolbar.querySelector("details[open]")) {
    e.preventDefault(); e.stopPropagation(); closeToolbarPanels(); container.focus();
  }
}, true);
document.getElementById("refreshRecordings").addEventListener("click", refreshRecordingLibrary);
document.getElementById("closeRecordingResult").addEventListener("click", () => {
  document.getElementById("recordingResult").hidden = true;
  resultRecordingId = null; resultRecordingPending = false;
});
function recordingFileActions(item) {
  const actions = document.createElement("div"); actions.className = "file-actions";
  const play = document.createElement("a"); play.textContent = "Play";
    play.href = item.url; play.target = "_blank"; play.rel = "noopener noreferrer"; actions.append(play);
  const download = document.createElement("a"); download.textContent = "Download";
  download.href = item.url + "&download=1"; download.download = item.name; actions.append(download);
  if (localRecordingActions) for (const [action, label] of [["open","Open file"],["reveal","Show in folder"]]) {
    const button = document.createElement("button"); button.textContent = label;
    button.addEventListener("click", () => wsSend({type:"recordings_open", id:item.id, action}));
    actions.append(button);
  }
  return actions;
}
function renderRecordingLibrary(msg) {
  recentRecordingItems = msg.items; localRecordingActions = !!msg.local;
  clearTimeout(recordingLibraryTimer);
  if (msg.items.some(item => item.state.includes("processing") || minutesActive(item)) ||
      (msg.meeting_notes || []).some(minutesActive))
    recordingLibraryTimer = setTimeout(refreshRecordingLibrary, 3000);
  if (msg.minutes_enabled !== undefined) minutesEnabled = !!msg.minutes_enabled;
  const list = document.getElementById("recentRecordings"); list.replaceChildren();
  if (!msg.items.length) list.textContent = "No saved recordings yet.";
  for (const item of msg.items) {
    const row = document.createElement("div"); row.className = "recent-file";
    const name = document.createElement("strong"); name.textContent = item.name; row.append(name);
    const info = document.createElement("small");
    info.textContent = new Date(item.modified * 1000).toLocaleString() + " · " + (item.size/1048576).toFixed(1) + " MB · " + item.state;
    row.append(info);
    const minutes = minutesLine(item);
    if (minutes) row.append(minutes);
    row.append(recordingFileActions(item)); list.append(row);
  }
  renderMinutesList(msg);
  renderMeetingNotes(msg);
  const item = msg.items.find(item => item.id === resultRecordingId);
  if (item) {
    const card = document.getElementById("recordingResult");
    const actions = document.getElementById("recordingResultActions");
    document.getElementById("recordingResultName").textContent = item.name;
    document.getElementById("recordingResultState").textContent = item.state;
    const resultMinutes = document.getElementById("recordingResultMinutes");
    const line = minutesLine(item);
    resultMinutes.replaceChildren(...(line ? [line] : []));
    resultMinutes.hidden = !line;
    actions.replaceChildren(recordingFileActions(item));
    if (resultRecordingPending) { card.hidden = false; resultRecordingPending = false; }
  }
}
function recordingFileSaved(msg) {
  closeToolbarPanels();
  // Only completion of a recording brings up the card; background updates do not reopen a dismissed card.
  resultRecordingId = msg.recording_id || resultRecordingId;
  resultRecordingPending = true;
  refreshRecordingLibrary();
}
function recordingFileUpdated(msg) { refreshRecordingLibrary(); }

// --- Meeting minutes (hub transcription + agy/codex/Mac Qwen summary) ---
// The server queues minutes when a recording session finishes, using the
// choice sent with rec_session_start. Status rides on recordings_list items.
const recMinutesSelect = document.getElementById("recMinutes");
const MINUTES_ACTIVE = ["queued", "extracting", "transcribing", "summarizing"];
let minutesEnabled = false;
function minutesConfigure(info) {
  minutesEnabled = !!(info && info.enabled);
  document.getElementById("recMinutesRow").hidden = !minutesEnabled;
  recMinutesSelect.disabled = !minutesEnabled;
  minutesShow((info && info.default) || "agy");
}
// Server-wide (shared by every viewer): the server echoes the choice back.
function minutesShow(choice) {
  if ([...recMinutesSelect.options].some(o => o.value === choice)) recMinutesSelect.value = choice;
}
function minutesActive(item) { return !!(item.minutes && MINUTES_ACTIVE.includes(item.minutes.state)); }
function minutesLink(label, href) {
  const a = document.createElement("a"); a.textContent = label; a.href = href;
  a.target = "_blank"; a.rel = "noopener noreferrer"; return a;
}
function minutesLine(item) {
  if (!minutesEnabled) return null;
  const m = item.minutes;
  const box = document.createElement("div"); box.className = "minutes-line";
  const label = document.createElement("span");
  if (!m) {
    if (recMinutesSelect.value === "off") return null;
    const make = document.createElement("button"); make.type = "button"; make.textContent = "生成会议记录";
    make.title = "用 " + recMinutesSelect.selectedOptions[0].textContent + " 转写并总结这段录像";
    make.addEventListener("click", () => wsSend({type:"minutes_retry", id:item.id, summarizer:recMinutesSelect.value}));
    box.append(make);
    return box;
  }
  label.textContent = "会议记录：" + m.label;
  box.append(label);
  if (m.state === "failed" && m.error) {
    const why = document.createElement("span"); why.className = "minutes-failed";
    why.textContent = m.error.length > 120 ? m.error.slice(0, 120) + "…" : m.error;
    why.title = m.error; box.append(why);
  }
  const links = m.links || {};
  if (links.minutes) box.append(minutesLink("会议记录", links.minutes));
  if (links.minutes_md) box.append(minutesLink(".md", links.minutes_md + "&download=1"));
  if (links.transcript) box.append(minutesLink("转写稿", links.transcript));
  if (links.live_vtt) box.append(minutesLink("实时字幕", links.live_vtt));
  if (m.state === "failed" || m.state === "done") {
    const retry = document.createElement("button"); retry.type = "button";
    retry.textContent = m.state === "failed" ? "重试" : "重新总结";
    retry.title = m.state === "failed" ? "从失败的步骤继续" : "用当前选择的方式重新总结（沿用转写稿）";
    retry.addEventListener("click", () => {
      const choice = recMinutesSelect.value;
      wsSend({type:"minutes_retry", id:item.id, summarizer: m.state === "done" && choice !== "off" ? choice : null});
    });
    box.append(retry);
  }
  return box;
}
function renderMinutesList(msg) {
  // With the SOP library enabled the plain recordings list is hidden, so
  // minutes get their own list; otherwise they appear inline per recording.
  const heading = document.getElementById("minutesHeading");
  const list = document.getElementById("recentMinutes");
  const show = minutesEnabled && !!msg.sop_enabled;
  heading.hidden = list.hidden = !show;
  list.replaceChildren();
  if (!show) return;
  const items = msg.items.filter(item => item.minutes);
  if (!items.length) { list.textContent = "暂无会议记录。"; return; }
  for (const item of items) {
    const row = document.createElement("div"); row.className = "recent-file";
    const name = document.createElement("strong"); name.textContent = item.name;
    row.append(name, minutesLine(item)); list.append(row);
  }
}

function renderMeetingNotes(msg) {
  // Minutes generated from live captions only (no recording); remote voice only.
  const notes = msg.meeting_notes || [];
  const heading = document.getElementById("notesHeading");
  const list = document.getElementById("meetingNotes");
  heading.hidden = list.hidden = !(minutesEnabled && notes.length);
  list.replaceChildren();
  for (const note of notes) {
    const row = document.createElement("div"); row.className = "recent-file";
    const name = document.createElement("strong");
    name.textContent = note.started_at ? "会议 " + new Date(note.started_at * 1000).toLocaleString() : note.name;
    const info = document.createElement("small"); info.textContent = "只包含对方（Target HDMI）的声音 · " + note.name;
    row.append(name, info);
    const line = minutesLine(note);
    if (line) row.append(line);
    list.append(row);
  }
}

// --- Live captions (Target HDMI audio -> kvm-ai-hub on the LAN) ---
// CC is one switch on the server, shared by every viewer: this page only
// shows it and asks the server to flip it. The server sends plain caption
// text, so this works for WAN viewers too.
const ccBtn = document.getElementById("btnCC");
const ccOverlay = document.getElementById("ccOverlay");
const ccLines = document.getElementById("ccLines");
const ccNotice = document.getElementById("ccNotice");
const ccStateEl = document.getElementById("ccState");
const ccHistoryList = document.getElementById("ccHistoryList");
const ccSizeSelect = document.getElementById("ccSize");
const ccPill = document.getElementById("lsCcPill");
const CC_STATE_TEXT = {off:"字幕关闭", connecting:"正在连接 kvm-ai-hub…", live:"字幕识别中",
  offline:"kvm-ai-hub 离线，正在重连", unavailable:"字幕不可用"};
const ccHistory = [];
const ccSeen = new Set();
let ccRecent = [];
let ccAvailable = false, ccState = "off", ccHideTimer = null;
let ccOn = false, ccSince = null;
try { ccSizeSelect.value = localStorage.getItem("kvmCaptionSize") || "24"; } catch (e) { ccSizeSelect.value = "24"; }
if (!ccSizeSelect.value) ccSizeSelect.value = "24";
ccOverlay.style.setProperty("--cc-size", ccSizeSelect.value + "px");
ccSizeSelect.addEventListener("change", () => {
  ccOverlay.style.setProperty("--cc-size", ccSizeSelect.value + "px");
  try { localStorage.setItem("kvmCaptionSize", ccSizeSelect.value); } catch (e) {}
  ccShowOverlay();
});
function ccRenderButton() {
  const live = ccOn && ccState === "live", wait = ccOn && !live;
  const word = !ccAvailable ? "不可用" : live ? "开" : !ccOn ? "关" : ccState === "offline" ? "离线" : "连接中";
  ccBtn.setAttribute("aria-pressed", String(ccOn));
  ccBtn.classList.toggle("cc-on", live);
  ccBtn.classList.toggle("cc-wait", wait);
  ccBtn.querySelector(".cc-word").textContent = word;
  ccBtn.setAttribute("aria-label", "CC " + word);
  const since = ccSince ? " · " + ccClock(ccSince * 1000).slice(0, 5) + " 起" : "";
  ccPill.textContent = !ccAvailable ? "字幕不可用" : live ? "● 字幕开启" + since
    : ccOn ? "● 字幕" + word + since : "字幕关闭";
  ccPill.className = "cc-pill" + (live ? " on" : wait ? " wait" : "");
}
function ccConfigure(info) {
  ccAvailable = !!(info && info.available);
  ccBtn.disabled = !ccAvailable;
  ccBtn.title = ccAvailable ? "实时字幕（Target HDMI 声音 → kvm-ai-hub）· 所有观看者共用一个开关"
    : "字幕不可用：" + ((info && info.reason) || "服务端未提供");
  if (!ccAvailable) { ccOn = false; ccSetState("unavailable", (info && info.reason) || ""); return; }
  ccApply(!!info.on, info.since || null);
  ccSetState(ccOn ? "connecting" : "off", "");
}
// The server's switch: on/off and since when (epoch seconds).
function ccApply(on, since) {
  const wasOn = ccOn;
  ccOn = on; ccSince = on ? since : null;
  if (wasOn && !on) { ccRecent = []; ccLines.replaceChildren(); ccOverlay.classList.add("idle"); meetingCheck(); }
}
function ccSetState(state, detail, on, since) {
  if (on !== undefined) ccApply(!!on, since || null);
  ccState = state;
  ccStateEl.textContent = (CC_STATE_TEXT[state] || state) + (detail ? "：" + detail : "");
  let notice = "";
  if (ccOn && state === "offline") notice = "字幕：kvm-ai-hub 离线，正在重连…" + (detail ? "（" + detail + "）" : "");
  else if (ccOn && state === "unavailable") notice = "字幕不可用" + (detail ? "：" + detail : "");
  ccNotice.textContent = notice;
  if (state === "unavailable") ccOn = false;
  ccRenderButton();
  if (notice) ccShowOverlay(); else if (!ccLines.childElementCount) ccOverlay.classList.add("idle");
}
function ccToggle(on) {
  // Shown at once; every viewer (this one too) then gets the server's state.
  wsSend({type:"captions", on});
  ccApply(on, on ? Date.now() / 1000 : null);
  ccSetState(on ? "connecting" : "off", "");
}
ccBtn.addEventListener("click", () => { if (ccAvailable) ccToggle(!ccOn); container.focus(); });
function ccShowOverlay() {
  ccOverlay.classList.remove("idle");
  clearTimeout(ccHideTimer);
  ccHideTimer = setTimeout(() => { if (!ccNotice.textContent) ccOverlay.classList.add("idle"); }, 10000);
}
function ccClock(ms) {
  const d = new Date(ms);
  return [d.getHours(), d.getMinutes(), d.getSeconds()].map(n => String(n).padStart(2, "0")).join(":");
}
function ccAppendHistory(entry) {
  if (ccHistory.length === 1) ccHistoryList.replaceChildren();
  const p = document.createElement("p");
  const t = document.createElement("time"); t.textContent = ccClock(entry.at);
  p.append(t, document.createTextNode(entry.text));
  const stick = ccHistoryList.scrollTop + ccHistoryList.clientHeight >= ccHistoryList.scrollHeight - 30;
  ccHistoryList.append(p);
  while (ccHistoryList.childElementCount > 2000) ccHistoryList.firstElementChild.remove();
  if (stick) ccHistoryList.scrollTop = ccHistoryList.scrollHeight;
}
function ccCaption(msg) {
  if (!ccOn || typeof msg.text !== "string") return;
  const final = msg.final !== false;
  if (final) {
    // Only finals enter the history; interim results are overlay-only.
    const key = msg.at + "|" + msg.text;
    if (ccSeen.has(key)) return;
    ccSeen.add(key);
    const entry = {at: msg.at || Date.now(), text: msg.text};
    ccHistory.push(entry);
    if (ccHistory.length > 5000) ccHistory.shift();
    ccAppendHistory(entry);
    if (!msg.replay) meetingFinal();
  }
  if (msg.replay) return;
  // Interim -> newer interim -> final of the same sentence (same seq) update
  // one line in place, so ~0.7 s interim updates never rebuild the overlay.
  const last = ccRecent[ccRecent.length - 1];
  const sameSeq = last && msg.seq != null && last.seq === msg.seq;
  if (last && (sameSeq || !last.final)) {
    if (sameSeq && last.final && !final) return;  // late interim after its final
    last.seq = msg.seq; last.final = final;
    if (last.el.textContent !== msg.text) last.el.textContent = msg.text;
    last.el.className = final ? "" : "interim";
  } else {
    const el = document.createElement("div"); el.textContent = msg.text;
    if (!final) el.className = "interim";
    ccLines.append(el);
    ccRecent.push({seq: msg.seq, final, el});
    while (ccRecent.length > 3) ccRecent.shift().el.remove();
  }
  ccShowOverlay();
}
function ccResetHistory() {
  ccHistory.length = 0; ccSeen.clear();
  ccHistoryList.replaceChildren(Object.assign(document.createElement("p"), {textContent: "还没有字幕。"}));
}
function ccHistoryText() { return ccHistory.map(h => ccClock(h.at) + "  " + h.text).join("\n") + "\n"; }
async function ccCopy() {
  if (!ccHistory.length) { showHint("还没有字幕"); return; }
  try { await navigator.clipboard.writeText(ccHistoryText()); showHint("已复制 " + ccHistory.length + " 行字幕"); }
  catch (e) { showHint("复制失败：" + e.message); }
}
function ccDownload() {
  if (!ccHistory.length) { showHint("还没有字幕"); return; }
  const stamp = new Date(Date.now() + 9*3600*1000).toISOString().slice(0, 16).replace(/[-:]/g, "").replace("T", "-");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([ccHistoryText()], {type: "text/plain;charset=utf-8"}));
  a.download = "captions-" + stamp + "-JST.txt";
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
}
for (const id of ["btnCcCopy", "ccHistoryCopy"]) document.getElementById(id).addEventListener("click", ccCopy);
for (const id of ["btnCcDownload", "ccHistoryDownload"]) document.getElementById(id).addEventListener("click", ccDownload);

// --- Docked side panel: caption history (top) + meeting points (bottom) ---
// Docked, not floating: #container loses the panel width (CSS --side-w), so
// the KVM picture is never covered. Mouse mapping is unaffected because
// mouseCoords() measures activeEl() itself at event time.
const sidePanel = document.getElementById("sidePanel");
const sideResize = document.getElementById("sideResize");
const sideSplit = document.getElementById("sideSplit");
const historySection = document.getElementById("ccHistory");
const pointsSection = document.getElementById("livePoints");
const meetingBar = document.getElementById("meetingBar");
const rootStyle = document.documentElement.style;
const narrowQuery = window.matchMedia("(max-width:600px)");
function sideLoad(key, fallback) {
  try { const v = localStorage.getItem(key); return v === null ? fallback : v; } catch (e) { return fallback; }
}
function sideStore(key, value) { try { localStorage.setItem(key, String(value)); } catch (e) {} }
let sideWidth = parseInt(sideLoad("kvmSideWidth", "420"), 10) || 420;
let sideRatio = parseFloat(sideLoad("kvmSideRatio", "0.5")) || 0.5;
function clampSideWidth(w) { return Math.max(260, Math.min(Math.round(window.innerWidth * 0.7), w)); }
function updateLayout() {
  const open = !sidePanel.hidden;
  rootStyle.setProperty("--side-w", open && !narrowQuery.matches ? clampSideWidth(sideWidth) + "px" : "0px");
  const overlayToolbar = document.body.classList.contains("fs-autohide") || document.body.classList.contains("tb-hidden");
  rootStyle.setProperty("--tb-h", overlayToolbar ? "0px" : toolbar.offsetHeight + "px");
  rootStyle.setProperty("--bar-h", meetingBar.hidden ? "0px" : meetingBar.offsetHeight + "px");
  updateCanvasSize();
}
window.addEventListener("resize", updateLayout);
narrowQuery.addEventListener("change", updateLayout);
document.addEventListener("fullscreenchange", () => requestAnimationFrame(updateLayout));
new MutationObserver(updateLayout).observe(document.body, {attributes: true, attributeFilter: ["class"]});
if (window.ResizeObserver) new ResizeObserver(updateLayout).observe(toolbar);
function setSidePanel(open) {
  sidePanel.hidden = !open;
  sideStore("kvmSideOpen", open ? "1" : "0");
  if (open) {
    if (!pointsSection.classList.contains("folded")) ccBtn.classList.remove("has-badge");
    ccHistoryList.scrollTop = ccHistoryList.scrollHeight;
  }
  updateLayout();
}
document.getElementById("btnCcHistory").addEventListener("click", () => { closeToolbarPanels(); setSidePanel(sidePanel.hidden); });
document.getElementById("ccHistoryClose").addEventListener("click", () => { setSidePanel(false); container.focus(); });
function dragHandle(handle, onMove, onDone) {
  handle.addEventListener("pointerdown", e => {
    e.preventDefault(); handle.setPointerCapture(e.pointerId); handle.classList.add("dragging");
  });
  handle.addEventListener("pointermove", e => { if (handle.hasPointerCapture(e.pointerId)) onMove(e); });
  const end = e => {
    if (!handle.classList.contains("dragging")) return;
    handle.classList.remove("dragging"); onDone();
  };
  handle.addEventListener("pointerup", end);
  handle.addEventListener("pointercancel", end);
}
dragHandle(sideResize, e => { sideWidth = clampSideWidth(window.innerWidth - e.clientX); updateLayout(); },
  () => sideStore("kvmSideWidth", sideWidth));
function applySplit() {
  const folded = historySection.classList.contains("folded") || pointsSection.classList.contains("folded");
  sidePanel.classList.toggle("split-off", folded);
  historySection.style.flex = sideRatio + " 1 0";
  pointsSection.style.flex = (1 - sideRatio) + " 1 0";
}
dragHandle(sideSplit, e => {
  const r = sidePanel.getBoundingClientRect();
  sideRatio = Math.max(0.12, Math.min(0.88, (e.clientY - r.top) / r.height)); applySplit();
}, () => sideStore("kvmSideRatio", sideRatio.toFixed(3)));
for (const fold of sidePanel.querySelectorAll(".side-fold")) {
  const section = document.getElementById(fold.dataset.section);
  const setFolded = folded => {
    section.classList.toggle("folded", folded);
    fold.setAttribute("aria-expanded", String(!folded));
    sideStore("kvmSideFold:" + section.id, folded ? "1" : "0");
    if (!folded && section === pointsSection && !sidePanel.hidden) ccBtn.classList.remove("has-badge");
    applySplit();
  };
  fold.addEventListener("click", () => setFolded(!section.classList.contains("folded")));
  setFolded(sideLoad("kvmSideFold:" + section.id, "0") === "1");
}
applySplit();
setSidePanel(sideLoad("kvmSideOpen", "0") === "1");

// --- Live meeting points (server summarizes final captions periodically) ---
const lsInterval = document.getElementById("lsInterval");
const lsRun = document.getElementById("lsRun");
const lsMeta = document.getElementById("lsMeta");
const lsContent = document.getElementById("lsContent");
const lsMinutesLine = document.getElementById("lsMinutesLine");
const LS_NAMES = {agy:"agy", codex:"codex", "mac-qwen":"Mac Qwen", none:"—"};
let lsLast = null;
function lsSummarizer() { return recMinutesSelect.value; }
recMinutesSelect.addEventListener("change", () =>
  wsSend({type:"live_summary", action:"set_summarizer", summarizer:lsSummarizer()}));
lsInterval.addEventListener("change", () =>
  wsSend({type:"live_summary", action:"set_interval", minutes:parseInt(lsInterval.value, 10), summarizer:lsSummarizer()}));
// With CC off this asks for the (final) summary of the meeting that just ended.
function lsRequestRun() { wsSend({type:"live_summary", action:"run", summarizer:lsSummarizer()}); }
lsRun.addEventListener("click", lsRequestRun);
document.getElementById("lsRetry").addEventListener("click", lsRequestRun);
document.getElementById("lsNewMeeting").addEventListener("click", () => {
  if (!ccOn) { showHint("请先打开 CC"); return; }
  if (!confirm("结束这次会议，从现在开始新的会议？（这次的字幕和要点会保存）")) return;
  wsSend({type:"captions_new_meeting"});
});
document.getElementById("lsMinutes").addEventListener("click", () => {
  if (!lsLast || !lsLast.session || lsLast.session.closed) { showHint("没有进行中的会议；请先打开 CC"); return; }
  if (recMinutesSelect.value === "off") { showHint("「会议记录」设置为关闭；请先在录制菜单里选择总结方式"); return; }
  wsSend({type:"live_summary", action:"minutes", summarizer:lsSummarizer()});
  showHint("已用这次会议的字幕排队生成会议记录（只包含对方的声音）");
});
function lsAgo(ms) {
  const minutes = Math.floor((Date.now() - ms) / 60000);
  return minutes < 1 ? "刚刚" : minutes + " 分钟前";
}
function lsMarkdown() {
  const d = lsLast && lsLast.data;
  if (!d) return "";
  const list = items => (items.length ? items.map(x => "- " + x) : ["—"]).join("\n");
  return "# 会议要点 · " + new Date(lsLast.at).toLocaleString() + "\n\n- 生成：" + (LS_NAMES[lsLast.used] || lsLast.used) +
    " · 只包含对方（Target HDMI）的声音\n\n## 现在的话题\n\n" + (d.current_topic || "—") +
    "\n\n## 要点\n\n" + list(d.points) + "\n\n## 决定\n\n" + list(d.decisions) +
    "\n\n## TODO\n\n" + list(d.todos.map(t => t.task + "（" + (t.owner || "未定") + " / " + (t.due || "未定") + "）")) +
    "\n\n## 未回答的问题\n\n" + list(d.open_questions) + "\n\n## 本次新增\n\n" + list(d.new_since_last) + "\n";
}
document.getElementById("lsCopy").addEventListener("click", async () => {
  const md = lsMarkdown();
  if (!md) { showHint("还没有要点"); return; }
  try { await navigator.clipboard.writeText(md); showHint("已复制会议要点"); } catch (e) { showHint("复制失败：" + e.message); }
});
document.getElementById("lsExport").addEventListener("click", () => {
  const md = lsMarkdown();
  if (!md) { showHint("还没有要点"); return; }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([md], {type: "text/markdown;charset=utf-8"}));
  a.download = "meeting-points-" + new Date(Date.now() + 9*3600*1000).toISOString().slice(0, 16).replace(/[-:]/g, "").replace("T", "-") + "-JST.md";
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
});
function lsRender() {
  const m = lsLast;
  if (!m) return;
  lsMeta.classList.toggle("failed", m.state === "failed");
  lsMeta.classList.toggle("final", m.state === "idle" && !!m.final && !!m.data);
  const finals = (m.session && m.session.finals) || 0;
  const hhmm = ms => { const d = new Date(ms); return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0"); };
  let meta;
  if (m.state === "running") meta = (m.final ? "最终总结生成中…（" : "正在总结…（") + (LS_NAMES[m.summarizer] || m.summarizer) + "）";
  else if (m.state === "failed") meta = (m.final ? "最终总结失败：" : "总结失败：") + (m.error || "unknown");
  else if (m.data && m.at && m.final) meta = "最终总结（" + hhmm(m.at) + " 完成 · " + (LS_NAMES[m.used] || m.used) + "）";
  else if (m.data && m.at) meta = lsAgo(m.at) + "更新 · " + (LS_NAMES[m.used] || m.used);
  else meta = finals ? "已收到 " + finals + " 句字幕，尚未总结" + (m.interval ? "" : "（点「立即总结」或设置「自动」）")
    : ccOn ? "会议已开始，等待字幕…" : "打开 CC 后，用「立即总结」或「自动」生成会议要点。";
  if (m.summarizer === "off") meta += " · 「会议记录」为关闭，不生成要点";
  if (m.session && m.session.closed) meta = "上一场会议 · " + meta;
  lsMeta.textContent = meta;
  lsRun.disabled = m.state === "running" || !!(m.session && m.session.closed);
  document.getElementById("lsRetry").hidden = m.state !== "failed";
  lsMinutesLine.replaceChildren(...(m.minutes ? [minutesLine({id: m.minutes.id, minutes: m.minutes})].filter(Boolean) : []));
  const d = m.data;
  if (!d) { lsContent.replaceChildren(); return; }
  const prev = m.previous;
  const fresh = new Set(d.new_since_last || []);
  const known = key => new Set(prev ? (key === "todos" ? prev.todos.map(t => t.task) : prev[key]) : []);
  const nodes = [];
  const heading = text => Object.assign(document.createElement("h4"), {textContent: text});
  nodes.push(heading("现在的话题"), Object.assign(document.createElement("div"), {className: "ls-topic", textContent: d.current_topic || "—"}));
  for (const [title, key] of [["要点", "points"], ["决定", "decisions"], ["TODO", "todos"], ["未回答的问题", "open_questions"]]) {
    const before = known(key);
    const ul = document.createElement("ul");
    for (const item of d[key]) {
      const text = key === "todos" ? item.task + "（" + (item.owner || "负责人未定") + " / " + (item.due || "期限未定") + "）" : item;
      const raw = key === "todos" ? item.task : item;
      const li = Object.assign(document.createElement("li"), {textContent: text});
      if ((prev && !before.has(raw)) || fresh.has(raw)) li.className = "ls-new";
      ul.append(li);
    }
    if (!d[key].length) ul.append(Object.assign(document.createElement("li"), {textContent: "—"}));
    nodes.push(heading(title), ul);
  }
  if (d.new_since_last.length) {
    const ul = Object.assign(document.createElement("ul"), {className: "ls-new-list"});
    for (const item of d.new_since_last) ul.append(Object.assign(document.createElement("li"), {textContent: item}));
    nodes.push(heading("本次新增"), ul);
  }
  lsContent.replaceChildren(...nodes);
}
function onLiveSummary(msg) {
  const oldSession = lsLast && lsLast.session && lsLast.session.id;
  if (oldSession && msg.session && msg.session.id !== oldSession) {
    ccResetHistory(); meetRunStart = 0; meetLastFinal = 0;
  }
  const isNew = !!(lsLast && msg.data && msg.at && msg.at !== lsLast.at);
  lsLast = msg;
  if (msg.notice) showHint("会议要点：" + msg.notice);
  if (msg.interval !== undefined) lsInterval.value = String(msg.interval);
  if (msg.summarizer) minutesShow(msg.summarizer);
  lsRender();
  if (isNew && (sidePanel.hidden || pointsSection.classList.contains("folded"))) ccBtn.classList.add("has-badge");
  meetingCheck();
}
setInterval(() => {
  lsRender();
  // Minutes from captions run in the background: refresh their status.
  if (ccOn && lsLast && lsLast.minutes && MINUTES_ACTIVE.includes(lsLast.minutes.state)) wsSend({type:"live_summary", action:"sync"});
}, 5000);

// --- Forgot to record? Offer it once a meeting is clearly under way ---
// CC live + finals for 2+ minutes (gaps < 60 s) + this browser not recording.
let meetRunStart = 0, meetLastFinal = 0;
function meetingFinal() {
  const now = Date.now();
  if (!meetRunStart || now - meetLastFinal > 60000) meetRunStart = now;
  meetLastFinal = now;
  meetingCheck();
}
function meetingSessionId() { return (lsLast && lsLast.session && lsLast.session.id) || "none"; }
function meetingCheck() {
  let ignored = null;
  try { ignored = sessionStorage.getItem("kvmMeetingIgnored"); } catch (e) {}
  const now = Date.now();
  const busy = recording || recFinishing || recStarting || srecState.active || srecBusy;
  const show = ccOn && ccState === "live" && !busy && meetRunStart > 0 &&
    meetLastFinal - meetRunStart >= 120000 && now - meetLastFinal < 60000 && ignored !== meetingSessionId();
  if (meetingBar.hidden === show) { meetingBar.hidden = !show; updateLayout(); }
}
setInterval(meetingCheck, 5000);
document.getElementById("meetingBarRecord").addEventListener("click", () => {
  meetingBar.hidden = true; updateLayout();
  toggleRecording(); container.focus();
});
document.getElementById("meetingBarIgnore").addEventListener("click", () => {
  try { sessionStorage.setItem("kvmMeetingIgnored", meetingSessionId()); } catch (e) {}
  meetingBar.hidden = true; updateLayout(); container.focus();
});

__RECORDING_AUDIO_MIXER__

// --- Screen recording ---
// Records the live canvas (video) plus streamed audio via MediaRecorder, and
// streams the resulting WebM chunks back to the server over the WebSocket.
// The server writes them to its configured recording folder — no save dialog.
let mediaRecorder = null;
let recStream = null;
let recording = false;
let recStartTime = 0;
let recTimer = null;
let recFinishing = false;
let recStarting = false;
let recResumePending = false;
let recConnectionReady = false;
let recMediaReady = false;
let recAudioSession = null;
let recAudioPreview = null;
let recInputDevices = [];
let recUpload = null;
let recPaintTimer = null;
let recPictureGap = false;

function attachRecordingUpload() {
  if (!recUpload || !recConnectionReady || !ws || ws.readyState !== WebSocket.OPEN) return;
  recUpload.ready = false;
  recUpload.inflight = null;
  recUpload.finishSent = false;
  ws.send(JSON.stringify({type:"rec_session_start", session:recUpload.id, filename:recUpload.filename,
    minutes:recUpload.minutes}));
}
function pumpRecordingUpload() {
  const u = recUpload;
  if (!u || u.failed || !u.ready || !recConnectionReady || !ws || ws.readyState !== WebSocket.OPEN ||
      u.inflight !== null) return;
  if (u.queue.length) {
    const block = u.queue[0];
    const packet = new Uint8Array(block.data.byteLength + 5);
    packet[0] = 0x11;
    new DataView(packet.buffer).setUint32(1, block.sequence);
    packet.set(block.data, 5);
    u.inflight = block.sequence;
    try { ws.send(packet.buffer); }
    catch (e) { u.inflight = null; u.ready = false; }
  } else if (u.finishing && !u.finishSent) {
    u.finishSent = true;
    ws.send(JSON.stringify({type:"rec_session_finish", session:u.id, next:u.next, metadata:u.metadata}));
  }
}
function recordingUploadEvent(msg) {
  const u = recUpload;
  if (!u || msg.session !== u.id) return;
  if (msg.type === "rec_session_error") {
    u.failed = true;
    statusEl.textContent = "Recording upload error; keep this page open: " + msg.error;
    showHint(statusEl.textContent);
    if (recording) stopRecording();
    return;
  }
  if (msg.type === "rec_session_saved") {
    recUpload = null;
    recFinishing = false;
    lockRecordingAudio(false);
    const btn = document.getElementById("btnRec");
    btn.disabled = false;
    btn.classList.remove("recording");
    btn.textContent = "⏺ Record";
    recordingFileSaved(msg);
    statusEl.textContent = "Recording saved · MP4 processing";
    return;
  }
  if (msg.type === "rec_session_ready" || msg.type === "rec_ack") {
    if (msg.type === "rec_ack" && msg.next <= u.ack) return;
    if (!Number.isInteger(msg.next) || msg.next < u.ack || msg.next > u.next) {
      u.failed = true;
      statusEl.textContent = "Recording upload offset mismatch; keep this page open";
      showHint(statusEl.textContent);
      if (recording) stopRecording();
      return;
    }
    u.ack = msg.next;
    while (u.queue.length && u.queue[0].sequence < msg.next)
      u.bytes -= u.queue.shift().data.byteLength;
    u.inflight = null;
    u.ready = true;
    pumpRecordingUpload();
  }
}

__RECORDING_AUDIO_CONTROLS__

function recordingConnectionLost() {
  recConnectionReady = false;
  recMediaReady = false;
  recPictureGap = true;
  recordingBackendAudioConnection(false);
  if (recUpload) {
    recUpload.ready = false;
    recUpload.inflight = null;
    recUpload.finishSent = false;
  }
}

function recordingMediaReady() {
  if (!recConnectionReady) return;
  recMediaReady = true;
  recPictureGap = false;
  recordingBackendAudioConnection(true);
}

async function maybeResumeRecording() {
  // Retained as a no-op for callers from older video-mode code. The recorder
  // is continuous now; reconnect resumes upload, never creates another file.
  pumpRecordingUpload();
}

async function ensureAudioForRecording() {
  if (!audioCfg) return;            // no audio device on this server
  if (!audioCtx) await startAudio();
  if (audioCtx.state === "suspended") await audioCtx.resume();
  if (!recDest) {
    recDest = audioCtx.createMediaStreamDestination();
    audioNode.connect(recDest);    // independent of playGain → always captured
  }
}

function pickMimeType() {
  // H.264 first: Chrome encodes it in the GPU process (likely hardware). With
  // VP8 a 1080p tab grew to ~1.9 GB queueing frames and crashed (Out of Memory);
  // H.264 stayed at ~0.2 GB in the same test.
  const prefs = [
    "video/webm;codecs=h264,opus",
    "video/webm;codecs=h264",
    "video/webm;codecs=vp8,opus",
    "video/webm;codecs=vp9,opus",
    "video/webm;codecs=vp8",
    "video/webm",
  ];
  for (const t of prefs) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported(t)) return t;
  }
  return "";
}

function recordingFilename(now = new Date()) {
  // Fixed JST (UTC+09:00), independent of the browser computer's timezone.
  const stamp = new Date(now.getTime() + 9*60*60*1000).toISOString()
    .replace(/Z$/, "-JST").replace(/[:.]/g, "-");
  return "recording-" + stamp + ".webm";
}

async function startRecording() {
  if (recording || recFinishing || recStarting) return;
  recStarting = true;
  try {
    await startRecordingSession();
  } catch (e) {
    if (recAudioSession) recAudioSession.close();
    recAudioSession = null;
    syncLegacyAudioPlayback();
    if (recStream) recStream.getVideoTracks().forEach(t => t.stop());
    recStream = null;
    clearInterval(recPaintTimer);
    recPaintTimer = null;
    recUpload = null;
    lockRecordingAudio(false);
    document.getElementById("btnRec").disabled = false;
    statusEl.textContent = "Cannot start recording: " + e.message;
    showHint(statusEl.textContent);
  } finally {
    recStarting = false;
  }
}

async function startRecordingSession() {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    statusEl.textContent = "Cannot record: not connected";
    return;
  }
  if (!mediaW() || !mediaH()) {
    statusEl.textContent = "Cannot record: no video yet";
    return;
  }
  // Video from the active display (canvas in stream mode, <video> in direct).
  const startSocket = ws;
  document.getElementById("recordingResult").hidden = true;
  resultRecordingId = null;
  resultRecordingPending = false;
  closeRecordingPreview();
  lockRecordingAudio(true);
  document.getElementById("btnRec").disabled = true;
  recAudioSession = await openRecordingAudio();
  syncLegacyAudioPlayback();
  recStream = captureDisplayStream();
  // Explicitly remove any audio from <video>.captureStream(): exactly the
  // chosen input (or no audio) is recorded, never a second HDMI track.
  for (const t of recStream.getAudioTracks()) recStream.removeTrack(t);
  if (ws !== startSocket || startSocket.readyState !== WebSocket.OPEN) {
    for (const t of recStream.getVideoTracks()) t.stop();
    recStream = null;
    throw new Error("Connection changed while starting recording");
  }
  for (const t of recAudioSession.tracks) recStream.addTrack(t);

  const mimeType = pickMimeType();
  const filename = recordingFilename();
  // Capped bitrate keeps the encoder queue and upload small for 1080p screens.
  const recorderOptions = {videoBitsPerSecond: 6000000, audioBitsPerSecond: 128000};
  if (mimeType) recorderOptions.mimeType = mimeType;
  mediaRecorder = new MediaRecorder(recStream, recorderOptions);
  // The minutes choice is fixed when recording starts and stored server-side
  // with the session, so a later dropdown change only affects the next one.
  const minutesSelect = document.getElementById("recMinutes");
  recUpload = {id:crypto.randomUUID().replace(/-/g,""), filename,
    minutes:minutesSelect && !minutesSelect.disabled ? minutesSelect.value : "off",
    queue:[], next:0, ack:0, bytes:0, ready:false, inflight:null, finishing:false, finishSent:false};
  attachRecordingUpload();

  const sessionStream = recStream;
  const sessionAudio = recAudioSession;
  let pendingChunks = Promise.resolve();
  const sessionUpload = recUpload;
  mediaRecorder.ondataavailable = (ev) => {
    if (!ev.data || ev.data.size === 0) return;
    pendingChunks = pendingChunks.then(async () => {
      const buf = await ev.data.arrayBuffer();
      for (let offset = 0; offset < buf.byteLength; offset += 256*1024) {
        const data = new Uint8Array(buf.slice(offset, offset + 256*1024));
        sessionUpload.queue.push({sequence:sessionUpload.next++, data});
        sessionUpload.bytes += data.byteLength;
      }
      pumpRecordingUpload();
      if (sessionUpload.bytes > 128*1024*1024 && recording) {
        stopRecording();
        showHint("Recording stopped: 128 MB awaiting upload. Keep this page open to save.");
      }
    }).catch((e) => {
      sessionUpload.failed = true;
      statusEl.textContent = "Recording data error: " + e.message;
      showHint(statusEl.textContent);
      if (recording) stopRecording();
    });
  };
  mediaRecorder.onstop = async () => {
    recFinishing = true;
    recording = false;
    clearInterval(recTimer);
    const btn = document.getElementById("btnRec");
    btn.disabled = true;
    btn.textContent = "Saving…";
    // dataavailable precedes stop, but Blob.arrayBuffer() is asynchronous.
    // The server must receive ALL chunks before it closes the output file.
    try {
      await pendingChunks;
      sessionUpload.finishing = true;
      pumpRecordingUpload();
    } catch (e) {
      statusEl.textContent = "Recording incomplete: " + e.message;
      showHint(statusEl.textContent);
    } finally {
      for (const t of sessionStream.getVideoTracks()) t.stop();
      clearInterval(recPaintTimer);
      recPaintTimer = null;
      sessionAudio.close();
      recAudioStatus.textContent = "Inputs released · Check audio to test or listen again";
      recAudioSession = null;
      syncLegacyAudioPlayback();
      recStream = null;
      // Keep Saving… until the server confirms every block and manual finish.
      btn.textContent = recConnectionReady ? "Saving…" : "Waiting to upload…";
    }
  };

  mediaRecorder.start(1000);        // flush a chunk every second
  recording = true;
  // The mixer's output track is stable. Physical input loss is handled inside
  // the mixer and must never stop MediaRecorder or split the recording.
  recStartTime = Date.now();
  const btn = document.getElementById("btnRec");
  btn.disabled = false;
  btn.classList.add("recording");
  const tick = () => {
    const s = Math.floor((Date.now() - recStartTime) / 1000);
    const mm = String(Math.floor(s / 60)).padStart(2, "0");
    const ss = String(s % 60).padStart(2, "0");
    btn.textContent = "■ Stop " + mm + ":" + ss;
  };
  tick();
  recTimer = setInterval(tick, 500);
}

function stopRecording(forReconnect = false) {
  if (recording && recUpload) recUpload.metadata = {recorded_end_at:new Date().toISOString(), duration_seconds:(Date.now()-recStartTime)/1000};
  if (!forReconnect) recResumePending = false;
  if (!recording) return;
  recording = false;
  recFinishing = true;
  clearInterval(recTimer);
  recTimer = null;
  const btn = document.getElementById("btnRec");
  btn.disabled = true;
  btn.textContent = "Saving…";
  // Keep tracks alive until MediaRecorder has emitted its final chunk.
  mediaRecorder.stop();
}

window.addEventListener("beforeunload", (ev) => {
  if (!recording && !recFinishing) return;
  ev.preventDefault();
  ev.returnValue = "";
});
window.addEventListener("pagehide", () => {
  stopHdmiMonitoring();
  closeRecordingPreview();
  recAudioSession?.close();
});

function captureDisplayStream() {
  // Stable video track across JPEG/WebRTC reconnects and source track ends.
  // Drawing on our own canvas preserves the last picture while audio keeps
  // its original continuous timeline. Never draw UI text onto the KVM canvas.
  const surface = document.createElement("canvas");
  surface.width = mediaW();
  surface.height = mediaH();
  const paint = surface.getContext("2d");
  const draw = () => {
    try {
      const source = videoMode ? video : canvas;
      if (!recPictureGap) paint.drawImage(source, 0, 0, surface.width, surface.height);
      if (recPictureGap) {
        // Repaint the last frame so canvas.captureStream keeps ticking.
        paint.drawImage(surface, 0, 0);
        paint.fillStyle = "#a00000";
        paint.fillRect(0, 0, 300, 28);
        paint.fillStyle = "white";
        paint.font = "16px sans-serif";
        paint.fillText("Picture disconnected — last received frame", 5, 20);
      }
    } catch (e) { /* Keep the previous picture during video source swaps. */ }
  };
  draw();
  recPaintTimer = setInterval(draw, 1000/30);
  return surface.captureStream(30);
}

document.getElementById("btnRec").addEventListener("click", () => {
  closeToolbarPanels();
  if (recResumePending) {
    recResumePending = false;
    document.getElementById("btnRec").textContent = "⏺ Record";
    statusEl.textContent = "Automatic recording resume cancelled";
  } else toggleRecording();
  container.focus();
});

// --- Server-side recording (default) ---
// The server records capture + audio itself; this page only sends start/stop
// and mirrors srec_state, which every viewer of this Target receives.
let srecAvailable = false, srecState = {active:false}, srecBusy = false, srecTimer = null, srecBase = 0;
const recModeSelect = document.getElementById("recMode");
const recFpsSelect = document.getElementById("recFps");
try {
  const savedMode = localStorage.getItem("kvmRecMode");
  if (savedMode === "server" || savedMode === "browser") recModeSelect.value = savedMode;
  const savedFps = localStorage.getItem("kvmRecFps");
  if (savedFps === "15" || savedFps === "30") recFpsSelect.value = savedFps;
} catch (e) {}
function recMode() { return srecAvailable && recModeSelect.value === "server" ? "server" : "browser"; }
function applyRecMode() {
  const server = recMode() === "server";
  document.querySelector(".record-panel").classList.toggle("server-mode", server);
  document.getElementById("recFpsRow").hidden = !server;
  document.getElementById("recServerNote").hidden = !server;
  recModeSelect.disabled = !srecAvailable || srecState.active || recording || recFinishing;
  recFpsSelect.disabled = !!srecState.active;
}
recModeSelect.addEventListener("change", () => {
  try { localStorage.setItem("kvmRecMode", recModeSelect.value); } catch (e) {}
  applyRecMode();
});
recFpsSelect.addEventListener("change", () => {
  try { localStorage.setItem("kvmRecFps", recFpsSelect.value); } catch (e) {}
});
function srecConfigure(cfg) {
  srecAvailable = !!(cfg && cfg.available);
  if (cfg && cfg.state) srecApply(cfg.state); else applyRecMode();
}
function srecTick() {
  const s = Math.max(0, Math.floor((Date.now() - srecBase) / 1000));
  const hh = Math.floor(s / 3600), mm = Math.floor(s / 60) % 60, ss = s % 60;
  document.getElementById("btnRec").textContent = "■ Stop " + (hh ? hh + ":" : "") +
    String(mm).padStart(2, "0") + ":" + String(ss).padStart(2, "0");
}
function srecApply(st) {
  srecState = st || {active:false};
  srecBusy = false;
  const btn = document.getElementById("btnRec");
  clearInterval(srecTimer); srecTimer = null;
  if (srecState.active) {
    srecBase = Date.now() - (srecState.elapsed || 0) * 1000;
    btn.disabled = !!srecState.starting;
    btn.classList.add("recording");
    if (srecState.starting) btn.textContent = "Starting…";
    else { srecTick(); srecTimer = setInterval(srecTick, 500); }
    recSourceSelect.disabled = true;
    const warnings = srecState.warnings || [];
    statusEl.textContent = "Server recording · " + (srecState.encoder || "") + " · " + (srecState.fps || "") + " fps" +
      (warnings.length ? " · " + warnings.join("; ") : "");
    if (warnings.length) showHint(warnings.join("; "));
  } else if (!recording && !recFinishing && !recStarting) {
    btn.disabled = false;
    btn.classList.remove("recording");
    btn.textContent = "⏺ Record";
    recSourceSelect.disabled = false;
  }
  applyRecMode();
  meetingCheck();
}
function toggleRecording() {
  // A running server recording is stopped from any page, whatever its mode.
  if (srecState.active || recMode() === "server") {
    if (srecBusy || srecState.starting) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      statusEl.textContent = "Cannot record: not connected";
      return;
    }
    const btn = document.getElementById("btnRec");
    srecBusy = true;
    btn.disabled = true;
    if (srecState.active) {
      btn.textContent = "Saving…";
      wsSend({type:"srec_stop"});
    } else {
      btn.textContent = "Starting…";
      const minutesSelect = document.getElementById("recMinutes");
      wsSend({type:"srec_start", fps:Number(recFpsSelect.value), source:recSourceSelect.value,
              minutes:minutesSelect && !minutesSelect.disabled ? minutesSelect.value : "off"});
    }
    return;
  }
  if (recording) stopRecording(); else startRecording();
}

// --- Toolbar show/hide hotkey (Ctrl+Alt+Enter) ---
// The toolbar is visible by default.
// Ctrl+Alt+Enter toggles it; the combo is captured at the window level (capture
// phase) and swallowed so it is never forwarded to the target.
function showHint(text) {
  if (text) hint.textContent = text;
  hint.classList.add("show");
  clearTimeout(hintTimer);
  hintTimer = setTimeout(() => hint.classList.remove("show"), 2500);
}
function hideHint() {
  clearTimeout(hintTimer);
  hint.classList.remove("show");
}
function toggleToolbar() {
  toolbarHidden = !toolbarHidden;
  document.body.classList.toggle("tb-hidden", toolbarHidden);
  if (toolbarHidden) {
    toolbar.querySelectorAll("details[open]").forEach(d => d.open = false);
    toolbar.classList.remove("visible");
    showHint("Ctrl+Alt+Enter: toolbar");
  } else {
    hideHint();
    if (document.fullscreenElement) {   // make it appear despite fs auto-hide
      toolbar.classList.add("visible");
      requestAnimationFrame(_tbCenter);
    }
  }
  updateCanvasSize();
  container.focus();
}

let _hotkeyEnter = false;   // suppress the matching Enter keyup to the target
window.addEventListener("keydown", (e) => {
  if (e.ctrlKey && e.altKey && (e.code === "Enter" || e.code === "NumpadEnter")) {
    e.preventDefault();
    e.stopPropagation();
    _hotkeyEnter = true;
    if (!e.repeat) toggleToolbar();
  }
}, true);
window.addEventListener("keyup", (e) => {
  if (_hotkeyEnter && (e.code === "Enter" || e.code === "NumpadEnter")) {
    e.preventDefault();
    e.stopPropagation();
    _hotkeyEnter = false;
  }
}, true);

// --- Direct (native) video mode ---
// The browser opens the capture card itself (getUserMedia) and renders a real
// <video>, matching the official app's smoothness. Input keeps flowing over the
// WebSocket. The server releases the capture device while direct mode is on.
function isWebcamLabel(l) {
  l = (l || "").toLowerCase();
  return ["webcam", "camera", "ir camera", "facetime", "front", "rear"]
    .some((k) => l.includes(k));
}

function openCapture(deviceId) {
  const v = {width: {ideal: captureRequest?.width || 1920},
             height: {ideal: captureRequest?.height || 1080}, frameRate: {ideal: 60}};
  if (deviceId) v.deviceId = {exact: deviceId};
  return navigator.mediaDevices.getUserMedia({video: v, audio: false});
}

// The server needs a moment to release the capture device; retry while it's
// still busy (but never retry a denied-permission error).
async function openCaptureRetry(deviceId, tries) {
  let lastErr;
  for (let i = 0; i < tries; i++) {
    try { return await openCapture(deviceId); }
    catch (e) {
      if (e && e.name === "NotAllowedError") throw e;
      lastErr = e;
      await new Promise((r) => setTimeout(r, 300));
    }
  }
  throw lastErr;
}

async function findCaptureDeviceId() {
  try {
    const devs = await navigator.mediaDevices.enumerateDevices();
    const cams = devs.filter((d) => d.kind === "videoinput" && d.label);
    if (serverCaptureLabel) {
      const m = cams.find((d) => d.label.includes(serverCaptureLabel) ||
                                 serverCaptureLabel.includes(d.label));
      if (m) return m.deviceId;
    }
    const nonCam = cams.find((d) => !isWebcamLabel(d.label));
    return nonCam ? nonCam.deviceId : null;
  } catch (e) { return null; }
}

async function enableDirect() {
  const btn = document.getElementById("btnDirect");
  btn.disabled = true;
  statusEl.textContent = "Starting direct video…";
  wsSend({type: "stream", on: false});   // ask server to release the device
  try {
    // First open (any device) to obtain permission, then refine by label.
    let stream = await openCaptureRetry(null, 15);
    const wantId = await findCaptureDeviceId();
    const curId = stream.getVideoTracks()[0] &&
                  stream.getVideoTracks()[0].getSettings().deviceId;
    if (wantId && curId && wantId !== curId) {
      stream.getTracks().forEach((t) => t.stop());
      stream = await openCaptureRetry(wantId, 15);
    }
    directStream = stream;
    video.srcObject = stream;
    await video.play().catch(() => {});
    videoMode = true;
    canvas.style.display = "none";
    video.style.display = "block";
    updateCanvasSize();
    btn.classList.add("active");
    btn.textContent = "Direct ●";
    fpsEl.textContent = "native";
    statusEl.textContent = "Direct video (native)";
  } catch (e) {
    videoMode = false;
    wsSend({type: "stream", on: true});  // resume server stream on failure
    const emsg = "Direct failed: " + (e && e.message ? e.message : e);
    statusEl.textContent = emsg;
    if (toolbarHidden) showHint(emsg);
  }
  btn.disabled = false;
  container.focus();
}

function disableDirect() {
  const btn = document.getElementById("btnDirect");
  videoMode = false;
  if (directStream) {
    directStream.getTracks().forEach((t) => t.stop());
    directStream = null;
  }
  video.srcObject = null;
  video.style.display = "none";
  canvas.style.display = "block";
  btn.classList.remove("active");
  btn.textContent = "Direct";
  // Give the browser a moment to fully release the device before the server
  // reopens it for the JPEG stream.
  setTimeout(() => wsSend({type: "stream", on: true}), 400);
  updateCanvasSize();
  statusEl.textContent = "Connected";
  container.focus();
}

document.getElementById("btnDirect").addEventListener("click", () => {
  if (directStream) { disableDirect(); }
  else {
    if (rtcMode) disableRtc();   // the two <video> modes are exclusive
    enableDirect();
  }
});

// --- WebRTC (H264) mode ---
// The server encodes the shared capture as H.264 and streams it over a
// loopback RTCPeerConnection. Signaling is non-trickle over the WebSocket:
// send a complete offer (ICE gathering finished), get a complete answer.
function iceComplete(pc) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((res) => {
    const check = () => {
      if (pc.iceGatheringState === "complete") {
        pc.removeEventListener("icegatheringstatechange", check);
        res();
      }
    };
    pc.addEventListener("icegatheringstatechange", check);
  });
}

// STUN is only useful when the viewer reaches the server across the internet
// (NAT traversal for the H264 media path; the server side defaults to the
// same STUN server via aiortc).  Private/local hosts skip it so offline LANs
// don't sit out the STUN timeout during ICE gathering.
function isPrivateHost(h) {
  h = (h || "").replace(/^\[|\]$/g, "").toLowerCase();
  if (h === "localhost" || h === "::1" || h.endsWith(".local")) return true;
  if (/^127\./.test(h) || /^10\./.test(h) || /^192\.168\./.test(h)) return true;
  if (/^172\.(1[6-9]|2[0-9]|3[01])\./.test(h)) return true;
  if (/^169\.254\./.test(h) || /^fe80:/.test(h) || /^f[cd]/.test(h)) return true;
  return false;
}
const RTC_CONFIG = isPrivateHost(location.hostname) ? {} :
  {iceServers: [{urls: "stun:stun.l.google.com:19302"}]};

async function enableRtc() {
  if (rtcMode) return;
  const btn = document.getElementById("btnRtc");
  if (btn.disabled) return;
  btn.disabled = true;
  if (directStream) disableDirect();   // hand the device back to the server
  statusEl.textContent = "Starting H.264 stream…";
  _rtcGen++;                           // new negotiation generation
  try {
    rtcPc = new RTCPeerConnection(RTC_CONFIG);
    const pc = rtcPc;
    pc.addTransceiver("video", {direction: "recvonly"});
    // Generous timeout: on a cold server the first offer can sit queued
    // behind the capture-device open (~tens of seconds on MSMF).
    const streamReady = new Promise((res, rej) => {
      _rtcWait = {res, rej};
      setTimeout(() => rej(new Error("timed out")), 30000);
    });
    pc.ontrack = (ev) => {
      // Minimise the receive-side buffer on loopback/LAN links only.
      // Across the internet, jitter is real: forcing a zero buffer turns
      // every delivery wobble into a visible stutter, so remote viewers
      // keep the browser's adaptive jitter buffer.
      try {
        if (IS_LAN) {
          if ("jitterBufferTarget" in ev.receiver) ev.receiver.jitterBufferTarget = 0;
          if ("playoutDelayHint" in ev.receiver) ev.receiver.playoutDelayHint = 0;
        }
      } catch (e) {}
      video.srcObject = (ev.streams && ev.streams[0])
        ? ev.streams[0] : new MediaStream([ev.track]);
      const ok = () => { if (_rtcWait) _rtcWait.res(); };
      if (video.readyState >= 1) ok();
      else video.addEventListener("loadedmetadata", ok, {once: true});
    };
    pc.onconnectionstatechange = () => {
      if (pc !== rtcPc) return;
      const st = pc.connectionState;
      if (rtcMode && (st === "failed" || st === "disconnected" || st === "closed")) {
        const emsg = "H.264 stream lost (" + st + ")";
        disableRtc();
        statusEl.textContent = emsg;
        if (toolbarHidden) showHint(emsg);
      }
    };
    await pc.setLocalDescription(await pc.createOffer());
    await iceComplete(pc);
    // The quality preset rides in the offer: "auto" lets the server pick
    // (LAN: configured fps/bitrate; WAN: 30 fps / 4 Mbps start with free
    // REMB adaptation), explicit presets set the cap/fps directly.
    const offerMsg = {type: "webrtc_offer", sdp: pc.localDescription.sdp,
                      gen: _rtcGen};
    const preset = document.getElementById("rtcQuality").value;
    if (preset !== "auto") {
      const parts = preset.split("/");
      offerMsg.bitrate = parseInt(parts[0], 10);
      offerMsg.fps = parseInt(parts[1], 10);
    }
    wsSend(offerMsg);
    await streamReady;
    _rtcWait = null;
    await video.play().catch(() => {});
    rtcMode = true;
    videoMode = true;
    canvas.style.display = "none";
    video.style.display = "block";
    updateCanvasSize();
    btn.classList.add("active");
    btn.textContent = "H264 ●";
    fpsEl.textContent = "h264";
    statusEl.textContent = "H.264 (WebRTC)";
    recordingMediaReady();
  } catch (e) {
    _rtcWait = null;
    if (rtcPc) { try { rtcPc.close(); } catch (e2) {} rtcPc = null; }
    wsSend({type: "webrtc_stop"});   // server resumes the JPEG stream
    const emsg = "H264 failed: " + (e && e.message ? e.message : e);
    statusEl.textContent = emsg;
    if (toolbarHidden) showHint(emsg);
  }
  btn.disabled = false;
  container.focus();
}

function disableRtc() {
  const btn = document.getElementById("btnRtc");
  rtcMode = false;
  _rtcWait = null;
  _rtcGen++;                           // invalidate any in-flight answer
  if (rtcPc) { try { rtcPc.close(); } catch (e) {} rtcPc = null; }
  wsSend({type: "webrtc_stop"});     // server resumes the JPEG stream
  if (!directStream) {
    videoMode = false;
    video.srcObject = null;
    video.style.display = "none";
    canvas.style.display = "block";
    updateCanvasSize();
    statusEl.textContent = "Connected";
  }
  btn.classList.remove("active");
  btn.textContent = "H264";
  container.focus();
}

function restartRtc() {
  // Drop the local pc without touching the display mode, then renegotiate.
  rtcMode = false;
  _rtcWait = null;
  _rtcGen++;                           // invalidate any in-flight answer
  if (rtcPc) { try { rtcPc.close(); } catch (e) {} rtcPc = null; }
  enableRtc();
}

document.getElementById("btnRtc").addEventListener("click", () => {
  if (rtcMode) disableRtc(); else enableRtc();
});

document.getElementById("rtcQuality").addEventListener("change", () => {
  if (rtcMode) restartRtc();   // renegotiate with the new preset
  container.focus();
});

// --- PWA ---
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");

// --- Start ---
updateCursor();                             // apply default cursor visibility
container.focus();
connect();

// H.264 is the default video mode: the first hello auto-starts it, retrying
// while the server is still warming up (cold device open). If it never comes
// up the viewer stays on the JPEG stream. ?rtc=0 opts out (JPEG default).
let autoRtcTries = 0;
async function autoRtc() {
  if (rtcMode || directStream || autoRtcTries >= 8) return;
  if (document.getElementById("btnRtc").disabled) return;  // no aiortc
  if (ws && ws.readyState === WebSocket.OPEN) {
    autoRtcTries++;
    await enableRtc();
    if (!rtcMode) setTimeout(autoRtc, 4000);
  } else {
    setTimeout(autoRtc, 500);
  }
}
</script>
</body>
</html>"""

# Short fingerprint of the embedded frontend. Sent to each client on connect so
# an already-open viewer can auto-reload itself after the server is restarted
# with changed HTML/JS — otherwise the tab silently reconnects its WebSocket and
# keeps running the OLD code, making edits look like they "didn't take effect".
# It only changes when _VIEWER_HTML changes, so a plain restart never forces a
# needless reload.
_VIEWER_HTML = _VIEWER_HTML.replace("__RECORDING_AUDIO_MIXER__", RECORDING_AUDIO_MIXER_JS).replace(
    "__RECORDING_AUDIO_CONTROLS__", RECORDING_AUDIO_CONTROLS_JS)
_VIEWER_HTML = install_sop_viewer(_VIEWER_HTML)

_BUILD_ID = hashlib.md5(_VIEWER_HTML.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# WebViewerServer
# ---------------------------------------------------------------------------

class WebViewerServer:
    """WebSocket server that streams JPEG frames and accepts input events.

    Uses a single port for both HTTP (HTML delivery via process_request)
    and WebSocket (video + input).
    """

    def __init__(self, hardware, config, audio: AudioCapture | None = None):
        """
        Args:
            hardware: KvmHardware instance.
            config: Config instance with web_port, web_fps, web_quality.
            audio: Optional shared AudioCapture instance.
        """
        self._hw = hardware
        self._config = config
        self._server = None
        self._clients: set = set()
        self._audio = audio
        self._recording_jobs: set = set()
        self._conversion_lock = asyncio.Lock()
        self._recording_sessions = RecordingSessions(config.recording_dir)
        self._recording_library = RecordingLibrary(config.recording_dir)
        self._session_exports: dict = {}
        # Server-side recording (default): ffmpeg encodes the capture + audio,
        # the browser only sends start/stop. It outlives viewer disconnects.
        self._server_recorder = ServerRecorder(
            hardware.get_capture, audio,
            mic_name=getattr(config, "recording_mic", "EarPods") or "",
            max_seconds=getattr(config, "recording_max_seconds", 3 * 3600),
            on_stop=self._server_recording_stopped)
        self._sop = SopService(self)
        self._captions = CaptionService(
            audio, getattr(config, "ai_hub_url", ""), getattr(config, "ai_hub_key", None),
            getattr(config, "captions_language", "auto"), getattr(config, "captions_enabled", True))
        self._minutes = MinutesService(config, self._recording_library)
        self._meeting = LiveMeeting(config.recording_dir, self._captions, self._minutes,
                                    getattr(config, "captions_summary_interval", 5))
        self._meeting_task = None
        self._meeting_jobs: set = set()  # not awaited on stop (summaries can be long)
        self._viewers: set = set()  # authenticated /ws clients (live_summary audience)
        self._loop = None
        self._meeting.publish = self._publish_meeting
        # Number of clients currently consuming the server JPEG stream.  The
        # capture device is only held while this is > 0, so a client switching
        # to "direct" (native getUserMedia) video frees the device for the
        # browser to open it.
        self._stream_count = 0
        # Serialises the (slow, backgrounded) device open/close operations so
        # a release can never interleave with an open still in flight.
        self._device_lock = asyncio.Lock()
        self._cap_label: str | None = None  # cached capture-device name
        # Auth state (only used when config.web_password is set): reconnect
        # tokens issued after a successful password login, and per-IP failed
        # attempt throttling.  Both live in memory only, so a server restart
        # invalidates every session.
        self._auth_tokens: set[str] = set()
        self._auth_fails: dict[str, tuple[int, float]] = {}

    def _map_mouse_abs(self, x: int, y: int) -> tuple[int, int]:
        """Apply target-specific absolute HID axis mapping."""
        x = max(0, min(4095, int(x)))
        y = max(0, min(4095, int(y)))
        if self._config.mouse_invert_x:
            x = 4095 - x
        if self._config.mouse_invert_y:
            y = 4095 - y
        return x, y

    async def start(self):
        host = self._config.web_host
        port = self._config.web_port
        ssl_ctx = self._build_ssl_context()
        self._server = await websockets.serve(
            self._handle_client,
            host,
            port,
            process_request=self._process_http,
            ssl=ssl_ctx,
            compression=None,  # JPEG/WebM are already compressed.
            ping_interval=20,
            ping_timeout=90,   # Tolerate temporary busy/background browser work.
            max_size=8 * 1024 * 1024,
        )
        await asyncio.to_thread(self._sop.start)
        await asyncio.to_thread(self._minutes.start)
        self._loop = asyncio.get_running_loop()
        self._meeting_task = asyncio.create_task(self._meeting.loop())
        try:
            await self._recover_server_recordings()
        except Exception:
            logger.exception("Recovering interrupted server recordings failed")
        scheme = "https" if ssl_ctx else "http"
        auth = " (password required)" if self._config.web_password else ""
        logger.info(f"Web viewer listening on {scheme}://{host}:{port}{auth}")
        if host not in ("127.0.0.1", "localhost", "::1"):
            if not self._config.web_password:
                logger.warning(
                    "Web viewer is reachable from the network WITHOUT a "
                    "password — anyone who can connect controls the target. "
                    "Set --web-password / SHKVM_WEB_PASSWORD.")
            elif ssl_ctx is None:
                logger.warning(
                    "Web viewer password will travel unencrypted (no TLS). "
                    "Use --web-tls-cert/--web-tls-key or terminate TLS at a "
                    "reverse proxy / tunnel.")

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        """Build the TLS context from config, or None for plain HTTP."""
        cert = self._config.web_tls_cert
        key = self._config.web_tls_key
        if not cert and not key:
            return None
        if not (cert and key):
            raise ValueError(
                "--web-tls-cert and --web-tls-key must be given together")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        return ctx

    async def stop(self):
        if self._server_recorder.active:
            # Close the file cleanly; the stop callback queues save/export,
            # which the _recording_jobs gather below waits for.
            await asyncio.to_thread(self._server_recorder.stop, "shutdown")
            await asyncio.sleep(0)
        await asyncio.to_thread(self._sop.stop)
        await asyncio.to_thread(self._minutes.stop)
        self._meeting.stopping = True
        if self._meeting_task is not None:
            self._meeting_task.cancel()
            try:
                await self._meeting_task
            except (asyncio.CancelledError, Exception):
                pass
            self._meeting_task = None
        await self._captions.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._recording_jobs:
            await asyncio.gather(*list(self._recording_jobs), return_exceptions=True)

    def _export_recording(self, path, ws, destination=None, session=None):
        """Keep exports alive after a viewer disconnect; serialize CPU work."""
        async def export():
            async with self._conversion_lock:
                try:
                    if destination is None:
                        result = await asyncio.to_thread(convert_recording, path)
                    else:
                        result = await asyncio.to_thread(convert_recording, path, destination)
                    message = {"type": "rec_exported", "path": str(result), "session":session}
                    logger.info("Recording MP4 verified: %s", result)
                except Exception as exc:
                    logger.warning("Recording export failed (%s): %s", path, exc)
                    message = {"type": "rec_export_error", "path": str(path), "error": str(exc), "session":session}
                if ws is None:      # server recordings: every viewer shows the result
                    self._broadcast(message)
                    return
                try:
                    await ws.send(json.dumps(message))
                except Exception:
                    pass  # Conversion result is also persisted beside the WebM.
        task = asyncio.create_task(export())
        self._recording_jobs.add(task)
        task.add_done_callback(self._recording_jobs.discard)
        return task

    # Pre-encoded PWA assets (immutable after module load)
    _pwa_routes: dict[str, tuple[str, bytes]] = {
        "/manifest.json": ("application/manifest+json",
                           _MANIFEST_JSON.encode()),
        "/sw.js": ("application/javascript; charset=utf-8",
                   _SW_JS.encode()),
        "/icon.svg": ("image/svg+xml", _ICON_SVG.encode()),
    }

    async def _process_http(self, connection, request):
        """Serve the HTML viewer and PWA assets for HTTP requests."""
        route = urlsplit(request.path).path
        if route == "/library":
            return self._sop.page(connection, request)
        if route == "/sop/ws":
            if not self._sop.store or not local_request(connection, request, origin=True):
                return Response(403, "Forbidden", websockets.Headers(), b"Local same-origin clients only")
            return None
        if urlsplit(request.path).path == "/recordings/file":
            return await self._serve_recording(connection, request)
        if route == "/recordings/minutes":
            return await self._serve_minutes(request)

        if request.path == "/ws":
            # Stash User-Agent for logging when _handle_client runs
            ua = request.headers.get("User-Agent", "")
            connection._kvm_user_agent = ua
            return None  # let WebSocket handshake proceed

        pwa = self._pwa_routes.get(request.path)
        if pwa is not None:
            content_type, body = pwa
            return Response(
                200, "OK",
                websockets.Headers({
                    "Content-Type": content_type,
                    "Content-Length": str(len(body)),
                    "Cache-Control": "no-cache",
                }),
                body,
            )

        html_bytes = _VIEWER_HTML.encode("utf-8")
        return Response(
            200, "OK",
            websockets.Headers({
                "Content-Type": "text/html; charset=utf-8",
                "Content-Length": str(len(html_bytes)),
                "Cache-Control": "no-cache",
            }),
            html_bytes,
        )

    async def _serve_recording(self, connection, request):
        """Signed links are issued only over an authenticated viewer socket."""
        try:
            query = parse_qs(urlsplit(request.path).query)
            path = self._recording_library.resolve_link(query)
            size = path.stat().st_size
            kind = {".mp4": "video/mp4", ".webm": "video/webm"}.get(path.suffix.lower(), "text/plain; charset=utf-8")
            headers = {"Content-Type": kind,
                       "Accept-Ranges": "bytes", "Cache-Control": "private, no-store",
                       "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}
            disposition = "attachment" if query.get("download") == ["1"] else "inline"
            headers["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{quote(path.name)}"
            start, end, status, reason = 0, size - 1, 200, "OK"
            byte_range = request.headers.get("Range")
            if byte_range:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", byte_range)
                if not match or not any(match.groups()):
                    raise IndexError("Invalid byte range")
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = min(int(last), end) if last else end
                else:
                    start = max(0, size - int(last))
                if start > end or start >= size:
                    raise IndexError("Byte range is outside the recording")
                # Bound individual playback range reads on long recordings.
                end = min(end, start + 4 * 1024 * 1024 - 1)
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
                status, reason = 206, "Partial Content"
            def read():
                with path.open("rb") as file:
                    file.seek(start)
                    return file.read(end - start + 1)
            body = await asyncio.to_thread(read)
            headers["Content-Length"] = str(len(body))
            return Response(status, reason, websockets.Headers(headers), body)
        except IndexError:
            return Response(416, "Range Not Satisfiable", websockets.Headers({"Content-Range": f"bytes */{size}"}), b"")
        except (OSError, ValueError):
            return Response(404, "Not Found", websockets.Headers({"Content-Type":"text/plain", "Cache-Control":"no-store"}), b"Recording unavailable; refresh the recordings list.")

    async def _serve_minutes(self, request):
        """HTML minutes view for a signed ``<recording>.minutes.md`` link."""
        try:
            query = parse_qs(urlsplit(request.path).query)
            path = self._recording_library.resolve_link(query, suffixes=(".md",))
            nonce = secrets.token_urlsafe(16)
            body = (await asyncio.to_thread(self._minutes.page, path, nonce)).encode("utf-8")
            return Response(200, "OK", websockets.Headers({
                "Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(body)),
                "Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": f"default-src 'none'; script-src 'nonce-{nonce}'; "
                                           "style-src 'unsafe-inline'; media-src 'self'; frame-ancestors 'none'; "
                                           "base-uri 'none'; form-action 'none'"}), body)
        except (OSError, ValueError, KeyError, TypeError):
            return Response(404, "Not Found", websockets.Headers({"Content-Type": "text/plain", "Cache-Control": "no-store"}),
                            b"Minutes unavailable; refresh the recordings list.")

    def _queue_minutes(self, session, source, destination):
        """Blocking: enqueue minutes with the choice stored at recording start."""
        manifest = json.loads(Path(source).with_name("session.json").read_text(encoding="utf-8"))
        return self._minutes.enqueue(self._recording_library.identifier(destination.name), source,
                                     destination.stem, manifest.get("minutes"), session)

    def _retry_minutes(self, key, summarizer=None):
        """Blocking: retry a failed job, or create one for an older recording."""
        if self._minutes.load(key) is not None:
            return self._minutes.retry(key, summarizer)
        entry = self._recording_library.catalog().get(key)
        if entry is None:
            raise ValueError("Recording no longer exists")
        return self._minutes.retry(key, summarizer, source=entry["path"], output_stem=Path(entry["name"]).stem)

    def _publish_meeting(self, message):
        """Send live_summary to every viewer; callable from worker threads too."""
        self._broadcast(message)

    # ---- server-side recording ---------------------------------------------
    def _server_recording_state(self):
        state = self._server_recorder.status()
        state.pop("path", None)
        state["type"] = "srec_state"
        return state

    async def _start_server_recording(self, ev):
        """Begin a server recording; every viewer sees the new state."""
        fps = ev.get("fps", 15)
        source = ev.get("source", "mix")
        if fps not in FPS_CHOICES or source not in SERVER_REC_SOURCES:
            raise ValueError("Invalid recording settings")
        if self._server_recorder.active:
            raise ValueError("A server recording is already running")
        session = secrets.token_hex(16)
        stamp = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%dT%H-%M-%S-%f")[:-3]
        state, source_path = await asyncio.to_thread(
            self._recording_sessions.create_server, session, f"recording-{stamp}-JST",
            {"minutes": ev.get("minutes"), "fps": fps, "audio_source": source})
        try:
            await asyncio.to_thread(self._server_recorder.start, session, source_path, fps, source,
                                    state["started_epoch"])
        except Exception:
            # Nothing usable was written: drop the empty session folder.
            await asyncio.to_thread(shutil.rmtree, source_path.parent, True)
            raise
        self._meeting.note_recording()
        self._captions.begin_recording(
            session, self._recording_library.root / (Path(state["filename"]).stem + ".live.vtt"),
            state["started_epoch"])
        self._broadcast(self._server_recording_state())

    def _server_recording_stopped(self, status):
        """Recorder thread callback (manual stop, 3 h limit, disk full, error)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        def schedule():
            task = loop.create_task(self._finish_server_recording(status))
            self._recording_jobs.add(task)
            task.add_done_callback(self._recording_jobs.discard)
        loop.call_soon_threadsafe(schedule)

    async def _finish_server_recording(self, status):
        session = status.get("session")
        self._captions.end_recording(session)
        self._broadcast(self._server_recording_state())
        reason = status.get("stopped_reason", "manual")
        if reason in ("max_duration", "disk_full", "error"):
            label = {"max_duration": "已达 3 小时上限，录制已自动停止",
                     "disk_full": "磁盘空间不足，录制已自动停止",
                     "error": "录制出错：" + str(status.get("error", ""))}[reason]
            self._broadcast({"type": "srec_notice", "message": label})
        try:
            ended = datetime.now(timezone.utc)
            source, destination = await asyncio.to_thread(
                self._recording_sessions.finish_server, session, status.get("duration"), ended.isoformat())
        except Exception as exc:
            logger.warning("Server recording %s not saved: %s", session, exc)
            self._broadcast({"type": "srec_notice", "message": "录制没有保存：" + str(exc)})
            return
        try:
            await asyncio.to_thread(self._queue_minutes, session, source, destination)
        except Exception:
            logger.exception("Minutes queueing failed; recording was saved")
        if self._sop.store:
            try:
                await asyncio.to_thread(self._sop.store.notice_saved, self._recording_library.identifier(destination.name))
            except Exception:
                logger.exception("SOP indexing deferred; recording was saved")
        self._broadcast({"type": "rec_session_saved", "session": session, "server": True,
                         "path": str(destination),
                         "recording_id": self._recording_library.identifier(destination.name)})
        self._session_exports[session] = self._export_recording(source, None, destination, session)
        logger.info("Server recording saved: %s -> %s", session, destination)

    async def _recover_server_recordings(self):
        """Finish server sessions a crash or restart left open (file kept as is)."""
        for session in await asyncio.to_thread(self._recording_sessions.unfinished_server_sessions):
            logger.warning("Recovering interrupted server recording %s", session)
            await self._finish_server_recording({"session": session, "stopped_reason": "recovered"})

    def _broadcast(self, message):
        """Send a JSON message to every viewer; callable from worker threads too."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        data = json.dumps(message, ensure_ascii=False)

        async def send(ws):
            try:
                await ws.send(data)
            except Exception:
                pass

        def schedule():
            for ws in list(self._viewers):
                loop.create_task(send(ws))
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is loop:
            schedule()
        else:
            loop.call_soon_threadsafe(schedule)

    def _live_summary_done(self, ws):
        """Report a failed manual live summary to the viewer that asked."""
        def done(task):
            self._meeting_jobs.discard(task)
            if task.cancelled() or task.exception() is None:
                return
            message = {**self._meeting.message(), "notice": str(task.exception())}

            async def send():
                try:
                    await ws.send(json.dumps(message, ensure_ascii=False))
                except Exception:
                    pass
            asyncio.ensure_future(send())
        return done

    def _minutes_views(self, items):
        return {item["id"]: self._minutes.view(item["id"]) for item in items}

    def _set_captions(self, state: dict, on: bool):
        """Join/leave the shared caption feed (does not switch CC itself)."""
        if on and state["captions_queue"] is None:
            state["captions_queue"] = self._captions.subscribe()
            state["captions_event"].set()
        elif not on and state["captions_queue"] is not None:
            q = state["captions_queue"]
            state["captions_queue"] = None
            state["captions_event"].clear()
            self._captions.unsubscribe(q)
            try:
                if q.full():
                    q.get_nowait()
                q.put_nowait(None)  # wake _send_captions off the old queue
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    async def _send_captions(self, ws, state: dict):
        """Forward this client's caption feed (CC state, captions, meeting points)."""
        while True:
            q = state["captions_queue"]
            if q is None:
                await state["captions_event"].wait()
                continue
            msg = await q.get()
            if msg is None or state["captions_queue"] is not q:
                continue
            try:
                await ws.send(json.dumps(msg, ensure_ascii=False))
            except websockets.ConnectionClosed:
                break

    @staticmethod
    def _local_recording_client(ws):
        """OS actions require a loopback peer AND a same-origin browser request."""
        try:
            if not ws.remote_address or not ipaddress.ip_address(ws.remote_address[0]).is_loopback:
                return False
            origin = urlsplit(ws.request.headers.get("Origin", ""))
            host = ws.request.headers.get("Host", "")
            return os.name == "nt" and origin.scheme in ("http", "https") and origin.netloc == host
        except (ValueError, AttributeError):
            return False

    def _client_is_wan(self, ip: str) -> bool:
        """Whether this client connects across the public internet.

        WAN clients get the adaptive stream treatment; LAN/loopback clients
        keep the original latency/quality tuning.  Separate method so tests
        can override the classification.
        """
        return not _is_private_address(ip)

    async def _handle_client(self, ws):
        """Handle a single WebSocket client: stream frames + process input."""
        if urlsplit(ws.request.path).path == "/sop/ws":
            await self._sop.socket(ws)
            return
        self._clients.add(ws)
        addr = ws.remote_address
        ip = f"{addr[0]}:{addr[1]}" if addr else "unknown"
        wan = self._client_is_wan(addr[0]) if addr else True
        ua = getattr(ws, "_kvm_user_agent", "")
        ua_short = ua[:120] + "…" if len(ua) > 120 else ua
        msg = (f"Web client connected from {ip} "
               f"({'WAN' if wan else 'LAN'}, {len(self._clients)} total)")
        if ua_short:
            msg += f"  UA: {ua_short}"
        logger.info(msg)

        # Disable Nagle so small writes (input echoes, audio chunks, control
        # messages) aren't batched behind an unacked segment — on a WAN RTT
        # that batching is a visible latency wobble.  websockets' asyncio
        # implementation does not set this itself.
        try:
            sock = ws.transport.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass

        # Per-client stream state.  "event" gates _send_frames so the client
        # can pause the server stream (e.g. when using direct/native video).
        # "webrtc" pauses JPEG frames while the same frames flow over a
        # WebRTC peer connection instead (device stays with the server).
        # "wan" selects the adaptive JPEG stream + on-demand audio;
        # "inflight"/"ack_event" are its frame-credit bookkeeping and
        # "audio_queue"/"audio_event" the dynamic audio subscription.
        state = {"stream": False, "event": asyncio.Event(),
                 "webrtc": False, "rtc_session": None,
                 "wan": wan,
                 "inflight": 0, "ack_event": asyncio.Event(),
                 "audio_queue": None, "audio_event": asyncio.Event(),
                 "captions_queue": None, "captions_event": asyncio.Event()}

        loop = asyncio.get_running_loop()
        try:
            # Auth gate: when a password is configured, nothing happens (no
            # frames, no input, no capture-device open) until this client
            # authenticates over the WebSocket.
            if self._config.web_password:
                if not await self._authenticate(ws, addr[0] if addr else "?"):
                    return

            # Client starts in server-stream mode (acquires the capture device).
            await self._acquire_stream(state)

            # Frontend fingerprint: lets an open viewer auto-reload when the
            # server has been restarted with changed HTML/JS.  "webrtc" tells
            # the client whether the H264 (WebRTC) mode is available;
            # "adaptive" tells a WAN client to ack received frames so the
            # credit-based stream can pace itself.
            await ws.send(json.dumps({
                "type": "hello", "build": _BUILD_ID,
                "webrtc": _WEBRTC_AVAILABLE,
                "direct": self._config.direct_enabled,
                "target_name": self._config.target_name,
                "adaptive": wan,
                "capture_request": {"width": self._config.capture_width,
                                    "height": self._config.capture_height},
                "capture_status": self._hw.get_capture().get_cached_info(),
                "captions": {"available": self._captions.available,
                             "reason": self._captions.reason,
                             "on": self._captions.on, "since": self._captions.on_since},
                "minutes": {"enabled": self._minutes.enabled,
                            "default": self._meeting.summarizer},
                "server_recording": {"available": True, "fps": list(FPS_CHOICES),
                                     "state": self._server_recording_state()},
            }))

            # Tell the client which capture device the server uses, so direct
            # mode can pick the matching device via getUserMedia.
            if self._cap_label is None:
                self._cap_label = await loop.run_in_executor(
                    None, self._capture_label)
            await ws.send(json.dumps({
                "type": "capture_device", "label": self._cap_label,
            }))

            # Notify client about audio availability.  LAN clients are
            # subscribed immediately (original always-on behaviour); WAN
            # clients start unsubscribed — uncompressed PCM is ~1.5 Mbps
            # that would compete with video on the uplink — and opt in
            # with an {"type": "audio", "on": true} message.
            if self._audio is not None:
                if not wan:
                    self._set_audio(state, True)
                await ws.send(json.dumps({
                    "type": "audio_config",
                    "sampleRate": self._audio.samplerate,
                    "channels": self._audio.channels,
                    "on": not wan,
                }))

            # Every authenticated viewer follows the server-wide CC switch:
            # its feed starts with the CC state, recent captions (while on)
            # and the meeting points. Joining or leaving never changes CC.
            self._viewers.add(ws)
            if self._captions.available:
                self._set_captions(state, True)

            tasks = [
                asyncio.create_task(self._send_capture_status(ws)),
                asyncio.create_task(self._send_frames(ws, state)),
                asyncio.create_task(self._recv_input(ws, state)),
                asyncio.create_task(self._send_captions(ws, state)),
            ]
            if self._audio is not None:
                tasks.append(asyncio.create_task(
                    self._send_audio(ws, state)))
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            for t in done:
                # Retrieve the exception (e.g. ConnectionClosedError from an
                # abrupt disconnect) so asyncio doesn't log "Task exception
                # was never retrieved" for every dead client.
                if not t.cancelled():
                    error = t.exception()
                    if error:
                        logger.warning("Viewer task ended: %s", error)
        finally:
            self._set_audio(state, False)
            self._set_captions(state, False)
            self._viewers.discard(ws)
            self._clients.discard(ws)
            await self._stop_webrtc(state)
            await self._release_stream(state)
            logger.info("Web client disconnected from %s (%s total); code=%s reason=%s",
                        ip, len(self._clients), ws.close_code, ws.close_reason)

    # Auth throttle: after _AUTH_MAX_FAILS consecutive wrong passwords from
    # one IP, that IP is locked out for _AUTH_LOCKOUT_S seconds.  A client
    # that never authenticates is dropped after _AUTH_TIMEOUT_S.
    _AUTH_MAX_FAILS = 5
    _AUTH_LOCKOUT_S = 30.0
    _AUTH_TIMEOUT_S = 300.0
    _AUTH_FAIL_DELAY_S = 1.5

    async def _authenticate(self, ws, ip: str) -> bool:
        """WebSocket-level auth gate; True once the client is authenticated.

        Protocol (client ↔ server, JSON text messages):
            ← {"type": "auth_required"}
            → {"type": "auth", "password": "..."}   or  {"token": "..."}
            ← {"type": "auth_ok", "token": "..."}   on success
            ← {"type": "auth_failed", "error": "...", "expired"?: true}

        A successful password login issues a random reconnect token so the
        browser can re-authenticate silently after a WebSocket reconnect or
        tab reload.  Tokens live in server memory only.  Every other message
        type is dropped until authentication succeeds.
        """
        loop = asyncio.get_running_loop()
        password = self._config.web_password
        try:
            fails, locked_until = self._auth_fails.get(ip, (0, 0.0))
            if time.monotonic() < locked_until:
                await ws.send(json.dumps({
                    "type": "auth_failed",
                    "error": "too many attempts — try again later",
                }))
                return False

            await ws.send(json.dumps({"type": "auth_required"}))
            deadline = loop.time() + self._AUTH_TIMEOUT_S
            while True:
                timeout = deadline - loop.time()
                if timeout <= 0:
                    return False
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout)
                except asyncio.TimeoutError:
                    return False
                if not isinstance(message, str):
                    continue
                try:
                    ev = json.loads(message)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") != "auth":
                    continue

                token = ev.get("token")
                if token:
                    if isinstance(token, str) and token in self._auth_tokens:
                        await ws.send(json.dumps(
                            {"type": "auth_ok", "token": token}))
                        return True
                    # Stale token (server restarted): tell the client to fall
                    # back to the password prompt.  Not a brute-force signal.
                    await ws.send(json.dumps({
                        "type": "auth_failed", "expired": True,
                        "error": "session expired",
                    }))
                    continue

                attempt = ev.get("password", "")
                if isinstance(attempt, str) and hmac.compare_digest(
                        attempt.encode(), password.encode()):
                    self._auth_fails.pop(ip, None)
                    token = secrets.token_urlsafe(24)
                    self._auth_tokens.add(token)
                    while len(self._auth_tokens) > 256:
                        self._auth_tokens.pop()
                    await ws.send(json.dumps(
                        {"type": "auth_ok", "token": token}))
                    return True

                # Wrong password: throttle and either lock out or let the
                # client retry on the same connection.
                fails += 1
                if len(self._auth_fails) > 1024:
                    self._auth_fails.clear()
                locked = fails >= self._AUTH_MAX_FAILS
                if locked:
                    self._auth_fails[ip] = (
                        0, time.monotonic() + self._AUTH_LOCKOUT_S)
                else:
                    self._auth_fails[ip] = (fails, 0.0)
                logger.warning(f"Web viewer auth failure from {ip}")
                await asyncio.sleep(self._AUTH_FAIL_DELAY_S)
                await ws.send(json.dumps({
                    "type": "auth_failed",
                    "error": ("too many attempts — try again later"
                              if locked else "wrong password"),
                }))
                if locked:
                    return False
        except websockets.ConnectionClosed:
            return False

    @staticmethod
    def _update_frame_gate(state: dict):
        """(Re)compute the _send_frames gate for *state*.

        JPEG frames flow only while the client consumes the server stream
        AND is not receiving the same frames over WebRTC instead — WebRTC
        keeps the device with the server but makes the JPEG encode per
        frame pure waste.
        """
        if state["stream"] and not state["webrtc"]:
            state["event"].set()
        else:
            state["event"].clear()

    async def _acquire_stream(self, state: dict):
        """Mark *state* as consuming the server stream; open device if first.

        The device open runs in a background task: an MSMF open can take
        tens of seconds (measured ~28 s right after a browser hands the
        device back from direct mode), and every caller of this method sits
        on the client's message loop, where such a stall freezes input and
        delays WebRTC signaling past the client's timeout.  Frames simply
        start flowing when the device is ready.
        """
        if state["stream"]:
            return
        state["stream"] = True
        self._update_frame_gate(state)
        self._stream_count += 1
        if self._stream_count == 1:
            capture = self._hw.get_capture()
            loop = asyncio.get_running_loop()

            async def _open():
                async with self._device_lock:
                    if self._stream_count == 0:
                        return  # released again before the open got the lock
                    # Non-fatal: the device may still be held by a browser in
                    # direct mode (e.g. right after a reconnect). The capture
                    # loop retries opening, so nothing is torn down here.
                    try:
                        await loop.run_in_executor(
                            None, capture.start_capture_thread)
                        logger.info("Capture thread started (stream active)")
                    except Exception as e:
                        logger.warning(f"Capture start failed (device busy?): {e}")

            asyncio.create_task(_open())

    async def _release_stream(self, state: dict):
        """Release the last stream's device only when Direct can take it.

        Like the open, the close is backgrounded and serialised behind
        ``_device_lock`` so it can never interleave with an in-flight open.
        Bound multi-Target servers disable Direct and keep capture warm for
        API snapshots and subsequent viewers, avoiding slow MSMF reopen.
        """
        if not state["stream"]:
            return
        state["stream"] = False
        self._update_frame_gate(state)
        self._stream_count -= 1
        if self._stream_count == 0 and self._config.direct_enabled:
            capture = self._hw.get_capture()
            loop = asyncio.get_running_loop()

            async def _close_device():
                async with self._device_lock:
                    if self._stream_count > 0:
                        return  # re-acquired while the close was pending
                    # Fully release the device (close, not just stop the
                    # thread) so a browser in direct mode can open it via
                    # getUserMedia. stop alone leaves the OpenCV VideoCapture
                    # holding the device → "Device in use".
                    await loop.run_in_executor(None, capture.close)
                    logger.info("Capture device released (no active streams)")

            asyncio.create_task(_close_device())

    async def _start_webrtc(self, ws, state: dict, ev: dict):
        """Negotiate a WebRTC session streaming the capture to this client.

        The server keeps the capture device open (unlike direct mode), so
        API/OCR/MCP consumers stay functional; only this client's JPEG
        WebSocket stream is paused to avoid encoding the frames twice.

        The client's ``gen`` value is echoed back in the answer/error so it
        can discard replies that belong to an offer it has already abandoned
        (a late answer applied to a newer RTCPeerConnection corrupts it).
        """
        sdp = ev.get("sdp", "")
        gen = ev.get("gen")
        try:
            from ._webrtc import WebRtcSession
        except ImportError:
            await ws.send(json.dumps({
                "type": "webrtc_error", "gen": gen,
                "error": "aiortc is not installed on the server "
                         "(pip install serial-hid-kvm[webrtc])",
            }))
            return

        # The client may come straight from direct mode, which released the
        # device — make sure the server owns it again.
        await self._acquire_stream(state)

        # Re-offer replaces any previous session (e.g. after a WS reconnect).
        # Close the old one in the background — see _stop_webrtc.
        old = state.get("rtc_session")
        state["rtc_session"] = None
        if old is not None:
            asyncio.create_task(old.close())

        session = None

        def on_closed():
            # Runs when the peer connection dies (browser gone, ICE failure).
            # Resume the JPEG stream so the client is never left frameless.
            if state.get("rtc_session") is session:
                state["rtc_session"] = None
                state["webrtc"] = False
                self._update_frame_gate(state)

        fps, cap, start, floor = self._webrtc_params(
            self._config, state["wan"], ev)
        session = WebRtcSession(
            self._hw.get_capture(),
            fps=fps,
            bitrate=cap,
            start_bitrate=start,
            min_bitrate=floor,
            on_closed=on_closed,
        )
        try:
            answer = await session.handle_offer(sdp)
        except Exception as e:
            logger.warning(f"WebRTC negotiation failed: {e}")
            await session.close()
            await ws.send(json.dumps({
                "type": "webrtc_error", "gen": gen, "error": str(e),
            }))
            return

        state["rtc_session"] = session
        state["webrtc"] = True
        self._update_frame_gate(state)
        await ws.send(json.dumps({
            "type": "webrtc_answer", "sdp": answer, "gen": gen,
        }))
        logger.info(
            f"WebRTC session established "
            f"({'WAN' if state['wan'] else 'LAN'}, {fps} fps, "
            f"start {start // 1000} kbps, cap {cap // 1000} kbps)")

    @staticmethod
    def _webrtc_params(config, wan: bool, ev: dict
                       ) -> tuple[int, int, int, int | None]:
        """Resolve ``(fps, bitrate_cap, start_bitrate, min_bitrate)`` for an offer.

        LAN: configured fps/bitrate with a floor at half the cap (REMB dips
        would blur desktop text).  WAN: 30 fps / 4 Mbps start with NO floor,
        so REMB congestion feedback can settle wherever the uplink allows.
        An explicit client preset ("bitrate"/"fps" in the offer message,
        from the toolbar quality selector) overrides the defaults but is
        clamped to the configured maxima.
        """
        fps_max = config.webrtc_fps
        cap_max = config.webrtc_bitrate

        req_fps = ev.get("fps")
        req_bitrate = ev.get("bitrate")

        if isinstance(req_fps, (int, float)) and req_fps > 0:
            fps = int(max(5, min(req_fps, fps_max)))
        else:
            fps = min(fps_max, _WAN_RTC_FPS_CAP) if wan else fps_max

        if isinstance(req_bitrate, (int, float)) and req_bitrate > 0:
            cap = int(max(200_000, min(req_bitrate, cap_max)))
        else:
            cap = cap_max

        if wan:
            start = min(_WAN_RTC_START, cap)
            floor = None
        else:
            start = cap
            floor = cap // 2
        return fps, cap, start, floor

    async def _stop_webrtc(self, state: dict):
        """Close this client's WebRTC session (if any) and resume JPEG.

        The actual peer-connection teardown is backgrounded: aiortc's
        ``pc.close()`` can take a while (DTLS shutdown against a browser pc
        that is already gone), and awaiting it here would stall the input
        loop — including a follow-up ``webrtc_offer`` from the same client.
        """
        session = state.get("rtc_session")
        state["rtc_session"] = None
        state["webrtc"] = False
        self._update_frame_gate(state)
        if session is not None:
            async def _close():
                t0 = asyncio.get_running_loop().time()
                await session.close()
                dt = asyncio.get_running_loop().time() - t0
                logger.info(f"WebRTC session closed ({dt:.2f}s)")
            asyncio.create_task(_close())

    def _capture_label(self) -> str:
        """Best-effort human-readable name of the configured capture device.

        Lets the browser match the same device via ``getUserMedia`` in direct
        mode.  Runs device enumeration (may shell out on Windows) so callers
        should invoke it off the event loop and cache the result.
        """
        try:
            from .capture import list_capture_devices, _is_webcam_name
            devices = list_capture_devices()
            dev = self._config.capture_device
            if dev is not None:
                dev_str = str(dev)
                for d in devices:
                    if d["device"] == dev_str:
                        return d["name"]
            # Auto-detect: first non-webcam, else first device.
            for d in devices:
                if not _is_webcam_name(d["name"]):
                    return d["name"]
            if devices:
                return devices[0]["name"]
        except Exception as e:
            logger.debug(f"Capture label lookup failed: {e}")
        return ""

    async def _send_capture_status(self, ws):
        """Report negotiation asynchronously, including while H264 is active.

        This reads cached metadata only; a slow/busy device never stalls the
        viewer handshake, input loop or launcher.
        """
        capture = self._hw.get_capture()
        last_info = None
        while True:
            info = capture.get_cached_info()
            if info != last_info:
                await ws.send(json.dumps({"type": "capture_status", "info": info}))
                last_info = info
            await asyncio.sleep(0.25)

    async def _send_frames(self, ws, state: dict):
        """Stream JPEG frames: fixed-pace for LAN, adaptive for WAN."""
        if state["wan"]:
            await self._send_frames_wan(ws, state)
        else:
            await self._send_frames_lan(ws, state)

    async def _send_frames_lan(self, ws, state: dict):
        """Stream JPEG frames to the client at configured FPS (original)."""
        fps = self._config.web_fps
        quality = self._config.web_quality
        interval = 1.0 / fps
        capture = self._hw.get_capture()
        loop = asyncio.get_running_loop()

        # Pace against a moving deadline so encode time is absorbed into the
        # frame interval instead of added on top of it (keeps the real frame
        # rate close to the configured target).
        next_t = loop.time()
        while True:
            # Pause while the client is in direct mode (device released).
            if not state["event"].is_set():
                await state["event"].wait()
                next_t = loop.time()
            result = await loop.run_in_executor(
                None, capture.get_frame_jpeg, quality
            )
            if result is not None:
                jpeg_bytes, _w, _h = result
                try:
                    await ws.send(b"\x01" + jpeg_bytes)
                except websockets.ConnectionClosed:
                    break
            next_t += max(interval, 1 / 30) if state.get("recording_session") else interval
            delay = next_t - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                # Fell behind; reset the schedule to avoid a catch-up burst.
                next_t = loop.time()

    async def _send_frames_wan(self, ws, state: dict):
        """Credit-paced, quality-adaptive JPEG stream for WAN clients.

        Two things make the fixed-pace LAN loop miserable across the
        internet: frames pile up in TCP buffers (bufferbloat — the client
        drops stale frames, but only after they crossed the link), and the
        configured fps/quality assume LAN bandwidth.

        - Credits: at most ``_WAN_CREDITS`` frames are unacknowledged at a
          time; the client acks every received frame (``hello.adaptive``
          told it to).  In-flight data stays bounded regardless of OS
          socket buffering, so added latency is ~one frame transfer and the
          send rate automatically tracks what the link actually delivers.
        - Quality: achieved fps over a sliding window drives the JPEG
          quality down when the link falls behind (cheaper frames preserve
          motion) and back up when it keeps up.
        """
        target_fps = min(self._config.web_fps, _WAN_FPS_CAP)
        interval = 1.0 / target_fps
        q_hi = min(self._config.web_quality, _WAN_QUALITY_CAP)
        quality = min(q_hi, 60)  # modest start; adapts both ways from here
        capture = self._hw.get_capture()
        loop = asyncio.get_running_loop()

        window_start = loop.time()
        window_sent = 0
        next_t = loop.time()
        while True:
            # Pause while the client is in direct/H264 mode.
            if not state["event"].is_set():
                await state["event"].wait()
                next_t = loop.time()

            # Credit gate: wait until the client acked in-flight frames.
            # If acks stop entirely (stalled link, tab in background),
            # reset after a timeout so the stream can never freeze forever.
            credit_deadline = loop.time() + _WAN_ACK_RESET_S
            while state["inflight"] >= _WAN_CREDITS:
                state["ack_event"].clear()
                if state["inflight"] < _WAN_CREDITS:
                    break  # ack raced the clear
                remaining = credit_deadline - loop.time()
                if remaining <= 0:
                    state["inflight"] = 0
                    break
                try:
                    await asyncio.wait_for(
                        state["ack_event"].wait(), remaining)
                except asyncio.TimeoutError:
                    state["inflight"] = 0
                    break

            result = await loop.run_in_executor(
                None, capture.get_frame_jpeg, quality, True)
            if result is not None:
                jpeg_bytes, _w, _h = result
                try:
                    await ws.send(b"\x01" + jpeg_bytes)
                except websockets.ConnectionClosed:
                    break
                state["inflight"] += 1
                window_sent += 1

            # Quality controller: evaluate every ~2 s.
            now = loop.time()
            elapsed = now - window_start
            if elapsed >= 2.0:
                achieved = window_sent / elapsed
                if achieved < target_fps * 0.7 and quality > _WAN_QUALITY_MIN:
                    quality = max(_WAN_QUALITY_MIN, quality - 10)
                    logger.debug(
                        f"WAN stream {achieved:.1f}/{target_fps} fps, "
                        f"quality -> {quality}")
                elif achieved >= target_fps * 0.95 and quality < q_hi:
                    quality = min(q_hi, quality + 5)
                window_start = now
                window_sent = 0

            next_t += interval
            delay = next_t - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                next_t = loop.time()

    def _set_audio(self, state: dict, on: bool):
        """Subscribe/unsubscribe this client's dynamic audio feed.

        While unsubscribed no PCM is queued for (or sent to) the client at
        all — that's the WAN bandwidth saving, not just a client-side mute.
        LAN clients are subscribed at connect and stay subscribed (their
        mute is a local gain, so recordings keep audio — original
        behaviour); WAN clients toggle via {"type": "audio"} messages.
        """
        if self._audio is None:
            return
        if on and state["audio_queue"] is None:
            state["audio_queue"] = self._audio.subscribe()
            state["audio_event"].set()
        elif not on and state["audio_queue"] is not None:
            q = state["audio_queue"]
            state["audio_queue"] = None
            state["audio_event"].clear()
            self._audio.unsubscribe(q)

    async def _send_audio(self, ws, state: dict):
        """Stream PCM audio chunks to the client while it is subscribed."""
        loop = asyncio.get_running_loop()
        while True:
            audio_queue = state["audio_queue"]
            if audio_queue is None:
                await state["audio_event"].wait()
                continue
            chunk = await loop.run_in_executor(
                None, self._get_audio_chunk, audio_queue
            )
            if chunk is not None and state["audio_queue"] is audio_queue:
                try:
                    await ws.send(b"\x02" + chunk)
                except websockets.ConnectionClosed:
                    break

    @staticmethod
    def _get_audio_chunk(q: queue.Queue) -> bytes | None:
        try:
            return q.get(timeout=0.1)
        except queue.Empty:
            return None

    def _open_recording(self, filename: str):
        """Open a recording file in the configured directory for writing.

        The client-supplied *filename* is reduced to a safe basename inside
        ``config.recording_dir`` and forced to a ``.webm`` extension, so a
        malicious or odd name can never escape the recording folder.

        Returns:
            ``(file_handle, Path)`` — caller is responsible for closing.
        """
        rec_dir = Path(self._config.recording_dir).expanduser()
        rec_dir.mkdir(parents=True, exist_ok=True)
        name = Path(filename or "").name  # drop any directory components
        name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        if not name or name in (".", ".."):
            now = datetime.now(timezone(timedelta(hours=9)))
            name = f"recording-{now:%Y-%m-%dT%H-%M-%S}-{now.microsecond // 1000:03d}-JST"
        if not name.lower().endswith(".webm"):
            name += ".webm"
        path = rec_dir / name
        # Colliding names must not truncate another browser's recording.
        return open(path, "xb"), path

    async def _recv_input(self, ws, state: dict):
        """Receive and process input events from the client.

        Serial writes are decoupled from the WebSocket receive loop by a
        background *sender* task.  This prevents a flood of mouse-move events
        from backing up behind the (relatively slow) serial port and making
        the target cursor lag further and further behind: consecutive mouse
        moves are coalesced so only the most recent position is ever sent,
        while clicks, scrolls and keystrokes stay strictly ordered.
        """
        held_modifiers = 0
        held_keys: set[int] = set()  # currently pressed non-modifier HID keycodes
        ch9329 = self._hw.get_ch9329()
        loop = asyncio.get_running_loop()
        rec_file = None  # open file handle while a recording is in progress
        upload_session = None

        # (is_move, packet) entries.  Consecutive moves collapse to the latest.
        send_queue: list[tuple[bool, bytes]] = []
        wake = asyncio.Event()
        stopping = False

        async def sender():
            while True:
                await wake.wait()
                wake.clear()
                while send_queue:
                    _is_move, pkt = send_queue.pop(0)
                    await loop.run_in_executor(None, ch9329.send, pkt)
                if stopping:
                    return

        def push(pkt: bytes):
            send_queue.append((False, pkt))
            wake.set()

        def push_move(pkt: bytes):
            # Coalesce with the previous pending move (if no discrete event has
            # been queued since) so stale intermediate positions are dropped.
            if send_queue and send_queue[-1][0]:
                send_queue[-1] = (True, pkt)
            else:
                send_queue.append((True, pkt))
            wake.set()

        sender_task = asyncio.create_task(sender())
        try:
            async for message in ws:
                # Binary frames from the client are screen-recording chunks
                # (tag byte 0x10 + WebM data), written to the open recording.
                if not isinstance(message, str):
                    if upload_session and len(message) > 5 and message[0] == 0x11:
                        try:
                            sequence = int.from_bytes(message[1:5], "big")
                            result = await asyncio.to_thread(
                                self._recording_sessions.write, upload_session, sequence, message[5:])
                            await ws.send(json.dumps({"type":"rec_ack", "session":upload_session, "next":result["next"]}))
                        except Exception as exc:
                            logger.warning("Recording upload failed (%s): %s", upload_session, exc)
                            await ws.send(json.dumps({"type":"rec_session_error", "session":upload_session, "error":str(exc)}))
                        continue
                    if (rec_file is not None and len(message) >= 1
                            and message[0] == 0x10):
                        await loop.run_in_executor(
                            None, rec_file.write, message[1:])
                    continue
                try:
                    ev = json.loads(message)
                except json.JSONDecodeError:
                    continue

                ev_type = ev.get("type")

                if ev_type == "recordings_list":
                    items = await asyncio.to_thread(self._recording_library.items)
                    local_sop = bool(self._sop.store and local_request(ws, ws.request, origin=True))
                    sop_prompt = None
                    try:
                        if local_sop:
                            await asyncio.to_thread(self._sop.store.sync)
                            snapshot = await asyncio.to_thread(self._sop.store.snapshot)
                            records = {r['id']: r for r in snapshot['recordings']}
                            prompt = await asyncio.to_thread(self._sop.store.pending_prompt)
                            if prompt:
                                sop_prompt = prompt['id']
                                # A pending saved card can outlive the recent-ten menu.
                                if sop_prompt not in {i['id'] for i in items}:
                                    all_items = await asyncio.to_thread(self._recording_library.items, None)
                                    items += [i for i in all_items if i['id'] == sop_prompt]
                            for item in items:
                                record = records.get(item['id'])
                                if record:
                                    item.update(sop_choice=record['choice'],recorded_start_at=record['media'].get('recorded_start_at'))
                    except Exception:
                        local_sop = False
                        sop_prompt = None
                        logger.exception("SOP list enrichment deferred")
                    minutes = {}
                    if self._minutes.enabled:
                        try:
                            minutes = await asyncio.to_thread(self._minutes_views, items)
                        except Exception:
                            logger.exception("Minutes status unavailable")
                    for item in items:
                        item["minutes"] = minutes.get(item["id"])
                    notes = []
                    if self._minutes.enabled:
                        try:
                            notes = await asyncio.to_thread(self._minutes.notes)
                        except Exception:
                            logger.exception("Meeting notes list unavailable")
                    await ws.send(json.dumps({"type":"recordings_list", "items":items,
                                              "local":self._local_recording_client(ws),
                                              "sop_enabled":local_sop,"sop_prompt":sop_prompt,
                                              "minutes_enabled":self._minutes.enabled,
                                              "meeting_notes":notes}))
                    continue

                if ev_type == "recordings_open":
                    try:
                        if not self._local_recording_client(ws):
                            raise ValueError("Open file/folder is available only from this computer")
                        await asyncio.to_thread(self._recording_library.open_local, ev.get("id"), ev.get("action"))
                        result = {"type":"recordings_action", "message":"Opened on this computer"}
                    except (OSError, ValueError) as exc:
                        result = {"type":"recordings_action", "error":str(exc)}
                    await ws.send(json.dumps(result))
                    continue

                if ev_type == "captions":
                    # The CC button switches CC for every viewer; the new
                    # state reaches all of them through their caption feeds.
                    try:
                        self._meeting.set_cc(bool(ev.get("on")))
                    except ValueError as exc:
                        await ws.send(json.dumps({"type":"captions_state", "state":"unavailable", "detail":str(exc)}))
                    continue

                if ev_type == "live_summary":
                    action = ev.get("action")
                    try:
                        if ev.get("summarizer"):
                            self._meeting.set_summarizer(str(ev["summarizer"]))
                        if action == "set_interval":
                            self._meeting.set_interval(ev.get("minutes"))
                        elif action == "run":
                            # Long CLI run in the background (never blocks this
                            # viewer's input loop); with CC off it is the final summary.
                            task = self._meeting.request_run()
                            self._meeting_jobs.add(task)
                            task.add_done_callback(self._live_summary_done(ws))
                        elif action == "minutes":
                            await asyncio.to_thread(self._meeting.generate_minutes)
                        elif action not in (None, "sync", "set_summarizer"):
                            raise ValueError("未知的要点操作")
                        if action in (None, "sync", "set_summarizer"):
                            await ws.send(json.dumps(self._meeting.message(), ensure_ascii=False))
                    except (OSError, ValueError, TypeError) as exc:
                        await ws.send(json.dumps({**self._meeting.message(), "notice": str(exc)}, ensure_ascii=False))
                    continue

                if ev_type == "captions_new_meeting":
                    self._meeting.new_meeting()
                    continue

                if ev_type == "minutes_retry":
                    try:
                        summarizer = ev.get("summarizer") or None
                        await asyncio.to_thread(self._retry_minutes, ev.get("id"), summarizer)
                        result = {"type":"minutes_action", "id":ev.get("id"), "message":"已加入会议记录队列"}
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        result = {"type":"minutes_action", "id":ev.get("id"), "error":str(exc)}
                    await ws.send(json.dumps(result, ensure_ascii=False))
                    continue

                if ev_type == "srec_start":
                    try:
                        await self._start_server_recording(ev)
                    except Exception as exc:
                        logger.warning("Server recording did not start: %s", exc)
                        await ws.send(json.dumps({"type": "srec_error", "error": str(exc)}, ensure_ascii=False))
                        await ws.send(json.dumps(self._server_recording_state()))
                    continue

                if ev_type == "srec_stop":
                    if self._server_recorder.active:
                        # The recorder thread finishes the file and calls
                        # _server_recording_stopped, which saves and exports it.
                        await asyncio.to_thread(self._server_recorder.stop, "manual")
                    else:
                        await ws.send(json.dumps(self._server_recording_state()))
                    continue

                if ev_type == "rec_session_start":
                    try:
                        result = await asyncio.to_thread(self._recording_sessions.attach,
                                                        ev.get("session"), ev.get("filename"),
                                                        {"minutes": ev.get("minutes")})
                        upload_session = ev["session"]
                        state["recording_session"] = not result["finished"]
                        if not result["finished"]:
                            # This meeting is recorded: its minutes come from the
                            # recording, not from captions alone.
                            self._meeting.note_recording()
                        if not result["finished"] and result.get("started_epoch"):
                            self._captions.begin_recording(
                                upload_session,
                                self._recording_library.root / (Path(result["filename"]).stem + ".live.vtt"),
                                result["started_epoch"])
                        logger.info("Recording session attached: %s next=%s", upload_session, result["next"])
                        await ws.send(json.dumps({"type":"rec_session_ready", "session":upload_session,
                                                  "next":result["next"], "finished":result["finished"]}))
                    except Exception as exc:
                        await ws.send(json.dumps({"type":"rec_session_error", "session":ev.get("session"), "error":str(exc)}))
                    continue

                if ev_type == "rec_session_finish":
                    try:
                        if ev.get("session") != upload_session:
                            raise ValueError("Recording session is not attached")
                        source, destination = await asyncio.to_thread(self._recording_sessions.finish,
                                                                     upload_session, ev.get("next"), ev.get("metadata"))
                        state["recording_session"] = False
                        self._captions.end_recording(upload_session)
                        try:
                            await asyncio.to_thread(self._queue_minutes, upload_session, source, destination)
                        except Exception:
                            logger.exception("Minutes queueing failed; recording was saved")
                        if self._sop.store:
                            try:
                                await asyncio.to_thread(self._sop.store.notice_saved, self._recording_library.identifier(destination.name))
                            except Exception:
                                logger.exception("SOP indexing deferred; recording was saved")
                        await ws.send(json.dumps({"type":"rec_session_saved", "session":upload_session,
                                                  "path":str(destination), "recording_id":self._recording_library.identifier(destination.name)}))
                        if destination.exists():
                            await ws.send(json.dumps({"type":"rec_exported", "session":upload_session, "path":str(destination), "recording_id":self._recording_library.identifier(destination.name)}))
                        elif upload_session not in self._session_exports or self._session_exports[upload_session].done():
                            self._session_exports[upload_session] = self._export_recording(source, ws, destination, upload_session)
                        logger.info("Recording session finished: %s -> %s", upload_session, destination)
                    except Exception as exc:
                        await ws.send(json.dumps({"type":"rec_session_error", "session":ev.get("session"), "error":str(exc)}))
                    continue

                if ev_type == "stream":
                    # Client toggling the server JPEG stream on/off (direct mode
                    # turns it off so the browser can open the device natively).
                    if ev.get("on"):
                        await self._acquire_stream(state)
                    else:
                        await self._release_stream(state)
                    continue

                elif ev_type == "webrtc_offer":
                    # Client requesting the low-latency H.264 (WebRTC) stream.
                    logger.info("webrtc_offer received")
                    await self._start_webrtc(ws, state, ev)
                    continue

                elif ev_type == "webrtc_stop":
                    logger.info("webrtc_stop received")
                    await self._stop_webrtc(state)
                    continue

                elif ev_type == "frame_ack":
                    # WAN adaptive stream: one in-flight credit returned.
                    if state["inflight"] > 0:
                        state["inflight"] -= 1
                    state["ack_event"].set()
                    continue

                elif ev_type == "audio":
                    # Dynamic audio subscription; while off, no PCM is sent
                    # at all (saves ~1.5 Mbps of uplink for WAN clients).
                    self._set_audio(state, bool(ev.get("on")))
                    continue

                elif ev_type == "rec_start":
                    if rec_file is not None:
                        previous = rec_file.name
                        rec_file.close()
                        rec_file = None
                        self._export_recording(previous, ws)
                    try:
                        rec_file, path = self._open_recording(
                            ev.get("filename", ""))
                        logger.info(f"Recording started: {path}")
                    except Exception as e:
                        logger.warning(f"Failed to start recording: {e}")
                        await ws.send(json.dumps(
                            {"type": "rec_error", "error": str(e)}))
                    continue

                elif ev_type == "rec_stop":
                    if rec_file is not None:
                        path = rec_file.name
                        rec_file.close()
                        rec_file = None
                        logger.info(f"Recording saved: {path}")
                        self._export_recording(path, ws)
                        await ws.send(json.dumps(
                            {"type": "rec_saved", "path": str(path), "recording_id":self._recording_library.identifier(Path(path).stem + ".mp4")}))
                    continue

                elif ev_type == "keydown":
                    code = ev.get("code", "")
                    # Modifier key?
                    mod_bit = _JS_MOD_BITS.get(code)
                    if mod_bit is not None:
                        held_modifiers |= mod_bit
                        push(build_keyboard_report(held_modifiers, held_keys))
                        continue
                    # Regular key
                    hid = _JS_CODE_TO_HID.get(code)
                    if hid is not None:
                        held_keys.add(hid)
                        push(build_keyboard_report(held_modifiers, held_keys))

                elif ev_type == "keyup":
                    code = ev.get("code", "")
                    mod_bit = _JS_MOD_BITS.get(code)
                    if mod_bit is not None:
                        held_modifiers &= ~mod_bit
                        push(build_keyboard_report(held_modifiers, held_keys))
                        continue
                    # Release key
                    hid = _JS_CODE_TO_HID.get(code)
                    if hid is not None:
                        held_keys.discard(hid)
                    push(build_keyboard_report(held_modifiers, held_keys))

                elif ev_type == "mousemove":
                    x = ev.get("x", 0)
                    y = ev.get("y", 0)
                    buttons = ev.get("buttons", 0)
                    x, y = self._map_mouse_abs(x, y)
                    push_move(build_mouse_abs_packet(buttons, x, y))

                elif ev_type == "mousedown":
                    x = ev.get("x", 0)
                    y = ev.get("y", 0)
                    buttons = ev.get("buttons", 0)
                    x, y = self._map_mouse_abs(x, y)
                    push(build_mouse_abs_packet(buttons, x, y))

                elif ev_type == "mouseup":
                    x = ev.get("x", 0)
                    y = ev.get("y", 0)
                    buttons = ev.get("buttons", 0)
                    x, y = self._map_mouse_abs(x, y)
                    push(build_mouse_abs_packet(buttons, x, y))

                elif ev_type == "scroll":
                    dy = ev.get("deltaY", 0)
                    scroll = max(-127, min(127, int(dy)))
                    push(build_mouse_rel_packet(0, 0, 0, scroll=scroll))

                elif ev_type == "release_all":
                    held_modifiers = 0
                    held_keys.clear()
                    push(build_keyboard_report(0, ()))
                    push(build_mouse_rel_packet(0, 0, 0))
        finally:
            stopping = True
            wake.set()
            try:
                await sender_task
            except Exception:
                pass
            if rec_file is not None:
                try:
                    rec_file.close()
                    logger.info(f"Recording saved (client gone): {rec_file.name}")
                    self._export_recording(rec_file.name, ws)
                except Exception:
                    pass
