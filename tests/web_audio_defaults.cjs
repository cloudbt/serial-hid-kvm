const vm=require('node:vm'),assert=require('node:assert/strict');
const {controls}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const setup=`
let recording=false,recFinishing=false,recStarting=false,recAudioSession=null,recAudioPreview=null;
let recInputDevices=[];
const audioCfg=null,playGain=null,audioOn=false,IS_LAN=true;
const elements={};
class Option{constructor(label,value){this.label=label;this.value=value;}}
function element(id){return elements[id]??=(
 {value:id==='recAudioSource'?'mic':id.endsWith('Gain')?'100':'',checked:false,
 options:[],classList:{toggle(){},remove(){}},addEventListener(){},
 add(o){this.options.push(o);if(this.options.length===1)this.value=o.value;},
 replaceChildren(){this.options=[];this.value='';}});}
const document={getElementById:element};
const location={host:'127.0.0.1:9330'};
let stored=initial;
const localStorage={getItem(){return stored?JSON.stringify(stored):null;},setItem(k,v){stored=JSON.parse(v);}};
const navigator={mediaDevices:{async enumerateDevices(){return devices;},addEventListener(){}}};
let capturedConfig;
class RecordingAudioMixer{
 async open(c){capturedConfig=c;return this;}get tracks(){return [];}
 setGain(){}setMuted(){}setMonitoring(){}close(){}
}
`;
const makeDevice=(deviceId,label)=>({kind:'audioinput',deviceId,label});
const mic=makeDevice('host-earpods','ヘッドセット (EarPods) (05ac:110b)');
const first=[mic,makeDevice('long057af2','デジタル オーディオ インターフェイス (USB3 Digital Audio) (345f:2131)'),makeDevice('longd0c23b','デジタル オーディオ インターフェイス (3- USB3 Digital Audio) (345f:2131)')];
async function create(initial,devices,target){
 const c=vm.createContext({initial,devices,Option:undefined});
 vm.runInContext(setup+controls,c);
 await new Promise(setImmediate);
 vm.runInContext('setRecordingAudioTarget('+JSON.stringify(target)+')',c);
 return {get:code=>vm.runInContext(code,c),ctx:c};
}
(async()=>{
 const old={source:'mix',micGain:'200',hdmiGain:'200',micDevice:'wrong-mic',hdmiDevice:'longd0c23b'};
 const a=await create(old,first,'TargetPC1');
 assert.equal(a.get('recEl("recMicGain").value'),'100');
 assert.equal(a.get('recEl("recHdmiGain").value'),'100');
 assert.equal(a.get('recDeviceSelect.value'),'auto');assert.equal(a.get('recHdmiSelect.value'),'auto');
 assert(a.get('recHdmiSelect.options[0].label').includes('Target1 HDMI · 057AF2'));
 assert.equal(a.get('stored.defaultsVersion'),2);
 await a.get('openRecordingAudio()');
 assert.equal(a.get('capturedConfig.inputs.mic.deviceId'),'host-earpods');
 assert.equal(a.get('capturedConfig.inputs.hdmi.deviceId'),'long057af2');
 assert.equal(a.get('capturedConfig.inputs.mic.gain'),1);
 // The IDs at the other port differ; Windows labels still identify the card.
 const second=[mic,makeDevice('origin250cb90',first[1].label),makeDevice('origin20a9f3d',first[2].label)];
 const b=await create({source:'mix'},second,'target2');
 assert(b.get('recHdmiSelect.options[0].label').includes('Target2 HDMI · 0A9F3D'));
 await b.get('openRecordingAudio()');
 assert.equal(b.get('capturedConfig.inputs.hdmi.deviceId'),'origin20a9f3d');
 // Deliberate future overrides survive reload and reconnect.
 const custom=await create({defaultsVersion:2,source:'mix',micDevice:mic.deviceId,hdmiDevice:second[1].deviceId,micGain:120,hdmiGain:130},second,'target2');
 assert.equal(Number(custom.get('recEl("recMicGain").value')),120);
 assert.equal(Number(custom.get('recEl("recHdmiGain").value')),130);
 assert.equal(custom.get('recHdmiSelect.value'),second[1].deviceId);
 await custom.get('openRecordingAudio()');
 assert.equal(custom.get('capturedConfig.inputs.hdmi.deviceId'),second[1].deviceId);
 const absent=await create({source:'mix'},[mic,first[1]],'target2');
 await assert.rejects(()=>absent.get('openRecordingAudio()'),/Default Target2 HDMI not available/);
 const ambiguous=await create({source:'mix'},[mic,first[1],makeDevice('another057af2',first[1].label)],'target1');
 await assert.rejects(()=>ambiguous.get('openRecordingAudio()'),/Default Target1 HDMI not available/);
 const missingMic=await create({source:'mic'},[first[1]],'target1');
 await assert.rejects(()=>missingMic.get('openRecordingAudio()'),/EarPods microphone not available/);
})().catch(e=>{console.error(e);process.exitCode=1;});
