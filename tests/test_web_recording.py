"""Run the actual recorder JS against delayed Blob reads (no capture hardware)."""
import json
import shutil
import subprocess
import unittest

from serial_hid_kvm._web_viewer import _VIEWER_HTML


class WebRecordingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for viewer JS tests")
    def test_final_chunk_order_and_disconnected_session(self):
        script = _VIEWER_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
        recorder = script.split("// --- Screen recording ---", 1)[1]
        recorder = recorder.split("// --- Toolbar show/hide", 1)[0]
        harness = r'''
const vm = require('vm'), assert = require('assert');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
new vm.Script(input.script);
const stubs = `
const sent = [], handlers = {}, btn = {classList:{add(){},remove(){}},
  addEventListener(name, fn){handlers[name]=fn;}};
const document = {getElementById(){return btn;}};
const window = {MediaRecorder: true, addEventListener(){}};
const statusEl = {}, container = {focus(){}}, IS_LAN = true;
const audioCfg = null, recDest = null;
const WebSocket = {OPEN:1};
let ws = {readyState:1,send(v){sent.push(v);}};
let videoMode = false, stoppedTracks = 0;
const canvas = {captureStream(){return {getVideoTracks(){return [{stop(){stoppedTracks++;}}];}};}};
function mediaW(){return 1920;} function mediaH(){return 1080;}
function showHint(){} function wsSend(){}
function setInterval(){return 1;} function clearInterval(){}
class MediaRecorder {
  static isTypeSupported(){return true;}
  start(){} stop(){this.stopCalled=true;}
}
`;
const checks = `
(async () => {
  await startRecording();
  let release;
  const first = new Promise(resolve => {release=resolve;});
  mediaRecorder.ondataavailable({data:{size:1,arrayBuffer:()=>first}});
  stopRecording();
  assert.equal(stoppedTracks,0, 'tracks must survive the final dataavailable');
  mediaRecorder.ondataavailable({data:{size:1,arrayBuffer:async()=>new Uint8Array([2]).buffer}});
  const finished = mediaRecorder.onstop();
  await Promise.resolve();
  assert.equal(sent.length,1, 'rec_stop must wait for Blob reads');
  await startRecording();
  assert.equal(sent.length,1, 'a new recording must wait for finalization');
  release(new Uint8Array([1]).buffer);
  await finished;
  assert.equal(new Uint8Array(sent[1])[1],1);
  assert.equal(new Uint8Array(sent[2])[1],2);
  assert.equal(JSON.parse(sent[3]).type,'rec_stop');
  assert.equal(stoppedTracks,1);
  assert.equal(btn.disabled,false);
  await startRecording();
  const oldSocket=ws;
  oldSocket.readyState=3;
  ws={readyState:1,send(){throw new Error('old chunks sent to reconnected socket');}};
  mediaRecorder.ondataavailable({data:{size:1,arrayBuffer:async()=>new Uint8Array([3]).buffer}});
  stopRecording();
  await mediaRecorder.onstop();
  assert(statusEl.textContent.includes('incomplete'));
  assert.equal(recFinishing,false);
  ws={readyState:1,send(){}};
  await startRecording();
  ws.send=()=>{throw new Error('send failed while closing');};
  stopRecording();
  await mediaRecorder.onstop();
  assert(statusEl.textContent.includes('send failed'));
  assert.equal(recFinishing,false);
  assert.equal(btn.disabled,false);
  // A disconnect during recording arms resume, but a new connection alone
  // must not record stale canvas contents or an old WebRTC track.
  ws={readyState:1,send(v){sent.push(v);}};
  await startRecording();
  const disconnected=ws;
  disconnected.readyState=3;
  recordingConnectionLost();
  assert.equal(recording,false);
  assert.equal(recResumePending,true);
  let releaseTail;
  mediaRecorder.ondataavailable({data:{size:1,arrayBuffer:()=>new Promise(r=>{releaseTail=r;})}});
  const stopped=mediaRecorder.onstop();
  await Promise.resolve();
  ws={readyState:1,send(v){sent.push(v);}};
  recordingMediaReady(); // Before authentication/hello: must not resume.
  await maybeResumeRecording();
  assert.equal(recording,false);
  recConnectionReady=true;
  await maybeResumeRecording(); // Still waiting for fresh video.
  assert.equal(recording,false);
  recordingMediaReady(); // Fresh video arrived, old session still draining.
  assert.equal(recording,false);
  releaseTail(new Uint8Array([9]).buffer);
  await stopped;
  await new Promise(setImmediate);
  assert.equal(recording,true);
  assert.equal(recResumePending,false);
  assert.equal(JSON.parse(sent[sent.length-1]).type,'rec_start');
  assert(statusEl.textContent.includes('new file'));
  const count=sent.length;
  recordingMediaReady();
  await new Promise(setImmediate);
  assert.equal(sent.length,count, 'video frames must not start duplicate files');
  // A deliberate stop is never followed by automatic recording.
  stopRecording();
  await mediaRecorder.onstop();
  recordingMediaReady();
  await maybeResumeRecording();
  assert.equal(recording,false);
  // Waiting for reconnect can be cancelled explicitly with the same button.
  await startRecording();
  ws.readyState=3;
  recordingConnectionLost();
  await mediaRecorder.onstop();
  handlers.click();
  assert.equal(recResumePending,false);
})()
`;
vm.runInNewContext(stubs + input.recorder + checks, {assert, Uint8Array, Promise, setImmediate})
  .catch(e=>{console.error(e); process.exitCode=1;});
'''
        result = subprocess.run(
            [shutil.which("node"), "-e", harness],
            input=json.dumps({"script": script, "recorder": recorder}),
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
