import pytest

from q2c.salesforce import soql


def test_literal_escapes_quotes_and_backslashes():
    assert soql.literal("O'Brien\\") == "'O\\'Brien\\\\'"


def test_contains_treats_wildcards_literally():
    assert soql.contains("50%_off") == "'%50\\%\\_off%'"


@pytest.mark.parametrize("value", ["006000000000001", "006000000000001AAA"])
def test_record_id_accepts_15_and_18_chars(value):
    assert soql.record_id(value) == f"'{value}'"


@pytest.mark.parametrize("value", ["", "006' OR Id != '", "0060000000000012", "006-00000000001"])
def test_record_id_rejects_injection_and_bad_lengths(value):
    with pytest.raises(ValueError):
        soql.record_id(value)


def test_date_rejects_non_iso_input():
    with pytest.raises(ValueError):
        soql.date("2026-01-01'; --")
