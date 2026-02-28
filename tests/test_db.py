import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from backend.db import Base, get_engine
from backend.models import AuctionHouse, Snipe, DealCheck

def test_create_auction_house():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        house = AuctionHouse(name="Burlington Auction Centre", premium_pct=15.0)
        session.add(house)
        session.commit()
        session.refresh(house)
        assert house.id == 1
        assert house.name == "Burlington Auction Centre"
        assert house.premium_pct == 15.0

def test_create_snipe():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        house = AuctionHouse(name="Test House", premium_pct=14.0)
        session.add(house)
        session.commit()
        snipe = Snipe(
            lot_url="https://hibid.com/lot/123/test-item",
            lot_title="Test Item",
            lot_id="123",
            max_cap=100.0,
            current_bid=50.0,
            increment=5.0,
            status="watching",
            auction_house_id=house.id,
        )
        session.add(snipe)
        session.commit()
        session.refresh(snipe)
        assert snipe.id == 1
        assert snipe.status == "watching"
        assert snipe.max_cap == 100.0

def test_create_deal_check():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        house = AuctionHouse(name="Test House", premium_pct=16.0)
        session.add(house)
        session.commit()
        deal = DealCheck(
            item_name="Milwaukee M18 Drill",
            bid_price=80.0,
            true_cost=104.52,
            ebay_avg_sold=120.0,
            ebay_low=90.0,
            ebay_high=150.0,
            verdict="good_deal",
            auction_house_id=house.id,
        )
        session.add(deal)
        session.commit()
        assert deal.id == 1
        assert deal.verdict == "good_deal"
