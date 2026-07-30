from odc_core import validate_username


def test_valid_username():
    assert validate_username.validate("jdoe123") is True


def test_username_with_punctuation_is_valid():
    assert validate_username.validate("j.doe@example") is True


def test_empty_username_is_invalid():
    assert validate_username.validate("") is False


def test_none_username_is_invalid():
    assert validate_username.validate(None) is False


def test_punctuation_only_username_is_invalid():
    assert validate_username.validate("...---") is False
