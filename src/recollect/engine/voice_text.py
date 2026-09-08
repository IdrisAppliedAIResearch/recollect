"""Plain spoken rendering of reply text, without changing the stored answer."""

from __future__ import annotations

import re
from decimal import Decimal

_SMALL = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
]
_TENS = [
    "zero", "ten", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
    "eighty", "ninety",
]
_CURRENCY = r"(?:US|CA|C|AU|A)\$|(?:USD|CAD|AUD)\s*\$?|[$£€]"
_AMOUNT = r"(?:\d+(?:,\d{3})*(?:\.\d+)?|\.\d+)"
_SCALE = r"[kmb](?!\w)|\s+(?:thousand|million|billion|trillion)\b"
# Reject partial parses of ambiguous amounts such as €1.234,56 or $1,23.
_AMOUNT_END = r"(?!\w|[.,]\d)"
_MONEY = re.compile(
    rf"(?<![\w$])(?P<sign>[-+−]?)(?P<currency>{_CURRENCY})\s*"
    rf"(?P<amount>{_AMOUNT})(?P<scale>{_SCALE})?{_AMOUNT_END}"
    rf"(?:\s*[-–—]\s*(?P<range_sign>[-+−]?)"
    rf"(?:(?P<range_currency>{_CURRENCY})\s*)?"
    rf"(?P<range_amount>{_AMOUNT})(?P<range_scale>{_SCALE})?{_AMOUNT_END})?",
    re.I,
)


def _integer(value: int) -> str:
    if value >= 10**15:
        return " ".join(_SMALL[int(digit)] for digit in str(value))
    if value < 20:
        return _SMALL[value]
    if value < 100:
        tens, rest = divmod(value, 10)
        return _TENS[tens] + (" " + _integer(rest) if rest else "")
    for size, label in (
        (10**12, "trillion"), (10**9, "billion"), (10**6, "million"),
        (1000, "thousand"), (100, "hundred"),
    ):
        if value >= size:
            count, rest = divmod(value, size)
            prefix = _integer(count) + " " + label
            return prefix + (" " + _integer(rest) if rest else "")
    raise ValueError("Expected a non-negative amount")


def _amount(value: str, scale: str | None, currency: str, sign: str) -> str:
    amount = Decimal(value.replace(",", ""))
    amount *= {
        "k": 1000, "thousand": 1000, "m": 10**6, "million": 10**6,
        "b": 10**9, "billion": 10**9, "trillion": 10**12,
    }.get(
        (scale or "").strip().lower(), 1,
    )
    whole = int(amount)
    fraction = amount - whole
    currency = re.sub(r"\s+", "", currency).upper()
    if currency in {"C$", "CA$", "CAD", "CAD$"}:
        unit, minor = "Canadian dollar", "cent"
    elif currency in {"A$", "AU$", "AUD", "AUD$"}:
        unit, minor = "Australian dollar", "cent"
    else:
        unit, minor = {"£": ("pound", "penny"), "€": ("euro", "cent")}.get(
            currency, ("dollar", "cent"),
        )
    words = _integer(whole)
    if fraction and fraction * 100 != int(fraction * 100):
        digits = format(amount, "f").split(".")[1].rstrip("0")
        words += " point " + " ".join(_SMALL[int(digit)] for digit in digits)
        words += " " + unit + "s"
    else:
        words += " " + unit + ("" if whole == 1 else "s")
        if fraction:
            cents = int(fraction * 100)
            plural = "pence" if minor == "penny" else minor + "s"
            words += " and " + _integer(cents) + " " + (minor if cents == 1 else plural)
    return {"-": "minus ", "−": "minus ", "+": "plus "}.get(sign, "") + words


def _money(match: re.Match) -> str:
    words = _amount(match["amount"], match["scale"], match["currency"], match["sign"])
    if match["range_amount"] is not None:
        words += " to " + _amount(
            match["range_amount"], match["range_scale"],
            match["range_currency"] or match["currency"], match["range_sign"],
        )
    return words


def _tables(text: str) -> str:
    lines = text.splitlines()
    output = []
    index = 0
    while index < len(lines):
        if index + 1 < len(lines) and "|" in lines[index]:
            divider = lines[index + 1].strip().strip("|").split("|")
            if len(divider) > 1 and all(re.fullmatch(r"\s*:?-{3,}:?\s*", cell)
                                        for cell in divider):
                headers = [cell.strip() for cell in lines[index].strip().strip("|")
                           .split("|")]
                index += 2
                while index < len(lines) and "|" in lines[index]:
                    cells = [cell.strip() for cell in lines[index].strip().strip("|")
                             .split("|")]
                    output.append("; ".join(
                        f"{headers[position]}: {cell}"
                        if position < len(headers) and headers[position] else cell
                        for position, cell in enumerate(cells) if cell
                    ) + ".")
                    index += 1
                continue
        output.append(lines[index])
        index += 1
    return "\n".join(output)


def spoken_text(text: str) -> str:
    """Keep meaning and amounts while removing visual formatting from speech."""
    marker = "\x00"
    while marker in text:
        marker += "\x00"
    code = []
    math = []

    def keep_code(match: re.Match) -> str:
        code.append(match[2])
        return f"{marker}code{len(code) - 1}{marker}"

    def keep_math(value: str) -> str:
        value = re.sub(
            re.escape(marker) + r"math(\d+)" + re.escape(marker),
            lambda match: math[int(match[1])], value,
        )
        # Only explicitly escaped dollars denote currency inside an expression.
        if r"\$" in value and not re.search(r"(?<!\\)\$", value):
            value = _MONEY.sub(_money, value.replace(r"\$", "$"))
        value = re.sub(
            r"(?<=\d)\s*\*\s*(?=[+-]?(?:\d|\.\d))", " times ", value,
        )
        value = re.sub(r"(?<=\d)\s*%", " percent", value)
        math.append(value)
        return f"{marker}math{len(math) - 1}{marker}"

    text = re.sub(r"```.*?```", " Code omitted from speech. ", text, flags=re.S)
    # Literal snippets may contain money-like variables and numeric operators.
    text = re.sub(r"(`+)([^`]*?)\1", keep_code, text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = _tables(text)
    text = re.sub(r"(?m)^\s*(?:[-*_]\s*){3,}$", "", text)
    text = re.sub(r"(?m)^\s{0,3}(?:#{1,6}|>|[-*+]|\d+[.)])\s+", "", text)
    text = re.sub(r"\\(?:text|mathrm|mathbf)\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"\1 divided by \2", text)
    # A closing math delimiter is not the start of another amount ($1 and $2).
    # Escaped currency inside math still becomes ordinary currency afterward.
    text = re.sub(
        r"(?<!\\)(\${1,2})(?!\$)((?:\\.|[^$\\])*?)\1(?![\w$]|\s*(?:\d|\.\d))",
        lambda match: keep_math(match[2]), text,
    )
    text = re.sub(r"\\\((.*?)\\\)", lambda match: keep_math(match[1]), text, flags=re.S)
    text = re.sub(r"\\\[(.*?)\\\]", lambda match: keep_math(match[1]), text, flags=re.S)
    text = text.replace(r"\$", "$")
    text = re.sub(rf"~\s*(?=(?:{_CURRENCY})\s*{_AMOUNT})", "approximately ", text)
    text = _MONEY.sub(_money, text)
    text = re.sub(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", r"\2", text)
    text = re.sub(r"(?<!\w)([*_])(?=\S)(.+?)(?<=\S)\1(?!\w)", r"\2", text)
    text = re.sub(r"(?<=\d)\s*\*\s*(?=[+-]?(?:\d|\.\d))", " times ", text)
    text = re.sub(r"\band\s*/\s*or\b", "and or", text, flags=re.I)
    text = text.replace("|", ", ")
    text = re.sub(r"(?<=\d)\s*%", " percent", text)
    text = re.sub(
        re.escape(marker) + r"math(\d+)" + re.escape(marker),
        lambda match: math[int(match[1])], text,
    )
    text = re.sub(
        r"\b(dollars?|cents?|pounds?|penn(?:y|ies)|pence|euros?)\s*/\s*(?=\w)",
        r"\1 per ", text,
    )
    text = re.sub(
        re.escape(marker) + r"code(\d+)" + re.escape(marker),
        lambda match: code[int(match[1])], text,
    )
    return re.sub(r"\s+", " ", text).strip()
