"""Edition barcodes, not provider or library identifiers."""

import re
from typing import Literal

type NativeBarcode = Literal["isbn", "upc"]
NATIVE_BARCODES: dict[NativeBarcode, str] = {"isbn": "ISBN", "upc": "UPC"}
BARCODE_HELP = {
    "isbn": "Enter an ISBN-10 or ISBN-13 with a valid check digit. Spaces and hyphens are kept.",
    "upc": "Enter a 12-digit UPC-A or 8-digit UPC-E, with an optional 2- or 5-digit add-on.",
    "gtin": "Enter one retail barcode (GTIN-8, -12, -13, -14 or ISBN), or choose the Metron "
    "ISBN or UPC. Only its retail number is copied, not a UPC add-on.",
}


def _check_digit(body: str) -> str:
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return str(-total % 10)


def _expand_upce(symbol: str) -> str:
    # GS1 zero suppression: the sixth symbol character determines where zeros belong.
    a, b, c, d, e, last = tuple(symbol[1:-1])
    if last in "012":
        body = f"0{a}{b}{last}0000{c}{d}{e}"
    elif last == "3" and c not in "012":
        body = f"0{a}{b}{c}00000{d}{e}"
    elif last == "4" and d != "0":
        body = f"0{a}{b}{c}{d}00000{e}"
    elif last in "56789" and e != "0":
        body = f"0{a}{b}{c}{d}{e}0000{last}"
    else:
        return ""
    return body + symbol[-1]


def barcode_gtin(field: str, value: object) -> str | None:
    """Return the comparable retail number, without altering the original display text."""
    if not isinstance(value, str) or len(value) > 40 or value != value.strip():
        return None
    if field == "upc":
        match = re.fullmatch(r"([0-9]{12}|0[0-9]{7})(?:[ +\-]?(?:[0-9]{2}|[0-9]{5}))?", value)
        code = match[1] if match else ""
        if len(code) == 8:
            code = _expand_upce(code)
        return code if code and _check_digit(code[:-1]) == code[-1] else None
    if field not in {"gtin", "isbn"}:
        return None
    if field == "gtin" and re.fullmatch(r"[0-9]{8}|[0-9]{12,14}", value):
        return value if _check_digit(value[:-1]) == value[-1] else None
    if not re.fullmatch(r"[0-9](?:[ -]?[0-9]){7,11}[ -]?[0-9Xx]", value):
        return None
    code = value.replace(" ", "").replace("-", "").upper()
    if re.fullmatch("[0-9]{9}[0-9X]", code):
        total = sum(int(d) * (10 - i) for i, d in enumerate(code[:-1]))
        if (total + (10 if code[-1] == "X" else int(code[-1]))) % 11:
            return None
        body = "978" + code[:-1]
        return body + _check_digit(body)
    if re.fullmatch(r"97[89][0-9]{10}", code) and not code.startswith("9790"):
        return code if _check_digit(code[:-1]) == code[-1] else None
    return None


def native_kind(gtin: str) -> NativeBarcode | None:
    padded = gtin.zfill(14)
    if padded.startswith("0") and barcode_gtin("isbn", padded[1:]):
        return "isbn"
    if len(gtin) >= 12 and padded.startswith("00"):
        return "upc"
    return None
