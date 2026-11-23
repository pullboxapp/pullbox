import pytest

from pullbox.core.issue_barcodes import barcode_gtin


@pytest.mark.parametrize(
    ("field", "text", "expected"),
    [
        ("isbn", "0-8044-2957-x", "9780804429573"),
        ("isbn", "978 0 306 40615 7", "9780306406157"),
        ("isbn", "0-8044-2957-0", None),
        ("isbn", "4006381333931", None),
        ("isbn", "9790060115615", None),  # ISMN, not ISBN
        ("gtin", "96385074", "96385074"),
        ("gtin", "00036000291452", "00036000291452"),
        ("gtin", "03600029145200311", None),
        ("upc", "036000291452+05", "036000291452"),
        ("upc", "01234558", "012345000058"),  # GS1 General Specifications, Fig. 5-14
        ("upc", "01234558-00311", "012345000058"),
        ("upc", "01234559", None),
        ("upc", "123455", None),
        ("upc", "036000291452-123", None),
        ("upc", "\uff1036000291452", None),
    ],
)
def test_barcode_comparison_does_not_guess_or_drop_leading_zeroes(field, text, expected):
    assert barcode_gtin(field, text) == expected
