export type VoicePhase =
  | 'off'
  | 'starting'
  | 'waiting'
  | 'listening'
  | 'paused'
  | 'thinking'
  | 'speaking'

export interface VoiceState {
  phase: VoicePhase
  wakePhrase: string
  partial: string
  error: string | null
  micPaused: boolean
  micResuming: boolean
  canReplay: boolean
  pendingTranscript: string | null
  limitSeconds: number | null
  recoveredDraft: string | null
  playbackKind: 'reply' | 'notification' | null
}

export const initialVoiceState: VoiceState = {
  phase: 'off',
  wakePhrase: '',
  partial: '',
  error: null,
  micPaused: false,
  micResuming: false,
  canReplay: false,
  pendingTranscript: null,
  limitSeconds: null,
  recoveredDraft: null,
  playbackKind: null,
}

interface Options {
  workletUrl: string
  onState: (state: VoiceState) => void
  send: (message: string, signal: AbortSignal, inputMode: 'voice') => Promise<string | null>
}

interface VoiceStatus {
  available: boolean
  wake_phrase: string
  sample_rate: number
  error?: string
}

type VoiceEvent =
  | {
      type: 'state'
      state: 'waiting' | 'listening' | 'paused'
      wake_phrase: string
      control_id?: number
    }
  | { type: 'speech_start' }
  | { type: 'partial'; text: string }
  | { type: 'transcript'; text: string }
  | { type: 'limit'; text: string; limit_s: number }
  | { type: 'error'; message: string }

const MAX_BUFFERED_BYTES = 32_000
const MAX_SPEECH_RECORD_CHARS = 6 * 1024 * 1024

/** One explicit microphone opt-in owns every resource, including pending replies. */
export class VoiceClient {
  private state: VoiceState = { ...initialVoiceState }
  private stopped = false
  private started = false
  private utteranceSubmitted = false
  private dictationState: 'idle' | 'recording' | 'ending' = 'idle'
  private capturing = false
  private captureStartedAt = 0
  private sessionAbort = new AbortController()
  private replyAbort: AbortController | null = null
  private replyEpoch = 0
  private listenerState: 'waiting' | 'listening' = 'waiting'
  private microphoneControl = 0
  private pendingControl: 'pause' | 'unpause' | null = null
  private lastReplyTurnId: string | null = null
  private notificationIds = new Set<string>()
  private pendingSendText: string | null = null
  private sendSettled: Promise<void> = Promise.resolve()
  private context: AudioContext | null = null
  private stream: MediaStream | null = null
  private input: MediaStreamAudioSourceNode | null = null
  private worklet: AudioWorkletNode | null = null
  private silentOutput: GainNode | null = null
  private socket: WebSocket | null = null
  private playback: AudioBufferSourceNode | null = null
  private playbackActive = false
  private connectionTimer: ReturnType<typeof setTimeout> | null = null
  private options: Options

  constructor(options: Options) {
    this.options = options
  }

  async start(): Promise<void> {
    if (this.started || this.stopped) return
    this.started = true
    this.update({ phase: 'starting', error: null })
    try {
      if (!globalThis.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
        throw new Error('Microphone access needs localhost or HTTPS in a supported browser.')
      }
      // Resume before the first await, while the Enable voice click is still
      // a user gesture. The same context plays Kokoro without an autoplay prompt.
      const context = new AudioContext()
      this.context = context
      await context.resume()
      if (this.stopped) return
      if (!context.audioWorklet) throw new Error('This browser does not support voice capture.')
      context.onstatechange = () => {
        if (!this.stopped && context.state !== 'running') {
          this.stop('Browser audio was suspended. Enable voice again to resume.')
        }
      }

      const response = await fetch('/api/voice/status', { signal: this.sessionAbort.signal })
      if (!response.ok) throw new Error('Could not check the local voice service.')
      const status = (await response.json()) as VoiceStatus
      if (this.stopped) return
      if (!status.available) throw new Error(status.error || 'Local voice models are not ready.')
      if (status.sample_rate !== 16000) throw new Error('Unsupported voice sample rate.')
      this.update({ wakePhrase: status.wake_phrase })

      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      })
      if (this.stopped) {
        stream.getTracks().forEach((track) => track.stop())
        return
      }
      this.stream = stream
      stream.getAudioTracks().forEach((track) => {
        track.onended = () => this.stop('Microphone disconnected. Enable voice to reconnect.')
        track.onmute = () => this.stop('Microphone became unavailable. Enable voice to reconnect.')
      })

      await context.audioWorklet.addModule(this.options.workletUrl)
      if (this.stopped) return
      const worklet = new AudioWorkletNode(context, 'recollect-voice-capture', {
        channelCount: 1,
        channelCountMode: 'explicit',
      })
      this.worklet = worklet
      worklet.onprocessorerror = () => this.stop('Microphone processing stopped unexpectedly.')
      worklet.port.onmessage = ({ data }: MessageEvent<{ pcm: ArrayBuffer; at: number }>) => {
        const socket = this.socket
        if (!this.capturing || this.stopped || socket?.readyState !== WebSocket.OPEN) return
        if (data.at < this.captureStartedAt) return
        if (socket.bufferedAmount > MAX_BUFFERED_BYTES || context.currentTime - data.at > 1) {
          this.stop('The voice connection fell behind. Enable voice to reconnect.')
          return
        }
        socket.send(data.pcm)
      }
      this.input = context.createMediaStreamSource(stream)
      this.silentOutput = context.createGain()
      this.silentOutput.gain.value = 0
      this.input.connect(worklet)
      worklet.connect(this.silentOutput)
      this.silentOutput.connect(context.destination)

      const url = new URL('/api/voice/listen', location.href)
      url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
      const socket = new WebSocket(url)
      this.socket = socket
      this.connectionTimer = setTimeout(() => {
        this.stop('The voice service did not become ready. Enable voice to retry.')
      }, 60_000)
      socket.onmessage = ({ data }: MessageEvent<string>) => {
        if (this.stopped) return
        try {
          this.receive(JSON.parse(data) as VoiceEvent)
        } catch {
          this.stop('The voice service sent an unreadable response.')
        }
      }
      socket.onerror = () => this.stop('Could not connect to the local voice service.')
      socket.onclose = () => this.stop('Voice connection closed. Enable voice to reconnect.')
    } catch (error) {
      if (!this.stopped) this.stop(errorMessage(error))
    }
  }

  stop(error: string | null = null): void {
    if (this.stopped) return
    const unsent = this.state.pendingTranscript ?? this.pendingSendText ?? (
      this.dictationState !== 'idle' && !this.utteranceSubmitted ? this.state.partial : null
    )
    const recoveredDraft = error && unsent?.trim() ? unsent : null
    this.stopped = true
    this.capture(false)
    this.sessionAbort.abort()
    this.interrupt()
    if (this.connectionTimer !== null) clearTimeout(this.connectionTimer)
    if (this.socket) {
      this.socket.onmessage = this.socket.onclose = this.socket.onerror = null
      this.socket.close()
    }
    if (this.worklet) {
      this.worklet.port.onmessage = null
      this.worklet.port.close()
      this.worklet.disconnect()
    }
    this.input?.disconnect()
    this.silentOutput?.disconnect()
    this.stream?.getTracks().forEach((track) => {
      track.onended = track.onmute = null
      track.stop()
    })
    if (this.context) {
      this.context.onstatechange = null
      void this.context.close().catch(() => {})
    }
    this.lastReplyTurnId = null
    this.dictationState = 'idle'
    this.update({ ...initialVoiceState, wakePhrase: this.state.wakePhrase, error, recoveredDraft })
  }

  pauseMicrophone(): void {
    if (!this.started || this.stopped || this.state.phase === 'starting' ||
        (this.state.micPaused && !this.state.micResuming)) return
    this.capture(false)
    this.enableMicrophone(false)
    this.utteranceSubmitted = true
    this.dictationState = 'idle'
    this.pendingControl = 'pause'
    this.update({
      micPaused: true,
      micResuming: false,
      ...(!this.replyAbort ? { phase: 'paused', partial: '' } : {}),
    })
    this.sendMicrophoneControl('pause')
  }

  resumeMicrophone(): void {
    if (this.stopped || !this.state.micPaused || this.state.micResuming ||
        this.state.pendingTranscript !== null) return
    this.pendingControl = 'unpause'
    this.update({ micResuming: true })
    this.sendMicrophoneControl('unpause')
  }

  stopReply(): void {
    if (!this.started || this.stopped) return
    this.interrupt()
    this.utteranceSubmitted = true
    this.update({ phase: this.idlePhase(), error: null })
  }

  speakNotification(sessionId: string, notificationId: string): boolean {
    if (!this.started || this.stopped || !this.context || this.replyAbort ||
        !['listening', 'paused'].includes(this.state.phase) ||
        this.dictationState !== 'idle' || this.state.pendingTranscript !== null ||
        this.notificationIds.has(notificationId)) return false
    this.notificationIds.add(notificationId)
    if (this.notificationIds.size > 128) this.notificationIds.delete(this.notificationIds.values().next().value!)
    void this.reply(this.state.partial, null, { session_id: sessionId, notification_id: notificationId })
    return true
  }

  stopNotification(): void {
    if (this.state.playbackKind === 'notification') this.stopReply()
  }

  replayLast(): void {
    if (!this.started || this.stopped || this.state.pendingTranscript !== null ||
        this.dictationState !== 'idle') return
    this.interrupt()
    this.utteranceSubmitted = true
    if (!this.lastReplyTurnId) {
      this.update({ phase: this.idlePhase(), error: 'There is no completed reply to replay yet.' })
      return
    }
    void this.reply(this.state.partial, this.lastReplyTurnId)
  }

  sendCaptured(): void {
    if (this.stopped || !this.state.pendingTranscript?.trim()) return
    const text = this.state.pendingTranscript.trim()
    this.update({ pendingTranscript: null, limitSeconds: null, error: null })
    void this.reply(text)
    this.resumeMicrophone()
  }

  discardCaptured(): void {
    if (this.stopped || this.state.pendingTranscript === null) return
    this.update({ pendingTranscript: null, limitSeconds: null, partial: '', error: null })
    this.resumeMicrophone()
  }

  private idlePhase(): VoicePhase {
    return this.state.micPaused ? 'paused' : this.listenerState
  }

  private enableMicrophone(enabled: boolean): void {
    this.stream?.getAudioTracks().forEach((track) => { track.enabled = enabled })
  }

  private sendMicrophoneControl(type: 'pause' | 'unpause'): void {
    if (this.socket?.readyState !== WebSocket.OPEN) {
      this.stop('Voice connection closed. Enable voice to reconnect.')
      return
    }
    this.socket.send(JSON.stringify({ type, control_id: ++this.microphoneControl }))
  }

  private update(change: Partial<VoiceState>): void {
    this.state = { ...this.state, ...change }
    this.state.canReplay = this.lastReplyTurnId !== null &&
      this.dictationState === 'idle' && this.state.pendingTranscript === null
    this.options.onState(this.state)
  }

  private capture(enabled: boolean): void {
    if (this.capturing === enabled) return
    this.capturing = enabled
    if (enabled) this.captureStartedAt = this.context?.currentTime ?? 0
    this.worklet?.port.postMessage({ enabled })
  }

  private receive(event: VoiceEvent): void {
    if (this.state.micPaused && event.type !== 'state' && event.type !== 'error' &&
        event.type !== 'limit') return
    switch (event.type) {
      case 'error':
        this.stop(event.message)
        break
      case 'state':
        if (this.connectionTimer !== null) clearTimeout(this.connectionTimer)
        if (event.control_id !== undefined) {
          if (event.control_id !== this.microphoneControl) return
          if (this.pendingControl === 'unpause' && event.state !== 'paused') {
            this.pendingControl = null
            this.utteranceSubmitted = false
            this.enableMicrophone(true)
            this.update({ micPaused: false, micResuming: false })
          } else {
            this.pendingControl = null
          }
        }
        if (this.state.micPaused) return
        if (event.state === 'paused') {
          this.capture(false)
          this.enableMicrophone(false)
          this.update({ micPaused: true, phase: this.replyAbort ? this.state.phase : 'paused' })
          return
        }
        this.listenerState = event.state
        if (event.state === 'waiting') this.dictationState = 'idle'
        else if (this.dictationState === 'recording') this.dictationState = 'ending'
        this.capture(event.state === 'waiting' || event.state === 'listening')
        this.update({
          ...(!this.replyAbort ? { phase: event.state } : {}),
          wakePhrase: event.wake_phrase,
          ...(event.state === 'waiting' ? { partial: '' } : {}),
        })
        break
      case 'speech_start':
        this.listenerState = 'listening'
        this.interrupt()
        this.utteranceSubmitted = false
        this.dictationState = 'recording'
        this.update({ phase: 'listening', partial: '', error: null })
        break
      case 'partial':
        if (!this.utteranceSubmitted && this.state.phase === 'listening') {
          // An empty endpoint clears a noise-only attempt. An empty live
          // hypothesis still belongs to speech whose final text is pending.
          if (this.dictationState === 'ending' && !event.text.trim()) {
            this.dictationState = 'idle'
          }
          this.update({ partial: event.text })
        }
        break
      case 'transcript':
        if (!this.utteranceSubmitted) {
          this.dictationState = 'idle'
          if (!event.text.trim()) {
            this.update({ partial: '' })
            break
          }
          this.listenerState = 'listening'
          this.utteranceSubmitted = true
          const text = event.text.trim()
          if (!this.spokenControl(text)) void this.reply(text)
        }
        break
      case 'limit': {
        const resumeWasPending = this.pendingControl === 'unpause'
        this.interrupt()
        this.capture(false)
        this.enableMicrophone(false)
        this.pendingControl = null
        this.utteranceSubmitted = true
        this.dictationState = 'idle'
        this.update({
          phase: 'paused', micPaused: true, micResuming: false,
          partial: event.text, pendingTranscript: event.text,
          limitSeconds: event.limit_s, error: null,
        })
        if (resumeWasPending) {
          this.pendingControl = 'pause'
          this.sendMicrophoneControl('pause')
        }
        break
      }
    }
  }

  private spokenControl(text: string): boolean {
    const normalize = (value: string) => value.toLowerCase().replace(/[.,!?]/g, ' ')
      .replace(/\s+/g, ' ').trim()
    let command = normalize(text)
    const wake = normalize(this.state.wakePhrase)
    if (wake && command.startsWith(`${wake} `)) command = command.slice(wake.length + 1)
    switch (command) {
      case 'stop talking': this.stopReply(); return true
      case 'repeat that': this.replayLast(); return true
      case 'pause microphone': this.pauseMicrophone(); return true
      case 'stop listening':
      case 'turn off voice': this.stop(); return true
      default: return false
    }
  }

  private interrupt(): void {
    this.replyEpoch++
    this.replyAbort?.abort()
    this.replyAbort = null
    this.pendingSendText = null
    this.state.playbackKind = null
    this.playbackState(false)
  }

  private playbackState(active: boolean): void {
    if (this.playbackActive === active) return
    this.playbackActive = active
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({ type: 'playback', active }))
    }
  }

  private async reply(
    text: string, replayTurnId: string | null = null,
    notification: { session_id: string; notification_id: string } | null = null,
  ): Promise<void> {
    const abort = new AbortController()
    this.replyAbort = abort
    const epoch = ++this.replyEpoch
    const current = () => !this.stopped && !abort.signal.aborted && epoch === this.replyEpoch
    this.update({ phase: 'thinking', partial: text, error: null,
      playbackKind: notification ? 'notification' : 'reply' })
    try {
      // App.send owns a single in-flight chat. Abort must finish releasing its
      // lock before the next utterance enters it, even if speech ends quickly.
      let turnId = replayTurnId
      if (!turnId && !notification) {
        this.pendingSendText = text
        const sending = this.sendSettled.then(() => {
          if (!current()) return null
          this.pendingSendText = null
          return this.options.send(text, abort.signal, 'voice')
        })
        this.sendSettled = sending.then(() => {}, () => {})
        turnId = await sending
        if (!current()) return
        if (!turnId) throw new Error('The reply did not complete. Check the conversation and try again.')
        this.lastReplyTurnId = turnId
        this.update({ canReplay: true })
      }
      const response = await fetch(notification ? '/api/voice/notification' : '/api/voice/speech', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(notification ? { ...notification, stream: true } : { turn_id: turnId, stream: true }),
        signal: abort.signal,
      })
      if (!current()) return
      if (!response.ok) {
        const detail = await response.json().catch(() => null)
        throw new Error(detail?.detail || 'Kokoro could not prepare the spoken reply.')
      }
      const context = this.context!
      let playing: Promise<void> | null = null
      for await (const wav of speechAudio(response, abort.signal)) {
        if (!current()) return
        const audio = await context.decodeAudioData(wav)
        if (!current()) return
        // Decode at most one chunk ahead while the current chunk plays. Keep
        // echo protection active across chunk boundaries and synthesis gaps.
        if (playing) await playing
        if (!current()) return
        this.update({ phase: 'speaking' })
        this.playbackState(true)
        playing = this.play(context, audio, abort.signal).catch((error) => {
          if (current()) this.replyFailed(error)
        })
      }
      if (playing) await playing
      if (!current()) return
      if (!playing) throw new Error('Kokoro returned no spoken audio.')
      this.playbackState(false)
      this.replyAbort = null
      this.update({ phase: this.idlePhase(), playbackKind: null })
    } catch (error) {
      if (current()) this.replyFailed(error)
    }
  }

  private replyFailed(error: unknown): void {
    this.interrupt()
    this.update({ phase: this.idlePhase(), error: errorMessage(error) })
  }

  private play(context: AudioContext, audio: AudioBuffer, signal: AbortSignal): Promise<void> {
    return new Promise((resolve, reject) => {
      const source = context.createBufferSource()
      source.buffer = audio
      source.connect(context.destination)
      this.playback = source
      const finish = () => {
        source.onended = null
        signal.removeEventListener('abort', cancel)
        source.disconnect()
        if (this.playback === source) {
          this.playback = null
        }
        resolve()
      }
      const cancel = () => {
        source.stop()
        finish()
      }
      source.onended = finish
      signal.addEventListener('abort', cancel, { once: true })
      try {
        source.start()
      } catch (error) {
        source.onended = null
        signal.removeEventListener('abort', cancel)
        source.disconnect()
        this.playback = null
        reject(error)
      }
    })
  }
}

async function* speechAudio(response: Response, signal: AbortSignal): AsyncGenerator<ArrayBuffer> {
  if (!response.headers?.get('content-type')?.includes('application/x-ndjson')) {
    yield await response.arrayBuffer()
    return
  }
  if (!response.body) throw new Error('The speech response carried no audio stream.')
  const reader = response.body.getReader()
  const cancel = () => { void reader.cancel().catch(() => {}) }
  signal.addEventListener('abort', cancel, { once: true })
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read()
      if (signal.aborted) return
      if (done) throw new Error('The speech connection ended before the reply completed.')
      buffer += decoder.decode(value, { stream: true })
      let boundary = buffer.indexOf('\n')
      while (boundary !== -1) {
        if (boundary > MAX_SPEECH_RECORD_CHARS) throw new Error('A speech chunk was too large.')
        const event = JSON.parse(buffer.slice(0, boundary)) as Record<string, unknown>
        buffer = buffer.slice(boundary + 1)
        if (event.type === 'done') return
        if (event.type === 'error') {
          throw new Error(typeof event.message === 'string' ? event.message : 'Speech synthesis failed.')
        }
        if (event.type !== 'audio' || typeof event.wav !== 'string' || !event.wav) {
          throw new Error('The speech service sent an invalid audio chunk.')
        }
        yield Uint8Array.from(atob(event.wav), (character) => character.charCodeAt(0)).buffer
        if (signal.aborted) return
        boundary = buffer.indexOf('\n')
      }
      if (buffer.length > MAX_SPEECH_RECORD_CHARS) throw new Error('A speech chunk was too large.')
    }
  } finally {
    signal.removeEventListener('abort', cancel)
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}

function errorMessage(error: unknown): string {
  if (error instanceof DOMException && error.name === 'NotFoundError') {
    return 'No microphone found. Connect a microphone or headset, then enable voice.'
  }
  if (error instanceof DOMException && error.name === 'NotAllowedError') {
    return 'Microphone permission was denied. Allow it in your browser, then enable voice.'
  }
  return error instanceof Error ? error.message : 'Voice stopped unexpectedly.'
}
