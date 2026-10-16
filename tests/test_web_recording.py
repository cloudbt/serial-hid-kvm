"""Run the actual recorder JS against delayed Blob reads (no capture hardware)."""
import json
import shutil
import subprocess
import unittest

from serial_hid_kvm._web_viewer import _VIEWER_HTML
from serial_hid_kvm._web_audio import RECORDING_AUDIO_MIXER_JS


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
const elements={btnRec:btn};
function element(id){
  if (!elements[id]) elements[id]={value:id==='recAudioSource'?'mic':id.endsWith('Gain')?'70':id==='recHdmiDevice'?'':'default',
    style:{},setAttribute(){},classList:{toggle(){},remove(){},contains(){return false;}},checked:false,
    options:[],add(o){this.options.push(o);},replaceChildren(){this.options=[];},
    addEventListener(){}};
  return elements[id];
}
const document = {getElementById:element,addEventListener(){},createElement(){return {width:0,height:0,getContext(){return {drawImage(){},fillRect(){},fillText(){}};},captureStream(){return canvas.captureStream();}};}};
const location={host:'127.0.0.1:9330'};
const localStorage={getItem(){return null;},setItem(){}};
class Option {constructor(label,value){this.label=label;this.value=value;}}
let micRequests=0, micClosed=0, denyMic=false;
const micTrack={readyState:'live',addEventListener(){},stop(){micClosed++;}};
const navigator={mediaDevices:{
  async enumerateDevices(){return [{kind:'audioinput',deviceId:'earpods-test',label:'EarPods'},
    {kind:'audioinput',deviceId:'hdmi057af2',label:'(USB3 Digital Audio)'}];},
  async getUserMedia(){micRequests++; if(denyMic)throw new Error('permission denied');
    return {getTracks(){return [micTrack];},getAudioTracks(){return [micTrack];}};}
}};
class AudioContext {
  constructor(){this.state='running';this.currentTime=0;this.destination={};}
  async resume(){} async close(){}
  node(){return {connect(){},disconnect(){},gain:{value:0,setTargetAtTime(v){this.value=v;}}};}
  createGain(){return this.node();}
  createAnalyser(){return {...this.node(),fftSize:512,getFloatTimeDomainData(){}};}
  createMediaStreamSource(){return this.node();}
  createMediaStreamDestination(){return {...this.node(),stream:{getAudioTracks(){return [mixedTrack];}}};}
  createConstantSource(){return {...this.node(),offset:{value:0},start(){},stop(){}};}
  createDynamicsCompressor(){const n=this.node();for(const p of ['threshold','knee','ratio','attack','release'])n[p]={value:0};return n;}
}
const mixedTrack={readyState:'live',stop(){}};
const playGain=null, audioOn=false;
const window = {MediaRecorder: true, addEventListener(){}};
const statusEl = {}, container = {focus(){}}, IS_LAN = true;
let resultRecordingId = null, resultRecordingPending = false;
const savedFiles = [];
function recordingFileSaved(msg) { savedFiles.push(msg.path); }
const audioCfg = null, recDest = null;
const crypto = {randomUUID(){return "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";}};
const WebSocket = {OPEN:1};
let ws = {readyState:1,send(v){sent.push(v);}};
let videoMode = false, stoppedTracks = 0;
let lastCaptured;
const canvas = {captureStream(){lastCaptured={audio:[{hdmi:true}],
  getVideoTracks(){return [{stop(){stoppedTracks++;}}];},
  getAudioTracks(){return this.audio;},removeTrack(t){this.audio=this.audio.filter(x=>x!==t);},
  addTrack(t){this.audio.push(t);}};return lastCaptured;}};
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
  assert.equal(recordingFilename(new Date('2026-10-15T05:19:20.163Z')),
    'recording-2026-10-15T14-19-20-163-JST.webm');
  assert.equal(recordingFilename(new Date('2026-12-31T15:00:00.000Z')),
    'recording-2027-01-01T00-00-00-000-JST.webm');
  recConnectionReady=true;
  await startRecording();
  const session=recUpload.id;
  const requests=()=>sent.filter(x=>typeof x==='string').map(x=>JSON.parse(x));
  assert.equal(requests()[0].type,'rec_session_start');
  assert.equal(recSourceSelect.value,'mic');
  assert.deepEqual(lastCaptured.audio,[mixedTrack]);
  recordingUploadEvent({type:'rec_session_ready',session,next:0});
  mediaRecorder.ondataavailable({data:{size:1,arrayBuffer:async()=>new Uint8Array([1]).buffer}});
  await new Promise(setImmediate);
  assert.equal(new Uint8Array(sent[sent.length-1])[0],0x11);
  assert.equal(recUpload.queue.length,1,'data must remain until durable ACK');
  // The first block reached the server, but the ACK was lost. Audio and the
  // SAME recorder must keep running across multiple picture disconnects.
  ws.readyState=3;
  recordingConnectionLost();
  assert.equal(recording,true);
  assert.equal(micClosed,0);
  mediaRecorder.ondataavailable({data:{size:1,arrayBuffer:async()=>new Uint8Array([2]).buffer}});
  await new Promise(setImmediate);
  assert.equal(recUpload.queue.length,2);
  ws={readyState:1,send(v){sent.push(v);}};
  recConnectionReady=true; attachRecordingUpload();
  assert.equal(recUpload.id,session);
  recordingUploadEvent({type:'rec_session_ready',session,next:1});
  assert.equal(recUpload.queue.length,1,'lost ACK must not duplicate block 0');
  assert.equal(new Uint8Array(sent[sent.length-1])[5],2);
  // Manual Stop while disconnected must still save the final delayed Blob.
  ws.readyState=3; recordingConnectionLost();
  stopRecording();
  assert.equal(stoppedTracks,0);
  let releaseTail;
  mediaRecorder.ondataavailable({data:{size:1,arrayBuffer:()=>new Promise(r=>{releaseTail=r;})}});
  const finished=mediaRecorder.onstop();
  await Promise.resolve();
  await startRecording();
  assert.equal(recUpload.id,session);
  releaseTail(new Uint8Array([3]).buffer);
  await finished;
  assert.equal(recording,false);
  assert.equal(recFinishing,true,'saving stays active until server acknowledgement');
  assert.equal(micClosed,1);
  ws={readyState:1,send(v){sent.push(v);}};
  recConnectionReady=true; attachRecordingUpload();
  recordingUploadEvent({type:'rec_session_ready',session,next:2});
  assert.equal(new Uint8Array(sent[sent.length-1])[5],3);
  recordingUploadEvent({type:'rec_ack',session,next:3});
  assert.equal(requests()[requests().length-1].type,'rec_session_finish');
  assert.equal(requests().filter(x=>x.type==='rec_session_finish').length,1);
  recordingUploadEvent({type:'rec_ack',session,next:3}); // Duplicate ACK.
  assert.equal(requests().filter(x=>x.type==='rec_session_finish').length,1);
  recordingUploadEvent({type:'rec_session_saved',session,path:'one.mp4'});
  assert.equal(recFinishing,false);
  assert.equal(recUpload,null);
  const count=sent.length;
  recordingConnectionLost(); recConnectionReady=true; recordingMediaReady();
  await maybeResumeRecording();
  assert.equal(sent.length,count,'manual stop must never automatically start another session');
  // A delayed large Blob is split below the message size limit.
  await startRecording();
  mediaRecorder.ondataavailable({data:{size:600000,arrayBuffer:async()=>new Uint8Array(600000).buffer}});
  await new Promise(setImmediate);
  assert.equal(recUpload.queue.length,3);
  assert(recUpload.queue.every(x=>x.data.byteLength<=256*1024));
  recordingUploadEvent({type:'rec_session_ready',session:recUpload.id,next:0});
  recordingUploadEvent({type:'rec_ack',session:recUpload.id,next:1});
  recordingUploadEvent({type:'rec_ack',session:recUpload.id,next:2});
  recordingUploadEvent({type:'rec_ack',session:recUpload.id,next:3});
  stopRecording(); await mediaRecorder.onstop();
  recordingUploadEvent({type:'rec_session_saved',session:recUpload.id,path:'two.mp4'});
  assert.equal(savedFiles.length,2,'both completions must update saved-file UI');
  // No-audio and permission-denied flows still behave explicitly.
  recSourceSelect.value='none';
  const beforeMic=micRequests;
  await startRecording();
  assert.equal(recording,true);
  assert.equal(micRequests,beforeMic);
  assert.equal(lastCaptured.getAudioTracks().length,0);
  stopRecording(); await mediaRecorder.onstop();
  recordingUploadEvent({type:'rec_session_ready',session:recUpload.id,next:0});
  recordingUploadEvent({type:'rec_session_saved',session:recUpload.id,path:'silent.mp4'});
  recSourceSelect.value='hdmi'; recHdmiSelect.value='';
  const beforeFailure=sent.length;
  await startRecording();
  assert.equal(recording,false);
  assert.equal(sent.length,beforeFailure);
  recSourceSelect.value='mic'; recDeviceSelect.value='default'; denyMic=true;
  await startRecording();
  assert.equal(recording,false);
  assert.equal(sent.length,beforeFailure);
  assert(statusEl.textContent.includes('permission denied'));
  // Standalone listening opens ONLY HDMI, even with No audio recording selected.
  denyMic=false;recSourceSelect.value='none';setRecordingAudioTarget('target1');
  recHdmiSelect.value='auto';
  const beforeListen=micRequests;
  await startHdmiMonitoring();
  assert.equal(micRequests,beforeListen+1);
  assert.equal(hdmiMonitor.mixer.inputs.has('mic'),false);
  const listener=hdmiMonitor;
  // Record->Stop must leave the listener alive and the requested state on.
  recSourceSelect.value='hdmi';
  await startRecording();
  assert.equal(micRequests,beforeListen+1,'HDMI capture must be shared');
  stopRecording();await mediaRecorder.onstop();
  recordingUploadEvent({type:'rec_session_saved',session:recUpload.id,path:'listen.mp4'});
  assert.equal(hdmiMonitor,listener);
  assert.equal(hdmiMonitorWanted,true);
  assert.equal(listener.mixer.closed,false);
  // Listener->Off while recording must leave the recorder output alive.
  await startRecording();
  const outputTrack=recAudioSession.tracks[0];
  stopHdmiMonitoring();
  assert.equal(recording,true);
  assert.equal(recAudioSession.tracks[0],outputTrack);
  assert.equal(recAudioSession.mixer.closed,false);
  assert.equal(hdmiMonitorWanted,false);
  stopRecording();await mediaRecorder.onstop();
  recordingUploadEvent({type:'rec_session_saved',session:recUpload.id,path:'record-only.mp4'});
})()
`;
vm.runInNewContext(stubs + input.mixer + input.recorder + checks, {assert, Uint8Array, Float32Array, Promise, setImmediate})
  .catch(e=>{console.error(e); process.exitCode=1;});
'''
        result = subprocess.run(
            [shutil.which("node"), "-e", harness],
            input=json.dumps({"script": script, "recorder": recorder, "mixer":RECORDING_AUDIO_MIXER_JS}),
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
