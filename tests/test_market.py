from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from trading_signal import market

NY = ZoneInfo("America/New_York")


def ny(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=NY).astimezone(timezone.utc)


def test_asset_class():
    assert market.asset_class("EUR/USD") == "fx"
    assert market.asset_class("btc/usd") == "crypto"
    assert market.asset_class("XAU/USD") == "metal"
    assert market.asset_class("AAPL") == "stock"


def test_fx_hours():
    assert market.fx_is_open(ny(2026, 9, 2, 12))  # Wednesday noon
    assert market.fx_is_open(ny(2026, 9, 4, 16, 59))  # Friday before close
    assert not market.fx_is_open(ny(2026, 9, 4, 17))  # Friday 17:00 NY close
    assert not market.fx_is_open(ny(2026, 9, 5, 12))  # Saturday
    assert not market.fx_is_open(ny(2026, 9, 6, 16))  # Sunday before open
    assert market.fx_is_open(ny(2026, 9, 6, 17))  # Sunday 17:00 NY open


def test_us_stock_hours():
    assert market.us_stocks_are_open(ny(2026, 9, 2, 9, 30))
    assert market.us_stocks_are_open(ny(2026, 9, 2, 15, 59))
    assert not market.us_stocks_are_open(ny(2026, 9, 2, 16, 0))
    assert not market.us_stocks_are_open(ny(2026, 9, 2, 9, 29))
    assert not market.us_stocks_are_open(ny(2026, 9, 5, 12))


def test_market_status():
    assert market.market_status("BTC/USD", ny(2026, 9, 5, 12)) == ("crypto", True)
    assert market.market_status("EUR/USD", ny(2026, 9, 5, 12)) == ("fx", False)
    assert market.market_status("AAPL", ny(2026, 9, 2, 12)) == ("stock", True)
