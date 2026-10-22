"""Exercise the actual browser mixer with deterministic device loss/recovery."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from serial_hid_kvm._web_audio import RECORDING_AUDIO_MIXER_JS
from serial_hid_kvm._web_recording_audio import RECORDING_AUDIO_CONTROLS_JS


class WebAudioMixerTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for audio JS tests")
    def test_audio_routing_and_device_lifecycle(self):
        result = subprocess.run(
            [shutil.which("node"), str(Path(__file__).with_name("web_audio_mixer.cjs"))],
            input=json.dumps({"mixer": RECORDING_AUDIO_MIXER_JS}),
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for audio JS tests")
    def test_target_specific_defaults_and_saved_overrides(self):
        result = subprocess.run(
            [shutil.which("node"), str(Path(__file__).with_name("web_audio_defaults.cjs"))],
            input=json.dumps({"controls": RECORDING_AUDIO_CONTROLS_JS,
                              "pool":RECORDING_AUDIO_MIXER_JS.split("class RecordingAudioMixer", 1)[0]}),
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for audio JS tests")
    def test_hdmi_playback_worklet_buffering(self):
        import re
        from serial_hid_kvm._web_viewer import _VIEWER_HTML
        worklet = re.search(r"const WORKLET_SRC = `(.*?)`;", _VIEWER_HTML, re.S).group(1)
        result = subprocess.run(
            [shutil.which("node"), str(Path(__file__).with_name("web_audio_worklet.cjs"))],
            input=json.dumps({"worklet": worklet}),
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
