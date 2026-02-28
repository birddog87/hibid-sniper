from backend.hibid_scraper import parse_lot_id_from_url, parse_increment, parse_premium_from_text


def test_parse_lot_id():
    url = "https://www.hibid.com/lot/284765708/john-deere-lp-72-land-plane-attachment"
    assert parse_lot_id_from_url(url) == "284765708"


def test_parse_lot_id_trailing_slash():
    url = "https://www.hibid.com/lot/123456/some-item/"
    assert parse_lot_id_from_url(url) == "123456"


def test_parse_lot_id_with_params():
    url = "https://www.hibid.com/lot/123456/some-item?ref=search"
    assert parse_lot_id_from_url(url) == "123456"


def test_parse_increment():
    assert parse_increment("$2.00") == 2.0
    assert parse_increment("$5.00") == 5.0
    assert parse_increment("$10.00") == 10.0


def test_parse_premium_from_text():
    text = "Buyer's Premium: 15%"
    assert parse_premium_from_text(text) == 15.0


def test_parse_premium_from_text_variant():
    text = "A 16% buyer's premium applies"
    assert parse_premium_from_text(text) == 16.0
