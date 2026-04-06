from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Text
from sqlalchemy.sql import func
from backend.db import Base

class AuctionHouse(Base):
    __tablename__ = "auction_houses"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    premium_pct = Column(Float, nullable=False)
    address = Column(String)
    per_item_fee = Column(Float, default=0.0)
    distance_km = Column(Float)
    drive_minutes = Column(Float)
    auctioneer_id = Column(Integer)
    always_include = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())

class Snipe(Base):
    __tablename__ = "snipes"
    id = Column(Integer, primary_key=True)
    lot_url = Column(String, nullable=False)
    lot_title = Column(String)
    lot_id = Column(String)
    thumbnail_url = Column(String)
    max_cap = Column(Float, nullable=False)
    current_bid = Column(Float)
    increment = Column(Float)
    our_last_bid = Column(Float)
    winning_bid = Column(Float)
    status = Column(String, default="watching")
    end_time = Column(DateTime)
    auction_house_id = Column(Integer, ForeignKey("auction_houses.id"))
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

class DealCheck(Base):
    __tablename__ = "deal_checks"
    id = Column(Integer, primary_key=True)
    item_name = Column(String, nullable=False)
    bid_price = Column(Float, nullable=False)
    true_cost = Column(Float)
    ebay_avg_sold = Column(Float)
    ebay_low = Column(Float)
    ebay_high = Column(Float)
    ebay_results = Column(Text)
    amazon_search_url = Column(String)
    verdict = Column(String)
    auction_house_id = Column(Integer, ForeignKey("auction_houses.id"))
    created_at = Column(DateTime, server_default=func.now())

class Settings(Base):
    __tablename__ = "settings"
    id = Column(Integer, primary_key=True, default=1)
    global_spend_cap = Column(Float, nullable=False, default=0.0)
    max_single_snipe_cap = Column(Float, nullable=False, default=200.0)
    home_address = Column(String)
    gas_price_per_liter = Column(Float, default=1.80)
    fuel_consumption = Column(Float, default=11.6)
    watchlist_postal_code = Column(String)
    watchlist_radius_km = Column(Integer, default=50)

class BidLog(Base):
    __tablename__ = "bid_log"
    id = Column(Integer, primary_key=True)
    snipe_id = Column(Integer, ForeignKey("snipes.id"), nullable=False)
    lot_title = Column(String)
    lot_url = Column(String)
    bid_amount = Column(Float, nullable=False)
    result = Column(String, nullable=False)
    message = Column(String)
    created_at = Column(DateTime, server_default=func.now())

class WatchlistSearch(Base):
    __tablename__ = "watchlist_searches"
    id = Column(Integer, primary_key=True)
    search_term = Column(String, nullable=False)
    enabled = Column(Integer, default=1)
    created_at = Column(DateTime, server_default=func.now())

class WatchlistResult(Base):
    __tablename__ = "watchlist_results"
    id = Column(Integer, primary_key=True)
    search_id = Column(Integer, ForeignKey("watchlist_searches.id"), nullable=False)
    hibid_lot_id = Column(Integer, unique=True)
    title = Column(String)
    lot_url = Column(String)
    thumbnail_url = Column(String)
    current_bid = Column(Float)
    bid_count = Column(Integer)
    min_bid = Column(Float)
    closes_at = Column(DateTime)
    is_closed = Column(Integer, default=0)
    auction_name = Column(String)
    auction_city = Column(String)
    auctioneer_name = Column(String)
    auctioneer_id = Column(Integer)
    distance_miles = Column(Float)
    buyer_premium_pct = Column(Float)
    shipping_offered = Column(Integer, default=0)
    currency = Column(String, default="CAD")
    status = Column(String, default="new")
    matched_house_id = Column(Integer, ForeignKey("auction_houses.id"))
    first_seen_at = Column(DateTime, server_default=func.now())
    last_seen_at = Column(DateTime, server_default=func.now())
