const assert = require('node:assert/strict');
const vm = require('node:vm');
const {mixer} = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const harness = `
let now=0, denied='', pending=null;
const requests=[], tracks=[], contexts=[], listeners=new Map();
function param(){return {value:0,setTargetAtTime(v){this.value=v;}};}
function node(){return {edges:[],connect(n){this.edges.push(n);return n;},disconnect(){this.edges=[];}};}
function track(id){return {id,readyState:'live',muted:false,stops:0,events:{},
  getSettings(){return {deviceId:id};},
  addEventListener(n,f){(this.events[n]??=[]).push(f);},
  emit(n){for(const f of this.events[n]||[])f();},
  stop(){this.stops++;this.readyState='ended';}};}
function stream(t){return {getAudioTracks(){return [t];},getTracks(){return [t];}};}
const navigator={mediaDevices:{
  addEventListener(n,f){listeners.set(f,n);},removeEventListener(n,f){listeners.delete(f);},
  async getUserMedia(config){
    const id=config.audio.deviceId?.exact||'resolved-default';requests.push(config);
    if(id===denied)throw Error('device absent');
    if(pending)return await pending;
    const t=track(id);tracks.push(t);return stream(t);
  }
}};
class AudioContext {
  constructor(){this.state='running';this.currentTime=0;this.destination=node();this.closed=0;contexts.push(this);}
  async resume(){} async close(){this.closed++;}
  createGain(){return {...node(),gain:param()};}
  createMediaStreamDestination(){this.output=track('mixed-output');return {...node(),stream:stream(this.output)};}
  createDynamicsCompressor(){const n=node();for(const p of ['threshold','knee','ratio','attack','release'])n[p]=param();return n;}
  createConstantSource(){return {...node(),offset:param(),start(){},stop(){}};}
  createMediaStreamSource(s){return {...node(),source:s};}
  createAnalyser(){return {...node(),fftSize:0,getFloatTimeDomainData(a){a.fill(.1);}};}
}
const Date={now(){return now;}};
function setInterval(){return 1;}function clearInterval(){}
`;
const checks = `
(async()=>{
 const states=[],levels=[];
 const m=new RecordingAudioMixer({onState(...s){states.push(s);},onLevel(...s){levels.push(s);}});
 await m.open({inputs:{mic:{deviceId:'mic',gain:.7},hdmi:{deviceId:'hdmi',gain:.7}},monitor:true,monitorVolume:.4});
 const output=m.tracks[0],mic=m.inputs.get('mic'),hdmi=m.inputs.get('hdmi');
 assert.equal(m.tracks.length,1);
 assert(!mic.node.edges.includes(m.monitor),'microphone must never reach speakers');
 assert(hdmi.node.edges.includes(hdmi.monitorGate));
 assert(hdmi.monitorGate.edges.includes(m.monitor));
 const volume=hdmi.volume.gain.value;
 m.setMonitoring(true,.9);
 assert.equal(hdmi.volume.gain.value,volume,'monitor volume must not change recording');
 m.setMuted('hdmi',true);
 assert.equal(hdmi.volume.gain.value,0);
 assert.equal(m.monitor.gain.value,.9,'record mute must not mute monitoring');
 m.setGain('mic',1.2);assert.equal(mic.volume.gain.value,1.2);
 m.setInputAvailable('hdmi',false);
 assert.equal(hdmi.monitorGate.gain.value,0);
 m.setInputAvailable('hdmi',true);
 assert.equal(hdmi.volume.gain.value,0,'recovering input must honor record mute');
 m._tick();assert(levels.some(x=>x[0]==='mic'&&x[1]>.09));
 const firstMic=mic.track;
 firstMic.readyState='ended';firstMic.emit('ended');
 assert.equal(m.tracks[0],output);assert.equal(output.readyState,'live');
 assert.equal(hdmi.track.readyState,'live');
 now=4000;m._tick();await new Promise(setImmediate);
 assert.notEqual(mic.track,firstMic);assert.equal(m.tracks[0],output);
 assert.equal(requests.at(-1).audio.deviceId.exact,'mic');
 assert.equal(mic.volume.gain.value,1.2);
 // Both inputs may disappear. Output and session clock remain alive.
 for(const slot of m.inputs.values()){slot.track.readyState='ended';slot.track.emit('ended');}
 denied='hdmi';now=8000;m._tick();await new Promise(setImmediate);
 assert.equal(m.tracks[0],output);assert.equal(output.readyState,'live');
 denied='';now=12000;m._tick();await new Promise(setImmediate);
 assert.equal(hdmi.state,'live');assert.equal(hdmi.volume.gain.value,0);
 m.close();m.close();assert.equal(output.stops,1);assert.equal(listeners.size,0);
 const count=requests.length;now=20000;m._tick();assert.equal(requests.length,count,'closed mixer must not reacquire devices');
 // Reopening a resolved default input pins its original physical device.
 const pinned=await new RecordingAudioMixer().open({inputs:{mic:{deviceId:'default'}}});
 const p=pinned.inputs.get('mic');p.track.readyState='ended';p.track.emit('ended');
 now+=4000;pinned._tick();await new Promise(setImmediate);
 assert.equal(requests.at(-1).audio.deviceId.exact,'resolved-default');pinned.close();
 // Partial startup failure releases the already-opened first input.
 denied='hdmi';const before=tracks.length;
 await assert.rejects(()=>new RecordingAudioMixer().open({inputs:{mic:{deviceId:'mic'},hdmi:{deviceId:'hdmi'}}}),/device absent/);
 assert.equal(tracks[before].stops,1);assert.equal(contexts.at(-1).closed,1);denied='';
 // Default aliases resolving to the HDMI input must not record it twice.
 await assert.rejects(()=>new RecordingAudioMixer().open({inputs:{mic:{deviceId:'default'},hdmi:{deviceId:'resolved-default'}}}),/different input/);
 // A slow device retry that finishes AFTER Stop must be released immediately.
 const late=await new RecordingAudioMixer().open({inputs:{mic:{deviceId:'mic'}}});
 late.inputs.get('mic').track.emit('ended');let resolve;
 pending=new Promise(r=>resolve=r);now+=4000;late._tick();late.close();
 const orphan=track('mic');resolve(stream(orphan));await new Promise(setImmediate);
 assert.equal(orphan.stops,1);assert.equal(late.tracks[0].readyState,'ended');pending=null;
})()
`;
vm.runInNewContext(harness+mixer+checks,{assert,Float32Array,setImmediate,Promise})
 .catch(e=>{console.error(e);process.exitCode=1;});
