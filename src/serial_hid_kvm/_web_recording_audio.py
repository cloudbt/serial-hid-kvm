"""Recording audio controls for the embedded browser viewer."""

RECORDING_AUDIO_CONTROLS_JS = r"""
const recSourceSelect = document.getElementById("recAudioSource");
const recDeviceSelect = document.getElementById("recAudioDevice");
const recHdmiSelect = document.getElementById("recHdmiDevice");
const recAudioStatus = document.getElementById("recAudioStatus");
const recInputStates = {mic:"idle", hdmi:"idle"};
let recAudioBusy = false;
let recAudioTarget = "";
const recEl = id => document.getElementById(id);
// IDs are origin-scoped: the same card has a different ID at :9332. Match the
// user-confirmed :9330 suffixes, then the distinct Windows device names.
const recHdmiDefaults = {
  target1:{suffix:"057af2", name:/\(USB3 Digital Audio\)/i, label:"Target1 HDMI"},
  target2:{suffix:"d0c23b", name:/\(3-\s*USB3 Digital Audio\)/i, label:"Target2 HDMI"}
};
function recordingDeviceTarget(device) {
  const suffix = device.deviceId.slice(-6).toLowerCase();
  for (const [target, rule] of Object.entries(recHdmiDefaults))
    if (suffix === rule.suffix) return target;
  for (const [target, rule] of Object.entries(recHdmiDefaults))
    if (rule.name.test(device.label || "")) return target;
  return null;
}
function recordingDefaultDevice(name) {
  const matches = recInputDevices.filter(d => !["default","communications"].includes(d.deviceId) &&
    (name === "mic" ? /EarPods/i.test(d.label || "") : !!recHdmiDefaults[recAudioTarget] && recordingDeviceTarget(d) === recAudioTarget));
  return matches.length === 1 ? matches[0] : null;
}
function setRecordingAudioTarget(name) {
  recAudioTarget = String(name || "").toLowerCase().replace(/[\s_-]/g, "").replace("targetpc", "target");
  updateRecordingDevices();
}
function recUses(name) {
  return recSourceSelect.value === name || recSourceSelect.value === "mix";
}
function recAudioKey() { return "recording-audio:" + location.host; }
function rememberRecordingAudio() {
  try { localStorage.setItem(recAudioKey(), JSON.stringify({
    defaultsVersion:2, source:recSourceSelect.value, micDevice:recDeviceSelect.value, hdmiDevice:recHdmiSelect.value,
    micGain:recEl("recMicGain").value, hdmiGain:recEl("recHdmiGain").value,
    monitorGain:recEl("recMonitorGain").value})); } catch (e) {}
}
function updateRecordingDevices(preferredMic, preferredHdmi) {
  if (recAudioBusy || recording || recFinishing || recStarting) return;
  for (const [name, select, preferred] of [["mic",recDeviceSelect,preferredMic],["hdmi",recHdmiSelect,preferredHdmi]]) {
    const old = preferred === undefined ? select.value : preferred;
    select.replaceChildren();
    const add = (value, label) => select.add(new Option(label, value));
    const device = recordingDefaultDevice(name);
    const defaultName = name === "mic" ? "EarPods" : recHdmiDefaults[recAudioTarget]?.label || "this Target's HDMI";
    add("auto", "Default — " + defaultName + (device ? " · " + device.deviceId.slice(-6).toUpperCase() : " (not available)"));
    if (name === "mic") add("default", "Windows default microphone");
    else add("", "Select an HDMI input manually…");
    if (name === "hdmi" && audioCfg) add("server", "Target HDMI (server capture)");
    for (const d of recInputDevices) {
      if (["default","communications"].includes(d.deviceId)) continue;
      const target = recordingDeviceTarget(d);
      const label = target ? recHdmiDefaults[target].label : /EarPods/i.test(d.label || "") ? "Host Mic — EarPods" : d.label || "Audio input";
      add(d.deviceId, label + " · " + d.deviceId.slice(-6).toUpperCase());
    }
    if (old && ![...select.options].some(o => o.value === old))
      add(old, "Saved device unavailable · " + old.slice(-6));
    if ([...select.options].some(o => o.value === old)) select.value = old;
  }
  updateRecordingAudioControls();
}
function updateRecordingAudioControls() {
  recEl("recMicChannel").hidden = !recUses("mic");
  recEl("recHdmiChannel").hidden = !recUses("hdmi");
  recEl("recMonitorControls").hidden = !recEl("recMonitor").checked;
  for (const name of ["Mic","Hdmi","Monitor"])
    recEl("rec"+name+"GainValue").textContent = recEl("rec"+name+"Gain").value + "%";
}
function syncLegacyAudioPlayback() {
  const hdmiOwned = recAudioSession?.hasHdmi || recAudioPreview?.hasHdmi;
  if (playGain) playGain.gain.value = audioOn && !hdmiOwned ? 1 : 0;
  const btn = recEl("btnAudio");
  btn.disabled = !!hdmiOwned;
  btn.title = hdmiOwned ? "Use HDMI monitoring in Recording audio" : "Toggle audio playback";
}
function recordingBackendAudioConnection(connected) {
  const active = recAudioSession || recAudioPreview;
  if (recHdmiSelect.value === "server") active?.mixer?.setInputAvailable("hdmi", connected);
}
async function refreshRecordingInputs(requestPermission = false) {
  if (!navigator.mediaDevices) throw new Error("Audio input needs localhost or HTTPS");
  if (requestPermission) {
    const permission = await navigator.mediaDevices.getUserMedia({audio:true, video:false});
    permission.getTracks().forEach(t => t.stop());
  }
  recInputDevices = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === "audioinput");
  updateRecordingDevices();
}
function lockRecordingAudio(locked) {
  for (const id of ["recAudioSource","recAudioDevice","recHdmiDevice","btnRecInputs","btnRecCheck"])
    recEl(id).disabled = locked;
}
async function openRecordingAudio() {
  if (recSourceSelect.value === "none") return {tracks:[], close(){}};
  await refreshRecordingInputs();
  const selectedDevice = name => {
    const id = (name === "mic" ? recDeviceSelect : recHdmiSelect).value;
    if (id !== "auto") return id;
    const device = recordingDefaultDevice(name);
    if (!device) throw new Error(name === "mic"
      ? "Default EarPods microphone not available. Connect EarPods and click Inputs, or choose a microphone manually."
      : "Default " + (recHdmiDefaults[recAudioTarget]?.label || "Target HDMI") + " not available. Click Inputs or choose its HDMI device manually.");
    return device.deviceId;
  };
  const inputs = {};
  if (recUses("mic")) inputs.mic = {deviceId:selectedDevice("mic"),
    gain:Number(recEl("recMicGain").value)/100, muted:recEl("recMicMute").checked};
  if (recUses("hdmi")) {
    const id = selectedDevice("hdmi");
    if (!id || id === "default") throw new Error("Click Inputs, then select this Target's HDMI capture audio device");
    inputs.hdmi = {deviceId:id, gain:Number(recEl("recHdmiGain").value)/100, muted:recEl("recHdmiMute").checked};
    if (id === "server") inputs.hdmi.open = async () => {
      if (!audioCfg) throw new Error("Target HDMI audio is not available");
      if (!IS_LAN) wsSend({type:"audio", on:true});
      await ensureAudioForRecording();
      return {stream:recDest.stream, close(){
        if (!IS_LAN && !audioOn) wsSend({type:"audio", on:false});
      }};
    };
    if (inputs.mic && inputs.mic.deviceId !== "default" && inputs.mic.deviceId === id)
      throw new Error("Host Mic and Target HDMI must be different input devices");
  }
  const mixer = new RecordingAudioMixer({
    onState(name, state) {
      recInputStates[name] = state;
      const tag = recEl(name === "mic" ? "recMicState" : "recHdmiState");
      tag.textContent = state === "live" ? "Ready" : "Interrupted · retrying";
      tag.classList.toggle("interrupted", state !== "live");
      if (recording && state !== "live")
        showHint((name === "mic" ? "Host Mic" : "Target HDMI") + " interrupted — recording continues; waiting for this device");
      const lost = Object.keys(inputs).filter(n => recInputStates[n] !== "live");
      recAudioStatus.textContent = lost.length
        ? lost.map(n => n === "mic" ? "Host Mic" : "Target HDMI").join(" + ") + " unavailable — remaining inputs continue; waiting for recovery"
        : "Audio ready · record volume and headphone volume are independent";
    },
    onLevel(name, rms, peak) {
      recEl(name === "mic" ? "recAudioLevel" : "recHdmiLevel").value = Math.min(1, rms*4);
      if (recInputStates[name] === "live") {
        const muted = recEl(name === "mic" ? "recMicMute" : "recHdmiMute").checked;
        recEl(name === "mic" ? "recMicState" : "recHdmiState").textContent = muted ? "Record muted" : peak > 0.98 ? "Input too loud" : rms < 0.001 ? "Ready · quiet" : "Signal";
      }
    }
  });
  await mixer.open({inputs, monitor:recUses("hdmi") && recEl("recMonitor").checked,
    monitorVolume:Number(recEl("recMonitorGain").value)/100});
  // Gain controls remain usable while a permission/device open is pending.
  for (const name of ["Mic","Hdmi"]) {
    mixer.setGain(name.toLowerCase(), Number(recEl("rec"+name+"Gain").value)/100);
    mixer.setMuted(name.toLowerCase(), recEl("rec"+name+"Mute").checked);
  }
  mixer.setMonitoring(recUses("hdmi") && recEl("recMonitor").checked, Number(recEl("recMonitorGain").value)/100);
  return {mixer, hasHdmi:!!inputs.hdmi, tracks:mixer.tracks, close(){
    mixer.close();
    for (const id of ["recAudioLevel","recHdmiLevel"]) recEl(id).value = 0;
    for (const id of ["recMicState","recHdmiState"]) {
      recEl(id).textContent = "Input released"; recEl(id).classList.remove("interrupted");
    }
  }};
}
function closeRecordingPreview() {
  if (recAudioPreview) recAudioPreview.close();
  recAudioPreview = null;
  syncLegacyAudioPlayback();
  recEl("btnRecCheck").textContent = "Check audio";
}
recSourceSelect.addEventListener("change", () => {
  closeRecordingPreview(); updateRecordingAudioControls(); rememberRecordingAudio();
  recAudioStatus.textContent = "";
});
for (const select of [recDeviceSelect, recHdmiSelect]) select.addEventListener("change", () => {
  closeRecordingPreview(); rememberRecordingAudio(); recAudioStatus.textContent = "";
});
for (const name of ["Mic","Hdmi"]) {
  const source = name.toLowerCase();
  recEl("rec"+name+"Gain").addEventListener("input", () => {
    (recAudioSession || recAudioPreview)?.mixer?.setGain(source, Number(recEl("rec"+name+"Gain").value)/100);
    updateRecordingAudioControls(); rememberRecordingAudio();
  });
  recEl("rec"+name+"Mute").addEventListener("change", () => {
    (recAudioSession || recAudioPreview)?.mixer?.setMuted(source, recEl("rec"+name+"Mute").checked);
  });
}
for (const id of ["recMonitor","recMonitorGain"]) recEl(id).addEventListener("input", () => {
  (recAudioSession || recAudioPreview)?.mixer?.setMonitoring(recEl("recMonitor").checked, Number(recEl("recMonitorGain").value)/100);
  updateRecordingAudioControls(); rememberRecordingAudio();
});
recEl("btnRecInputs").addEventListener("click", async () => {
  recAudioBusy = true;
  closeRecordingPreview(); lockRecordingAudio(true); recEl("btnRec").disabled = true;
  try { await refreshRecordingInputs(true); }
  catch (e) { recAudioStatus.textContent = "Audio access failed: " + e.message; }
  finally { recAudioBusy = false; updateRecordingDevices(); lockRecordingAudio(false); recEl("btnRec").disabled = false; }
});
recEl("btnRecCheck").addEventListener("click", async () => {
  if (recAudioPreview) { closeRecordingPreview(); recAudioStatus.textContent = "Audio check stopped"; return; }
  recAudioBusy = true;
  lockRecordingAudio(true); recEl("btnRec").disabled = true;
  try {
    recAudioPreview = await openRecordingAudio(); syncLegacyAudioPlayback();
    recEl("btnRecCheck").textContent = "Stop check";
    await refreshRecordingInputs();
  } catch (e) { recAudioStatus.textContent = "Audio check failed: " + e.message; }
  finally { recAudioBusy = false; updateRecordingDevices(); lockRecordingAudio(false); recEl("btnRec").disabled = false; }
});
try {
  const saved = JSON.parse(localStorage.getItem(recAudioKey()) || "null");
  const currentDefaults = saved?.defaultsVersion === 2;
  if (!currentDefaults) {
    recEl("recMicGain").value = "100";
    recEl("recHdmiGain").value = "100";
  }
  if (saved && ["mic","hdmi","mix","none"].includes(saved.source)) recSourceSelect.value = saved.source;
  for (const name of ["mic","hdmi","monitor"]) {
    const value = Number(saved?.[name+"Gain"]);
    if ((currentDefaults || name === "monitor") && saved?.[name+"Gain"] !== undefined && Number.isFinite(value))
      recEl("rec"+name[0].toUpperCase()+name.slice(1)+"Gain").value = Math.max(0, Math.min(name === "monitor" ? 100 : 200, value));
  }
  // Apply the requested defaults once to existing profiles, including old 200%
  // gains or a manually selected wrong card. Later user overrides are retained.
  const mic = currentDefaults ? saved.micDevice || "auto" : "auto";
  const hdmi = currentDefaults ? saved.hdmiDevice || "auto" : "auto";
  updateRecordingDevices(mic, hdmi);
  if (!currentDefaults) rememberRecordingAudio();
  refreshRecordingInputs().then(() => {
    updateRecordingDevices();
  }).catch(() => {});
} catch (e) { updateRecordingDevices(); }
navigator.mediaDevices?.addEventListener?.("devicechange", () => {
  refreshRecordingInputs().catch(() => {});
});
"""
