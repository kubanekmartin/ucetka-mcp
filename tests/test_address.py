from ucetka.models import Address


def test_auto_extract_simple() -> None:
    a = Address(street="28. října 15", city="Kolín", zip="28002")
    assert a.street_name == "28. října"
    assert a.building == "15"


def test_auto_extract_with_slash() -> None:
    a = Address(street="Vzorová 1234/5", city="Praha", zip="110 00")
    assert a.street_name == "Vzorová"
    assert a.building == "1234/5"


def test_auto_extract_with_letter() -> None:
    a = Address(street="Hlavní 12a", city="Brno", zip="60200")
    assert a.street_name == "Hlavní"
    assert a.building == "12a"


def test_explicit_building_number_wins() -> None:
    a = Address(street="Dlouhá 1", building_number="123", city="Praha", zip="11000")
    assert a.street_name == "Dlouhá 1"
    assert a.building == "123"


def test_no_number_in_street() -> None:
    a = Address(street="Anonymous Place", city="Praha", zip="11000")
    assert a.street_name == "Anonymous Place"
    assert a.building == ""
