import assert from 'node:assert/strict'
import test from 'node:test'

import { VoiceClient } from '../src/voice/client.ts'

const tick = () => new Promise((resolve) => setImmediate(resolve))
function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

function setup(t, overrides = {}) {
  const states = []
  const requests = []
  const tracks = [{ enabled: true, stop() { this.stopped = true } }]
  const stream = { getTracks: () => tracks, getAudioTracks: () => tracks }
  const microphones = []
  const sockets = []
  const contexts = []
  const worklets = []
  const sends = []

  function replace(name, value) {
    const original = Object.getOwnPropertyDescriptor(globalThis, name)
    Object.defineProperty(globalThis, name, { value, configurable: true, writable: true })
    t.after(() => {
      if (original) Object.defineProperty(globalThis, name, original)
      else delete globalThis[name]
    })
  }
  class Node {
    connect() {}
    disconnect() { this.disconnected = true }
  }
  class Context {
    state = 'running'
    currentTime = 0
    audioWorklet = { addModule: async () => {} }
    playback = []
    constructor() { contexts.push(this) }
    async resume() {}
    async close() { this.state = 'closed' }
    createMediaStreamSource() { return new Node() }
    createGain() { return Object.assign(new Node(), { gain: {} }) }
    async decodeAudioData() { return overrides.decode?.() ?? { duration: 1 } }
    createBufferSource() {
      const node = Object.assign(new Node(), {
        start() { this.started = true },
        stop() { this.stopped = true },
      })
      this.playback.push(node)
      return node
    }
  }
  class Socket {
    static OPEN = 1
    readyState = 1
    bufferedAmount = 0
    sent = []
    constructor(url) { this.url = url; sockets.push(this) }
    send(data) { this.sent.push(data) }
    close() { this.readyState = 3 }
    event(data) { this.onmessage?.({ data: JSON.stringify(data) }) }
  }
  class Worklet extends Node {
    port = {
      messages: [],
      postMessage(data) { this.messages.push(data) },
      close() { this.closed = true },
    }
    constructor() { super(); worklets.push(this) }
  }
  replace('isSecureContext', true)
  replace('location', new URL('http://localhost:8080/'))
  replace('AudioContext', Context)
  replace('AudioWorkletNode', Worklet)
  replace('WebSocket', Socket)
  replace('navigator', {
    mediaDevices: {
      getUserMedia: (...args) => {
        microphones.push(args)
        return overrides.microphone?.() ?? Promise.resolve(stream)
      },
    },
  })
  replace('fetch', async (url, options) => {
    requests.push({ url, options })
    if (url === '/api/voice/status') {
      return {
        ok: true,
        json: async () => ({ available: true, sample_rate: 16000, wake_phrase: 'Hey Idris' }),
      }
    }
    return overrides.speech?.(options) ?? { ok: true, arrayBuffer: async () => new ArrayBuffer(12) }
  })
  const client = new VoiceClient({
    workletUrl: '/assets/voice-capture.js',
    onState: (state) => states.push(state),
    send: (...args) => {
      sends.push(args)
      return overrides.send?.(...args) ?? Promise.resolve('verified-turn')
    },
  })
  t.after(() => client.stop())
  return { client, states, requests, tracks, stream, microphones, contexts, sockets, worklets, sends }
}

function ready(env) {
  env.sockets[0].event({ type: 'state', state: 'waiting', wake_phrase: 'Hey Idris' })
  env.sockets[0].event({ type: 'state', state: 'listening', wake_phrase: 'Hey Idris' })
}

function utterance(env, text) {
  env.sockets[0].event({ type: 'speech_start' })
  env.sockets[0].event({ type: 'transcript', text })
}

function pcm(env) {
  const data = new ArrayBuffer(1600)
  env.worklets[0].port.onmessage({ data: { pcm: data, at: 0 } })
  assert.equal(env.sockets[0].sent.at(-1), data)
}

function control(env) {
  return env.sockets[0].sent.filter((data) => typeof data === 'string')
    .map((data) => JSON.parse(data)).filter((event) => event.control_id).at(-1)
}

function acknowledge(env, state, id = control(env).control_id) {
  env.sockets[0].event({ type: 'state', state, wake_phrase: 'Hey Idris', control_id: id })
}

function speechStream() {
  let controller
  let cancelled = false
  const body = new ReadableStream({
    start(next) { controller = next },
    cancel() { cancelled = true },
  })
  const raw = (text) => controller.enqueue(new TextEncoder().encode(text))
  return {
    response: new Response(body, { headers: { 'Content-Type': 'application/x-ndjson' } }),
    raw,
    event: (event) => raw(`${JSON.stringify(event)}\n`),
    audio: () => raw(`${JSON.stringify({ type: 'audio', wav: btoa('RIFF test audio') })}\n`),
    close: () => controller.close(),
    get cancelled() { return cancelled },
  }
}

test('one opt-in and wake phrase keep capture open through replies and follow-up turns', async (t) => {
  const env = setup(t)
  assert.equal(env.microphones.length, 0)
  await env.client.start()
  const socket = env.sockets[0]
  socket.event({ type: 'state', state: 'waiting', wake_phrase: 'Hey Idris' })
  assert.equal(env.states.at(-1).phase, 'waiting')
  assert.equal(env.states.at(-1).wakePhrase, 'Hey Idris')
  assert.deepEqual(env.worklets[0].port.messages.at(-1), { enabled: true })
  assert.equal(env.microphones[0][0].audio.echoCancellation, true)
  pcm(env)
  socket.event({ type: 'state', state: 'listening', wake_phrase: 'Hey Idris' })
  socket.event({ type: 'speech_start' })
  socket.event({ type: 'partial', text: 'what is' })
  assert.equal(env.states.at(-1).partial, 'what is')
  socket.event({ type: 'transcript', text: 'what is memory' })
  socket.event({ type: 'transcript', text: 'duplicate ignored' })
  pcm(env)
  await tick()
  assert.equal(env.sends.length, 1)
  assert.equal(env.sends[0][0], 'what is memory')
  assert.equal(env.sends[0][2], 'voice')
  assert.deepEqual(env.worklets[0].port.messages, [{ enabled: true }])
  assert.deepEqual(JSON.parse(env.requests[1].options.body), { turn_id: 'verified-turn', stream: true })
  assert.equal(env.states.at(-1).phase, 'speaking')
  assert.equal(env.contexts[0].playback[0].started, true)
  assert.equal(socket.sent.at(-1), '{"type":"playback","active":true}')
  pcm(env)
  env.contexts[0].playback[0].onended()
  await tick()
  assert.equal(env.states.at(-1).phase, 'listening')
  assert.equal(env.states.at(-1).partial, 'what is memory')
  assert.equal(socket.sent.at(-1), '{"type":"playback","active":false}')
  utterance(env, 'tell me more')
  await tick()
  assert.equal(env.sends.length, 2)
  assert.equal(env.sends[1][0], 'tell me more')
  assert.equal(env.contexts[0].playback.length, 2)
  assert.equal(env.microphones.length, 1)
  assert.deepEqual(env.worklets[0].port.messages, [{ enabled: true }])
  assert.equal(socket.sent.some((data) => typeof data === 'string' && /pause|resume/.test(data)), false)
})

test('failed or incomplete chat never requests speech and keeps the listener ready', async (t) => {
  const env = setup(t, { send: async () => null })
  await env.client.start()
  ready(env)
  env.sockets[0].event({ type: 'transcript', text: 'hello' })
  await tick()
  assert.equal(env.requests.length, 1)
  assert.equal(env.states.at(-1).phase, 'listening')
  assert.match(env.states.at(-1).error, /did not complete/)
  assert.equal(env.states.at(-1).partial, 'hello')
  assert.equal(env.states.at(-1).canReplay, false)
  assert.equal(env.tracks[0].stopped, undefined)
  pcm(env)
})

test('stop during inference aborts it and never speaks its late result', async (t) => {
  const answer = deferred()
  const env = setup(t, { send: () => answer.promise })
  await env.client.start()
  env.sockets[0].event({ type: 'transcript', text: 'hello' })
  await tick()
  env.client.stop()
  answer.resolve('late-turn')
  await tick()
  assert.equal(env.sends[0][1].aborted, true)
  assert.equal(env.requests.length, 1)
  assert.equal(env.contexts[0].state, 'closed')
  assert.equal(env.tracks[0].stopped, true)
  assert.equal(env.sockets[0].readyState, 3)
  assert.equal(env.worklets[0].port.closed, true)
})

test('stop during Kokoro request aborts fetch and discards delayed audio', async (t) => {
  const speech = deferred()
  const env = setup(t, { speech: () => speech.promise })
  await env.client.start()
  env.sockets[0].event({ type: 'transcript', text: 'hello' })
  await tick()
  env.client.stop()
  speech.resolve({ ok: true, arrayBuffer: async () => new ArrayBuffer(12) })
  await tick()
  assert.equal(env.requests[1].options.signal.aborted, true)
  assert.equal(env.contexts[0].playback.length, 0)
  assert.equal(env.states.at(-1).phase, 'off')
})

test('stop cancels active playback and closes capture without rearming', async (t) => {
  const env = setup(t)
  await env.client.start()
  env.sockets[0].event({ type: 'transcript', text: 'hello' })
  await tick()
  env.client.stop()
  await tick()
  assert.equal(env.contexts[0].playback[0].stopped, true)
  assert.equal(env.contexts[0].playback[0].disconnected, true)
  assert.equal(env.sockets[0].sent.includes('{"type":"resume"}'), false)
  assert.equal(env.sockets[0].sent.at(-1), '{"type":"playback","active":false}')
})

test('speech interrupts generation immediately and a fast follow-up waits for the chat lock', async (t) => {
  const answer = deferred()
  let locked = false
  const env = setup(t, {
    send: async (text) => {
      assert.equal(locked, false, 'next chat entered before the interrupted chat settled')
      locked = true
      try {
        return text === 'first question' ? await answer.promise : 'second-turn'
      } finally {
        locked = false
      }
    },
  })
  await env.client.start()
  ready(env)
  utterance(env, 'first question')
  await tick()
  env.sockets[0].event({ type: 'speech_start' })
  assert.equal(env.sends[0][1].aborted, true)
  assert.equal(env.requests[0].options.signal.aborted, false)
  assert.equal(env.states.at(-1).phase, 'listening')
  assert.equal(env.states.at(-1).partial, '')
  env.sockets[0].event({ type: 'partial', text: 'second' })
  assert.equal(env.states.at(-1).partial, 'second')
  env.sockets[0].event({ type: 'transcript', text: 'second question' })
  env.sockets[0].event({ type: 'state', state: 'listening', wake_phrase: 'Hey Idris' })
  assert.equal(env.states.at(-1).phase, 'thinking')
  await tick()
  assert.equal(env.sends.length, 1)
  pcm(env)
  answer.resolve('stale-first-turn')
  await tick()
  assert.equal(env.sends.length, 2)
  assert.equal(env.sends[1][0], 'second question')
  assert.equal(env.sends[1][1].aborted, false)
  assert.equal(env.requests.length, 2)
  assert.deepEqual(JSON.parse(env.requests[1].options.body), { turn_id: 'second-turn', stream: true })
  assert.equal(env.states.at(-1).partial, 'second question')
  assert.equal(env.contexts[0].playback.length, 1)
})

test('another interruption supersedes queued speech before it enters chat', async (t) => {
  const answer = deferred()
  const env = setup(t, { send: (text) => text === 'first' ? answer.promise : Promise.resolve('third-turn') })
  await env.client.start()
  ready(env)
  utterance(env, 'first')
  await tick()
  utterance(env, 'second')
  utterance(env, 'third')
  answer.resolve('stale-turn')
  await tick()
  assert.deepEqual(env.sends.map(([text]) => text), ['first', 'third'])
  assert.deepEqual(JSON.parse(env.requests[1].options.body), { turn_id: 'third-turn', stream: true })
  assert.equal(env.contexts[0].playback.length, 1)
})

test('stop discards a follow-up waiting for an interrupted chat to settle', async (t) => {
  const answer = deferred()
  const env = setup(t, { send: () => answer.promise })
  await env.client.start()
  utterance(env, 'first')
  await tick()
  utterance(env, 'second')
  env.client.stop()
  answer.resolve(null)
  await tick()
  assert.equal(env.sends.length, 1)
  assert.equal(env.requests.length, 1)
  assert.equal(env.states.at(-1).phase, 'off')
})

test('speech cancels Kokoro without waiting for its late response to start the next chat', async (t) => {
  const speech = deferred()
  let count = 0
  const env = setup(t, {
    speech: () => ++count === 1 ? speech.promise : { ok: true, arrayBuffer: async () => new ArrayBuffer(12) },
  })
  await env.client.start()
  ready(env)
  utterance(env, 'first')
  await tick()
  pcm(env)
  utterance(env, 'second')
  assert.equal(env.requests[1].options.signal.aborted, true)
  await tick()
  assert.equal(env.sends.length, 2)
  assert.equal(env.contexts[0].playback.length, 1)
  speech.resolve({ ok: false, json: async () => ({ detail: 'stale failure' }) })
  await tick()
  assert.equal(env.states.at(-1).phase, 'speaking')
  assert.equal(env.states.at(-1).error, null)
  assert.equal(env.contexts[0].playback.length, 1)
  assert.equal(env.tracks[0].stopped, undefined)
})

test('audio arriving after an interruption is discarded before decoding', async (t) => {
  const wav = deferred()
  let decodes = 0
  const env = setup(t, {
    speech: () => ({ ok: true, arrayBuffer: () => wav.promise }),
    decode: () => { decodes++; return { duration: 1 } },
  })
  await env.client.start()
  ready(env)
  utterance(env, 'hello')
  await tick()
  env.sockets[0].event({ type: 'speech_start' })
  env.sockets[0].event({ type: 'partial', text: 'wait please' })
  wav.resolve(new ArrayBuffer(12))
  await tick()
  assert.equal(decodes, 0)
  assert.equal(env.contexts[0].playback.length, 0)
  assert.equal(env.states.at(-1).phase, 'listening')
  assert.equal(env.states.at(-1).partial, 'wait please')
})

test('interruption during non-cancellable audio decode cannot start stale playback', async (t) => {
  const audio = deferred()
  let decodes = 0
  const env = setup(t, {
    decode: () => ++decodes === 1 ? audio.promise : { duration: 1 },
  })
  await env.client.start()
  ready(env)
  utterance(env, 'first')
  await tick()
  pcm(env)
  utterance(env, 'second')
  await tick()
  assert.equal(env.contexts[0].playback.length, 1)
  audio.resolve({ duration: 1 })
  await tick()
  assert.equal(env.contexts[0].playback.length, 1)
  assert.equal(env.states.at(-1).phase, 'speaking')
  assert.equal(env.states.at(-1).partial, 'second')
})

test('barge-in stops playback immediately while PCM and live words keep flowing', async (t) => {
  const env = setup(t)
  await env.client.start()
  ready(env)
  utterance(env, 'first')
  await tick()
  const first = env.contexts[0].playback[0]
  const lateEnded = first.onended
  env.sockets[0].event({ type: 'speech_start' })
  assert.equal(first.stopped, true)
  assert.equal(first.disconnected, true)
  assert.equal(env.sockets[0].sent.at(-1), '{"type":"playback","active":false}')
  assert.equal(env.states.at(-1).phase, 'listening')
  env.sockets[0].event({ type: 'partial', text: 'actually' })
  assert.equal(env.states.at(-1).partial, 'actually')
  pcm(env)
  env.sockets[0].event({ type: 'transcript', text: 'actually tell me this' })
  await tick()
  const second = env.contexts[0].playback[1]
  assert.equal(second.started, true)
  lateEnded()
  await tick()
  assert.equal(env.states.at(-1).phase, 'speaking')
  assert.equal(second.stopped, undefined)
  assert.equal(env.sockets[0].sent.at(-1), '{"type":"playback","active":true}')
  assert.deepEqual(env.worklets[0].port.messages, [{ enabled: true }])
})

test('stream begins playback before synthesis completes and buffers just one decoded chunk ahead', async (t) => {
  const stream = speechStream()
  let decodes = 0
  const env = setup(t, {
    speech: () => stream.response,
    decode: () => { decodes++; return { duration: 1 } },
  })
  await env.client.start()
  ready(env)
  utterance(env, 'a longer question')
  await tick()
  const first = `${JSON.stringify({ type: 'audio', wav: btoa('first') })}\n`
  stream.raw(first.slice(0, 9))
  await tick()
  assert.equal(env.contexts[0].playback.length, 0)
  stream.raw(first.slice(9))
  await tick()
  assert.equal(env.contexts[0].playback[0].started, true)
  assert.equal(env.states.at(-1).phase, 'speaking')
  pcm(env)
  stream.audio()
  stream.audio()
  stream.event({ type: 'done' })
  await tick()
  assert.equal(decodes, 2)
  assert.equal(env.contexts[0].playback.length, 1)
  env.contexts[0].playback[0].onended()
  await tick()
  assert.equal(decodes, 3)
  assert.equal(env.contexts[0].playback.length, 2)
  env.contexts[0].playback[1].onended()
  await tick()
  assert.equal(env.contexts[0].playback.length, 3)
  assert.equal(env.states.at(-1).phase, 'speaking')
  env.contexts[0].playback[2].onended()
  await tick()
  assert.equal(env.states.at(-1).phase, 'listening')
  assert.deepEqual(env.sockets[0].sent.filter((event) => typeof event === 'string'), [
    '{"type":"playback","active":true}', '{"type":"playback","active":false}',
  ])
  assert.equal(stream.cancelled, true)
  assert.deepEqual(env.worklets[0].port.messages, [{ enabled: true }])
})

test('stream interruption discards queued audio and cancels the remaining response', async (t) => {
  const stream = speechStream()
  const env = setup(t, { speech: () => stream.response })
  await env.client.start()
  ready(env)
  utterance(env, 'hello')
  stream.audio()
  stream.audio()
  await tick()
  assert.equal(env.contexts[0].playback.length, 1)
  env.sockets[0].event({ type: 'speech_start' })
  await tick()
  assert.equal(env.contexts[0].playback[0].stopped, true)
  assert.equal(env.contexts[0].playback.length, 1)
  assert.equal(stream.cancelled, true)
  assert.equal(env.requests[1].options.signal.aborted, true)
  assert.equal(env.states.at(-1).phase, 'listening')
  pcm(env)
})

test('speech protection stays active during synthesis gaps and barge-in cancels the pending read', async (t) => {
  const stream = speechStream()
  const env = setup(t, { speech: () => stream.response })
  await env.client.start()
  ready(env)
  utterance(env, 'hello')
  stream.audio()
  await tick()
  env.contexts[0].playback[0].onended()
  await tick()
  assert.equal(env.sockets[0].sent.at(-1), '{"type":"playback","active":true}')
  pcm(env)
  env.sockets[0].event({ type: 'speech_start' })
  await tick()
  assert.equal(stream.cancelled, true)
  assert.equal(env.sockets[0].sent.at(-1), '{"type":"playback","active":false}')
  assert.equal(env.states.at(-1).phase, 'listening')
  assert.equal(env.tracks[0].stopped, undefined)
})

for (const failure of ['error', 'truncated', 'invalid', 'oversized']) {
  test(`stream ${failure} fails visibly and cancels playback while preserving the listener`, async (t) => {
    const stream = speechStream()
    const env = setup(t, { speech: () => stream.response })
    await env.client.start()
    ready(env)
    utterance(env, 'hello')
    stream.audio()
    await tick()
    if (failure === 'error') stream.event({ type: 'error', message: 'Synthesis failed midway.' })
    else if (failure === 'truncated') stream.close()
    else if (failure === 'invalid') stream.event({ type: 'audio', wav: 'invalid base64!' })
    else stream.raw('x'.repeat(6 * 1024 * 1024 + 1))
    await tick()
    assert.equal(env.states.at(-1).phase, 'listening')
    assert.ok(env.states.at(-1).error)
    assert.equal(env.contexts[0].playback[0].stopped, true)
    assert.equal(env.tracks[0].stopped, undefined)
    assert.equal(env.requests[1].options.signal.aborted, true)
    assert.equal(env.states.at(-1).partial, 'hello')
    assert.equal(env.states.at(-1).canReplay, true)
    pcm(env)
  })
}

test('pause disables microphone immediately but lets the reply finish, then resumes without another wake', async (t) => {
  const answer = deferred()
  const env = setup(t, { send: () => answer.promise })
  await env.client.start()
  ready(env)
  utterance(env, 'first question')
  await tick()
  env.client.pauseMicrophone()
  assert.equal(env.tracks[0].enabled, false)
  assert.equal(env.tracks[0].stopped, undefined)
  assert.equal(env.sends[0][1].aborted, false)
  assert.deepEqual(control(env), { type: 'pause', control_id: 1 })
  const sent = env.sockets[0].sent.length
  env.worklets[0].port.onmessage({ data: { pcm: new ArrayBuffer(1600), at: 0 } })
  assert.equal(env.sockets[0].sent.length, sent)
  utterance(env, 'unwanted speech while paused')
  assert.equal(env.sends.length, 1)
  answer.resolve('completed-turn')
  await tick()
  assert.equal(env.states.at(-1).phase, 'speaking')
  assert.equal(env.states.at(-1).micPaused, true)
  env.contexts[0].playback[0].onended()
  await tick()
  assert.equal(env.states.at(-1).phase, 'paused')
  env.client.resumeMicrophone()
  assert.deepEqual(control(env), { type: 'unpause', control_id: 2 })
  env.sockets[0].event({ type: 'state', state: 'listening', wake_phrase: 'Hey Idris' })
  acknowledge(env, 'paused', 1)
  assert.equal(env.tracks[0].enabled, false)
  assert.equal(env.states.at(-1).micResuming, true)
  acknowledge(env, 'listening', 2)
  assert.equal(env.tracks[0].enabled, true)
  assert.equal(env.states.at(-1).micPaused, false)
  assert.equal(env.states.at(-1).phase, 'listening')
  pcm(env)
  utterance(env, 'second question')
  await tick()
  assert.equal(env.sends.length, 2)
  assert.equal(env.microphones.length, 1)
})

test('pause discards partial transcription and stale acknowledgments cannot enable the microphone', async (t) => {
  const env = setup(t)
  await env.client.start()
  ready(env)
  env.sockets[0].event({ type: 'speech_start' })
  env.sockets[0].event({ type: 'partial', text: 'unfinished words' })
  env.client.pauseMicrophone()
  assert.equal(env.states.at(-1).partial, '')
  env.client.resumeMicrophone()
  env.client.pauseMicrophone()
  acknowledge(env, 'listening', 2)
  assert.equal(env.tracks[0].enabled, false)
  assert.equal(env.states.at(-1).micResuming, false)
  acknowledge(env, 'paused', 3)
  env.sockets[0].event({ type: 'partial', text: 'late words' })
  env.sockets[0].event({ type: 'transcript', text: 'late words' })
  assert.equal(env.states.at(-1).partial, '')
  assert.equal(env.sends.length, 0)
  env.client.resumeMicrophone()
  acknowledge(env, 'listening', 4)
  acknowledge(env, 'paused', 1)
  assert.equal(env.tracks[0].enabled, true)
  assert.equal(env.states.at(-1).phase, 'listening')
})

test('pause before activation resumes waiting for the first wake phrase', async (t) => {
  const env = setup(t)
  await env.client.start()
  env.sockets[0].event({ type: 'state', state: 'waiting', wake_phrase: 'Hey Idris' })
  env.client.pauseMicrophone()
  acknowledge(env, 'paused')
  env.client.resumeMicrophone()
  acknowledge(env, 'waiting')
  assert.equal(env.states.at(-1).phase, 'waiting')
  assert.equal(env.tracks[0].enabled, true)
  assert.equal(env.sends.length, 0)
})

test('microphone frames recorded before pause cannot leak into a resumed conversation', async (t) => {
  const env = setup(t)
  await env.client.start()
  ready(env)
  env.client.pauseMicrophone()
  env.contexts[0].currentTime = 2
  env.client.resumeMicrophone()
  acknowledge(env, 'listening')
  const sent = env.sockets[0].sent.length
  env.worklets[0].port.onmessage({ data: { pcm: new ArrayBuffer(1600), at: 1.9 } })
  assert.equal(env.sockets[0].sent.length, sent)
  const fresh = new ArrayBuffer(1600)
  env.worklets[0].port.onmessage({ data: { pcm: fresh, at: 2 } })
  assert.equal(env.sockets[0].sent.at(-1), fresh)
})

test('Stop reply cancels generation without closing voice or saving its late result for replay', async (t) => {
  const answer = deferred()
  const env = setup(t, { send: () => answer.promise })
  await env.client.start()
  ready(env)
  utterance(env, 'first')
  await tick()
  env.client.stopReply()
  assert.equal(env.sends[0][1].aborted, true)
  answer.resolve('late-turn')
  await tick()
  assert.equal(env.requests.length, 1)
  assert.equal(env.states.at(-1).canReplay, false)
  assert.equal(env.states.at(-1).phase, 'listening')
  assert.equal(env.tracks[0].stopped, undefined)
  pcm(env)
})

test('Stop reply cancels playback while leaving a deliberately paused microphone paused', async (t) => {
  const env = setup(t)
  await env.client.start()
  ready(env)
  utterance(env, 'hello')
  await tick()
  env.client.pauseMicrophone()
  const audio = env.contexts[0].playback[0]
  assert.equal(audio.stopped, undefined)
  env.client.stopReply()
  await tick()
  assert.equal(audio.stopped, true)
  assert.equal(env.tracks[0].enabled, false)
  assert.equal(env.states.at(-1).phase, 'paused')
  assert.equal(env.states.at(-1).canReplay, true)
  assert.equal(env.sockets[0].readyState, 1)
})

test('Replay uses the completed turn without new inference and supersedes an in-flight reply', async (t) => {
  const next = deferred()
  const env = setup(t, { send: (text) => text === 'first' ? Promise.resolve('first-turn') : next.promise })
  await env.client.start()
  ready(env)
  utterance(env, 'first')
  await tick()
  env.contexts[0].playback[0].onended()
  await tick()
  utterance(env, 'second')
  await tick()
  env.client.replayLast()
  await tick()
  assert.equal(env.sends.length, 2)
  assert.equal(env.sends[1][1].aborted, true)
  assert.deepEqual(JSON.parse(env.requests[2].options.body), { turn_id: 'first-turn', stream: true })
  assert.equal(env.contexts[0].playback[1].started, true)
  next.resolve('obsolete-second-turn')
  await tick()
  env.client.replayLast()
  await tick()
  assert.equal(env.sends.length, 2)
  assert.deepEqual(JSON.parse(env.requests[3].options.body), { turn_id: 'first-turn', stream: true })
  assert.equal(env.contexts[0].playback[1].stopped, true)
})

test('Kokoro failure keeps the transcript and supports replay without resubmitting chat', async (t) => {
  let requests = 0
  const env = setup(t, { speech: () => ++requests === 1 ? {
    ok: false, json: async () => ({ detail: 'Speech engine temporarily unavailable.' }),
  } : undefined })
  await env.client.start()
  ready(env)
  utterance(env, 'keep this question')
  await tick()
  assert.equal(env.states.at(-1).phase, 'listening')
  assert.equal(env.states.at(-1).partial, 'keep this question')
  assert.equal(env.states.at(-1).canReplay, true)
  assert.match(env.states.at(-1).error, /temporarily unavailable/)
  env.client.replayLast()
  await tick()
  assert.equal(env.sends.length, 1)
  assert.equal(env.requests.length, 3)
  assert.equal(env.states.at(-1).phase, 'speaking')
  assert.equal(env.states.at(-1).error, null)
  assert.equal(env.tracks[0].stopped, undefined)
})

test('replay while microphone is paused never enables input or performs new inference', async (t) => {
  const env = setup(t)
  await env.client.start()
  ready(env)
  utterance(env, 'hello')
  await tick()
  env.contexts[0].playback[0].onended()
  await tick()
  env.client.pauseMicrophone()
  acknowledge(env, 'paused')
  env.client.replayLast()
  await tick()
  assert.equal(env.contexts[0].playback[1].started, true)
  assert.equal(env.tracks[0].enabled, false)
  assert.equal(env.states.at(-1).micPaused, true)
  env.contexts[0].playback[1].onended()
  await tick()
  assert.equal(env.states.at(-1).phase, 'paused')
  assert.equal(env.sends.length, 1)
  assert.equal(control(env).type, 'pause')
})

for (const phrase of ['stop listening', 'turn off voice', 'Hey, Idris, stop listening.']) {
  test(`spoken control ${phrase} fully stops voice without an inference request`, async (t) => {
    const env = setup(t)
    await env.client.start()
    ready(env)
    utterance(env, phrase)
    await tick()
    assert.equal(env.sends.length, 0)
    assert.equal(env.states.at(-1).phase, 'off')
    assert.equal(env.tracks[0].stopped, true)
    assert.equal(env.sockets[0].readyState, 3)
  })
}

test('exact spoken pause, replay, and stop controls never become conversation turns', async (t) => {
  const env = setup(t)
  await env.client.start()
  ready(env)
  utterance(env, 'hello')
  await tick()
  utterance(env, 'Hey Idris stop talking')
  await tick()
  assert.equal(env.contexts[0].playback[0].stopped, true)
  assert.equal(env.states.at(-1).phase, 'listening')
  utterance(env, 'repeat that')
  await tick()
  assert.equal(env.contexts[0].playback[1].started, true)
  assert.equal(env.sends.length, 1)
  utterance(env, 'pause microphone')
  await tick()
  assert.equal(env.sends.length, 1)
  assert.equal(env.states.at(-1).micPaused, true)
  assert.equal(env.tracks[0].enabled, false)
})

for (const phrase of [
  'please explain stop listening', 'stop talking about tomatoes',
  'can you repeat that sentence', 'do not pause microphone',
  'Hey Idris tell me whether to turn off voice',
]) {
  test(`discussion containing control words remains ordinary speech: ${phrase}`, async (t) => {
    const env = setup(t)
    await env.client.start()
    ready(env)
    utterance(env, phrase)
    await tick()
    assert.equal(env.sends.length, 1)
    assert.equal(env.sends[0][0], phrase)
    assert.equal(env.states.at(-1).micPaused, false)
    assert.equal(env.tracks[0].stopped, undefined)
  })
}

test('long utterance review never auto-sends and requires explicit send before resuming', async (t) => {
  const env = setup(t)
  await env.client.start()
  ready(env)
  env.sockets[0].event({ type: 'state', state: 'paused', wake_phrase: 'Hey Idris' })
  env.sockets[0].event({ type: 'limit', text: 'A complete captured long request.', limit_s: 120 })
  env.client.resumeMicrophone()
  env.client.replayLast()
  await tick()
  assert.equal(env.sends.length, 0)
  assert.equal(env.tracks[0].enabled, false)
  assert.equal(env.states.at(-1).pendingTranscript, 'A complete captured long request.')
  assert.equal(control(env), undefined)
  env.client.sendCaptured()
  env.client.sendCaptured()
  await tick()
  assert.equal(env.sends.length, 1)
  assert.equal(env.sends[0][0], 'A complete captured long request.')
  assert.equal(env.states.at(-1).pendingTranscript, null)
  assert.equal(env.tracks[0].enabled, false)
  assert.equal(control(env).type, 'unpause')
  acknowledge(env, 'listening')
  assert.equal(env.tracks[0].enabled, true)
  pcm(env)
})

test('discarding a capped utterance resumes without submitting text or requiring a wake phrase', async (t) => {
  const env = setup(t)
  await env.client.start()
  ready(env)
  env.sockets[0].event({ type: 'limit', text: 'Do not send these words', limit_s: 120 })
  env.client.discardCaptured()
  acknowledge(env, 'listening')
  await tick()
  assert.equal(env.sends.length, 0)
  assert.equal(env.states.at(-1).partial, '')
  assert.equal(env.states.at(-1).pendingTranscript, null)
  assert.equal(env.states.at(-1).phase, 'listening')
  assert.equal(env.tracks[0].enabled, true)
})

test('a limit arriving during resume invalidates the stale unpause acknowledgment', async (t) => {
  const env = setup(t)
  await env.client.start()
  ready(env)
  env.client.pauseMicrophone()
  env.client.resumeMicrophone()
  env.sockets[0].event({ type: 'limit', text: 'long captured text', limit_s: 120 })
  assert.deepEqual(control(env), { type: 'pause', control_id: 3 })
  acknowledge(env, 'listening', 2)
  assert.equal(env.tracks[0].enabled, false)
  assert.equal(env.states.at(-1).pendingTranscript, 'long captured text')
  acknowledge(env, 'paused', 3)
  env.client.discardCaptured()
  acknowledge(env, 'listening', 4)
  assert.equal(env.tracks[0].enabled, true)
  assert.equal(env.sends.length, 0)
})

test('permission granted after stop immediately releases microphone tracks', async (t) => {
  const microphone = deferred()
  const env = setup(t, { microphone: () => microphone.promise })
  const starting = env.client.start()
  await tick()
  env.client.stop()
  microphone.resolve(env.stream)
  await starting
  assert.equal(env.tracks[0].stopped, true)
  assert.equal(env.sockets.length, 0)
})

test('bounded socket queue fails visibly instead of accumulating old microphone audio', async (t) => {
  const env = setup(t)
  await env.client.start()
  env.sockets[0].event({ type: 'state', state: 'waiting', wake_phrase: 'Hey Idris' })
  env.sockets[0].bufferedAmount = 32001
  env.worklets[0].port.onmessage({ data: { pcm: new ArrayBuffer(1600), at: 0 } })
  assert.equal(env.states.at(-1).phase, 'off')
  assert.match(env.states.at(-1).error, /fell behind/)
  assert.equal(env.tracks[0].stopped, true)
})

test('microphone disconnection stops resources visibly', async (t) => {
  const env = setup(t)
  await env.client.start()
  env.tracks[0].onended()
  assert.match(env.states.at(-1).error, /Microphone disconnected/)
  assert.equal(env.contexts[0].state, 'closed')
})

test('websocket closure releases microphone and displays a reconnect message', async (t) => {
  const env = setup(t)
  await env.client.start()
  env.sockets[0].onclose()
  assert.equal(env.tracks[0].stopped, true)
  assert.match(env.states.at(-1).error, /Voice connection closed/)
})

test('denied microphone permission closes audio context and explains recovery', async (t) => {
  const env = setup(t, {
    microphone: async () => { throw new DOMException('denied', 'NotAllowedError') },
  })
  await env.client.start()
  assert.equal(env.sockets.length, 0)
  assert.equal(env.contexts[0].state, 'closed')
  assert.match(env.states.at(-1).error, /Microphone permission was denied/)
})

test('conversation keeps one wake and microphone across five complete follow-up turns', async (t) => {
  const questions = [
    'Remember that my train leaves at eight.',
    'What time did I say?',
    'Actually, change that to nine thirty.',
    'Explain why someone might say stop talking about trains.',
    'Thanks. What time should I remember now?',
  ]
  const env = setup(t, { send: async (text) => `turn-${questions.indexOf(text)}` })
  await env.client.start()
  ready(env)
  for (const [index, question] of questions.entries()) {
    env.sockets[0].event({ type: 'speech_start' })
    env.sockets[0].event({ type: 'partial', text: question.slice(0, 12) })
    assert.equal(env.states.at(-1).partial, question.slice(0, 12))
    env.sockets[0].event({ type: 'partial', text: question })
    env.sockets[0].event({ type: 'transcript', text: question })
    env.sockets[0].event({ type: 'transcript', text: question })
    await tick()
    assert.equal(env.sends.length, index + 1)
    assert.equal(env.states.at(-1).phase, 'speaking')
    assert.deepEqual(JSON.parse(env.requests.at(-1).options.body), {
      turn_id: `turn-${index}`, stream: true,
    })
    env.contexts[0].playback[index].onended()
    await tick()
    assert.equal(env.states.at(-1).phase, 'listening')
    pcm(env)
  }
  assert.deepEqual(env.sends.map(([text]) => text), questions)
  assert.ok(env.sends.every(([, , mode]) => mode === 'voice'))
  assert.equal(env.microphones.length, 1)
  assert.equal(env.states.filter(({ phase }) => phase === 'waiting').length, 1)
  assert.deepEqual(env.worklets[0].port.messages, [{ enabled: true }])
})

for (const muteAt of ['during first chunk', 'between chunks']) {
  test(`conversation mute ${muteAt} ignores queued noise while the remaining answer plays`, async (t) => {
    const speech = speechStream()
    const env = setup(t, { speech: () => speech.response })
    await env.client.start()
    ready(env)
    utterance(env, 'Explain the whole process while I listen.')
    speech.audio()
    await tick()
    const first = env.contexts[0].playback[0]
    assert.equal(first.started, true)
    if (muteAt === 'between chunks') {
      first.onended()
      await tick()
    }
    env.client.pauseMicrophone()
    env.sockets[0].event({ type: 'speech_start' })
    env.sockets[0].event({ type: 'partial', text: 'background noise' })
    env.sockets[0].event({ type: 'transcript', text: 'stop listening' })
    acknowledge(env, 'paused')
    const before = env.sockets[0].sent.length
    env.worklets[0].port.onmessage({ data: { pcm: new ArrayBuffer(1600), at: 0 } })
    assert.equal(env.sockets[0].sent.length, before)
    assert.equal(env.tracks[0].enabled, false)
    assert.equal(first.stopped, undefined)
    assert.equal(env.requests[1].options.signal.aborted, false)
    assert.equal(env.states.at(-1).phase, 'speaking')
    assert.equal(env.sends.length, 1)
    speech.audio()
    speech.event({ type: 'done' })
    await tick()
    if (muteAt === 'during first chunk') first.onended()
    await tick()
    assert.equal(env.contexts[0].playback[1].started, true)
    assert.equal(env.tracks[0].enabled, false)
    env.contexts[0].playback[1].onended()
    await tick()
    assert.equal(env.states.at(-1).phase, 'paused')
    assert.equal(env.states.at(-1).micPaused, true)
    assert.equal(env.sends.length, 1)
    assert.equal(env.states.at(-1).error, null)
  })
}

test('conversation unmute waits for acknowledgment before a fresh interruption starts a follow-up', async (t) => {
  const env = setup(t)
  await env.client.start()
  ready(env)
  utterance(env, 'Read the answer.')
  await tick()
  const playing = env.contexts[0].playback[0]
  env.client.pauseMicrophone()
  acknowledge(env, 'paused')
  env.client.resumeMicrophone()
  env.sockets[0].event({ type: 'speech_start' })
  env.sockets[0].event({ type: 'transcript', text: 'queued old speech' })
  assert.equal(playing.stopped, undefined)
  assert.equal(env.tracks[0].enabled, false)
  acknowledge(env, 'listening')
  assert.equal(env.tracks[0].enabled, true)
  assert.equal(playing.stopped, undefined)
  utterance(env, 'Actually, just give me the short version.')
  await tick()
  assert.equal(playing.stopped, true)
  assert.deepEqual(env.sends.map(([text]) => text), [
    'Read the answer.', 'Actually, just give me the short version.',
  ])
  env.contexts[0].playback[1].onended()
  await tick()
  assert.equal(env.states.at(-1).phase, 'listening')
  assert.equal(env.microphones.length, 1)
})

test('conversation corrections interrupt research and speech before replaying only the latest completed answer', async (t) => {
  const research = deferred()
  const env = setup(t, {
    send: (text) => text === 'Research every option.' ? research.promise
      : Promise.resolve(text === 'Only compare two.' ? 'comparison-turn' : 'summary-turn'),
  })
  await env.client.start()
  ready(env)
  utterance(env, 'Research every option.')
  await tick()
  utterance(env, 'Only compare two.')
  await tick()
  assert.equal(env.sends[0][1].aborted, true)
  assert.equal(env.sends.length, 1)
  research.resolve('obsolete-research-turn')
  await tick()
  assert.equal(env.sends.length, 2)
  assert.equal(env.contexts[0].playback[0].started, true)
  utterance(env, 'Make that a one-sentence summary.')
  await tick()
  assert.equal(env.contexts[0].playback[0].stopped, true)
  assert.equal(env.contexts[0].playback[1].started, true)
  utterance(env, 'stop talking')
  await tick()
  assert.equal(env.contexts[0].playback[1].stopped, true)
  utterance(env, 'repeat that')
  await tick()
  assert.equal(env.contexts[0].playback[2].started, true)
  assert.deepEqual(env.sends.map(([text]) => text), [
    'Research every option.', 'Only compare two.', 'Make that a one-sentence summary.',
  ])
  assert.deepEqual(env.requests.slice(1).map(({ options }) => JSON.parse(options.body).turn_id), [
    'comparison-turn', 'summary-turn', 'summary-turn',
  ])
  env.contexts[0].playback[2].onended()
  await tick()
  assert.equal(env.states.at(-1).phase, 'listening')
})

for (const failure of ['chat', 'speech request', 'audio decode', 'speech stream']) {
  test(`conversation recovers from ${failure} failure and replays the next successful answer`, async (t) => {
    let sendCount = 0
    let speechCount = 0
    let decodeCount = 0
    const interruptedStream = speechStream()
    const env = setup(t, {
      send: async () => failure === 'chat' && ++sendCount === 1 ? null : 'recovered-turn',
      speech: () => {
        if (++speechCount !== 1) return
        if (failure === 'speech request') {
          return { ok: false, json: async () => ({ detail: 'Speech service unavailable.' }) }
        }
        if (failure === 'speech stream') return interruptedStream.response
      },
      decode: () => {
        if (failure === 'audio decode' && ++decodeCount === 1) {
          throw new Error('Could not decode this audio.')
        }
        return { duration: 1 }
      },
    })
    await env.client.start()
    ready(env)
    utterance(env, 'Please answer my first question.')
    if (failure === 'speech stream') {
      interruptedStream.audio()
      await tick()
      interruptedStream.event({ type: 'error', message: 'Speech stopped halfway.' })
    }
    await tick()
    assert.equal(env.states.at(-1).phase, 'listening')
    assert.ok(env.states.at(-1).error)
    assert.equal(env.states.at(-1).partial, 'Please answer my first question.')
    assert.equal(env.tracks[0].stopped, undefined)
    pcm(env)
    utterance(env, 'All right, answer this instead.')
    await tick()
    assert.equal(env.states.at(-1).phase, 'speaking')
    assert.equal(env.states.at(-1).error, null)
    env.contexts[0].playback.at(-1).onended()
    await tick()
    utterance(env, 'repeat that')
    await tick()
    assert.equal(env.sends.length, 2)
    assert.equal(env.states.at(-1).phase, 'speaking')
    assert.equal(JSON.parse(env.requests.at(-1).options.body).turn_id, 'recovered-turn')
    env.contexts[0].playback.at(-1).onended()
    await tick()
    assert.equal(env.states.at(-1).phase, 'listening')
    assert.equal(env.microphones.length, 1)
  })
}

for (const action of ['send', 'discard']) {
  test(`conversation can ${action} a long captured request then continue and replay`, async (t) => {
    const env = setup(t)
    await env.client.start()
    ready(env)
    utterance(env, 'Remember my first point.')
    await tick()
    env.contexts[0].playback[0].onended()
    await tick()
    env.sockets[0].event({ type: 'speech_start' })
    env.sockets[0].event({ type: 'partial', text: 'A long detailed explanation' })
    env.sockets[0].event({ type: 'state', state: 'paused', wake_phrase: 'Hey Idris' })
    env.sockets[0].event({
      type: 'limit', text: 'A long detailed explanation with all my requirements.', limit_s: 120,
    })
    assert.equal(env.sends.length, 1)
    assert.equal(env.tracks[0].enabled, false)
    if (action === 'send') env.client.sendCaptured()
    else env.client.discardCaptured()
    await tick()
    assert.equal(env.tracks[0].enabled, false)
    env.sockets[0].event({ type: 'transcript', text: 'trailing words beyond the limit' })
    acknowledge(env, 'listening')
    assert.equal(env.states.at(-1).pendingTranscript, null)
    assert.equal(env.tracks[0].enabled, true)
    if (action === 'send') {
      assert.equal(env.states.at(-1).phase, 'speaking')
      env.contexts[0].playback.at(-1).onended()
      await tick()
    }
    utterance(env, 'Now give me the next step.')
    await tick()
    env.contexts[0].playback.at(-1).onended()
    await tick()
    utterance(env, 'repeat that')
    await tick()
    assert.deepEqual(env.sends.map(([text]) => text), [
      'Remember my first point.',
      ...(action === 'send' ? ['A long detailed explanation with all my requirements.'] : []),
      'Now give me the next step.',
    ])
    env.contexts[0].playback.at(-1).onended()
    await tick()
    assert.equal(env.states.at(-1).phase, 'listening')
    assert.equal(env.microphones.length, 1)
  })
}

for (const [discussion, command, expected] of [
  ['Tell me what stop talking means.', 'stop talking', 'listening'],
  ['Explain the phrase repeat that.', 'Hey Idris repeat that', 'speaking'],
  ['When should I pause microphone input?', 'pause microphone', 'paused'],
  ['Explain when to stop listening.', 'Hey, Idris, stop listening.', 'off'],
]) {
  test(`conversation distinguishes discussing a control from issuing it: ${command}`, async (t) => {
    const env = setup(t)
    await env.client.start()
    ready(env)
    utterance(env, discussion)
    await tick()
    assert.equal(env.sends[0][0], discussion)
    assert.equal(env.states.at(-1).phase, 'speaking')
    utterance(env, command)
    await tick()
    assert.equal(env.sends.length, 1)
    assert.equal(env.states.at(-1).phase, expected)
    if (expected === 'off') assert.equal(env.tracks[0].stopped, true)
    if (expected === 'paused') assert.equal(env.tracks[0].enabled, false)
  })
}

for (const previous of ['pending reply', 'playing reply']) {
  test(`conversation replacement stops the ${previous} and isolates the new client's state`, async (t) => {
    const oldAnswer = deferred()
    const newTracks = [{ enabled: true, stop() { this.stopped = true } }]
    const newStream = { getTracks: () => newTracks, getAudioTracks: () => newTracks }
    let microphoneCalls = 0
    const env = setup(t, {
      microphone: () => Promise.resolve(++microphoneCalls === 1 ? env.stream : newStream),
      send: () => previous === 'pending reply' ? oldAnswer.promise : Promise.resolve('old-turn'),
    })
    await env.client.start()
    ready(env)
    utterance(env, 'A request from the previous conversation.')
    await tick()
    const oldSocket = env.sockets[0]
    const oldPlayback = env.contexts[0].playback[0]
    const oldEnded = oldPlayback?.onended
    env.client.stop()
    const oldStateCount = env.states.length
    const nextStates = []
    const nextSends = []
    const next = new VoiceClient({
      workletUrl: '/assets/voice-capture.js',
      onState: (state) => nextStates.push(state),
      send: async (text) => { nextSends.push(text); return 'new-turn' },
    })
    t.after(() => next.stop())
    await next.start()
    const socket = env.sockets[1]
    socket.event({ type: 'state', state: 'waiting', wake_phrase: 'Hey Idris' })
    assert.equal(nextStates.at(-1).canReplay, false)
    assert.equal(nextStates.at(-1).partial, '')
    oldAnswer.resolve('obsolete-old-turn')
    oldEnded?.()
    oldSocket.event({ type: 'transcript', text: 'late previous-session speech' })
    await tick()
    assert.equal(env.states.length, oldStateCount)
    assert.equal(nextStates.at(-1).phase, 'waiting')
    assert.equal(env.tracks[0].stopped, true)
    assert.equal(newTracks[0].stopped, undefined)
    assert.equal(oldSocket.readyState, 3)
    socket.event({ type: 'state', state: 'listening', wake_phrase: 'Hey Idris' })
    socket.event({ type: 'speech_start' })
    socket.event({ type: 'transcript', text: 'A fresh request after re-enabling.' })
    await tick()
    assert.deepEqual(nextSends, ['A fresh request after re-enabling.'])
    assert.equal(JSON.parse(env.requests.at(-1).options.body).turn_id, 'new-turn')
    assert.equal(env.contexts[1].playback[0].started, true)
    env.contexts[1].playback[0].onended()
    await tick()
    assert.equal(nextStates.at(-1).phase, 'listening')
    next.stop()
    assert.equal(newTracks[0].stopped, true)
    assert.equal(env.contexts[1].state, 'closed')
  })
}

for (const point of [
  'speech onset', 'partial text', 'empty revised hypothesis', 'endpoint before final text',
]) {
  test(`Replay cannot discard an unsent request at ${point}`, async (t) => {
    const env = setup(t)
    await env.client.start()
    ready(env)
    utterance(env, 'The first request.')
    await tick()
    env.contexts[0].playback[0].onended()
    await tick()
    assert.equal(env.states.at(-1).canReplay, true)
    env.sockets[0].event({ type: 'speech_start' })
    if (point !== 'speech onset') {
      env.sockets[0].event({ type: 'partial', text: 'The second request' })
    }
    if (point === 'empty revised hypothesis') {
      env.sockets[0].event({ type: 'partial', text: '' })
    }
    if (point === 'endpoint before final text') {
      env.sockets[0].event({ type: 'state', state: 'listening', wake_phrase: 'Hey Idris' })
    }
    const replayEnabled = env.states.at(-1).canReplay
    env.client.replayLast()
    env.sockets[0].event({ type: 'transcript', text: 'The second request with more detail.' })
    await tick()
    assert.deepEqual(env.sends.map(([text]) => text), [
      'The first request.', 'The second request with more detail.',
    ])
    assert.equal(replayEnabled, false)
    assert.equal(env.requests.length, 3)
    env.contexts[0].playback.at(-1).onended()
    await tick()
    assert.equal(env.states.at(-1).canReplay, true)
  })
}

for (const end of ['empty recognition', 'mute discards partial']) {
  test(`Replay becomes available after ${end} without sending a phantom turn`, async (t) => {
    const env = setup(t)
    await env.client.start()
    ready(env)
    utterance(env, 'A completed request.')
    await tick()
    env.contexts[0].playback[0].onended()
    await tick()
    env.sockets[0].event({ type: 'speech_start' })
    assert.equal(env.states.at(-1).canReplay, false)
    if (end === 'empty recognition') {
      env.sockets[0].event({ type: 'state', state: 'listening', wake_phrase: 'Hey Idris' })
      env.sockets[0].event({ type: 'partial', text: '' })
    } else {
      env.sockets[0].event({ type: 'partial', text: 'An unfinished correction' })
      env.client.pauseMicrophone()
      acknowledge(env, 'paused')
    }
    assert.equal(env.states.at(-1).canReplay, true)
    env.client.replayLast()
    await tick()
    assert.equal(env.contexts[0].playback[1].started, true)
    assert.equal(env.sends.length, 1)
    env.contexts[0].playback[1].onended()
    await tick()
    assert.equal(env.states.at(-1).phase, end === 'empty recognition' ? 'listening' : 'paused')
  })
}

for (const stage of ['partial dictation', 'long-request review']) {
  for (const failure of ['socket', 'microphone']) {
    test(`${failure} failure recovers unsent ${stage} without sending or retaining microphone access`, async (t) => {
      const env = setup(t)
      await env.client.start()
      ready(env)
      const captured = 'Keep this unfinished request available for editing.'
      env.sockets[0].event({ type: 'speech_start' })
      env.sockets[0].event({ type: 'partial', text: captured })
      if (stage === 'long-request review') {
        env.sockets[0].event({ type: 'limit', text: captured, limit_s: 120 })
      }
      if (failure === 'socket') env.sockets[0].onclose()
      else env.tracks[0].onended()
      assert.equal(env.states.at(-1).phase, 'off')
      assert.equal(env.states.at(-1).recoveredDraft, captured)
      assert.equal(env.states.at(-1).pendingTranscript, null)
      assert.equal(env.tracks[0].stopped, true)
      assert.equal(env.contexts[0].state, 'closed')
      assert.equal(env.sockets[0].readyState, 3)
      assert.equal(env.sends.length, 0)
    })
  }

  test(`deliberately stopping voice discards ${stage} without creating a recovered draft`, async (t) => {
    const env = setup(t)
    await env.client.start()
    ready(env)
    env.sockets[0].event({ type: 'speech_start' })
    env.sockets[0].event({ type: 'partial', text: 'Deliberately abandoned words' })
    if (stage === 'long-request review') {
      env.sockets[0].event({ type: 'limit', text: 'Deliberately abandoned words', limit_s: 120 })
    }
    env.client.stop()
    assert.equal(env.states.at(-1).recoveredDraft, null)
    assert.equal(env.sends.length, 0)
    assert.equal(env.tracks[0].stopped, true)
  })
}

for (const stage of ['thinking', 'speaking', 'completed', 'failed generation']) {
  test(`disconnect after a submitted ${stage} turn cannot duplicate its stale partial as a draft`, async (t) => {
    const pending = deferred()
    const env = setup(t, {
      send: () => stage === 'thinking' ? pending.promise
        : Promise.resolve(stage === 'failed generation' ? null : 'verified-turn'),
    })
    await env.client.start()
    ready(env)
    utterance(env, 'This request already reached chat.')
    await tick()
    if (stage === 'completed') {
      env.contexts[0].playback[0].onended()
      await tick()
    }
    assert.equal(env.states.at(-1).partial, 'This request already reached chat.')
    env.sockets[0].onclose()
    pending.resolve('obsolete-turn')
    await tick()
    assert.equal(env.states.at(-1).recoveredDraft, null)
    assert.equal(env.sends.length, 1)
    assert.equal(env.states.at(-1).phase, 'off')
    assert.equal(env.tracks[0].stopped, true)
  })
}

test('disconnect preserves a follow-up still queued behind an interrupted chat', async (t) => {
  const previous = deferred()
  const env = setup(t, { send: () => previous.promise })
  await env.client.start()
  ready(env)
  utterance(env, 'An earlier slow request.')
  await tick()
  utterance(env, 'A new request still waiting to be submitted.')
  await tick()
  assert.equal(env.sends.length, 1)
  env.sockets[0].onclose()
  previous.resolve('obsolete-turn')
  await tick()
  assert.equal(env.states.at(-1).recoveredDraft, 'A new request still waiting to be submitted.')
  assert.equal(env.sends.length, 1)
  assert.equal(env.states.at(-1).phase, 'off')
  assert.equal(env.tracks[0].stopped, true)
})
