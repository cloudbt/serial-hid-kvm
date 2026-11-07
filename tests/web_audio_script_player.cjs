// Plain-http fallback: the worklet player class driven by a ScriptProcessor
// must produce exactly what the AudioWorklet produces, and report stats.
const vm = require('node:vm'), assert = require('node:assert/strict');
const {worklet, player} = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const RATE = 48000, BLOCK = 2048;

// Reference: the worklet class itself, 128-frame quanta.
const ref = {sampleRate: RATE, currentTime: 0, registered: null,
  AudioWorkletProcessor: class { constructor() { this.port = {postMessage() {}}; } },
  registerProcessor(name, cls) { ref.registered = cls; }};
vm.createContext(ref);
vm.runInContext(worklet, ref);

// Fallback: scriptPlayerNode(ctx, options) from the viewer page.
const page = {WORKLET_SRC: worklet, Float32Array, Math};
vm.createContext(page);
vm.runInContext(player + '\nthis.scriptPlayerNode = scriptPlayerNode;', page);
const created = [];
const ctx = {sampleRate: RATE, currentTime: 0,
  createScriptProcessor(size, inputs, outputs) { const n = {size, inputs, outputs}; created.push(n); return n; }};
const options = {outputChannelCount: [2], processorOptions: {channels: 2, prebufferMs: 100, maxMs: 300}};
const node = page.scriptPlayerNode(ctx, options);
assert.equal(created.length, 1);
assert.equal(created[0].size, BLOCK);
const stats = [];
node.port.onmessage = e => stats.push(e.data);

const p = new ref.registered({processorOptions: options.processorOptions});
function pcm(frames, seed) {
  const a = new Int16Array(frames * 2);
  for (let i = 0; i < a.length; i++) a[i] = ((i * 7919 + seed) % 20000) - 10000;
  return a.buffer;
}
function block() {
  const ch = [new Float32Array(BLOCK), new Float32Array(BLOCK)];
  return {buffer: {length: BLOCK, numberOfChannels: 2, getChannelData: c => ch[c]}, ch};
}
let played = 0;
for (let round = 0; round < 6; round++) {
  const data = pcm(2400, round);                       // 50 ms per round
  p.port.onmessage({data: data.slice(0)});
  node.port.postMessage(data);
  const b = block();
  node.onaudioprocess({outputBuffer: b.buffer, playbackTime: round * BLOCK / RATE});
  for (let off = 0; off < BLOCK; off += 128) {
    const o = [new Float32Array(128), new Float32Array(128)];
    p.process([], [o]);
    for (let c = 0; c < 2; c++) for (let k = 0; k < 128; k++) {
      assert.equal(b.ch[c][off + k], o[c][k], `round ${round} ch ${c} frame ${off + k}`);
      if (o[c][k] !== 0) played++;
    }
  }
}
assert(played > 0, 'audio played after the prebuffer');
assert(stats.length > 0 && 'bufferedMs' in stats[0], 'player stats reach the page');
console.log('script player OK');
