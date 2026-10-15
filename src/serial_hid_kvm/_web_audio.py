"""Browser recording mixer, embedded in the viewer without a JS build step."""

RECORDING_AUDIO_MIXER_JS = r"""
class RecordingAudioMixer {
  constructor({onState = () => {}, onLevel = () => {}} = {}) {
    this.onState = onState;
    this.onLevel = onLevel;
    this.inputs = new Map();
    this.closed = false;
    this.monitorOn = false;
    this.monitorVolume = 0.5;
  }
  async open(config) {
    try {
      this.ctx = new AudioContext({sampleRate:48000});
      await this.ctx.resume();
      if (this.ctx.state !== "running") throw new Error("Audio engine is suspended; click again to allow audio");
      this.destination = this.ctx.createMediaStreamDestination();
      this.compressor = this.ctx.createDynamicsCompressor();
      this.compressor.threshold.value = -6;
      this.compressor.knee.value = 6;
      this.compressor.ratio.value = 12;
      this.compressor.attack.value = 0.003;
      this.compressor.release.value = 0.15;
      this.compressor.connect(this.destination);
      // A silent, continuous clock keeps the recorder's ONE output track alive
      // even when every physical input disappears. Never replace this track.
      this.clock = this.ctx.createConstantSource();
      this.clock.offset.value = 0;
      this.clock.connect(this.compressor);
      this.clock.start();
      this.monitor = this.ctx.createGain();
      this.monitor.gain.value = 0;
      this.monitor.connect(this.ctx.destination);
      for (const [name, options] of Object.entries(config.inputs)) {
        const slot = {name, ...options, state:"opening", gain:options.gain ?? 1,
          muted:!!options.muted, available:true, generation:0, opening:false, retryAt:0};
        slot.volume = this.ctx.createGain();
        slot.volume.gain.value = slot.muted ? 0 : slot.gain;
        slot.volume.connect(this.compressor);
        this.inputs.set(name, slot);
        await this._acquire(slot);
      }
      const mic = this.inputs.get("mic"), hdmi = this.inputs.get("hdmi");
      if (mic?.actualId && hdmi?.actualId && mic.actualId === hdmi.actualId)
        throw new Error("Host Mic and Target HDMI must be different input devices");
      this.setMonitoring(!!config.monitor, config.monitorVolume ?? 0.5);
      this.timer = setInterval(() => this._tick(), 100);
      this.deviceChange = () => {
        for (const slot of this.inputs.values()) slot.retryAt = 0;
        this._tick();
      };
      navigator.mediaDevices?.addEventListener?.("devicechange", this.deviceChange);
      return this;
    } catch (e) { this.close(); throw e; }
  }
  get outputStream() { return this.destination?.stream; }
  get tracks() { return this.outputStream?.getAudioTracks() || []; }
  _state(slot, state, detail = "") {
    if (slot.state === state && slot.detail === detail) return;
    slot.state = state; slot.detail = detail;
    this.onState(slot.name, state, detail);
  }
  async _acquire(slot) {
    if (this.closed || slot.opening) return;
    slot.opening = true;
    const generation = ++slot.generation;
    let resource = null;
    try {
      if (slot.open) {
        resource = await slot.open();
      } else {
        if (!navigator.mediaDevices) throw new Error("Audio input needs localhost or HTTPS");
        const audio = {echoCancellation:false, noiseSuppression:false, autoGainControl:false};
        // Pin the resolved default device after the first acquisition. Recovery
        // must never silently switch to a different microphone/capture card.
        const id = slot.actualId || slot.deviceId;
        if (id && id !== "default") audio.deviceId = {exact:id};
        const stream = await navigator.mediaDevices.getUserMedia({video:false, audio});
        resource = {stream, close(){stream.getTracks().forEach(t => t.stop());}};
      }
      if (this.closed || slot.generation !== generation) { resource.close(); return; }
      const track = resource.stream.getAudioTracks()[0];
      if (!track || track.readyState !== "live") throw new Error("Input has no live audio track");
      const actualId = track.getSettings?.().deviceId;
      const other = this.inputs.get(slot.name === "mic" ? "hdmi" : "mic");
      if (actualId && other?.actualId === actualId)
        throw new Error("Host Mic and Target HDMI must be different input devices");
      const node = this.ctx.createMediaStreamSource(resource.stream);
      const analyser = this.ctx.createAnalyser();
      analyser.fftSize = 512;
      node.connect(analyser);
      node.connect(slot.volume);
      if (slot.name === "hdmi") {
        slot.monitorGate = this.ctx.createGain();
        slot.monitorGate.gain.value = 1;
        node.connect(slot.monitorGate); slot.monitorGate.connect(this.monitor);
      }
      slot.resource = resource; slot.track = track; slot.node = node;
      slot.analyser = analyser; slot.samples = new Float32Array(analyser.fftSize);
      slot.actualId = actualId || slot.actualId;
      slot.mutedAt = track.muted ? Date.now() : 0;
      const current = () => !this.closed && slot.generation === generation;
      track.addEventListener("ended", () => { if (current()) this._lost(slot, "Device disconnected"); });
      track.addEventListener("mute", () => {
        if (!current()) return;
        slot.mutedAt = Date.now();
        this._state(slot, "interrupted", "Input temporarily unavailable");
      });
      track.addEventListener("unmute", () => {
        if (!current()) return;
        slot.mutedAt = 0;
        this._state(slot, "live");
      });
      this._state(slot, track.muted ? "interrupted" : "live");
    } catch (e) {
      if (resource && slot.resource !== resource) resource.close();
      if (!this.closed) this._state(slot, "interrupted", e.message);
      throw e;
    } finally { slot.opening = false; }
  }
  _release(slot) {
    ++slot.generation;
    slot.node?.disconnect();
    slot.analyser?.disconnect();
    slot.monitorGate?.disconnect();
    slot.resource?.close();
    slot.resource = slot.node = slot.analyser = slot.track = null;
  }
  _lost(slot, reason) {
    this._release(slot);
    slot.retryAt = Date.now() + 2000;
    this._state(slot, "interrupted", reason);
    this.onLevel(slot.name, 0, 0);
  }
  _tick() {
    if (this.closed) return;
    for (const slot of this.inputs.values()) {
      if (slot.track?.readyState === "ended") this._lost(slot, "Device disconnected");
      if (slot.track?.muted && slot.mutedAt && Date.now() - slot.mutedAt > 3000)
        this._lost(slot, "Input unavailable; reconnecting selected device");
      if (!slot.resource && !slot.opening && Date.now() >= slot.retryAt) {
        slot.retryAt = Date.now() + 3000;
        this._acquire(slot).catch(() => {});
      }
      if (slot.analyser && !slot.track?.muted && slot.available) {
        slot.analyser.getFloatTimeDomainData(slot.samples);
        let sum = 0, peak = 0;
        for (const x of slot.samples) { sum += x*x; peak = Math.max(peak, Math.abs(x)); }
        this.onLevel(slot.name, Math.sqrt(sum / slot.samples.length), peak);
      } else this.onLevel(slot.name, 0, 0);
    }
  }
  setGain(name, value) {
    const slot = this.inputs.get(name);
    if (!slot || this.closed) return;
    slot.gain = Math.max(0, Math.min(2, Number(value) || 0));
    slot.volume.gain.setTargetAtTime(slot.muted || !slot.available ? 0 : slot.gain, this.ctx.currentTime, 0.02);
  }
  setMuted(name, on) {
    const slot = this.inputs.get(name);
    if (!slot || this.closed) return;
    slot.muted = !!on;
    this.setGain(name, slot.gain);
  }
  setMonitoring(on, volume = this.monitorVolume) {
    this.monitorOn = !!on;
    this.monitorVolume = Math.max(0, Math.min(1, Number(volume) || 0));
    if (this.monitor && !this.closed)
      this.monitor.gain.setTargetAtTime(this.monitorOn ? this.monitorVolume : 0, this.ctx.currentTime, 0.02);
  }
  setInputAvailable(name, available) {
    const slot = this.inputs.get(name);
    if (!slot || this.closed) return;
    slot.available = !!available;
    this.setGain(name, slot.gain);
    slot.monitorGate?.gain.setTargetAtTime(available ? 1 : 0, this.ctx.currentTime, 0.02);
    this._state(slot, available ? "live" : "interrupted", available ? "" : "Server audio disconnected");
  }
  close() {
    if (this.closed) return;
    this.closed = true;
    clearInterval(this.timer);
    navigator.mediaDevices?.removeEventListener?.("devicechange", this.deviceChange);
    for (const slot of this.inputs.values()) this._release(slot);
    this.clock?.stop();
    this.tracks.forEach(t => t.stop());
    this.ctx?.close().catch(() => {});
  }
}
"""
