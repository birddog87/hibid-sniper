import pytest
from backend.ebay import build_ebay_search_url, build_ebay_sold_url, parse_price, build_amazon_search_url

def test_build_ebay_search_url():
    url = build_ebay_search_url("Milwaukee M18 Drill")
    assert "ebay.ca" in url or "ebay.com" in url
    assert "Milwaukee" in url
    assert "M18" in url
    assert "Drill" in url

def test_build_ebay_sold_url():
    url = build_ebay_sold_url("Milwaukee M18 Drill")
    assert "LH_Complete=1" in url
    assert "LH_Sold=1" in url

def test_build_amazon_search_url():
    url = build_amazon_search_url("Milwaukee M18 Drill")
    assert "amazon.ca" in url
    assert "Milwaukee" in url

def test_parse_price_simple():
    assert parse_price("$129.99") == 129.99

def test_parse_price_with_cad():
    assert parse_price("C $129.99") == 129.99

def test_parse_price_with_commas():
    assert parse_price("$1,299.99") == 1299.99

def test_parse_price_none():
    assert parse_price("") is None
    assert parse_price(None) is None
