import assert from 'node:assert/strict'
import test from 'node:test'

import { VoiceClient } from '../src/voice/client.ts'

const tick = () => new Promise((resolve) => setImmediate(resolve))

function setup(t) {
  const sockets = [], playback = [], requests = [], states = [], sends = []
  const replace = (name, value) => {
    const original = Object.getOwnPropertyDescriptor(globalThis, name)
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value })
    t.after(() => original ? Object.defineProperty(globalThis, name, original) : delete globalThis[name])
  }
  class Node { connect() {} disconnect() {} }
  class Context {
    state = 'running'
    currentTime = 0
    audioWorklet = { addModule: async () => {} }
    async resume() {}
    async close() {}
    createMediaStreamSource() { return new Node() }
    createGain() { return Object.assign(new Node(), { gain: {} }) }
    async decodeAudioData() { return { duration: 1 } }
    createBufferSource() {
      const node = Object.assign(new Node(), {
        start() { this.started = true }, stop() { this.stopped = true },
      })
      playback.push(node)
      return node
    }
  }
  class Socket {
    static OPEN = 1
    readyState = 1
    sent = []
    constructor() { sockets.push(this) }
    close() { this.readyState = 3 }
    send(value) { this.sent.push(value) }
    event(value) { this.onmessage?.({ data: JSON.stringify(value) }) }
  }
  class Worklet extends Node { port = { postMessage() {}, close() {} } }
  const tracks = [{ enabled: true, stop() {} }]
  replace('isSecureContext', true)
  replace('location', new URL('http://localhost:8080/'))
  replace('AudioContext', Context)
  replace('AudioWorkletNode', Worklet)
  replace('WebSocket', Socket)
  replace('navigator', { mediaDevices: { getUserMedia: async () => ({
    getTracks: () => tracks, getAudioTracks: () => tracks,
  }) } })
  replace('fetch', async (url, options) => {
    requests.push({ url, options })
    if (url === '/api/voice/status') return Response.json({
      available: true, sample_rate: 16000, wake_phrase: 'Hey Idris',
    })
    return new Response(new ArrayBuffer(12))
  })
  const client = new VoiceClient({
    workletUrl: '/voice.js', onState: (state) => states.push(state),
    send: async (...args) => { sends.push(args); return 'verified-turn' },
  })
  t.after(() => client.stop())
  const ready = async () => {
    await client.start()
    sockets[0].event({ type: 'state', state: 'listening', wake_phrase: 'Hey Idris' })
  }
  return { client, ready, sockets, playback, requests, states, sends }
}

test('notifications use their saved ID without a chat request or a fake replayable turn', async (t) => {
  const env = setup(t)
  assert.equal(env.client.speakNotification('chat', 'notice'), false)
  await env.ready()
  assert.equal(env.client.speakNotification('chat', 'notice'), true)
  await tick()
  assert.deepEqual(JSON.parse(env.requests[1].options.body), {
    session_id: 'chat', notification_id: 'notice', stream: true,
  })
  assert.equal(env.requests[1].url, '/api/voice/notification')
  assert.equal(env.sends.length, 0)
  assert.equal(env.states.at(-1).playbackKind, 'notification')
  assert.equal(env.states.at(-1).canReplay, false)
  env.playback[0].onended()
  await tick()
  assert.equal(env.states.at(-1).phase, 'listening')
  assert.equal(env.states.at(-1).playbackKind, null)
  assert.equal(env.client.speakNotification('chat', 'notice'), false)
})

test('barge-in stops notification audio and only sends the new conversational request', async (t) => {
  const env = setup(t)
  await env.ready()
  env.client.speakNotification('chat', 'notice')
  await tick()
  env.sockets[0].event({ type: 'speech_start' })
  assert.equal(env.playback[0].stopped, true)
  assert.equal(env.requests[1].options.signal.aborted, true)
  env.sockets[0].event({ type: 'transcript', text: 'Focus on warranties.' })
  await tick()
  assert.equal(env.sends.length, 1)
  assert.equal(env.sends[0][0], 'Focus on warranties.')
  assert.equal(env.requests.filter((item) => item.url.includes('/tasks')).length, 0)
  assert.equal(env.states.at(-1).playbackKind, 'reply')
})

test('notification cannot interrupt a recording, pending transcript, or foreground playback', async (t) => {
  const env = setup(t)
  await env.ready()
  env.sockets[0].event({ type: 'speech_start' })
  assert.equal(env.client.speakNotification('chat', 'notice'), false)
  env.sockets[0].event({ type: 'partial', text: 'My unfinished question' })
  assert.equal(env.client.speakNotification('chat', 'notice'), false)
  env.sockets[0].event({ type: 'state', state: 'listening', wake_phrase: 'Hey Idris' })
  assert.equal(env.client.speakNotification('chat', 'notice'), false)
  env.sockets[0].event({ type: 'transcript', text: 'My finished question' })
  assert.equal(env.client.speakNotification('chat', 'notice'), false)
  await tick()
  assert.equal(env.client.speakNotification('chat', 'notice'), false)
  env.playback[0].onended()
  await tick()
  assert.equal(env.client.speakNotification('chat', 'notice'), true)
})

test('notification does not replace the last verified reply for replay', async (t) => {
  const env = setup(t)
  await env.ready()
  env.sockets[0].event({ type: 'transcript', text: 'Question' })
  await tick()
  env.playback[0].onended()
  await tick()
  env.client.speakNotification('chat', 'notice')
  await tick()
  env.client.stopNotification()
  assert.equal(env.playback[1].stopped, true)
  assert.equal(env.states.at(-1).canReplay, true)
  env.client.replayLast()
  await tick()
  assert.equal(env.sends.length, 1)
  assert.equal(env.requests.at(-1).url, '/api/voice/speech')
  assert.deepEqual(JSON.parse(env.requests.at(-1).options.body), {
    turn_id: 'verified-turn', stream: true,
  })
})

test('notification stop cannot cancel foreground reply playback', async (t) => {
  const env = setup(t)
  await env.ready()
  env.sockets[0].event({ type: 'transcript', text: 'Question' })
  await tick()
  env.client.stopNotification()
  assert.equal(env.playback[0].stopped, undefined)
  assert.equal(env.requests.at(-1).options.signal.aborted, false)
})
