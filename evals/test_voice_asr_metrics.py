"""Model-free checks for the opt-in dictation benchmark's evidence contract."""

from __future__ import annotations

import pytest

from .voice_asr_benchmark import (
    main,
    normalize_tokens,
    run_benchmark,
    summarize,
    transcript_metrics,
)
from .voice_asr_cases import asr_cases


def term(reference, *accepted):
    return {
        "reference": reference,
        "expected": reference,
        "accepted_forms": [reference, *accepted],
    }


def test_punctuation_abbreviations_and_grouping_are_not_word_errors():
    metrics = transcript_metrics(
        "Dr. Rao's ETA is 4:15 p.m., Central!", "dr raos E T A is 4.15 PM central", []
    )
    assert metrics["normalized_exact"]
    assert metrics["lexical_wer"] == 0
    assert normalize_tokens("$1,600.50") == ["$1600.50"]


def test_correct_numeric_orthography_is_visible_separately_from_lexical_wer():
    metrics = transcript_metrics(
        "At four thirty PM, not three PM.",
        "At 4.30 p.m., not 3 PM.",
        [term("four thirty PM", "4:30 PM"), term("not three PM", "not 3 PM")],
    )
    assert metrics["lexical_wer"] > 0
    assert metrics["all_critical_forms_match"]
    assert metrics["critical"][0]["observed_normalized"] == "4.30 pm"


def test_currency_symbol_alias_preserves_amount():
    metrics = transcript_metrics(
        "The fee is zero point zero five dollars per item.",
        "The fee is $0.05 per item.",
        [term("zero point zero five dollars", "$0.05")],
    )
    assert metrics["all_critical_forms_match"]
    wrong = transcript_metrics(
        "The fee is zero point zero five dollars per item.",
        "The fee is $0.50 per item.",
        [term("zero point zero five dollars", "$0.05")],
    )
    assert not wrong["all_critical_forms_match"]


def test_wrong_time_and_deleted_negation_fail_even_when_other_words_match():
    metrics = transcript_metrics(
        "Four thirty PM, not three PM.",
        "Three PM, four thirty PM.",
        [term("Four thirty PM", "4:30 PM"), term("not three PM", "not 3 PM")],
    )
    assert not metrics["all_critical_forms_match"]
    negation = transcript_metrics(
        "Do not send it yet.", "Do send it yet.", [term("Do not send it yet")]
    )
    assert not negation["all_critical_forms_match"]


def test_currency_identity_and_homophones_are_not_rewritten():
    metrics = transcript_metrics(
        "Four hundred Canadian dollars.",
        "Four hundred Australian dollars.",
        [term("Four hundred Canadian dollars", "400 Canadian dollars")],
    )
    assert not metrics["all_critical_forms_match"]
    homophone = transcript_metrics(
        "Clear the browser cache.", "Clear the browser cash.", [term("browser cache")]
    )
    assert homophone["substitution"] == 1
    assert not homophone["all_critical_forms_match"]


def test_edit_counts_and_empty_reference_are_explicit():
    metrics = transcript_metrics("red green blue", "red grey", [])
    assert metrics["word_errors"] == 2
    assert metrics["substitution"] == 1
    assert metrics["deletion"] == 1
    assert metrics["insertion"] == 0
    empty = transcript_metrics("", "", [])
    assert empty["lexical_wer"] is None
    assert empty["normalized_exact"]
    assert not empty["nonspeech_hallucination"]
    hallucination = transcript_metrics("", "Thank you for watching.", [])
    assert hallucination["lexical_wer"] is None
    assert hallucination["nonspeech_hallucination"]
    assert hallucination["insertion"] == 4


def test_fixture_set_is_fixed_diverse_and_self_consistent():
    cases = asr_cases()
    assert len(cases) == 52
    assert len({case["id"] for case in cases}) == 52
    assert {case.get("voice") for case in cases if case["reference"]} == {
        "af_heart",
        "am_adam",
        "bf_emma",
        "bm_george",
    }
    assert {case["split"] for case in cases} == {
        "baseline",
        "held_out_wording",
        "nonspeech",
    }
    assert {case["condition"] for case in cases} >= {
        "clean",
        "quiet",
        "hiss",
        "pause",
        "silence",
        "clicks",
    }
    for case in cases:
        metrics = transcript_metrics(
            case["reference"], case["reference"], case["critical"]
        )
        assert metrics["normalized_exact"]
        assert metrics["all_critical_forms_match"] is not False


def test_summary_does_not_mix_nonspeech_hallucinations_into_speech_wer():
    cases = [
        {
            "decodes": [
                {
                    "backend": "vosk",
                    "decode_s": 0.1,
                    "real_time_factor": 0.05,
                    "metrics": transcript_metrics("one two", "one three", []),
                },
                {
                    "backend": "vosk",
                    "decode_s": 0.2,
                    "real_time_factor": 0.1,
                    "metrics": transcript_metrics("", "hello there", []),
                },
            ]
        }
    ]
    summary = summarize(cases)["vosk"]
    assert summary["lexical_wer_weighted"] == 0.5
    assert summary["nonspeech_hallucinations"] == 1
    assert summary["speech_decodes"] == 1
    assert summary["warm_decode_s_median"] == pytest.approx(0.15)


def test_report_refusal_precedes_runtime_access(tmp_path):
    report = tmp_path / "existing.json"
    report.write_text("original evidence", encoding="utf-8")
    with pytest.raises(FileExistsError):
        run_benchmark(object(), report=report)
    with pytest.raises(SystemExit) as error:
        main(["--report", str(report)])
    assert error.value.code == 2
    assert report.read_text(encoding="utf-8") == "original evidence"


def test_invalid_cases_and_missing_critical_reference_fail_before_inference(tmp_path):
    report = tmp_path / "not-created.json"
    with pytest.raises(ValueError, match="Unknown case"):
        run_benchmark(object(), report=report, case_ids=["unknown"])
    assert not report.exists()
    with pytest.raises(ValueError, match="Critical reference is absent"):
        transcript_metrics("hello", "hello", [term("not in reference")])
