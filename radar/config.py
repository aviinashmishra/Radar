"""Load competitors.yaml + .env into one settings object."""
from __future__ import annotations
import os
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

CONFIG_PATH = Path(os.environ.get("RADAR_CONFIG", ROOT / "config" / "competitors.yaml"))


@dataclass
class Brand:
    name: str
    shopify: str | None = None
    self_brand: bool = False
    news_terms: list[str] = field(default_factory=list)
    watch_pages: list[str] = field(default_factory=list)
    apps: list[str] = field(default_factory=list)
    segments: list[str] = field(default_factory=list)   # e.g. ["band", "ring"]
    origin: str = "Indian"                                # Indian | Global
    tier: str = "core"                                    # core | adjacent (global premium, watched more lightly)

    @property
    def host(self) -> str | None:
        if not self.shopify:
            return None
        return self.shopify.split("//", 1)[-1].strip("/")


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

    def brand(self, name: str) -> Brand | None:
        for b in self.brands:
            if b.name.lower() == name.lower():
                return b
        return None

    @property
    def self_brand(self) -> Brand | None:
        return next((b for b in self.brands if b.self_brand), None)

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
            shopify=(b.get("shopify") or "").rstrip("/") or None,
            self_brand=bool(b.get("self")),
            news_terms=b.get("news_terms", []) or [],
            watch_pages=b.get("watch_pages", []) or [],
            apps=b.get("apps", []) or [],
            segments=b.get("segments", []) or [],
            origin=b.get("origin", "Indian"),
            tier=b.get("tier", "core"),
        )
        for b in raw["brands"]
    ]
    # Cloud mode overwrites its database with each downloaded snapshot, so it only ever uses the temp folder
    # (the one writable place on Vercel) or an absolute RADAR_DB set on purpose. A relative RADAR_DB, such as
    # the data/radar.db in a local .env, would point it at the local working database.
    db = os.environ.get("RADAR_DB") or "data/radar.db"
    if (os.environ.get("VERCEL") or os.environ.get("RADAR_HOSTED") == "1") and not Path(db).is_absolute():
        db = str(Path(tempfile.gettempdir()) / "radar" / "radar.db")
    db_path = Path(db) if Path(db).is_absolute() else ROOT / db
    db_path.parent.mkdir(parents=True, exist_ok=True)
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
    )


settings = load()
