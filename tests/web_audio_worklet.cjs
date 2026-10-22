// HDMI playback worklet: prebuffer, fades, underrun recovery, latency cap.
const vm = require('node:vm'), assert = require('node:assert/strict');
const {worklet} = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const RATE = 48000;

function make(options) {
  const ctx = {sampleRate: RATE, currentTime: 0, registered: null, posted: [],
    AudioWorkletProcessor: class { constructor() { this.port = {postMessage: m => ctx.posted.push(m)}; } },
    registerProcessor(name, cls) { ctx.registered = cls; }};
  vm.createContext(ctx);
  vm.runInContext(worklet, ctx);
  const P = ctx.registered;
  return {ctx, p: new P({processorOptions: options})};
}
// Interleaved stereo int16 frames, left = l, right = r.
function pcm(frames, l, r = l) {
  const a = new Int16Array(frames * 2);
  for (let i = 0; i < frames; i++) { a[2 * i] = l; a[2 * i + 1] = r; }
  return a.buffer;
}
function run(p, quanta) {
  const out = [];
  for (let q = 0; q < quanta; q++) {
    const o = [new Float32Array(128), new Float32Array(128)];
    p.process([], [o]);
    out.push(o);
  }
  return out;
}
const ms = n => Math.round(RATE * n / 1000);
const near = (a, b, msg = '') => assert(Math.abs(a - b) < 1e-6, `${msg}: ${a} vs ${b}`);

// Prebuffer: nothing plays until 100 ms are queued, then it fades in.
{
  const {p} = make({channels: 2, prebufferMs: 100, maxMs: 300});
  p.port.onmessage({data: pcm(ms(50), 16384)});
  assert(run(p, 4).every(o => o[0].every(v => v === 0)), 'silent while prebuffering');
  p.port.onmessage({data: pcm(ms(60), 16384)});
  const [first, second, third] = run(p, 3);
  near(first[0][0], 0.5 / 240, 'fade in starts near 0');
  assert(first[0][100] < 0.5 && first[0][100] > 0, 'ramping');
  near(third[0][127], 0.5, 'full level after the 5 ms ramp');
  assert.equal(second[1][127], second[0][127], 'both channels');
}

// Channels map L/R independently.
{
  const {p} = make({channels: 2, prebufferMs: 10, maxMs: 300});
  p.port.onmessage({data: pcm(ms(100), 16384, -8192)});
  const out = run(p, 5).at(-1);
  near(out[0][64], 0.5);
  near(out[1][64], -0.25);
}

// Underrun: fade out to 0 over the last samples, count it, prebuffer again.
{
  const {p} = make({channels: 2, prebufferMs: 10, maxMs: 300});
  p.port.onmessage({data: pcm(ms(10) + 64, 16384)});  // 544 frames = 4.25 quanta
  const out = run(p, 6);
  const tail = out[4][0];
  assert(tail[0] > 0 && tail[0] <= 0.5, 'still playing at the start of the short quantum');
  assert.equal(tail[63], 0, 'last available sample faded to 0');
  for (let i = 1; i < 64; i++) assert(tail[i] <= tail[i - 1] + 1e-9, 'monotonic fade out');
  assert(tail.slice(64).every(v => v === 0) && out[5][0].every(v => v === 0), 'silence after underrun');
  assert.equal(p.underruns, 1);
  assert.equal(p.playing, false);
  p.port.onmessage({data: pcm(ms(5), 16384)});
  assert(run(p, 2).every(o => o[0].every(v => v === 0)), 'waits for the prebuffer again');
  p.port.onmessage({data: pcm(ms(10), 16384)});
  const resumed = run(p, 1)[0][0];
  assert(resumed[0] < 0.01 && resumed[127] > resumed[0], 'fades back in');
}

// Soft limit: above maxMs the backlog is trimmed to the prebuffer after a
// fade out (no click), keeping the newest audio.
{
  const {p} = make({channels: 2, prebufferMs: 100, maxMs: 300});
  p.port.onmessage({data: pcm(ms(250), 1000)});
  p.port.onmessage({data: pcm(ms(250), 2000)});
  assert.equal(p.n, ms(500), 'room above the soft limit');
  assert.equal(p.trim, true);
  const out = run(p, 4);
  assert.equal(p.trims, 1);
  assert.equal(p.dropped, ms(400));
  near(out[0][0][0], 0, 'cut happens at gain 0');
  near(out[3][0][127], 2000 / 32768, 'newest audio plays after the trim');
  assert(Math.abs(p.n - (ms(100) - 4 * 128)) <= 1, 'back at the prebuffer');
  const before = p.buf;
  run(p, 50);
  assert.equal(p.buf, before, 'no reallocation while playing');
}

// A burst backlog that the jitter never uses is trimmed after a second.
{
  const {p} = make({channels: 2, prebufferMs: 100, maxMs: 300});
  p.port.onmessage({data: pcm(ms(250), 1000)});
  for (let q = 0; q < 400; q++) {  // ~1.07 s of steady input
    p.port.onmessage({data: pcm(128, 1000)});
    run(p, 1);
  }
  assert.equal(p.trims, 1);
  assert(p.n >= ms(100) - 256 && p.n <= ms(100) + 256, `settled near the prebuffer: ${p.n}`);
  assert.equal(p.underruns, 0);
}

// A jittery stream whose lowest level stays near the prebuffer is left alone.
{
  const {p} = make({channels: 2, prebufferMs: 100, maxMs: 300});
  p.port.onmessage({data: pcm(ms(100), 1000)});
  for (let q = 0; q < 800; q++) {  // 20 ms frames in bursts of two every 40 ms
    if (q % 15 === 0) { p.port.onmessage({data: pcm(ms(20), 1000)}); p.port.onmessage({data: pcm(ms(20), 1000)}); }
    run(p, 1);
  }
  assert.equal(p.trims, 0);
  assert.equal(p.underruns, 0);
}

// Mono output from a stereo stream (and vice versa) never reads past ch.
{
  const {p} = make({channels: 1, prebufferMs: 10, maxMs: 300});
  const mono = new Int16Array(ms(50)).fill(8192);
  p.port.onmessage({data: mono.buffer});
  const out = run(p, 6).at(-1);
  near(out[0][10], 0.25);
  near(out[1][10], 0.25);
}

// Periodic stats reach the main thread.
{
  const {ctx, p} = make({channels: 2});
  run(p, 1);
  assert.equal(ctx.posted.length, 1);
  assert.deepEqual(Object.keys(ctx.posted[0]).sort(), ['bufferedMs', 'dropped', 'playing', 'trims', 'underruns']);
}
