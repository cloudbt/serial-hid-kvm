"""Exercise viewer sizing and HID mapping in JavaScript without hardware."""

import json
import shutil
import subprocess
import unittest

from serial_hid_kvm._web_viewer import _VIEWER_HTML


class WebDisplayTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for viewer JS tests")
    def test_aspect_correction_preserves_mouse_mapping_in_canvas_and_video(self):
        sizing = _VIEWER_HTML.split("// --- Display sizing", 1)[1]
        sizing = sizing[sizing.index("let scaleMode"):].split("let viewOnly", 1)[0]
        full_script = _VIEWER_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
        harness = r'''
const vm = require("vm"), assert = require("assert"), fs = require("fs");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
new vm.Script(input.full); // Check the entire inline script, not just the excerpt.
const stubs = `
let videoMode = false;
const elements = {
  displayAspect: {value: "source", handlers: {},
    addEventListener(name, callback) { this.handlers[name] = callback; }},
  resolution: {classList: {toggle(name, value) { this[name] = value; }}},
  btnScale: {}
};
const document = {getElementById: id => elements[id]};
const window = {addEventListener() {}};
const storage = new Map();
const localStorage = {
  getItem(key) { return storage.get(key) || null; },
  setItem(key, value) { storage.set(key, value); }
};
const container = {clientWidth: 1920, clientHeight: 1280, focus() {}};
function rect() {
  return {left: 10, top: 20, width: parseFloat(this.style.width),
          height: parseFloat(this.style.height)};
}
const canvas = {width: 1920, height: 1080, style: {}, getBoundingClientRect: rect};
const video = {videoWidth: 1920, videoHeight: 1080, style: {},
               addEventListener() {}, getBoundingClientRect: rect};
`;
const checks = `
captureRequest = {width: 1920, height: 1280};
updateCanvasSize();
assert.equal(canvas.style.width, "1920px");
assert.equal(canvas.style.height, "1080px");
assert(elements.resolution.textContent.includes("Request 1920×1280"));
assert(elements.resolution.textContent.includes("Frame 1920×1080"));
assert.equal(elements.resolution.classList.mismatch, true);
displayAspect.value = "3:2";
for (const mode of [false, true]) {
  videoMode = mode;
  updateCanvasSize();
  const el = activeEl();
  assert.equal(el.style.width, "1920px");
  assert.equal(el.style.height, "1280px");
  assert.deepEqual(mouseCoords({clientX: 970, clientY: 660}), {x: 2048, y: 2048});
  assert.deepEqual(mouseCoords({clientX: 10, clientY: 20}), {x: 0, y: 0});
  assert.deepEqual(mouseCoords({clientX: 1930, clientY: 1300}), {x: 4095, y: 4095});
}
container.clientWidth = 900;
container.clientHeight = 900;
updateCanvasSize();
assert.equal(video.style.width, "900px");
assert.equal(video.style.height, "600px");
scaleMode = "native";
updateCanvasSize();
assert.equal(video.style.height, "1280px");
assert.equal(elements.btnScale.textContent, "Width 1:1");
displayAspect.value = "source";
updateCanvasSize();
assert.equal(video.style.height, "1080px");
assert.equal(elements.btnScale.textContent, "1:1");
captureRequest = {width: 1920, height: 1080};
updateCanvasSize();
assert.equal(elements.resolution.classList.mismatch, false);
captureRequest = {width: 1920, height: 1280};
displayAspect.value = "3:2";
displayAspect.handlers.change();
assert.equal(storage.get(displayAspectKey()), "3:2");
displayAspect.value = "source"; // Simulate the selector's state on a new page.
restoreDisplayAspect();
assert.equal(displayAspect.value, "3:2");
captureRequest = {width: 1920, height: 1080};
restoreDisplayAspect();
assert.equal(displayAspect.value, "source"); // A different profile has its own preference.
storage.set(displayAspectKey(), "invalid");
restoreDisplayAspect();
assert.equal(displayAspect.value, "source");
localStorage.setItem = () => { throw new Error("storage disabled"); };
localStorage.getItem = () => { throw new Error("storage disabled"); };
displayAspect.value = "3:2";
displayAspect.handlers.change();
restoreDisplayAspect();
assert.equal(displayAspect.value, "3:2");
// Automatic fallback uses negotiated dimensions, even without local storage.
aspectManuallySelected = false;
captureRequest = {width: 1920, height: 1280};
displayAspect.value = "source";
applyCaptureStatus({status: "pending"});
assert.equal(displayAspect.value, "source");
applyCaptureStatus({status: "ready", width: 1920, height: 1080});
assert.equal(displayAspect.value, "3:2");
assert(elements.resolution.textContent.includes("Capture 1920×1080"));
assert.equal(video.style.height, "1280px");
// Manual changes remain available during the current session.
displayAspect.value = "source";
displayAspect.handlers.change();
applyCaptureStatus({status: "ready", width: 1920, height: 1080});
assert.equal(displayAspect.value, "source");
// True 1920×1280 capture must not be treated as failed due to auto-crop.
aspectManuallySelected = false;
applyCaptureStatus({status: "ready", width: 1920, height: 1280});
assert.equal(displayAspect.value, "source");
captureRequest = {width: 1920, height: 1080};
applyCaptureStatus({status: "ready", width: 1280, height: 720});
assert.equal(displayAspect.value, "source");
`;
vm.runInNewContext(stubs + input.sizing + checks, {assert});
'''
        result = subprocess.run(
            [shutil.which("node"), "-e", harness],
            input=json.dumps({"full": full_script, "sizing": sizing}),
            text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
