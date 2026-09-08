"""Spoken formatting must not invent a currency, sign, magnitude, or operator."""

import pytest

from recollect.engine.voice_text import spoken_text


@pytest.mark.parametrize(("original", "expected"), [
    ("$1.2 million per year", "one million two hundred thousand dollars per year"),
    ("$2 thousand", "two thousand dollars"),
    ("$1.5 billion", "one billion five hundred million dollars"),
    ("$2 trillion", "two trillion dollars"),
    ("$.50 per request", "zero dollars and fifty cents per request"),
    ("-$.50", "minus zero dollars and fifty cents"),
    ("−$400", "minus four hundred dollars"),
    ("$400k-$600k", "four hundred thousand dollars to six hundred thousand dollars"),
    ("$400–600/month", "four hundred dollars to six hundred dollars per month"),
    ("$400 — $600", "four hundred dollars to six hundred dollars"),
    ("-$400--$200", "minus four hundred dollars to minus two hundred dollars"),
    ("$.50–.75", "zero dollars and fifty cents to zero dollars and seventy five cents"),
    ("C$1", "one Canadian dollar"),
    ("CA$400/month", "four hundred Canadian dollars per month"),
    ("CAD 400", "four hundred Canadian dollars"),
    ("CAD $400", "four hundred Canadian dollars"),
    ("A$400", "four hundred Australian dollars"),
    ("AU$400", "four hundred Australian dollars"),
    ("AUD $400", "four hundred Australian dollars"),
    ("CAD 400–600", "four hundred Canadian dollars to six hundred Canadian dollars"),
    ("C$400–A$600", "four hundred Canadian dollars to six hundred Australian dollars"),
    ("USD 1.2 million", "one million two hundred thousand dollars"),
    ("~$400/month", "approximately four hundred dollars per month"),
])
def test_unambiguous_amounts_preserve_value_sign_and_currency(original, expected):
    assert spoken_text(original) == expected


@pytest.mark.parametrize("original", [
    "€1.234,56", "$1,23", "$1.23.45", "$5e3", "HK$400", "NZ$400",
])
def test_unknown_currency_and_ambiguous_amounts_stay_literal(original):
    assert spoken_text(original) == original


@pytest.mark.parametrize(("original", "expected"), [
    ("$2 + 2 = 4$", "2 + 2 = 4"),
    ("$$2*3=6$$", "2 times 3=6"),
    ("Use 2*3=6.", "Use 2 times 3=6."),
    ("2**3 = 8", "2**3 = 8"),
    (r"\(x_1 + x_2 = y^2\)", "x_1 + x_2 = y^2"),
    (r"\[|x_1| \leq y^2\]", r"|x_1| \leq y^2"),
    (r"$\frac{2}{3}$", "2 divided by 3"),
    (r"\($2 + 2 = 4$\)", "2 + 2 = 4"),
    (r"\(\$400/month\)", "four hundred dollars per month"),
    (r"It costs $\$400$ / month.", "It costs four hundred dollars per month."),
    ("$1 and $2", "one dollar and two dollars"),
    ("$1 and $.50", "one dollar and zero dollars and fifty cents"),
    ("$ 1 and $ 2", "one dollar and two dollars"),
    ("**2** times *3* equals 6.", "2 times 3 equals 6."),
])
def test_math_delimiters_and_markdown_do_not_delete_meaning(original, expected):
    assert spoken_text(original) == expected


@pytest.mark.parametrize("snippet", [
    "API_KEY=$TOKEN ./start.sh", "$1.2 million", "2*3", "x_1", "https://example.com",
])
def test_quoted_inline_code_is_not_rewritten_as_prose(snippet):
    original = f'Use the literal "`{snippet}`", then continue.'
    unchanged = original.encode("utf-8")
    assert spoken_text(original) == f'Use the literal "{snippet}", then continue.'
    assert original.encode("utf-8") == unchanged


def test_rendering_leaves_the_original_markdown_and_amounts_intact():
    original = "**Budget:** $1.2 million, or CAD 400–600 per day."
    unchanged = original.encode("utf-8")
    assert spoken_text(original) == (
        "Budget: one million two hundred thousand dollars, or four hundred "
        "Canadian dollars to six hundred Canadian dollars per day."
    )
    assert original.encode("utf-8") == unchanged


def test_literal_control_characters_cannot_collide_with_protected_snippets():
    assert spoken_text("\x00code0\x00 and `x_1`") == "\x00code0\x00 and x_1"
