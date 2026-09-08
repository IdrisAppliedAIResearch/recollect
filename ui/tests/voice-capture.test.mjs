import assert from 'node:assert/strict'
import test from 'node:test'

import { PcmResampler } from '../public/assets/voice-capture.js'

function convert(rate, samples, blockSize) {
  const frames = []
  const resampler = new PcmResampler(rate)
  for (let i = 0; i < samples.length; i += blockSize) {
    resampler.push(samples.subarray(i, i + blockSize), (pcm) => frames.push(Buffer.from(pcm)))
  }
  return Buffer.concat(frames)
}

for (const rate of [16000, 44100, 48000, 96000]) {
  test(`${rate} Hz microphone produces exactly 16,000 PCM16 samples per second`, () => {
    const samples = new Float32Array(rate).fill(0.5)
    const pcm = convert(rate, samples, 128)
    assert.equal(pcm.length, 32000)
    for (let i = 0; i < pcm.length; i += 2) assert.equal(pcm.readInt16LE(i), 16384)
  })
}

test('fractional resampling is independent of the browser render block boundaries', () => {
  const samples = Float32Array.from({ length: 44100 }, (_, i) => Math.sin(i / 15))
  assert.deepEqual(convert(44100, samples, 128), convert(44100, samples, 997))
})

test('signed little-endian output clips to the PCM16 range', () => {
  const resampler = new PcmResampler(16000, 4)
  let output
  resampler.push(new Float32Array([-2, -0.5, 0.5, 2]), (pcm) => { output = Buffer.from(pcm) })
  assert.deepEqual([...output], [0, 128, 0, 192, 0, 64, 255, 127])
})

test('pause reset discards the previous partial audio frame', () => {
  const resampler = new PcmResampler(48000, 4)
  let output
  resampler.push(new Float32Array(7).fill(1), () => assert.fail('partial frame emitted'))
  resampler.reset()
  resampler.push(new Float32Array(12).fill(0), (pcm) => { output = Buffer.from(pcm) })
  assert.deepEqual(output, Buffer.alloc(8))
})
