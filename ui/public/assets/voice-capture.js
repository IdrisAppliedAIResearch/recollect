// Keep fractional sample coverage between render blocks: browser devices commonly
// run at 44.1 or 48 kHz while the local recognizer accepts 16 kHz PCM.
export class PcmResampler {
  constructor(inputRate, frameSize = 800) {
    if (!Number.isFinite(inputRate) || inputRate < 16000) {
      throw new Error('The microphone sample rate must be at least 16 kHz.')
    }
    this.ratio = inputRate / 16000
    this.frameSize = frameSize
    this.reset()
  }

  reset() {
    this.remaining = this.ratio
    this.sum = 0
    this.frame = new DataView(new ArrayBuffer(this.frameSize * 2))
    this.length = 0
  }

  push(samples, emit) {
    for (const sample of samples) {
      let available = 1
      while (available > 1e-9) {
        const take = Math.min(available, this.remaining)
        this.sum += sample * take
        this.remaining -= take
        available -= take
        if (this.remaining < 1e-9) {
          const value = Math.max(-1, Math.min(1, this.sum / this.ratio))
          this.frame.setInt16(
            this.length * 2,
            Math.min(32767, Math.round(value * 32768)),
            true,
          )
          this.length++
          this.remaining = this.ratio
          this.sum = 0
          if (this.length === this.frameSize) {
            emit(this.frame.buffer)
            this.frame = new DataView(new ArrayBuffer(this.frameSize * 2))
            this.length = 0
          }
        }
      }
    }
  }
}

if (typeof AudioWorkletProcessor !== 'undefined') {
  class VoiceCapture extends AudioWorkletProcessor {
    constructor() {
      super()
      this.resampler = new PcmResampler(sampleRate)
      this.enabled = false
      this.port.onmessage = ({ data }) => {
        this.enabled = data.enabled === true
        this.resampler.reset()
      }
    }

    process(inputs) {
      const mono = inputs[0]?.[0]
      if (this.enabled && mono) {
        this.resampler.push(mono, (pcm) => {
          this.port.postMessage({ pcm, at: currentTime }, [pcm])
        })
      }
      return true
    }
  }
  registerProcessor('recollect-voice-capture', VoiceCapture)
}
