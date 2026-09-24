"""Text and time helpers shared by the family parsers."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], start=1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9

ASSETS = {
    "bitcoin": "BTC", "btc": "BTC",
    "ethereum": "ETH", "ether": "ETH", "eth": "ETH",
    "solana": "SOL", "sol": "SOL",
    "xrp": "XRP", "ripple": "XRP",
}

SOURCE_TOKENS = {
    "binance": "binance", "coinbase": "coinbase", "chainlink": "chainlink", "pyth": "pyth",
    "kraken": "kraken", "cf benchmarks": "cfbenchmarks", "cfbenchmarks": "cfbenchmarks",
    "brti": "cfbenchmarks", "bureau of labor statistics": "bls", "bls": "bls",
    "federal reserve": "fed", "fomc": "fed", "federal open market committee": "fed",
    "cme": "cme", "polygon": "polygon", "coingecko": "coingecko", "coinmarketcap": "coinmarketcap",
}


def norm_text(s: str | None) -> str:
    s = (s or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^\w\s\.\$%:+-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_number(s: str) -> float | None:
    """'$115,000' -> 115000; '115k' -> 115000; '0.3%' -> 0.3; '1.2m' -> 1200000."""
    if s is None:
        return None
    t = s.strip().lower().replace(",", "").replace("$", "").replace("%", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([km])?", t)
    if not m:
        return None
    v = float(m.group(1))
    return v * {"k": 1e3, "m": 1e6}.get(m.group(2) or "", 1.0)


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    t = s.strip().replace("Z", "+00:00")
    if " " in t and "T" not in t:
        t = t.replace(" ", "T", 1)
    if re.search(r"[+-]\d{2}$", t):
        t = t + ":00"
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d


def to_ts(d: datetime | None) -> float | None:
    return d.timestamp() if d else None


def et_date(d: datetime) -> date:
    return d.astimezone(ET).date()


def et_datetime(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)


def find_month(text: str) -> int | None:
    for tok in re.findall(r"[a-z]+", text.lower()):
        if tok in MONTHS:
            return MONTHS[tok]
    return None


def find_month_day(text: str) -> tuple[int, int, int | None] | None:
    """'on September 18, 2026' / 'Sep 18' -> (9, 18, 2026|None)."""
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?"
                  r"(?:,?\s*(\d{4}))?", text.lower())
    if not m:
        return None
    return MONTHS[m.group(1)], int(m.group(2)), int(m.group(3)) if m.group(3) else None


def find_clock_et(text: str) -> tuple[int, int] | None:
    """'12:00 PM ET', '5pm EDT', 'noon' -> (hour, minute) in Eastern time."""
    t = text.lower()
    if re.search(r"\bnoon\b", t):
        return 12, 0
    if re.search(r"\bmidnight\b", t):
        return 0, 0
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b\s*(?:\(?\s*(et|est|edt|eastern))?", t)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return h, mi


def source_tokens(text: str | None) -> set[str]:
    t = (text or "").lower()
    return {v for k, v in SOURCE_TOKENS.items() if k in t}


def ticker_date(ticker: str | None) -> date | None:
    """Kalshi tickers embed dates as YYMMMDD, e.g. KXNFLGAME-26SEP18DALNYG -> 2026-09-18."""
    if not ticker:
        return None
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", ticker.upper())
    if not m:
        return None
    mon = MONTHS.get(m.group(2).lower())
    if not mon:
        return None
    try:
        return date(2000 + int(m.group(1)), mon, int(m.group(3)))
    except ValueError:
        return None


def ticker_month(ticker: str | None) -> tuple[int, int] | None:
    """KXFEDDECISION-26SEP -> (2026, 9)."""
    if not ticker:
        return None
    m = re.search(r"-(\d{2})([A-Z]{3})(?!\d)", ticker.upper())
    if not m:
        return None
    mon = MONTHS.get(m.group(2).lower())
    return (2000 + int(m.group(1)), mon) if mon else None


class TeamBook:
    """League -> canonical code -> aliases. Matching is longest-alias-first on word boundaries."""

    def __init__(self, table: dict[str, dict[str, list[str]]]):
        # YAML 1.1 turns unquoted NO/YES/ON/OFF keys into booleans; refuse anything that is not a code.
        for lg, teams in table.items():
            for code in teams:
                if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9]{2,4}", code):
                    raise ValueError(f"teams.yaml: league {lg!r} has a non-code key {code!r}; quote it, e.g. \"NO\"")
        self.table = {lg.upper(): {code: [norm_text(str(a)) for a in aliases if a]
                                   for code, aliases in teams.items()}
                      for lg, teams in table.items()}
        self._patterns: dict[str, list[tuple[re.Pattern, str]]] = {}
        for lg, teams in self.table.items():
            pats = [(re.compile(rf"(?<![\w]){re.escape(a)}(?![\w])"), code)
                    for code, aliases in teams.items() for a in aliases if a]
            pats.sort(key=lambda pc: -len(pc[0].pattern))
            self._patterns[lg] = pats

    @classmethod
    def load(cls, path: str | Path) -> "TeamBook":
        with open(path, "r", encoding="utf-8") as fh:
            return cls(yaml.safe_load(fh) or {})

    def leagues(self) -> list[str]:
        return list(self.table)

    def find(self, text: str, league: str) -> list[str]:
        """Canonical codes found in text, ordered by first occurrence, each once."""
        t = norm_text(text)
        hits: dict[str, int] = {}
        consumed = [False] * (len(t) + 1)
        for pat, code in self._patterns.get(league.upper(), []):
            for m in pat.finditer(t):
                if any(consumed[m.start():m.end()]):
                    continue
                for k in range(m.start(), m.end()):
                    consumed[k] = True
                hits.setdefault(code, m.start())
        return [c for c, _ in sorted(hits.items(), key=lambda kv: kv[1])]

    def resolve(self, text: str, league: str, candidates: list[str]) -> str | None:
        """Pick which of `candidates` a side label refers to. None when the label names both
        (a game title) or neither. A bare shared city ('New York') resolves when only one
        candidate's aliases contain it."""
        found = [c for c in self.find(text, league) if c in candidates]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            return None
        t = norm_text(text)
        if not t:
            return None
        pat = re.compile(rf"(?<![\w]){re.escape(t)}(?![\w])")
        containing = [c for c in candidates
                      if any(pat.search(a) for a in self.table.get(league.upper(), {}).get(c, []))]
        return containing[0] if len(containing) == 1 else None


def day_delta(a: date, b: date) -> int:
    return abs((a - b).days)


def utc_day(ts: float) -> date:
    return datetime.fromtimestamp(ts, tz=UTC).date()


def plus_days(d: date, n: int) -> date:
    return d + timedelta(days=n)
