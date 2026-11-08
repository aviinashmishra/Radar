"""Is this headline, video title, search suggestion or Reddit post really about the brand?

Search engines drift. "Urban" returns cycles and lip kits, "Polar" a cooktop, "Pebble" the Core Devices watch,
"Samsung" its phones, "Whoop" the dictionary. Every brand-scoped collector (news, YouTube, autocomplete, Reddit)
runs its results through `relevant()` before they land on a brand page, and `attribute()` decides which brand a
sector-wide result belongs to. Per-brand `aliases` (product lines) and `exclude` (look-alikes) in competitors.yaml
tune it without code.
"""
from __future__ import annotations
import re
from functools import lru_cache
from .config import Brand, settings

# Bump when the rules change: maintenance.py then re-checks stored events against the new rules.
VERSION = 3

_V = settings.vocab or {}
SECTOR_RX = re.compile(_V["sector"], re.I) if _V.get("sector") else re.compile(r"smart ?watch|wearable|earbud|ear ?phone|headphone|neckband|\btws\b|audio|speaker|soundbar|charger|"
                       r"power ?bank|gadget|electronics|smart ?ring|fitness band|bluetooth|\banc\b|launch|unveil|d2c|"
                       r"e-?commerce|shopify|amazon|flipkart|idc|counterpoint|funding|acqui|ipo|nexxbase|imagine marketing|"
                       r"srk powertech|boat|noise colorfit|fire-?boltt|boult|ptron|zebronics|portronics|ambrane|gizmore|"
                       r"crossbeats|corseca|blaupunkt|fastrack|mivi|cultsport|beatxp|pebble", re.I)
CATEGORY_RX = re.compile(_V["category"], re.I) if _V.get("category") else re.compile(r"smart ?watch|wearable|earbud|ear ?phone|headphone|neckband|\btws\b|speaker|soundbar|charger|power ?bank|"
                         r"smart ?ring|\brings?\b|fitness|\bbands?\b|tracker|bluetooth|gadget|electronics|\baudio\b|health|sleep|"
                         r"heart rate|\bhrv\b|spo2|\bd2c\b|\bwatch(es)?\b|\bbuds\b|\bpods\b|\banc\b", re.I)
BUSINESS_RX = re.compile(r"funding|raises|\bipo\b|\bacqui|\bvaluation|revenue|market share|\bidc\b|counterpoint|investor|layoff", re.I)
PRODUCT_TALK_RX = re.compile(r"price|specs|features|battery|review|unbox|\bai\b|₹|\brs\.? ?\d", re.I)
AMBIGUOUS = {"noise", "hammer", "pebble", "urban", "luna", "polar", "boat", "titan", "fire", "ring", "whoop"}  # everyday words
AMBIGUOUS |= {w.lower() for w in (_V.get("ambiguous") or [])} | {b.name.lower() for b in settings.brands if b.ambiguous}
GENERIC_HEAD = {"just", "imagine", "google", "the", "galaxy"}                                              # not the brand
COLLOCATIONS = re.compile(r"noise[- ]?(cancel\w*|reduc\w*|isolat\w*)|white noise|active noise", re.I)
CONNECTOR = r"(?:vs\.?|versus|or|and|with|&|x|\||-|:)"


@lru_cache(maxsize=None)
def _words_rx(words: tuple[str, ...]) -> re.Pattern | None:
    """Alternation of yaml phrases. Phrases are regex-capable ("Whoop \\d"), matched as whole words."""
    if not words:
        return None
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(f"(?:{w})" for w in words) + r")(?![a-z])", re.I)


@lru_cache(maxsize=1)
def _segments_rx() -> re.Pattern | None:
    """Product-line words from the segment keywords ("airdopes", "qore", "cirqa"). Short bare words ("arc", "loop",
    "flex", "rep", "stone", "party") name product lines inside a store but are everyday English in a headline."""
    alts = [a for sg in settings.segments for a in (sg.get("keywords") or "").split("|")
            if a and not re.fullmatch(r"\\b\w{1,5}\\b", a) and a != "party"]
    return re.compile("|".join(f"(?:{a})" for a in alts), re.I) if alts else None


def _name_rx(name: str) -> str:
    """"Fire-Boltt" also matches "Fireboltt" and "Fire Boltt"."""
    return r"[- ]?".join(re.escape(part) for part in re.split(r"[- ]", name))


def _product_of_other_brand(text: str, brand: Brand, word: str) -> bool:
    """"pTron Studio Urban" is a pTron headphone: an everyday-word brand right after another brand's name (no "vs"
    between) is that brand's product name."""
    for other in settings.brands:
        if other is brand or other.name.lower() in AMBIGUOUS:
            continue
        if re.search(rf"(?i)(?<![a-z]){_name_rx(other.name)}(?![a-z])\s+(?:(?!{CONNECTOR}\s)\S+\s+){{0,2}}{re.escape(word)}(?![a-z])", text):
            return True
    return False


def _context(text: str) -> bool:
    seg = _segments_rx()
    return bool(CATEGORY_RX.search(text) or BUSINESS_RX.search(text) or (seg and seg.search(text)))


def relevant(text: str, brand: Brand | None, term: str = "", *, summary: str = "", strict: bool = False,
             cased: bool = True) -> bool:
    """True when `text` is about `brand` (or, with brand None, about the sector at all).

    The brand must be named; an everyday-word name (Noise, Urban, Polar) and global brands (tier: adjacent, whose
    phones and cooktops we do not track) also need product or business context. `strict` drops "review/price" as
    context (every YouTube title is a review); `cased=False` is for lowercase text such as autocomplete suggestions,
    where "Urban" vs "urban" cannot tell the brand from the word.
    """
    if brand is None:
        return bool(SECTOR_RX.search(f"{text} {summary}"))
    exclude, aliases = _words_rx(tuple(brand.exclude)), _words_rx(tuple(brand.aliases))
    if exclude and exclude.search(text):
        return False
    if aliases and aliases.search(text):
        return True
    text = COLLOCATIONS.sub(" ", text)
    names = {w for w in (brand.name, term.split()[0] if term.strip() else "") if w and w.lower() not in GENERIC_HEAD}
    if cased:
        # an internal capital (boAt, BoAt, pTron, beatXP) is unmistakably the brand
        styled = any(t[1:] != t[1:].lower() and t != t.upper() for w in names
                     for t in re.findall(rf"(?<![A-Za-z]){_name_rx(w)}(?![a-z])", text, re.I))
        if styled and brand.tier != "adjacent":
            return True
    hit = ambiguous = False
    rest = text  # the text minus the brand name, so "Whoop" is not its own product context
    for w in names:
        if w.lower() in AMBIGUOUS:
            rx = (rf"(?<![A-Za-z])({re.escape(w.capitalize())}|{re.escape(w.upper())})(?![a-z])" if cased
                  else rf"(?i)(?<![a-z]){re.escape(w.lower())}(?![a-z])")
            if re.search(rx, text) and not _product_of_other_brand(text, brand, w):
                hit = ambiguous = True
                rest = re.sub(rx, " ", rest)
        elif re.search(rf"(?<![a-z]){_name_rx(w.lower())}(?![a-z])", text.lower()):
            hit = True
            rest = re.sub(rf"(?i)(?<![a-z]){_name_rx(w)}(?![a-z])", " ", rest)
    if not hit:
        return False
    if brand.tier == "adjacent" or ambiguous:
        if _context(rest):
            return True
        return ambiguous and brand.tier != "adjacent" and not strict and bool(PRODUCT_TALK_RX.search(rest))
    return True


def signature() -> str:
    """Rules version + the yaml that tunes them: maintenance re-checks stored events when either changes."""
    import hashlib
    cfg = [(b.name, b.tier, b.aliases, b.exclude, b.search_terms, b.ambiguous) for b in settings.brands] + [sorted(_V.items())]
    return f"{VERSION}:{hashlib.sha1(repr(cfg).encode()).hexdigest()[:10]}"


def attribute(text: str, *, cased: bool = True, strict: bool = False) -> str:
    """The tracked brand a sector-wide item is about, or "Sector"."""
    for b in settings.brands:
        if relevant(text, b, cased=cased, strict=strict):
            return b.name
    return "Sector"
