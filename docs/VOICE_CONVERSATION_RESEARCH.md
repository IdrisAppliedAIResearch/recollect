# Conversational speech in ChatGPT Voice and Recollect

This report records the research that guided the experiments. The subsequent
user-selected implementation and its limits are in
[VOICE_LISTENING.md](VOICE_LISTENING.md).

The strongest next direction for Recollect is to test conversational wording
and context-sensitive delivery before doing more pause tuning. The listening
samples evaluated a speaker reading prepared prose. They did not evaluate an
assistant responding to another person, acknowledging a correction, hesitating
at an appropriate moment, or deciding to keep listening. Those are different
behaviors, and they require different tests.

The public evidence supports three separate areas of work: choosing words that
fit the conversational moment; realizing those words with suitable emphasis,
pitch and rhythm; and coordinating speech with the other participant. Kokoro
can participate in the first area through better text supplied by the language
model. Its installed interface offers much less direct control over the second.
The third requires changes to the interaction system, beyond the speech
synthesizer. This is an engineering assessment, not a measured improvement.

## ChatGPT Voice: the current reference

OpenAI's current Help Center distinguishes **Live**, **Advanced**, and
**Standard** Voice. Live uses GPT-Live-1 or GPT-Live-1 mini, depending on the
plan. Advanced is the previous real-time experience; Standard uses transcribed
turns. Availability varies with account and application settings. Consequently,
an account's actual selected mode should be recorded before using its output
as an experimental reference. These product distinctions were checked against
the official documentation on September 9, 2026.[1]

OpenAI introduced GPT-Live on July 8, 2026. Its published architectural
description has two central elements: continuous, simultaneous audio input
and output, and delegation of deeper work to a separate frontier model.
The interaction model makes frequent decisions about speaking, listening,
pausing and tool use. The release explicitly describes short acknowledgments
such as “mhmm” and “yeah.” It also reports human comparisons using matched
five-to-ten-minute conversations that assessed interaction and flow.[2]

The system card independently confirms continuous response behavior and the
ability to react to pauses, interruptions and changes in pace. Its training
disclosure describes broad categories of public, licensed and supplied or
generated data. It does **not** provide enough detail to reproduce the voice
model: the reviewed card does not specify a complete audio architecture,
conversational data mixture, filler-placement objective, acoustic conditioning
recipe or production prompt.[3]

This limits the answer to “how did they do it?” We can describe the disclosed
system design. We can study open systems that learn similar behaviors. We
cannot honestly supply OpenAI's private recipe or claim that a particular
filler-insertion algorithm is what ChatGPT uses.

The Realtime API is a related reference, but its public model names and
application configuration are not evidence that it exactly reproduces the
ChatGPT Live product. The official voice-agent guide distinguishes direct
speech-to-speech sessions from explicitly chained transcription, reasoning
and synthesis. It associates the former with conversational responsiveness
and the latter with control over intermediate text and existing agent logic.[4]

## What conversational filler actually includes

“Filler” is a useful everyday description, but it groups together behaviors
that should have different implementation policies. A short expression can
acknowledge understanding, signal a transition, soften a correction, indicate
hesitation, or encourage the other person to continue. Its meaning also
depends on delivery. A rising “okay?” differs from a falling “okay.”

The following examples are proposed design distinctions, not quotations from
ChatGPT or a claim about its hidden instructions.

| Behavior | Example | Purpose | Failure to watch for |
| --- | --- | --- | --- |
| Acknowledgment at the start of a reply | “Ah, I see what you mean.” | Connect the answer to a clarification | Automatic agreement without understanding |
| Discourse transition | “So, the part I'd change first is…” | Introduce the next point | Starting every response with the same phrase |
| Reformulation | “The wording, I mean—the way it responds to you.” | Clarify a thought | Manufactured mistakes and excessive restarts |
| Filled hesitation | “Um…” | Mark a hesitation when its delivery fits | Repetitive delay, uncertainty where none is warranted |
| Listener feedback, or backchannel | “Mm-hm.” | Encourage the current speaker to continue | Taking the floor or signaling agreement unintentionally |
| Task acknowledgment | “I'll check the dates.” | Explain an actual action | Pretending to be checking something that has not started |
| Silence | No spoken response yet | Leave room for an unfinished thought | Appearing unresponsive when an answer is expected |

Figueroa and colleagues tested synthesized short feedback in recorded
conversational contexts. Their 2024 study found that TTS feedback sounded
more natural than signal-processed or monotone alternatives, while original
human feedback remained more natural. Fine-grained signal processing better
preserved some intended communicative functions. Naturalness, contextual
appropriateness and conveyed meaning did not collapse into a single score.[5]

The practical implication is to evaluate what an acknowledgment *means* as
well as whether it sounds pleasant. For example, a neutral acknowledgment of
hearing a concern should not accidentally sound like an enthusiastic endorsement.
Likewise, a backchannel should not be implemented as an ordinary full reply
with a shorter text string: its relationship to the other speaker is different.

## Research on inserting fillers

FillerSpeech, published at EMNLP 2025, models filler position, type, duration
and pitch. It combines a fine-tuned language model for prediction with a
speech model trained to condition on filler style. Its experiments compare
learned placement with random sampling and report an advantage for learned
placement. The paper also identifies a pronunciation tradeoff in some acoustic
variants and resource costs for the prediction model. Some placement judgments
use a language-model evaluator, so they are not independent evidence of live
user satisfaction.[6]

This is unusually relevant to the present question. It demonstrates a concrete
research approach to filler generation, while also showing why text insertion
alone is incomplete. A label saying “um” does not specify its pitch movement,
length, loudness, or relationship to the following word. Those choices affect
whether it resembles a conversational hesitation or an extra word being read.

It would be inappropriate to treat that paper as a Kokoro feature list. Its
style controls require a specifically trained synthesis system. A prompt to
Recollect's language model cannot add those conditioning inputs to the
installed Kokoro graph. Its findings justify testing a hypothesis; they do
not demonstrate that our current model will realize it convincingly.

Research on language-model outputs provides a complementary observation.
A SIGDIAL 2025 study evaluates disfluencies and feedback in models adapted
to spoken English and French, using conversational corpora and metrics for
these features. It supports examining spoken-language behavior separately
from ordinary written-response quality. Its experiments use different models
and data from Recollect, so they do not establish how the installed Qwen
model will respond to a new instruction.[7]

Random injection is therefore a weak starting point. It can increase the
frequency of surface markers without improving the reason for using them.
A better initial hypothesis is to let the language model select the response's
conversational function, then use ordinary natural wording to express it.
Actual hesitations should be a small, separately evaluated addition.

## Prosody and conversational context

Sesame's conversational speech research describes a one-to-many problem:
the same sentence has many plausible spoken realizations, and preceding
conversation helps determine which one fits. Its CSM approach conditions
speech generation on text and audio context. Its contextual human evaluation
still found a gap between generated and human continuations, even when
isolated naturalness was difficult to distinguish. Sesame also explicitly
distinguishes contextual speech generation from modeling the entire structure
of a live conversation.[8]

For Recollect, this suggests another missing experimental variable. A reply
like “That makes sense” should be heard after the statement it addresses.
Testing that phrase alone removes much of the information needed to judge it.
A passage about a garden can test narration comfort; it cannot establish
whether an assistant handles surprise, disagreement or clarification naturally.

The public CSM release is an audio generator. Its repository recommends
supplying conversational context and says it cannot generate text; a separate
language model is needed. The released base model is also distinct from the
fine-tuned model used in Sesame's interactive demonstration. Those distinctions
matter when estimating the benefit of swapping synthesizers.[9]

An initial CSM audition would therefore be a test of contextual rendering,
not a replacement for Recollect's reasoning system. It should receive the
same authored reply as Kokoro and a clearly documented, permitted audio
context. A favorable demo would still leave latency, cancellation, voice
consistency and deployment compatibility unmeasured.

## Turn-taking and concurrent listening

Moshi provides an open research example of full-duplex modeling. It represents
the user's speech and the system's speech as parallel streams, allowing overlap
and interjections without first reducing interaction to a sequence of explicit
speaker turns. Its paper also describes aligned text predictions before audio
generation. This is evidence that conversational timing can be learned within
an audio model; it is not evidence that GPT-Live uses Moshi's implementation.[10]

NVIDIA's PersonaPlex is another public example. Its published description
uses the Moshi architecture, a seven-billion-parameter backbone, voice and text
conditioning, and a mixture of natural and synthetic conversations. It describes
learning conversational behavior from separately represented speakers and
demonstrates contextual backchanneling. Its real-conversation data and synthetic
assistant dialogues serve different training purposes.[11]

These systems explain why “add more silence” has limited reach. Whether to
wait after a short pause depends on whether the person appears finished,
not just on the elapsed milliseconds. Whether to speak during another person's
turn depends on whether a brief acknowledgment would help or interrupt.

There are intermediate approaches short of replacing the entire stack.
OpenAI's semantic VAD documentation describes estimating whether an utterance
is complete rather than using only silence duration. Its example gives a
longer wait to speech trailing off in hesitation. This is an API mechanism
and an architectural reference, not an existing Kokoro capability or a
documented explanation of GPT-Live's own internal turn policy.[12]

Recollect already detects speech onset during playback and supports
interruption. That should not be mislabeled as having no concurrent audio
handling. However, the inspected path still completes an utterance using
silence and capture limits, obtains a text reply, and synthesizes it. Input
monitoring during playback is different from a model continuously reasoning
over both participants' acoustic streams.[16]

## Recollect's present behavior and constraints

The repository inspection establishes several details that narrow the next
step. These are observations of the current checkout, not general claims about
all Kokoro integrations.[16]

| Current behavior | Consequence for this investigation |
| --- | --- |
| `_VOICE_INSTRUCTIONS` already requests one to three short, conversational sentences by default | Adding only “be conversational” would repeat an existing instruction |
| Voice instructions avoid visual formatting and express amounts in spoken words | Preserve this useful behavior while improving interaction style |
| The listening generators supply authored text directly to TTS | Neither round tested live language generation or its voice instructions |
| `/api/voice/speech` speaks a verified completed reply | A new post-generation rewriting model would create another text version that needs explicit provenance |
| `spoken_text` normalizes formatting, money and symbols | It is a meaning-preserving renderer, not a conversational author |
| Production TTS bounds its first chunk at 120 characters and later chunks at 240 | Tiny standalone filler clips and extensive resegmentation are not neutral changes |
| The installed Kokoro wrapper takes text/phonemes, voice style and speed; no natural-language delivery instruction argument | A conversational prompt belongs in the language-generation layer, not the TTS text as a stage direction |
| The recorded ONNX output is audio only | The available wrapper's timing-dependent features are not enabled by this artifact |

Kokoro's model card describes an 82-million-parameter system based on StyleTTS 2
and ISTFTNet, with a decoder-only release and no diffusion or released encoder.
The architecture label should not be used to assume that every control in a
StyleTTS 2 paper or another implementation is present in the installed model.[13]

Its voice documentation also warns about weak short utterances, especially
below roughly 10–20 tokens, and rushing at long extremes. Those are model
tokens, not this application's character limits. The documentation suggests
bundling short utterances as one possible mitigation. This is a reason to
test “Ah, I see what you mean” together with its following sentence before
building a bank of isolated filler sounds.[14]

The earlier listening results do not select a production configuration.
Round one supplied tentative preferences among specific recordings. The
second round was abandoned because none of the sampled approaches matched
the desired direction. That feedback makes another broad timing sweep a poor
use of listening effort. The previous blend can be held constant as a test
control without being declared the preferred final voice.

## Recommended next experiment

The recommendation is a small conversation-based test with a stopping rule.
It should answer whether better response wording produces a meaningful
change before introducing another synthesizer or a new interaction engine.
The following design is proposed; no outcome is implied.

**First, approve the conversational character in text.** Prepare three short
exchanges: a clarification, a mild disagreement, and a practical request.
Write a direct version and a more conversational version with the same
substantive answer. Keep them short enough that the listener can compare
the intent without remembering a long passage.

For a clarification, an example prompt could be: “The voice is clear. It
just feels like it is reading something to me.” The direct reply might be:

> The next test should change the wording. We can compare two short replies
> using the same voice.

The conversational alternative could be:

> Ah, I see what you mean. Let's change the wording first—two short replies,
> same voice—and see whether either feels closer.

These are authored examples, not recovered ChatGPT prompts. The acknowledgment
is tied to a clarification. It does not invent uncertainty, agreement with a
false statement, or a reason to delay an immediately available answer.

**Second, audition wording with Kokoro held steady.** Use the same voice,
speed, normalization and production chunking for both versions. Include the
preceding conversational turn in the page and, if audio is supplied, make
clear whose voice is synthetic. Differences in text length will affect
duration; do not force equal duration by changing speed and thereby adding a
confound. This test evaluates the wording package rather than one phonetic
feature in isolation.

**Third, test actual hesitation only if the conversational wording helps.**
Compare an otherwise identical reply with and without one contextually
plausible hesitation. Keep acknowledgment and hesitation as separate factors.
The listener should be able to select “neither” and stop immediately if the
effect feels performative. There is no reason to require another seven-minute
suite before accepting a negative result.

**Fourth, compare synthesizers if delivery remains the obstacle.** Use the
same accepted dialogue text with Kokoro and one contextual or instruction-guided
speech model. The purpose is to discover whether the acoustic model can realize
the desired phrasing, not to select a winner from marketing examples. Only
after a convincing short comparison should sustained conversation be tested.

**Finally, evaluate turn-taking interactively.** A recording cannot establish
that the assistant waits through an unfinished thought, distinguishes an
acknowledgment from an interruption, or cancels speech promptly. That test
belongs to a later live prototype and should preserve the current verified
memory and answer path.

## A concrete language-policy prototype

The following is an original, untested prompt addition for a future isolated
experiment. It complements the current voice instructions rather than
replacing their formatting and accuracy requirements.

```text
Speak as someone responding to the person in this conversation.
Use contractions and ordinary spoken phrasing when they fit.
Respond to the user's actual point before introducing the next idea.
A brief acknowledgment or transition is welcome when it has a purpose.
Do not start every answer with an acknowledgment, and do not reuse the same
opening habitually. A direct answer may need no opening at all.
Keep each reply focused on the next useful thought. Expand when asked.
Use uncertainty language only when the answer is uncertain.
Do not manufacture mistakes, repeated words, or claims of thinking to sound human.
Do not emit stage directions, bracketed emotions, or instructions to the narrator.
If work is actually starting, acknowledge that action briefly and accurately.
Leave out routine invitations and follow-up questions when they add nothing.
```

OpenAI's public Realtime prompting guidance supports concise, purpose-specific
instructions, variation across openings, and avoiding overreliance on sample
phrases. Its preamble guidance also warns that unnecessary spoken updates
can add perceived delay and discourages filler used merely to occupy time.
This is useful application guidance, not ChatGPT's private system prompt.[15]

A later test can add permission for occasional filled hesitation after the
baseline conversational wording has been accepted. It should not impose a
quota such as one “um” per sentence. Repetition should be assessed over several
turns because a phrase can sound natural once and become irritating through
reuse. The goal is a responsive speaking style with room for variation, not
maximum disfluency.

The lowest-risk integration point, if the experiment succeeds, is the
existing voice-specific generation instruction. Let the generated, visible
reply contain the conversational wording and pass that reply through the
existing speech renderer. A separate rewriting pass would add latency and
create a risk that the stored answer differs from what was spoken. If a
separate realization layer later becomes necessary, its input, output and
meaning-preservation checks should be explicit rather than hidden inside
`spoken_text`.

## Options beyond the first experiment

| Option | What it could test | What remains unresolved |
| --- | --- | --- |
| Existing Qwen plus Kokoro | Whether conversational language is the largest missing ingredient | Acoustic realization of hesitation and emphasis |
| Qwen plus CSM with conversation context | Whether preceding speech helps the same reply fit its context | Local speed, memory use, Windows environment, voice stability and interruption |
| Qwen plus instruction-guided TTS | Explicit control over tone and delivery of fixed text | Provider dependence if hosted; no automatic improvement to turn-taking |
| A Moshi/PersonaPlex-style interaction model around Recollect | Learned listening, overlap and feedback behavior | A substantial integration with text authority, memory and task delegation |
| A hosted speech-to-speech comparison | A practical reference for a more integrated voice experience | Exact equivalence to ChatGPT Live is not established by API branding |

OpenAI's TTS guide documents promptable accent, tone, emotional range,
intonation and speaking speed for `gpt-4o-mini-tts`. That makes it a possible
controlled rendering reference for fixed text. It should not be confused
with ChatGPT Live, and it would introduce a hosted audio service rather
than improve the local Kokoro model.[17]

The local hardware already hosts a substantial chat model and GPU speech
components. Parameter count alone cannot establish that another speech model
will fit or meet latency requirements alongside them. Any audition of a new
model should use a separate environment and measure actual peak GPU memory,
time to first audio, sustained generation rate, and cancellation under the
intended concurrent workload. No such benchmark was performed for this report.

A duplex interaction model also must not silently become a second authority
on facts or memory. Recollect's verified answer path is a defining constraint.
An architectural experiment needs an explicit contract for which component
may acknowledge, ask a clarification, repeat an approved result, or contribute
new substantive content. Retaining that authority is a design requirement,
not a reason to weaken verification in pursuit of smoother speech.

## Evaluation and decision criteria

The most useful primary question is: “Does this feel like a response to what
was just said?” Audio clarity and pleasantness remain relevant but are not
sufficient. Separate questions should capture whether emphasis fits the
meaning, whether an acknowledgment sounds appropriate, whether hesitation
is helpful or distracting, and whether repeated turns become formulaic.

For an initial comparison, record the exact preceding turn, response text,
voice settings and generated file identity. Include ties and rejection of
both alternatives. Replay accepted examples on another occasion before
treating a preference as stable. An early rejection is valid evidence about
the direction of work, although it is not a quantitative ranking of every
unheard candidate.

For live interaction, include deliberately unfinished sentences, a short
mid-thought pause, a correction during assistant speech, a brief listener
acknowledgment, and a request to remain quiet. Record premature replies,
missed interruptions, repeated acknowledgments, speech after cancellation,
and any mismatch between the verified text and audible answer. These are
proposed test cases, not descriptions of failures measured in this session.

Acceptance should require a noticeable preference for the conversational
direction without loss of meaning or increased annoyance. If accepted text
still sounds like narration in Kokoro, proceed to the focused acoustic-model
comparison. If the text itself feels artificial, revise the response style
before spending time on another model. If turn-taking is the remaining issue,
evaluate an interaction architecture rather than more prerecorded pauses.

## Sources

Public sources were accessed September 9, 2026. Dates below are publication
dates where available; current documentation pages may change. Numbered
citations refer to the specific linked source, not to an implied consensus.

1. OpenAI. [ChatGPT Voice](https://help.openai.com/en/articles/20001274).
   Current Help Center documentation; available modes and product distinctions.
2. OpenAI. [Introducing GPT-Live](https://openai.com/index/introducing-gpt-live/).
   July 8, 2026; updated July 31, 2026. Public architecture and interaction claims.
3. OpenAI. [GPT-Live System Card](https://deploymentsafety.openai.com/gpt-live).
   July 8, 2026; correction log August 4, 2026. Introduction and data/training
   disclosure. Safety scores are not used as a naturalness benchmark here.
4. OpenAI. [Voice agents](https://developers.openai.com/api/docs/guides/voice-agents).
   Current API documentation; chained and speech-to-speech architecture choices.
5. Carol Figueroa, Marcel de Korte, Magalie Ochs and Gabriel Skantze.
   [Mhm... Yeah? Okay! Evaluating the Naturalness and Communicative Function of
   Synthesized Feedback Responses in Spoken Dialogue](https://aclanthology.org/2024.sigdial-1.46.pdf).
   SIGDIAL 2024, especially sections 3–5, pp. 546–549.
6. Seung-Bin Kim, Jun-Hyeok Cha, Hyung-Seok Oh, Heejin Choi and Seong-Whan Lee.
   [FillerSpeech: Towards Human-Like Text-to-Speech Synthesis with Filler
   Insertion and Filler Style Control](https://aclanthology.org/2025.emnlp-main.1730.pdf).
   EMNLP 2025, pp. 34108–34125; methods, evaluation and limitations.
7. Oussama Silem, Maïwenn Fleig, Houda Oufaida, Leonor Becerra and Philippe Blache.
   [Evaluating Spoken Language Features in Conversational Models: The Case of
   Disfluencies and Feedbacks](https://aclanthology.org/2025.sigdial-1.23.pdf).
   SIGDIAL 2025. Spoken English/French modeling and feature-specific evaluation.
8. Johan Schalkwyk, Ankit Kumar, Dan Lyth, Sefik Emre Eskimez, Zack Hodari,
   Cinjon Resnick, Ramon Sanabria and Raven Jiang; Sesame.
   [Crossing the uncanny valley of conversational voice](https://www.sesame.com/blog/crossing-the-uncanny-valley-of-voice).
   February 27, 2025. Technical section on conversational speech generation,
   context-sensitive evaluation and limitations.
9. Sesame AI Labs. [CSM repository](https://github.com/SesameAILabs/csm).
   Initial public checkpoint release March 13, 2025; current README and FAQ.
10. Alexandre Défossez and colleagues.
    [Moshi: a speech-text foundation model for real-time dialogue](https://arxiv.org/abs/2410.00037).
    2024. Parallel audio streams and speech-text modeling; an independent open
    system, not an account of OpenAI's implementation.
11. NVIDIA ADLR. [PersonaPlex: Natural Conversational AI With Any Role and
    Voice](https://research.nvidia.com/labs/adlr/personaplex/).
    2026. Architecture, training-data description and backchannel examples.
12. OpenAI. [Voice activity detection](https://developers.openai.com/api/docs/guides/realtime-vad).
    Current API documentation; silence-based and semantic turn completion.
13. Hexgrad. [Kokoro-82M model card](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/README.md).
    Current model card; release architecture and training description.
14. Hexgrad. [Kokoro voice guidance](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md).
    Current guidance; short-utterance and long-utterance limitations.
15. OpenAI. [Using realtime models](https://developers.openai.com/api/docs/guides/realtime-models-prompting).
    Current prompting guide; conversational variety and purposeful preambles.
    Its examples span API model versions and are not ChatGPT product prompts.
16. Recollect local source, inspected September 9, 2026, branch
    `codex/issue-12-kokoro-voice-naturalness`, production HEAD `c5a3e00`:
    `src/recollect/api.py` (`_VOICE_INSTRUCTIONS`, `_turn_system_prompt`);
    `src/recollect/voice_api.py` (`speech`);
    `src/recollect/engine/voice.py` (`synthesize_chunks`, `_speech_pieces`,
    `VoiceListener._frame`); `src/recollect/engine/voice_text.py` (`spoken_text`);
    the two untracked listening generators and their local manifests.
    Installed `kokoro_onnx` 0.6.1 source was inspected for `create_timed`,
    model timing detection and graph inputs. Local evidence; no public URL.
17. OpenAI. [Text to speech](https://developers.openai.com/api/docs/guides/text-to-speech).
    Current API documentation; instruction-guided speech synthesis controls.
