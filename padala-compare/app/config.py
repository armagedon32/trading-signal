"""Central settings: currencies, providers, affiliate links, environment variables.

Everything that a non-programmer might want to change lives here or in `.env`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("PADALA_DATA_DIR", BASE_DIR / ".data"))

SITE_NAME = os.getenv("SITE_NAME", "PadalaCompare")
SITE_URL = os.getenv("SITE_URL", "http://localhost:8000").rstrip("/")
SITE_TAGLINE = "Magkano ang matatanggap? Compare remittance rates to the Philippines."

# Seconds to keep live quotes before calling the providers again.
QUOTE_TTL_SECONDS = int(os.getenv("QUOTE_TTL_SECONDS", "600"))
HISTORY_TTL_SECONDS = int(os.getenv("HISTORY_TTL_SECONDS", "3600"))
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "8"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "")  # without the @
# Optional shared secret used to protect the /admin/* endpoints.
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")


@dataclass(frozen=True)
class Currency:
    code: str
    name: str
    country: str
    country_iso3: str  # used by Remitly's "conduit" parameter
    symbol: str
    default_amount: int
    amount_presets: tuple[int, ...]
    flag: str


# The corridors OFWs send from most. Order = order in the dropdown.
CURRENCIES: dict[str, Currency] = {
    c.code: c
    for c in (
        Currency("USD", "US Dollar", "United States", "USA", "$", 500, (100, 200, 500, 1000), "🇺🇸"),
        Currency("AED", "UAE Dirham", "United Arab Emirates", "ARE", "AED", 2000, (500, 1000, 2000, 4000), "🇦🇪"),
        Currency("SAR", "Saudi Riyal", "Saudi Arabia", "SAU", "SAR", 2000, (500, 1000, 2000, 4000), "🇸🇦"),
        Currency("CAD", "Canadian Dollar", "Canada", "CAN", "C$", 500, (100, 200, 500, 1000), "🇨🇦"),
        Currency("GBP", "British Pound", "United Kingdom", "GBR", "£", 500, (100, 200, 500, 1000), "🇬🇧"),
        Currency("AUD", "Australian Dollar", "Australia", "AUS", "A$", 500, (100, 200, 500, 1000), "🇦🇺"),
        Currency("SGD", "Singapore Dollar", "Singapore", "SGP", "S$", 500, (100, 200, 500, 1000), "🇸🇬"),
        Currency("JPY", "Japanese Yen", "Japan", "JPN", "¥", 50000, (10000, 30000, 50000, 100000), "🇯🇵"),
        Currency("EUR", "Euro", "Europe", "ITA", "€", 500, (100, 200, 500, 1000), "🇪🇺"),
        Currency("HKD", "Hong Kong Dollar", "Hong Kong", "HKG", "HK$", 5000, (1000, 2000, 5000, 10000), "🇭🇰"),
    )
}
DEFAULT_CURRENCY = "USD"
MIN_AMOUNT = 1
MAX_AMOUNT = 1_000_000


@dataclass(frozen=True)
class Provider:
    key: str                 # our stable id, also used in URLs
    name: str
    website: str             # plain (non-affiliate) sign-up page
    wise_alias: str | None   # alias used in the Wise comparison feed
    affiliate_env: str       # env var that holds YOUR affiliate/referral link
    note: str = ""

    @property
    def affiliate_url(self) -> str:
        """Your affiliate link if set in .env, otherwise the normal website."""
        return os.getenv(self.affiliate_env, "").strip() or self.website

    @property
    def has_affiliate(self) -> bool:
        return bool(os.getenv(self.affiliate_env, "").strip())


PROVIDERS: dict[str, Provider] = {
    p.key: p
    for p in (
        Provider("remitly", "Remitly", "https://www.remitly.com/", "remitly", "AFF_REMITLY",
                 "Promo rate for new customers on the first transfer."),
        Provider("wise", "Wise", "https://wise.com/", "wise", "AFF_WISE",
                 "Mid-market rate, fee shown up front."),
        Provider("worldremit", "WorldRemit", "https://www.worldremit.com/", "world-remit", "AFF_WORLDREMIT"),
        Provider("western-union", "Western Union", "https://www.westernunion.com/", "western-union", "AFF_WESTERNUNION"),
        Provider("xoom", "Xoom (PayPal)", "https://www.xoom.com/", "xoom", "AFF_XOOM"),
        Provider("instarem", "Instarem", "https://www.instarem.com/", "instarem", "AFF_INSTAREM"),
        Provider("moneygram", "MoneyGram", "https://www.moneygram.com/", "moneygram", "AFF_MONEYGRAM"),
        Provider("paypal", "PayPal", "https://www.paypal.com/", "paypal", "AFF_PAYPAL"),
        Provider("ofx", "OFX", "https://www.ofx.com/", "ofx", "AFF_OFX"),
    )
}

# Wise aliases we deliberately ignore (banks with poor rates clutter the table).
IGNORED_WISE_TYPES = {"bank"}

# Used for the "dollar is high" badge: top X% of the last N days.
HIGH_RATE_DAYS = 30
HIGH_RATE_PERCENTILE = 0.80


@dataclass
class Settings:
    """Runtime-tunable knobs grouped so tests can override them easily."""

    quote_ttl: int = QUOTE_TTL_SECONDS
    history_ttl: int = HISTORY_TTL_SECONDS
    http_timeout: float = HTTP_TIMEOUT_SECONDS
    data_dir: Path = field(default_factory=lambda: DATA_DIR)


settings = Settings()
