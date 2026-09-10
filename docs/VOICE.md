# Local voice conversations

Say **“Hey Idris”** once to start a conversation. Your speech appears in the
text box as it is transcribed, and a 1.4-second pause submits the request to
the existing Recollect chat model and verified memory retrieval. Kokoro 82M
speaks the reply. Keep talking for follow-up turns without repeating the wake
phrase; speaking during a reply interrupts it. All speech processing stays
local. Vosk transcription and Silero speech detection run on the CPU; optional
Whisper Turbo dictation runs on the GPU. Kokoro
can use an NVIDIA GPU or the CPU. The chat model still runs at the configured
local generator address.

## Setup

Use the working Python 3.13 environment from the main quickstart. For CPU
speech, install the optional packages without removing the manually provisioned
embedder:

```bash
uv sync --extra voice --inexact
uv run --no-sync recollect voice-setup
uv run --no-sync recollect voice-doctor
uv run --no-sync recollect doctor
uv run --no-sync recollect serve
```

The `--inexact` flag matters: the binary-pinned `llama_cpp` installation must
survive dependency synchronization. See [EMBEDDER.md](EMBEDDER.md). Voice setup
itself uses the Python standard library and can download assets before the
optional speech packages are installed. `voice-doctor` checks the speech
runtime; the ordinary `doctor` checks the existing embedder and generator.

### NVIDIA GPU synthesis

On Windows or Linux with a compatible NVIDIA GPU, use the `voice-gpu` extra.
Stop the Recollect server before changing the installed runtime, so its loaded
DLLs can be replaced. When switching from the CPU speech installation:

```bash
uv pip uninstall onnxruntime
uv sync --extra voice-gpu --inexact
uv run --no-sync recollect voice-setup
uv run --no-sync recollect voice-doctor
uv run --no-sync recollect serve
```

Set `RECOLLECT_VOICE_DEVICE=cuda` in `.env` to require GPU synthesis. Explicit
CUDA mode reports an initialization error if GPU execution is unavailable;
it does not silently use CPU. The default `auto` mode prefers CUDA when
available. `RECOLLECT_VOICE_DEVICE=cpu` keeps Kokoro on the CPU, including when
the GPU runtime is installed. Silero always stays on CPU.

The pinned `onnxruntime-gpu==1.29.0` uses CUDA 13.x and cuDNN 9.x. Those major
versions must match the installed runtime libraries. A compatible PyTorch
installation can supply the DLLs; importing or installing PyTorch in Recollect
is unnecessary when the DLL directory is provided explicitly. On Windows, set
`RECOLLECT_VOICE_CUDA_DLL_DIR` to the absolute directory containing the compatible
CUDA and cuDNN DLLs, such as a CUDA 13 PyTorch installation's `torch/lib`
directory. Otherwise, ONNX Runtime uses its normal DLL search. See the official
[CUDA compatibility and DLL preload documentation](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html#requirements).

The `voice` and `voice-gpu` extras are mutually exclusive because the CPU and
GPU packages both provide the `onnxruntime` module. ONNX Runtime documents
[installing only one distribution per environment](https://onnxruntime.ai/docs/get-started/with-python.html#install-onnx-runtime).
Kokoro 0.6.1 declares the CPU package even with its own GPU extra, so Recollect
uses a version-scoped [uv dependency exclusion](https://docs.astral.sh/uv/concepts/resolution/#dependency-exclusions)
and selects the runtime explicitly. The `--inexact` flag preserves the pinned
embedder, but also preserves unrelated installed packages; remove the old CPU
runtime before switching to GPU. To switch back, stop the server, uninstall
`onnxruntime-gpu`, and run `uv sync --extra voice --inexact`.

### GPU Whisper transcription

Install Whisper alongside the selected Kokoro runtime. For CUDA Kokoro:

```bash
uv sync --extra voice-gpu --extra voice-whisper --inexact
uv run --no-sync recollect voice-setup --whisper
```

Set `RECOLLECT_VOICE_ASR_BACKEND=whisper`, then restart Recollect and run
`uv run --no-sync recollect voice-doctor`. The default backend remains `vosk`.
Whisper model files go to `var/models/voice/whisper-large-v3-turbo`; use
`voice-setup --whisper --asr-model-dir <directory>` and matching
`RECOLLECT_VOICE_ASR_MODEL_DIR` to choose another location.

The selected model is the CTranslate2 conversion of OpenAI Whisper large-v3-turbo,
pinned to [revision 0a363e9](https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo/tree/0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf).
Setup downloads approximately 1.62 GB, checks the published weights SHA-256,
and records receipts for every file. Recognition loads local files only.
The `voice-whisper` extra pins faster-whisper 1.2.1 and CTranslate2 4.8.2.
On Windows it also installs CUDA 12 cuBLAS and cuDNN 9 libraries; their DLL
directories are discovered by the transcriber. An additional directory can be
specified with `RECOLLECT_VOICE_ASR_CUDA_DLL_DIR`. These settings are separate
from Kokoro's CUDA 13 runtime settings. Linux deployments must provision the
matching CUDA 12/cuDNN 9 libraries for CTranslate2.

Whisper supplies revisable live text and a final transcript. Vosk continues to
detect “Hey Idris,” so selecting Whisper alone does not fix wake-detection
errors. Silero continues to detect speech and interruptions without waiting for
Whisper. Model quality, draft delay, and concurrent GPU use require measurement
on the deployment machine; a completed transcription is not proof of correctness.

### Model files

Setup downloads about 397 MB once. Existing verified Vosk and Kokoro files are
reused; upgrading a prior voice installation downloads only the 2.3 MB Silero
model. The default model directory is
`var/models/voice`, which is gitignored. A different directory can be provisioned
with `recollect voice-setup --model-dir <directory>`; set
`RECOLLECT_VOICE_MODEL_DIR` to the same location before starting the server.

| Runtime path below the model directory | Source |
|---|---|
| `vosk-model-small-en-us-0.15/` | [Vosk small US English 0.15 archive](https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip) |
| `kokoro-v1.0.onnx` | [Kokoro v1.0 float32 ONNX](https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx) |
| `voices-v1.0.bin` | [Kokoro v1.0 voice vectors](https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin) |
| `silero-vad.onnx` | [Silero VAD v6.2.1, pinned upstream commit](https://raw.githubusercontent.com/snakers4/silero-vad/7e30209a3e901f9842f81b225f3e93d8199902b1/src/silero_vad/data/silero_vad.onnx) |

The installer checks exact sizes and Vosk's published MD5. Silero's size and
SHA-256 are pinned from the linked upstream commit; the SHA-256 is
`1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`.
Those older Kokoro GitHub assets have no publisher-reported digest. The
installer records local SHA-256 receipts in `.voice-models.json` and rechecks
them when setup runs again, including Silero's pinned hash. Corrupt or
incomplete assets are downloaded again; verified assets are reused. Downloads
and extraction are staged before replacing an existing installation, with
path and size checks on ZIP entries. Interrupted setup can be rerun.

## Using voice

1. Open the inspector at `http://127.0.0.1:8080/` and select a conversation.
2. Click **Enable voice** and grant the browser microphone permission.
3. Wait for the wake prompt. Speech before **“Hey Idris”** does not submit turns.
4. Say **“Hey Idris, what did we decide about the budget?”**. The phrase and
   request may be spoken together. Watch the live transcript in the text box;
   pause for about 1.4 seconds when finished to submit it.
5. The inspector shows retrieval and generation as usual, then Kokoro speaks
   the completed, verified reply. Playback starts when its first audio chunk is
   ready, and subsequent chunks play in order. Ask your next question directly.
   Speaking while an answer is being generated or played cancels the old reply
   and starts transcribing your new request.

Use **Mute mic** when you anticipate background noise. The current reply keeps
playing, and microphone audio cannot interrupt it while muted. **Unmute mic**
returns to the same conversation without another wake phrase.
**Stop reply** cancels the current answer while keeping voice
available. **Replay reply** speaks the last completed reply again without
another model request or a new stored turn.
Replay is unavailable while you are dictating, so it cannot discard your request.

You can also say the exact phrases **“stop talking”**, **“repeat that”**, or
**“pause microphone”**, optionally after “Hey Idris”. Say **“stop listening”**
or **“turn off voice”** to release the microphone completely. These controls
are handled locally after transcription and do not create chat turns. Unmute
with the button, since the microphone cannot hear commands while muted.

Click **Stop voice** to release the microphone and stop speech. Stopping voice
also cancels its pending client request. A turn that has already been stored
remains in the conversation.

An uninterrupted request can last up to two minutes by default. If it reaches
the limit while you are still speaking, capture pauses and the text stays in
the composer. Choose **Send captured text** or **Discard and resume**; the
incomplete request is never submitted automatically. Ordinary short pauses
still submit a completed request normally.

A failed chat request or audio reply leaves voice available and shows the
error. You can ask again or replay a completed reply after a speech error.
Recollect does not automatically retry a chat request, which could duplicate
a turn that was already stored before the connection failed.
If the microphone or voice connection unexpectedly disconnects before a request
is sent, the captured words return to the editable text box in that conversation.
Review or correct them before sending; recovery never submits automatically.

Voice requests ask the model for one to three short sentences by default,
using natural spoken amounts and units without tables or Markdown. Ask for
more detail when needed; detailed answers retain the normal generation budget
and are not cut off to enforce brevity. Typed requests keep their normal
response style.

Voice guidance uses a short, direct instruction: one conversational paragraph,
usually one to three sentences, with more detail when asked. Headings, point
labels, lists, and Markdown are excluded even in detailed replies. It encourages
contractions and natural punctuation variety without prescribing each mark.
The model still produces one reply for chat and speech, with the original
memory instructions retained. These are prompting instructions, not enforced
output constraints.

Kokoro renders each detected sentence separately. The renderer appends a fixed
500 ms of PCM silence between sentences, including in streamed playback and
replay. It adds no silence after the final sentence or at bounded splits inside
a long sentence. This is added silence: natural pauses in Kokoro's waveform and
any live delivery delays can make the audible interval longer. Periods in common
titles, initials, decimals and abbreviations are handled conservatively; English
sentence detection remains a heuristic. Punctuation is preserved, not replaced
with ellipses. See [the listening decision and evidence](VOICE_LISTENING.md).

Speech formatting expands unambiguous amounts and ranges, including written
magnitudes such as `$1.2 million` and explicitly Canadian or Australian dollars.
Math delimiters do not turn numbers into dollars, and numeric multiplication
keeps its operator. Unsupported formulas and ambiguous number formats remain
literal instead of being assigned an invented interpretation. Inline code also
stays literal; fenced code is announced as omitted from speech. Exact code,
formulas, links, and the original reply remain available in the conversation;
their spoken pronunciation depends on the English speech engine.

This is listening in an enabled browser page, not a Windows background service.
Keep the page open and the computer awake. A browser suspension, disconnected
microphone, or dropped connection stops voice and displays a reason; enable
voice again after resolving it. Localhost or HTTPS is required for browser
microphone access. Voice is unavailable in mock mode.

The microphone stays open while voice is enabled, including during generation
and playback. Pausing disables its audio track and transmission until you
resume; **Stop voice** releases the device. After the first wake phrase,
conversation remains active until
you stop voice; silence does not require another wake phrase. Browser echo
cancellation is enabled. Use headphones if loudspeaker echo causes unwanted
interruptions. Brief noises below the speech duration and confidence thresholds
do not interrupt a reply. A reply already stored remains in conversation
history even if you interrupt its playback.

## Settings

Settings belong to `RecollectConfig`; they do not change the research mechanism
or stored embedding identity. Restart Recollect after changing `.env`.

| Environment variable | Default | Purpose |
|---|---|---|
| `RECOLLECT_VOICE_MODEL_DIR` | `var/models/voice` | Local speech model files |
| `RECOLLECT_VOICE_WAKE_PHRASE` | `hey idris` | Phrase to start the conversation |
| `RECOLLECT_VOICE_NAME` | `af_heart` | Kokoro voice identifier |
| `RECOLLECT_VOICE_DEVICE` | `auto` | Kokoro device: `auto`, `cpu`, or required `cuda` |
| `RECOLLECT_VOICE_CUDA_DLL_DIR` | unset | Optional absolute CUDA 13/cuDNN 9 DLL directory |
| `RECOLLECT_VOICE_THREADS` | `2` | CPU threads for Kokoro's ONNX session |
| `RECOLLECT_VOICE_MAX_UTTERANCE_S` | `120` | Duration before pausing a long request for review |
| `RECOLLECT_VOICE_WAIT_S` | `8` | Timeout for an empty utterance; conversation stays active |
| `RECOLLECT_VOICE_END_S` | `1.4` | Silence before submitting a spoken request |
| `RECOLLECT_VOICE_SPEECH_START_S` | `0.224` | Sustained speech needed to start a follow-up or interruption |
| `RECOLLECT_VOICE_SPEECH_THRESHOLD` | `0.5` | Silero confidence threshold for speech |
| `RECOLLECT_VOICE_INTERRUPT_THRESHOLD` | `0.8` | Higher speech confidence required to interrupt a reply |
| `RECOLLECT_VOICE_ASR_BACKEND` | `vosk` | Dictation backend: `vosk` or `whisper`; wake detection remains Vosk |
| `RECOLLECT_VOICE_ASR_MODEL_DIR` | `var/models/voice/whisper-large-v3-turbo` | Provisioned local Whisper files |
| `RECOLLECT_VOICE_ASR_DEVICE` | `cuda` | Whisper device: `cuda` or `cpu` |
| `RECOLLECT_VOICE_ASR_COMPUTE_TYPE` | `float16` | Whisper precision; use a supported CPU type such as `int8` for CPU execution |
| `RECOLLECT_VOICE_ASR_CUDA_DLL_DIR` | unset | Optional additional CUDA 12/cuDNN 9 DLL directory |

Wake phrases must use words present in the Vosk model vocabulary. Startup checks
this instead of silently dropping unknown words. Use multiple distinctive
words; background speech containing the phrase can activate it. Transcription
quality and false activation rates depend on the microphone, room, and speaker.
The shipped input model is US English.

The default output uses American English `af_heart`, speed 1.0, and 24 kHz mono
audio. [Kokoro's voice list](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md)
describes the other voice IDs. `Kokoro.get_voices()` returns the IDs actually
present in the installed voice file. Selecting another language's voice does
not change this integration's English transcription and phonemization.

## How the local path works

The browser resamples microphone audio to 16 kHz mono PCM and sends small
chunks over the local voice WebSocket. A constrained Vosk recognizer checks the
wake phrase until the conversation starts. A short rolling audio buffer
preserves words spoken immediately after activation and at the start of
follow-up turns. An unrestricted recognizer provides the live transcript;
Silero VAD controls turn endings and detects speech during generation and
playback. Vosk's internal phrase boundaries do not immediately submit a turn.

With Whisper selected, the wake recognizer and Silero retain that fast input
path. A separate worker decodes bounded audio snapshots, with at most one
native decode and one replaceable pending snapshot. Drafts are requested at
one-second audio intervals. A silence endpoint prioritizes the final decode;
speech that resumes before that result is delivered extends the unsent request.
Mute, reset, or disconnect invalidates pending text. Native inference already
executing can finish, but its invalidated result cannot enter the conversation.
Actual inference is warmed before voice reports ready. Live text can change as
Whisper receives more context; only the final transcript is submitted.

The final request enters the ordinary chat path. There is no separate memory
engine and no relaxation of the authority/shadow comparison. Voice guidance
is placed before the changing memory block, keeping the system prefix stable
for prompt caching across spoken follow-ups. Trace character counts include
the voice instructions actually sent to the model. Kokoro synthesizes
the completed assistant reply in pieces while the microphone continues
listening for your next turn. Each audio chunk can play as it becomes ready;
the complete recording does not need to be prepared first. Speech interruption
stops playback and cancels pending client
requests; a stale reply is not spoken after a new request starts. Audio is
processed in memory. Before wake activation, ambient recognition is not added
to the episode store. Once the conversation is active, detected speech can
become a submitted request. Submitted requests and assistant replies follow
the existing conversation persistence rules.

The default [`kokoro-onnx`](https://github.com/thewh1teagle/kokoro-onnx) tokenizer
uses Phonemizer with eSpeak NG supplied by `espeakng-loader`; it needs no
separate Windows eSpeak installation. This integration does not install the
optional Misaki tokenizer or download language packages during a conversation.
Model downloads occur only through setup. These speech phonemes have no
connection to the separately pinned conversational embedder.

## Troubleshooting

- **Models or voice packages missing:** run the installation and setup commands
  above, then `recollect voice-doctor`. Restart the running server afterward.
- **CUDA is unavailable or DLL loading fails:** confirm that only
  `onnxruntime-gpu` is installed and that its CUDA 13/cuDNN 9 libraries are
  present. Set `RECOLLECT_VOICE_CUDA_DLL_DIR` if they live in another Python
  environment, then restart Recollect and run `voice-doctor`.
- **Microphone access denied:** allow microphone access in the browser's site
  permissions and Windows microphone privacy settings, then enable voice again.
- **No wake detection:** confirm the displayed phrase, microphone selection and
  input level. Run `voice-doctor` to check vocabulary and model initialization.
- **Transcription cuts off at a pause:** increase `RECOLLECT_VOICE_END_S` for
  more thinking time within a turn. Restart the server after changing it.
- **Answers stop when you have not spoken:** try headphones or lower speaker
  volume. Echo cancellation depends on the browser and audio hardware. Increase
  `RECOLLECT_VOICE_INTERRUPT_THRESHOLD` or `RECOLLECT_VOICE_SPEECH_START_S`
  if brief background sounds still interrupt replies.
- **New speech does not interrupt:** check microphone input and lower
  `RECOLLECT_VOICE_INTERRUPT_THRESHOLD` if your voice is consistently quiet.
- **Slow replies:** the overall wait includes existing model inference and
  Kokoro synthesis. GPU synthesis and audio chunk playback address the speech
  portion; the chat reply still completes before speech starts. Keeping the
  models loaded avoids repeated initialization. No latency target is claimed
  without a measurement on the deployment machine.

Vosk and Kokoro weights are Apache-2.0; Silero VAD and the Kokoro wrapper are
MIT. The default phonemizer and eSpeak NG components have GPL terms. See
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) for the component licences.
