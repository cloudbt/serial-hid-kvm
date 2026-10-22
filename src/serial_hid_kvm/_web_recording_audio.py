"""Recording audio controls for the embedded browser viewer."""

RECORDING_AUDIO_CONTROLS_JS = r"""
const recSourceSelect = document.getElementById("recAudioSource");
const recDeviceSelect = document.getElementById("recAudioDevice");
const recHdmiSelect = document.getElementById("recHdmiDevice");
const recAudioStatus = document.getElementById("recAudioStatus");
const recInputStates = {mic:"idle", hdmi:"idle"};
let recAudioBusy = false;
let recAudioTarget = "";
const recAudioInputs = new RecordingAudioInputPool();
let recServerAudioRefs = 0;
let recServerAudioConnected = true;
let hdmiMonitor = null;
let hdmiMonitorWanted = false;
let hdmiMonitorEpoch = 0;
let hdmiMonitorState = "off";
let autoHdmiMonitorPending = true;  // HDMI listening defaults ON (first hello)
const recEl = id => document.getElementById(id);
// IDs are origin-scoped: the same card has a different ID at :9332. Match the
// user-confirmed :9330 suffixes, then the distinct Windows device names.
const recHdmiDefaults = {
  target1:{suffix:"057af2", name:/\(USB3 Digital Audio\)/i, label:"Target1 HDMI"},
  target2:{suffix:"d0c23b", name:/\(3-\s*USB3 Digital Audio\)/i, label:"Target2 HDMI"},
  // No confirmed :9330 suffix yet; the Windows name is unique (checked via PnP container).
  target3:{suffix:null, name:/\(4-\s*USB3 Digital Audio\)/i, label:"Target3 HDMI"}
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
// Auto: this Target's capture card when the browser runs on the Host;
// otherwise (phone, iPad, other PC) the server's HDMI capture over the socket.
function recordingAutoDevice(name) {
  const device = recordingDefaultDevice(name);
  if (device) return device.deviceId;
  return name === "hdmi" && audioCfg ? "server" : null;
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
    const auto = recordingAutoDevice(name);
    const defaultName = name === "mic" ? "EarPods" : recHdmiDefaults[recAudioTarget]?.label || "this Target's HDMI";
    add("auto", "Default — " + defaultName + (device ? " · " + device.deviceId.slice(-6).toUpperCase()
      : auto === "server" ? " (server capture)" : " (not available)"));
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
  for (const name of ["Mic","Hdmi","Monitor"])
    recEl("rec"+name+"GainValue").textContent = recEl("rec"+name+"Gain").value + "%";
  recEl("btnMonitorVolume").textContent = recEl("recMonitorGain").value + "%";
}
function syncLegacyAudioPlayback() {
  // There is one playback control: the independent toolbar HDMI listener.
  if (playGain) playGain.gain.value = 0;
  recEl("btnAudio").style.display = "none";
}
function recordingBackendAudioConnection(connected) {
  if (connected && !recServerAudioConnected && recServerAudioRefs && !IS_LAN)
    wsSend({type:"audio", on:true});
  recServerAudioConnected = connected;
  for (const active of [recAudioSession, recAudioPreview])
    if (active?.serverHdmi) active.mixer?.setInputAvailable("hdmi", connected);
  if (hdmiMonitor?.serverHdmi) hdmiMonitor.mixer.setInputAvailable("hdmi", connected);
}
function selectedRecordingDevice(name) {
  const id = (name === "mic" ? recDeviceSelect : recHdmiSelect).value;
  if (id !== "auto") return id;
  const device = recordingAutoDevice(name);
  if (!device) throw new Error(name === "mic"
    ? "Default EarPods microphone not available. Connect EarPods and click Inputs, or choose a microphone manually."
    : "Default " + (recHdmiDefaults[recAudioTarget]?.label || "Target HDMI") + " not available. Click Inputs or choose its HDMI device manually.");
  return device;
}
// Keeps the server PCM feed subscribed (remote viewers) while anyone uses it.
function acquireServerAudioFeed() {
  recServerAudioRefs++;
  if (!IS_LAN) wsSend({type:"audio", on:true});
  let released = false;
  return () => {
    if (released) return;
    released = true;
    if (--recServerAudioRefs === 0 && !IS_LAN) wsSend({type:"audio", on:false});
  };
}
function recordingInputLease(deviceId) {
  if (deviceId !== "server") return {deviceId, open:id => recAudioInputs.acquire(id || deviceId)};
  return {deviceId, open:async () => {
    if (!audioCfg) throw new Error("Target HDMI audio is not available");
    await ensureAudioForRecording();
    return {stream:recDest.stream, close:acquireServerAudioFeed()};
  }};
}
// Listening to the server's HDMI audio plays inside audioCtx (worklet →
// serverMonitorGain → speakers), never through a MediaStream into a second
// context: on iOS that bridge is silent unless a microphone capture is open,
// and crackles when one is. Same interface as the mixer for the callers.
async function openServerHdmiMonitor() {
  if (!audioCfg) throw new Error("Target HDMI audio is not available");
  await startAudio();
  if (audioCtx.state !== "running") {
    // Without a user gesture iOS may leave resume() pending forever.
    await Promise.race([audioCtx.resume().catch(() => {}), new Promise(r => setTimeout(r, 1500))]);
    if (audioCtx.state !== "running") throw new Error("Audio engine is suspended; click again to allow audio");
  }
  const release = acquireServerAudioFeed();
  let closed = false;
  const ramp = value => serverMonitorGain?.gain.setTargetAtTime(value, audioCtx.currentTime, 0.02);
  return {
    setMonitoring(on, volume) { if (!closed) ramp(on ? Math.max(0, Math.min(1, Number(volume) || 0)) : 0); },
    setInputAvailable(name, available) {
      if (closed || hdmiMonitor?.mixer !== this) return;
      hdmiMonitorState = available ? "live" : "interrupted";
      updateHdmiMonitorControls();
    },
    close() { if (closed) return; closed = true; ramp(0); release(); }
  };
}
function updateHdmiMonitorControls(detail = "") {
  const btn = recEl("btnHdmiMonitor");
  const state = hdmiMonitorState;
  btn.setAttribute("aria-pressed", String(hdmiMonitorWanted));
  btn.classList.toggle("active", hdmiMonitorWanted);
  btn.classList.toggle("monitor-error", state === "interrupted" || state === "error");
  const status = state === "off" ? "HDMI listening off" : state === "opening"
    ? "Opening HDMI input…" : state === "live" ? "Listening to Target HDMI"
    : state === "interrupted" ? "HDMI input interrupted — waiting for recovery" : "HDMI listening failed";
  recEl("hdmiMonitorStatus").textContent = status + (detail ? ": " + detail : "");
  btn.title = status + (detail ? ": " + detail : "") + ". Output: Host default playback device; does not change recorded audio.";
}
function stopHdmiMonitoring() {
  hdmiMonitorWanted = false;
  syncAudioSession();
  ++hdmiMonitorEpoch;
  hdmiMonitor?.mixer.close();
  hdmiMonitor = null;
  hdmiMonitorState = "off";
  updateHdmiMonitorControls();
}
// iOS (Safari 16.4+): "playback" keeps Web Audio on the speaker and ignores
// the silent switch; the Mic button's capture needs "play-and-record".
function setAudioSessionType(type) {
  try {
    if (navigator.audioSession && navigator.audioSession.type !== type) navigator.audioSession.type = type;
  } catch (e) {}
}
function syncAudioSession() {
  const mic = typeof micWanted !== "undefined" && micWanted;
  setAudioSessionType(mic ? "play-and-record" : hdmiMonitorWanted ? "playback" : "auto");
}
// iOS starts an AudioContext only from inside a user gesture, so a click
// creates and resumes the listener's contexts before the first await.
function gestureAudioContext() {
  unlockServerAudio();
  // Server audio plays in audioCtx (unlocked above); only a local capture
  // card needs the mixer's own context.
  const id = recHdmiSelect.value;
  if (id === "server" || (id === "auto" && recordingAutoDevice("hdmi") === "server")) return null;
  try {
    const ctx = new AudioContext({sampleRate:48000});
    ctx.resume().catch(() => {});
    return ctx;
  } catch (e) { return null; }
}
async function startHdmiMonitoring({auto = false, context = null} = {}) {
  const epoch = ++hdmiMonitorEpoch;
  hdmiMonitorWanted = true;
  syncAudioSession();
  hdmiMonitorState = "opening";
  updateHdmiMonitorControls();
  let mixer;
  try {
    await refreshRecordingInputs();
    if (epoch !== hdmiMonitorEpoch) { context?.close().catch(() => {}); return; }
    const id = selectedRecordingDevice("hdmi");
    if (!id || id === "default") throw new Error("Select this Target's HDMI input in Recording audio");
    if (id === "server") {
      context?.close().catch(() => {});
      context = null;
      const monitor = await openServerHdmiMonitor();
      if (epoch !== hdmiMonitorEpoch) { monitor.close(); return; }
      hdmiMonitor = {mixer:monitor, serverHdmi:true};
      monitor.setMonitoring(true, Number(recEl("recMonitorGain").value)/100);
      hdmiMonitorState = "live";
      updateHdmiMonitorControls();
      return;
    }
    mixer = new RecordingAudioMixer({onState(name, state, detail){
      if (epoch !== hdmiMonitorEpoch) return;
      hdmiMonitorState = state;
      updateHdmiMonitorControls(detail);
    }});
    // Open muted. A canceled pending request must never play even briefly.
    const opening = mixer.open({inputs:{hdmi:{...recordingInputLease(id), gain:0}}, monitor:false, context});
    context = null;  // the mixer owns it now
    await opening;
    if (epoch !== hdmiMonitorEpoch) { mixer.close(); return; }
    hdmiMonitor = {mixer, serverHdmi:id === "server"};
    mixer.setMonitoring(true, Number(recEl("recMonitorGain").value)/100);
  } catch (error) {
    mixer?.close();
    context?.close().catch(() => {});
    if (epoch !== hdmiMonitorEpoch) return;
    if (auto && /suspended/i.test(error.message)) {
      // Browsers keep audio suspended until a user gesture: stay ON and
      // retry on the first click/key anywhere on the page.
      updateHdmiMonitorControls("click anywhere to start audio");
      const retry = () => {
        window.removeEventListener("pointerdown", retry, true);
        window.removeEventListener("keydown", retry, true);
        if (hdmiMonitorWanted && epoch === hdmiMonitorEpoch) startHdmiMonitoring({context:gestureAudioContext()});
      };
      window.addEventListener("pointerdown", retry, true);
      window.addEventListener("keydown", retry, true);
      return;
    }
    hdmiMonitorWanted = false;
    hdmiMonitorState = "error";
    syncAudioSession();
    updateHdmiMonitorControls(error.message);
    if (!auto) showHint("HDMI listening failed: " + error.message);
  }
}
function autoStartHdmiMonitoring() {
  if (!hdmiMonitorWanted) startHdmiMonitoring({auto:true});
}
recEl("btnHdmiMonitor").addEventListener("click", () => {
  if (hdmiMonitorWanted) stopHdmiMonitoring(); else startHdmiMonitoring({context:gestureAudioContext()});
});
recEl("btnMonitorVolume").addEventListener("click", () => {
  const panel = recEl("hdmiVolumePanel");
  const open = !panel.classList.contains("open");
  panel.classList.toggle("open", open);
  recEl("btnMonitorVolume").setAttribute("aria-expanded", String(open));
});
function closeHdmiVolumePanel() {
  recEl("hdmiVolumePanel").classList.remove("open");
  recEl("btnMonitorVolume").setAttribute("aria-expanded", "false");
}
document.addEventListener("pointerdown", e => {
  if (!recEl("hdmiMonitorControls").contains(e.target)) closeHdmiVolumePanel();
});
document.addEventListener("keydown", e => { if (e.key === "Escape") closeHdmiVolumePanel(); });
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
  const inputs = {};
  if (recUses("mic")) inputs.mic = {...recordingInputLease(selectedRecordingDevice("mic")),
    gain:Number(recEl("recMicGain").value)/100, muted:recEl("recMicMute").checked};
  if (recUses("hdmi")) {
    const id = selectedRecordingDevice("hdmi");
    if (!id || id === "default") throw new Error("Click Inputs, then select this Target's HDMI capture audio device");
    inputs.hdmi = {...recordingInputLease(id), gain:Number(recEl("recHdmiGain").value)/100, muted:recEl("recHdmiMute").checked};
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
  await mixer.open({inputs, monitor:false});
  // Gain controls remain usable while a permission/device open is pending.
  for (const name of ["Mic","Hdmi"]) {
    mixer.setGain(name.toLowerCase(), Number(recEl("rec"+name+"Gain").value)/100);
    mixer.setMuted(name.toLowerCase(), recEl("rec"+name+"Mute").checked);
  }
  return {mixer, hasHdmi:!!inputs.hdmi, serverHdmi:inputs.hdmi?.deviceId === "server", tracks:mixer.tracks, close(){
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
  if (select === recHdmiSelect && hdmiMonitorWanted) {
    stopHdmiMonitoring(); startHdmiMonitoring();
  }
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
recEl("recMonitorGain").addEventListener("input", () => {
  hdmiMonitor?.mixer.setMonitoring(true, Number(recEl("recMonitorGain").value)/100);
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
updateHdmiMonitorControls();
navigator.mediaDevices?.addEventListener?.("devicechange", () => {
  refreshRecordingInputs().catch(() => {});
});
"""
