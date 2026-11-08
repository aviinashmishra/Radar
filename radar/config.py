"""Load competitors.yaml + .env into one settings object."""
from __future__ import annotations
import os
import re
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

CONFIG_PATH = Path(os.environ.get("RADAR_CONFIG", ROOT / "config" / "competitors.yaml"))

# Where a product page lives on each storefront platform the radar can read (see collectors/shopify.py).
PRODUCT_PATH = {"shopify": "/products/", "justcorseca": "/in/product/"}


@dataclass
class Brand:
    name: str
    shopify: str | None = None                            # store root (`shopify:` or `store:` in competitors.yaml)
    self_brand: bool = False
    news_terms: list[str] = field(default_factory=list)
    watch_pages: list[str] = field(default_factory=list)
    apps: list[str] = field(default_factory=list)
    segments: list[str] = field(default_factory=list)   # e.g. ["band", "ring"]
    origin: str = "Indian"                                # Indian | Global
    tier: str = "core"                                    # core | adjacent (global premium, watched more lightly)
    platform: str = "shopify"                             # shopify | justcorseca (custom JSON backend)
    search_terms: list[str] = field(default_factory=list)  # YouTube + autocomplete phrases (default: the name)
    aliases: list[str] = field(default_factory=list)     # product lines that can only mean this brand
    exclude: list[str] = field(default_factory=list)     # look-alikes that are never this brand
    ambiguous: bool = False                               # the name is an everyday word (Hero, Jupiter, Slice): needs context
    country: str = ""                                    # HQ country (Nazar's India / world lens)
    kind: str = ""                                       # company type shown in the Nazar directory (OEM, bank, OTA, ...)
    website: str = ""                                    # public site when there is no storefront to poll

    @property
    def host(self) -> str | None:
        if not self.shopify:
            return None
        return self.shopify.split("//", 1)[-1].strip("/")

    def product_url(self, handle: str | None) -> str:
        if not self.shopify or not handle:
            return ""
        return f"{self.shopify}{PRODUCT_PATH.get(self.platform, '/products/')}{handle}"


@dataclass
class Settings:
    sector: str
    currency: str
    brands: list[Brand]
    festivals: list[dict]
    sector_news_terms: list[str]
    reddit: dict
    price_bands: list[int]
    db_path: Path
    anthropic_key: str | None
    model: str
    telegram_token: str | None
    telegram_chat: str | None
    webhook_url: str | None
    meta_token: str | None
    alert_threshold: int
    youtube_queries: list[str] = field(default_factory=list)
    search_seeds: list[str] = field(default_factory=list)
    screenshots: bool = False
    smtp: dict = field(default_factory=dict)
    segments: list[dict] = field(default_factory=list)   # [{key, name, keywords}] from competitors.yaml
    gemini_key: str | None = None
    gemini_model: str = "gemini-3.8-flash"
    ai_choice: str = "auto"                               # RADAR_AI_PROVIDER: auto | claude | gemini | off
    cloud_repo: str = ""                                  # owner/name: where the cloud snapshot lives
    companies: dict = field(default_factory=dict)         # brand -> corporate facts, tickers, seeded financials (companies.yaml)
    money_terms: list[str] = field(default_factory=list)  # financial news phrases run per brand
    market: dict = field(default_factory=dict)            # India market model + impact assumptions (market.yaml)
    sec_contact: str = ""                                 # SEC EDGAR asks for a contact in the user agent
    # ---- Nazar: one radar per sector. Everything below is optional and defaults to the Pebble behaviour. ----
    sector_key: str = ""                                  # short id of this sector (iot, auto, fintech, ...)
    sector_label: str = ""                                # display name ("Automotive & EV")
    perspective: str = ""                                 # whose seat the analysis is written from; empty = the self brand, else neutral
    vocab: dict = field(default_factory=dict)             # sector | category | hot | core | noise regexes (relevance.py, enrich.py)
    voice_themes: list = field(default_factory=list)      # [{key, rx}] review themes for this sector (collectors/voice.py)
    world: dict = field(default_factory=dict)             # free-form sector facts for the Nazar directory (regulators, trackers, ...)

    config_self: str = ""                                 # the brand competitors.yaml flags `self:` (set in __post_init__)

    def __post_init__(self) -> None:
        self.config_self = next((b.name for b in self.brands if b.self_brand), "")

    @property
    def me(self) -> str:
        """Who the radar writes for: the self brand, else the configured perspective, else 'you'."""
        if self.self_brand:
            return self.self_brand.name
        return self.perspective or "you"

    @property
    def self_market(self) -> dict:
        """The self brand's block of market.yaml (`self:`; the original key `pebble:` still works)."""
        if not self.self_brand or self.self_brand.name != self.config_self:
            return {}   # own revenue, mix and sales describe the YAML home brand only; another seat has no own data
        m = self.market or {}
        return m.get("self") or m.get("pebble") or {}

    @property
    def own_data(self) -> bool:
        """True when the current seat has first-party figures (market.yaml self block): at-risk money, own sales."""
        return bool(self.self_market)

    def localize(self, text: str | None) -> str | None:
        """The radar's templates were written for Pebble. In another sector the same sentence is addressed to the
        self brand by name, or, with no self brand, to 'you' (the reader competing in that sector)."""
        if not text or "Pebble" not in text or (self.self_brand and self.self_brand.name == "Pebble"):
            return text
        text = re.sub(r"\bPebble (Competitor )?Radar\b", "Nazar", text)
        if self.self_brand or self.perspective:
            return re.sub(r"\bPebble\b", self.me, text)
        for old, new in ((r"\bPebble's\b", "your"), (r"\bPebble is\b", "you are"), (r"\bPebble has\b", "you have"),
                         (r"\bPebble does\b", "you do"), (r"\bPebble needs\b", "you need"), (r"\bPebble ranks\b", "you rank"),
                         (r"\bPebble reads\b", "you read"), (r"\bPebble itself\b", "you yourself"), (r"\bPebble\b", "you")):
            text = re.sub(old, new, text)
        return text

    def company(self, name: str) -> dict:
        return self.companies.get(name) or self.companies.get(name.lower()) or {}

    def brand(self, name: str) -> Brand | None:
        for b in self.brands:
            if b.name.lower() == name.lower():
                return b
        return None

    @property
    def self_brand(self) -> Brand | None:
        return next((b for b in self.brands if b.self_brand), None)

    def set_perspective(self, name: str | None) -> str | None:
        """Choose at runtime whose seat the radar writes from: a brand of this sector, or None for the neutral view.
        Every reader goes through `self_brand` / `me`, so flipping the flags is enough; callers persist the choice."""
        target = self.brand(name) if name else None
        if name and target is None:
            raise ValueError(f"{name!r} is not a brand in this sector")
        for b in self.brands:
            b.self_brand = b is target
        return target.name if target else None

    @property
    def ai_provider(self) -> str | None:
        """'claude', 'gemini' or None. auto prefers Claude when both keys are set."""
        have = {"claude": bool(self.anthropic_key or os.environ.get("ANTHROPIC_AUTH_TOKEN")), "gemini": bool(self.gemini_key)}
        if self.ai_choice in have:
            return self.ai_choice if have[self.ai_choice] else None
        if self.ai_choice == "off":
            return None
        return next((p for p in ("claude", "gemini") if have[p]), None)

    @property
    def ai_enabled(self) -> bool:
        return self.ai_provider is not None

    @property
    def ai_model(self) -> str | None:
        return {"claude": self.model, "gemini": self.gemini_model}.get(self.ai_provider)


def load() -> Settings:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    brands = [
        Brand(
            name=b["name"],
            shopify=(b.get("shopify") or b.get("store") or "").rstrip("/") or None,
            self_brand=bool(b.get("self")),
            news_terms=b.get("news_terms", []) or [],
            watch_pages=b.get("watch_pages", []) or [],
            apps=b.get("apps", []) or [],
            segments=b.get("segments", []) or [],
            origin=b.get("origin", "Indian"),
            tier=b.get("tier", "core"),
            platform=b.get("platform", "shopify"),
            search_terms=b.get("search_terms", []) or [],
            aliases=b.get("aliases", []) or [],
            exclude=b.get("exclude", []) or [],
            ambiguous=bool(b.get("ambiguous")),
            country=str(b.get("country") or ""),
            kind=str(b.get("kind") or ""),
            website=str(b.get("website") or b.get("shopify") or b.get("store") or "").rstrip("/"),
        )
        for b in raw["brands"]
    ]
    # Cloud mode overwrites its database with each downloaded snapshot, so it only ever uses the temp folder
    # (the one writable place on Vercel) or an absolute RADAR_DB set on purpose. A relative RADAR_DB, such as
    # the data/radar.db in a local .env, would point it at the local working database.
    db = os.environ.get("RADAR_DB") or "data/radar.db"
    if os.environ.get("VERCEL") or os.environ.get("RADAR_HOSTED") == "1":
        p = Path(db)
        rel = p.relative_to(ROOT) if p.is_absolute() and p.is_relative_to(ROOT) else (None if p.is_absolute() else p)
        if rel is not None:   # keep the sector's own sub-path, so ten sectors in one hub do not share one file
            db = str(Path(tempfile.gettempdir()) / "radar" / rel)
    db_path = Path(db) if Path(db).is_absolute() else ROOT / db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    companies_raw = _side_yaml("companies.yaml")
    companies = {c["brand"]: c for c in (companies_raw.get("companies") or []) if c.get("brand")}
    market = _side_yaml("market.yaml")
    return Settings(
        sector=raw.get("sector", ""),
        currency=raw.get("currency", "INR"),
        brands=brands,
        festivals=raw.get("festivals", []),
        sector_news_terms=raw.get("sector_news_terms", []),
        reddit=raw.get("reddit", {}),
        price_bands=raw.get("price_bands", [0, 1000, 2000, 5000, 10000, 10**6]),
        db_path=db_path,
        anthropic_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        model=os.environ.get("RADAR_MODEL", "claude-opus-5-5"),
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat=os.environ.get("TELEGRAM_CHAT_ID") or None,
        webhook_url=os.environ.get("WEBHOOK_URL") or None,
        meta_token=os.environ.get("META_ACCESS_TOKEN") or None,
        alert_threshold=int(os.environ.get("ALERT_THRESHOLD", "6")),
        youtube_queries=raw.get("youtube_queries", ["new launch"]),
        search_seeds=raw.get("search_seeds", ["{brand} "]),
        screenshots=bool(raw.get("screenshots", False)),
        smtp={k: os.environ.get(f"SMTP_{k.upper()}") for k in ("host", "port", "user", "password", "to", "from")},
        segments=raw.get("segments", []) or [],
        gemini_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or None,
        gemini_model=os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash",
        ai_choice=(os.environ.get("RADAR_AI_PROVIDER") or "auto").strip().lower(),
        cloud_repo=str(raw.get("cloud_repo") or ""),
        companies=companies,
        money_terms=companies_raw.get("money_terms") or [],
        market=market,
        sec_contact=(os.environ.get("SEC_CONTACT") or "").strip(),
        sector_key=os.environ.get("RADAR_SECTOR") or str(raw.get("key") or ""),
        sector_label=os.environ.get("RADAR_SECTOR_LABEL") or str(raw.get("label") or ""),
        perspective=str(raw.get("perspective") or ""),
        vocab=raw.get("vocab") or {},
        voice_themes=[(t["key"], t["rx"]) for t in (raw.get("voice_themes") or []) if t.get("key") and t.get("rx")],
        world=raw.get("world") or {},
    )


def _side_yaml(name: str) -> dict:
    """A sibling of competitors.yaml (companies.yaml, market.yaml); missing or empty is fine."""
    p = CONFIG_PATH.parent / name
    if not p.exists():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


settings = load()
