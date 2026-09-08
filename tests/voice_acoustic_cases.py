"""Opt-in synthetic acoustic evaluation; importing this module runs no models.

Call run_acoustic_cases(voice) from the deployment evaluation harness while it
owns the GPU slot. No chat turns, files, microphone access, or service changes
are made here. This is deliberately outside pytest's collected test names.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence

import numpy as np

from recollect.engine.voice import SAMPLE_RATE, VoiceService
from recollect.engine.voice_vad import VAD_SAMPLES

ACOUSTIC_CASES = [
    {
        "id": "wake_same_breath_us_female", "mode": "wake", "voice": "af_heart",
        "segments": ["Hey, Idris, remember the blue door."],
        "should_activate": True, "expected_transcripts": 1,
        "critical": {"first_request": ["remember"], "object": ["blue door"]},
    },
    {
        "id": "wake_same_breath_us_male", "mode": "wake", "voice": "am_adam",
        "segments": ["Hey, Idris, remember the red folder."],
        "should_activate": True, "expected_transcripts": 1,
        "critical": {"first_request": ["remember"], "object": ["red folder"]},
    },
    {
        "id": "wake_same_breath_british", "mode": "wake", "voice": "bf_emma",
        "lang": "en-gb", "segments": ["Hey, Idris, remember the green room."],
        "should_activate": True, "expected_transcripts": 1,
        "critical": {"first_request": ["remember"], "object": ["green room"]},
    },
    {
        "id": "wake_alone", "mode": "wake", "segments": ["Hey, Idris."],
        "should_activate": True, "expected_transcripts": 0,
    },
    {
        "id": "confusable_iris", "mode": "wake", "segments": ["Hey, Iris."],
        "should_activate": False, "expected_transcripts": 0,
    },
    {
        "id": "confusable_address", "mode": "wake", "voice": "am_adam",
        "segments": ["Hey, address this package to my office."],
        "should_activate": False, "expected_transcripts": 0,
    },
    {
        "id": "ambient_idris_mention", "mode": "wake",
        "segments": ["I watched a film with Idris Elba yesterday."],
        "should_activate": False, "expected_transcripts": 0,
    },
    {
        "id": "synthetic_hiss_during_playback", "mode": "playback",
        "segments": [{"noise": "hiss", "seconds": 3, "amplitude": 0.03}],
        "expected_transcripts": 0,
    },
    {
        "id": "short_yes", "mode": "active", "segments": ["Yes."],
        "expected_transcripts": 1, "critical": {"decision": ["yes"]},
    },
    {
        "id": "short_no_fast", "mode": "active", "speed": 1.2,
        "segments": ["No."], "expected_transcripts": 1,
        "critical": {"decision": ["no"]},
    },
    {
        "id": "number_correction", "mode": "active", "speed": 0.9,
        "segments": ["Actually, sixteen hundred dollars, not fifteen hundred."],
        "expected_transcripts": 1,
        "critical": {"new_amount": ["sixteen hundred", "1600"],
                     "negation": ["not"], "old_amount": ["fifteen hundred", "1500"]},
    },
    {
        "id": "cents_not_dollars", "mode": "active",
        "segments": ["Fifty cents per request, not fifty dollars."],
        "expected_transcripts": 1,
        "critical": {"amount": ["fifty cents", "50 cents"], "rate": ["per request"],
                     "negation": ["not"], "contrast": ["fifty dollars", "50 dollars"]},
    },
    {
        "id": "currency_identity_british_male", "mode": "active",
        "voice": "bm_george", "lang": "en-gb",
        "segments": [
            "Four hundred Canadian dollars and four hundred Australian dollars."
        ],
        "expected_transcripts": 1,
        "critical": {"canadian": ["canadian dollars"],
                     "australian": ["australian dollars"]},
    },
    {
        "id": "bytes_not_bits", "mode": "active",
        "segments": ["Five megabytes per second, not five megabits per second."],
        "expected_transcripts": 1,
        "critical": {"bytes": ["megabytes", "mega bytes"],
                     "bits": ["megabits", "mega bits"], "negation": ["not"]},
    },
    {
        "id": "multiplication_and_power", "mode": "active",
        "segments": ["Two times three is six. Two to the power of three is eight."],
        "expected_transcripts": 1,
        "critical": {"multiply": ["times"], "power": ["power"],
                     "six": ["six", "6"], "eight": ["eight", "8"]},
    },
    {
        "id": "name_time_abbreviations", "mode": "active", "voice": "bf_emma",
        "lang": "en-gb", "segments": ["Dr. Rao's ETA is 4:15 p.m. Central."],
        "expected_transcripts": 1,
        "critical": {"name": ["rao"], "time": ["four fifteen", "4 15"],
                     "zone": ["central"]},
    },
    {
        "id": "patient_mid_sentence_pause", "mode": "active",
        "segments": ["Remember the blue folder", {"silence_s": 0.8},
                     "and put it on the top shelf."],
        "expected_transcripts": 1,
        "critical": {"first_part": ["blue folder"], "last_part": ["top shelf"]},
    },
    {
        "id": "two_turns_without_rewake", "mode": "active",
        "segments": [{"text": "Remember the blue folder.", "utterance_end": True},
                     {"silence_s": 2.5}, "Now remember the red door."],
        "expected_transcripts": 2,
        "critical": {"first_turn": ["blue folder"], "second_turn": ["red door"]},
    },
    {
        "id": "barge_in_with_quieter_speech", "mode": "playback", "gain": 0.35,
        "segments": ["Actually, stop. Just tell me the monthly budget."],
        "expected_transcripts": 1,
        "critical": {"first_word": ["actually"], "request": ["monthly budget"]},
    },
    {
        "id": "synthetic_clicks_during_playback", "mode": "playback",
        "segments": [{"noise": "clicks", "seconds": 3, "amplitude": 0.7}],
        "expected_transcripts": 0,
    },
]


def _normalized(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


def _contains(text: str, alternatives: list[str]) -> bool:
    return any(f" {_normalized(value)} " in f" {text} " for value in alternatives)


def _feed(listener, samples: np.ndarray) -> list[dict]:
    events = []
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    for start in range(0, len(pcm), VAD_SAMPLES):
        frame = pcm[start:start + VAD_SAMPLES]
        if len(frame) < VAD_SAMPLES:
            frame = np.pad(frame, (0, VAD_SAMPLES - len(frame)))
        at = min(start + VAD_SAMPLES, len(pcm)) / SAMPLE_RATE
        events.extend({**event, "audio_s": at}
                      for event in listener.feed(frame.tobytes()))
    return events


def run_acoustic_cases(
    voice: VoiceService,
    case_ids: Sequence[str] | None = None,
    on_case: Callable[[dict], None] | None = None,
) -> dict:
    """Return JSON-safe measurements; caller serializes and schedules model use.

    End latency is measured against the end of the synthesized speech segment,
    not a manually annotated last phoneme. Playback mode sets the actual
    listener's playback flag but does not simulate browser echo cancellation.
    """
    selected = set(case_ids) if case_ids is not None else None
    known = {case["id"] for case in ACOUSTIC_CASES}
    if selected is not None and selected - known:
        raise ValueError(f"Unknown acoustic cases: {sorted(selected - known)}")
    started = time.perf_counter()
    voice.warm_up()
    available = set(voice._kokoro.get_voices())
    config = voice.config
    cache = {}

    def speech(text: str, name: str, speed: float, lang: str) -> np.ndarray:
        key = (text, name, speed, lang)
        if key not in cache:
            with voice._speech_lock:
                samples, rate = voice._kokoro.create(
                    text, voice=name, speed=speed, lang=lang,
                )
            samples = np.asarray(samples, dtype=np.float32).reshape(-1)
            if not len(samples) or not np.isfinite(samples).all() or rate <= 0:
                raise ValueError("Evaluation synthesis produced invalid audio.")
            positions = np.arange(round(len(samples) * SAMPLE_RATE / rate))
            cache[key] = np.interp(
                positions * rate / SAMPLE_RATE, np.arange(len(samples)), samples,
            ).astype(np.float32)
        return cache[key]

    results = []
    for case in ACOUSTIC_CASES:
        if selected is not None and case["id"] not in selected:
            continue
        case_started = time.perf_counter()
        name, lang = case.get("voice", "af_heart"), case.get("lang", "en-us")
        speed = case.get("speed", 1.0)
        result = {
            "id": case["id"], "input_voice": name, "input_lang": lang,
            "input_speed": speed, "input_gain": case.get("gain", 1.0),
            "mode": case["mode"], "status": "error", "issues": [],
            "expected_transcripts": case["expected_transcripts"],
            "input_segments": case["segments"],
        }
        try:
            if name not in available:
                raise ValueError(f"Evaluation voice {name!r} is unavailable.")
            listener = voice.new_listener()
            if case["mode"] != "wake":
                setup = np.concatenate([
                    speech("Hey, Idris.", "af_heart", 1.0, "en-us"),
                    np.zeros(round((config.voice_end_s + 0.6) * SAMPLE_RATE)),
                ])
                result["setup_events"] = _feed(listener, setup)
                if listener.state != "listening" or any(
                    event["type"] == "transcript" for event in result["setup_events"]
                ):
                    result["status"] = "setup_failed"
                    result["issues"].append(
                        "Wake audio did not leave a clean active listener."
                    )
                    result["wall_s"] = time.perf_counter() - case_started
                    results.append(result)
                    if on_case is not None:
                        on_case(result)
                    continue
                listener.set_playback(case["mode"] == "playback")

            pieces = [np.zeros(round(0.25 * SAMPLE_RATE), dtype=np.float32)]
            elapsed = len(pieces[0]) / SAMPLE_RATE
            utterance_ends, speech_intervals = [], []
            rng = np.random.default_rng(20260908)
            for item in case["segments"]:
                segment = {"text": item} if isinstance(item, str) else item
                if "text" in segment:
                    audio = speech(segment["text"], name, speed, lang)
                    audio = audio * case.get("gain", 1.0)
                    end = elapsed + len(audio) / SAMPLE_RATE
                    speech_intervals.append({"start_s": elapsed, "end_s": end})
                    if segment.get("utterance_end"):
                        utterance_ends.append(end)
                elif "silence_s" in segment:
                    audio = np.zeros(round(segment["silence_s"] * SAMPLE_RATE))
                else:
                    length = round(segment["seconds"] * SAMPLE_RATE)
                    amplitude = segment["amplitude"]
                    if segment["noise"] == "hiss":
                        audio = rng.normal(0, amplitude, length)
                    elif segment["noise"] == "clicks":
                        audio = np.zeros(length)
                        for offset in range(0, length, round(0.23 * SAMPLE_RATE)):
                            count = min(32, length - offset)
                            audio[offset:offset + count] = (
                                amplitude * np.hanning(count) * rng.choice([-1, 1])
                            )
                    else:
                        raise ValueError(f"Unknown synthetic noise: {segment['noise']}")
                pieces.append(audio)
                elapsed += len(audio) / SAMPLE_RATE
            if speech_intervals and (
                not utterance_ends
                or utterance_ends[-1] != speech_intervals[-1]["end_s"]
            ):
                utterance_ends.append(speech_intervals[-1]["end_s"])
            pieces.append(np.zeros(round((config.voice_end_s + 0.7) * SAMPLE_RATE)))
            samples = np.concatenate(pieces)
            initial_state = listener.state
            listening_started = time.perf_counter()
            events = _feed(listener, samples)
            listener_wall_s = time.perf_counter() - listening_started
            transcript_events = [e for e in events if e["type"] == "transcript"]
            transcripts = [e["text"] for e in transcript_events]
            joined = _normalized(" ".join(transcripts))
            critical = [
                {"name": term, "alternatives": alternatives,
                 "preserved": _contains(joined, alternatives)}
                for term, alternatives in case.get("critical", {}).items()
            ]
            activated = any(e.get("state") == "listening" for e in events)
            speech_starts = [
                e["audio_s"] for e in events if e["type"] == "speech_start"
            ]
            false_trigger = (
                (case.get("should_activate") is False and activated)
                or (case["expected_transcripts"] == 0 and bool(transcripts))
                or (case["mode"] == "playback" and case["expected_transcripts"] == 0
                    and bool(speech_starts))
            )
            if case["mode"] == "wake" and activated != case["should_activate"]:
                result["issues"].append("Wake activation differed from expectation.")
            if len(transcripts) != case["expected_transcripts"]:
                result["issues"].append("Transcript count differed from expectation.")
            if any(not term["preserved"] for term in critical):
                result["issues"].append("Critical terms were not preserved.")
            if false_trigger:
                result["issues"].append("A negative case triggered the listener.")
            result.update({
                "status": "fail" if result["issues"] else "pass",
                "audio_s": len(samples) / SAMPLE_RATE,
                "listener_wall_s": listener_wall_s,
                "transcripts": transcripts,
                "partial_count": sum(e["type"] == "partial" for e in events),
                "speech_start_audio_s": speech_starts,
                "transcript_audio_s": [e["audio_s"] for e in transcript_events],
                "end_latency_s": [
                    e["audio_s"] - utterance_ends[index]
                    if index < len(utterance_ends) else None
                    for index, e in enumerate(transcript_events)
                ],
                "speech_intervals": speech_intervals,
                "intended_utterance_end_s": utterance_ends,
                "activated": activated or initial_state == "listening",
                "activated_during_case": activated, "initial_state": initial_state,
                "final_state": listener.state,
                "false_trigger": false_trigger, "critical_terms": critical,
                "events": events,
            })
        except Exception as error:
            result["issues"].append(f"{type(error).__name__}: {error}")
        result["wall_s"] = time.perf_counter() - case_started
        results.append(result)
        if on_case is not None:
            on_case(result)
    return {
        "schema_version": 1,
        "limits": [
            "Generated Kokoro speech is not evidence of human microphone comfort.",
            "Voice styles and en-gb phonemization are not an accent population sample.",
            "Noise is synthetic hiss/clicks; no room, microphone, or AEC is modeled.",
            "Audio is linearly resampled to 16 kHz and fed without wall-clock pacing.",
            "End latency references generated segment ends, not phoneme annotations.",
            "Critical-term matching is a text proxy; inspect transcripts and audio.",
        ],
        "config": {
            "wake_phrase": config.voice_wake_phrase,
            "voice_end_s": config.voice_end_s,
            "voice_speech_start_s": config.voice_speech_start_s,
            "voice_speech_threshold": config.voice_speech_threshold,
            "voice_interrupt_threshold": config.voice_interrupt_threshold,
        },
        "wall_s": time.perf_counter() - started,
        "cases": results,
    }
