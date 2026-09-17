"""Fixed synthetic dictation fixtures; no wake gating or model execution."""

from __future__ import annotations

from .voice_acoustic_cases import ACOUSTIC_CASES
from .voice_conversation_cases import conversations


def _term(reference: str, expected: str, *aliases: str) -> dict:
    return {
        "reference": reference,
        "expected": expected,
        "accepted_forms": [reference, *aliases],
    }


def asr_cases() -> list[dict]:
    prior = {row["id"]: row for group in conversations().values() for row in group}
    acoustic = {row["id"]: row for row in ACOUSTIC_CASES}
    baseline = [
        (
            "cache",
            prior["cache"]["text"],
            [
                _term("cache", "computing cache"),
            ],
        ),
        (
            "cache_repair",
            prior["technical_clarification"]["text"],
            [
                _term("cache in a computer", "computing cache in explicit context"),
            ],
        ),
        (
            "name_time",
            acoustic["name_time_abbreviations"]["segments"][0],
            [
                _term("Rao's", "Rao", "Rao", "Rao’s"),
                _term("ETA", "estimated time of arrival", "E T A"),
                _term("4:15 p.m.", "16:15", "four fifteen PM", "4.15 PM"),
            ],
        ),
        (
            "time_change",
            prior["update_again"]["text"],
            [
                _term("four thirty PM", "16:30", "4:30 PM", "4.30 PM"),
                _term("Central", "Central time zone"),
            ],
        ),
        (
            "cents",
            prior["cents"]["text"],
            [
                _term("fifty cents", "USD 0.50", "50 cents", "$0.50"),
                _term(
                    "not fifty dollars", "negated USD 50", "not 50 dollars", "not $50"
                ),
            ],
        ),
        (
            "number_correction",
            prior["correction"]["text"],
            [
                _term("sixteen hundred dollars", "USD 1600", "$1,600", "1600 dollars"),
                _term("not fifteen hundred", "negated 1500", "not 1500", "not 1,500"),
            ],
        ),
        ("short_yes", "Yes.", [_term("Yes", "affirmation")]),
        ("short_no", "No.", [_term("No", "rejection")]),
        (
            "units",
            prior["units"]["text"],
            [
                _term("five megabytes per second", "5 MB/s", "5 megabytes per second"),
                _term("five megabits per second", "5 Mb/s", "5 megabits per second"),
            ],
        ),
        (
            "currency_identity",
            prior["foreign_currency"]["text"],
            [
                _term(
                    "four hundred Canadian dollars", "CAD 400", "400 Canadian dollars"
                ),
                _term(
                    "four hundred Australian dollars",
                    "AUD 400",
                    "400 Australian dollars",
                ),
            ],
        ),
        (
            "math",
            acoustic["multiplication_and_power"]["segments"][0],
            [
                _term("two times three is six", "2 times 3 = 6", "2 times 3 is 6"),
                _term(
                    "two to the power of three is eight",
                    "2 cubed = 8",
                    "2 to the power of 3 is 8",
                ),
            ],
        ),
        (
            "appointment",
            prior["appointment"]["text"],
            [
                _term("September eighteenth", "September 18", "September 18th"),
                _term("four fifteen PM Central", "16:15 Central", "4:15 PM Central"),
            ],
        ),
    ]
    held_out = [
        (
            "cache_cash_contrast",
            "Clear the browser cache, but keep the cash receipt.",
            [
                _term("browser cache", "computing cache"),
                _term("cash receipt", "money receipt"),
            ],
        ),
        (
            "new_time_contrast",
            "Move the call to four thirty, not three o'clock.",
            [
                _term("four thirty", "04:30", "4:30", "4.30"),
                _term(
                    "not three o'clock", "negated 03:00", "not 3 o'clock", "not 3:00"
                ),
            ],
        ),
        (
            "new_names",
            "Priya Shah and Idris Khan will meet on Thursday.",
            [
                _term("Priya Shah", "Priya Shah"),
                _term("Idris Khan", "Idris Khan"),
                _term("Thursday", "Thursday"),
            ],
        ),
        (
            "small_decimal",
            "The fee is zero point zero five dollars per item.",
            [
                _term(
                    "zero point zero five dollars", "USD 0.05", "$0.05", "0.05 dollars"
                ),
                _term("per item", "per-item rate"),
            ],
        ),
        (
            "new_range",
            "Keep the estimate between eight hundred and nine hundred euros.",
            [
                _term("eight hundred", "800", "800"),
                _term("nine hundred euros", "EUR 900", "900 euros", "€900"),
            ],
        ),
        (
            "negation_scope",
            "Do not cancel the booking; cancel only the reminder.",
            [
                _term(
                    "Do not cancel the booking",
                    "keep booking",
                    "Don't cancel the booking",
                ),
                _term("cancel only the reminder", "cancel reminder only"),
            ],
        ),
        (
            "new_units",
            "The cable is fifteen millimeters long, not fifteen centimeters.",
            [
                _term(
                    "fifteen millimeters", "15 mm", "15 millimeters", "15 millimetres"
                ),
                _term(
                    "not fifteen centimeters",
                    "negated 15 cm",
                    "not 15 centimeters",
                    "not 15 centimetres",
                ),
            ],
        ),
        (
            "identifier",
            "Use order number B seven zero nine, not B seven nine zero.",
            [
                _term("B seven zero nine", "B709", "B709", "B 709", "B seven oh nine"),
                _term("not B seven nine zero", "negated B790", "not B790", "not B 790"),
            ],
        ),
        (
            "new_currency",
            "The total is seventy five Australian dollars, not Canadian dollars.",
            [
                _term(
                    "seventy five Australian dollars", "AUD 75", "75 Australian dollars"
                ),
                _term("not Canadian dollars", "exclude CAD"),
            ],
        ),
        (
            "new_date",
            "The appointment is on October third at ten oh five A M.",
            [
                _term("October third", "October 3", "October 3rd"),
                _term("ten oh five A M", "10:05 AM", "10:05 AM", "10.05 AM"),
            ],
        ),
        (
            "temperature",
            "Set the oven to one hundred eighty degrees Celsius, not Fahrenheit.",
            [
                _term(
                    "one hundred eighty degrees Celsius",
                    "180 Celsius",
                    "180 degrees Celsius",
                    "180°C",
                    "180 Celsius",
                ),
                _term("not Fahrenheit", "exclude Fahrenheit"),
            ],
        ),
        (
            "short_reversal",
            "Wait, do not send it yet.",
            [
                _term("do not send it yet", "withhold sending", "don't send it yet"),
            ],
        ),
    ]
    voices = [
        ("af_heart", "en-us"),
        ("am_adam", "en-us"),
        ("bf_emma", "en-gb"),
        ("bm_george", "en-gb"),
    ]
    result = []
    for index, (key, text, critical) in enumerate([*baseline, *held_out]):
        for variant in range(2):
            name, lang = voices[(index + 2 * variant) % len(voices)]
            condition = (
                "clean"
                if variant == 0
                else ("clean", "quiet", "hiss", "pause")[index % 4]
            )
            if condition == "pause" and len(text.split()) < 4:
                condition = "quiet"
            result.append(
                {
                    "id": f"{key}__{name}__{condition}",
                    "family": key,
                    "split": "baseline"
                    if index < len(baseline)
                    else "held_out_wording",
                    "reference": text,
                    "critical": critical,
                    "voice": name,
                    "lang": lang,
                    "speed": 1.0 if variant == 0 else (0.9, 1.15, 1.0)[index % 3],
                    "gain": 0.2 if condition == "quiet" else 1.0,
                    "noise_std": 0.01 if condition == "hiss" else 0.0,
                    "pause_s": 0.8 if condition == "pause" else 0.0,
                    "condition": condition,
                    "seed": 20260908 + index * 2 + variant,
                    "notes": "Cache/cash are homophones; inspect context and spelling."
                    if "cache" in key
                    else "",
                }
            )
    for index, (key, seconds, noise, amplitude) in enumerate(
        [
            ("silence_short", 1.0, "silence", 0.0),
            ("silence_long", 5.0, "silence", 0.0),
            ("hiss_only", 3.0, "hiss", 0.03),
            ("clicks_only", 3.0, "clicks", 0.7),
        ]
    ):
        result.append(
            {
                "id": key,
                "family": key,
                "split": "nonspeech",
                "reference": "",
                "critical": [],
                "condition": noise,
                "seconds": seconds,
                "amplitude": amplitude,
                "seed": 20261000 + index,
            }
        )
    return result
