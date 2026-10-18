"""Test isolation: never touch the real ~/Videos, the LAN AI hub or the Mac LLM.

Config() defaults recording_dir to ~/Videos and the hub/LLM URLs to the Mac
mini, and a started WebViewerServer runs the meeting-minutes worker (which
resumes queued jobs and uploads audio).  Point the home directory at a temp
folder, the network defaults at a closed local port, and keep the background
minutes worker off unless a test drives it explicitly.
"""
from pathlib import Path

import pytest

from serial_hid_kvm import config as config_module
from serial_hid_kvm import _minutes

DEAD_URL = "http://127.0.0.1:9"


@pytest.fixture(autouse=True, scope="session")
def _isolated_environment(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    patch = pytest.MonkeyPatch()
    patch.setattr(Path, "home", classmethod(lambda cls: home))
    patch.setenv("USERPROFILE", str(home))
    patch.setenv("HOME", str(home))
    original_init = config_module.Config.__init__

    def init(self):
        original_init(self)
        self.ai_hub_url = DEAD_URL
        self.mac_llm_url = DEAD_URL + "/v1"

    patch.setattr(config_module.Config, "__init__", init)
    patch.setattr(_minutes.MinutesService, "start", lambda self: None)
    yield home
    patch.undo()
