"""Core data types. Everything is expressed in canonical YES terms: a Leg's YES pays 1 when the
canonical event happens, regardless of how the venue labels its own sides."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Venue = Literal["kalshi", "polymarket"]
Klass = Literal["exact", "basis"]
Side = Literal["yes", "no"]

KALSHI: Venue = "kalshi"
POLYMARKET: Venue = "polymarket"


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass
class Book:
    """Two-sided book in canonical YES terms. Lists sorted best-first."""

    venue: str
    leg_id: str
    ts_recv: float
    ts_src: float | None
    yes_bids: list[Level] = field(default_factory=list)
    yes_asks: list[Level] = field(default_factory=list)
    no_bids: list[Level] = field(default_factory=list)
    no_asks: list[Level] = field(default_factory=list)

    def asks(self, side: Side) -> list[Level]:
        return self.yes_asks if side == "yes" else self.no_asks

    def bids(self, side: Side) -> list[Level]:
        return self.yes_bids if side == "yes" else self.no_bids

    def mid(self) -> float | None:
        if self.yes_bids and self.yes_asks:
            return (self.yes_bids[0].price + self.yes_asks[0].price) / 2
        return None

    def compact(self, depth: int) -> dict[str, Any]:
        f = lambda lv: [[round(l.price, 4), round(l.size, 2)] for l in lv[:depth]]  # noqa: E731
        return {
            "ts_recv": round(self.ts_recv, 3),
            "ts_src": self.ts_src,
            "yb": f(self.yes_bids),
            "ya": f(self.yes_asks),
            "nb": f(self.no_bids),
            "na": f(self.no_asks),
        }

    def fingerprint(self, depth: int) -> tuple:
        c = self.compact(depth)
        return (tuple(map(tuple, c["yb"])), tuple(map(tuple, c["ya"])),
                tuple(map(tuple, c["nb"])), tuple(map(tuple, c["na"])))


@dataclass
class Leg:
    venue: str
    market_id: str            # Kalshi ticker or Polymarket conditionId
    title: str
    yes_label: str            # what the venue calls the side that is canonical YES
    fee_category: str         # Polymarket fee category, or Kalshi series ticker
    fee_multiplier: float     # Kalshi series multiplier; 1.0 for Polymarket
    close_ts: float           # venue close / end time, unix seconds
    resolution_source: str
    resolution_ts: float | None
    yes_is_venue_no: bool = False   # Kalshi: canonical YES is the venue's NO side
    yes_token: str | None = None    # Polymarket token ids
    no_token: str | None = None
    volume: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def leg_id(self) -> str:
        if self.venue == KALSHI:
            return f"{self.venue}:{self.market_id}:{'n' if self.yes_is_venue_no else 'y'}"
        return f"{self.venue}:{self.market_id}:{self.yes_token}"


@dataclass
class Pair:
    pair_id: str
    family: str
    key: str
    klass: str
    legs: dict[str, Leg]       # venue -> Leg
    notes: list[str] = field(default_factory=list)
    created_ts: float = 0.0

    @property
    def close_ts(self) -> float:
        return max(l.close_ts for l in self.legs.values())

    @property
    def event_key(self) -> str:
        """Pairs that hedge the same underlying event share this key. A two-team game gives Kalshi
        two markets (each team wins) and Polymarket one market with two outcome tokens, so the pair
        built on team A and the pair built on team B buy the same Polymarket token and equivalent
        Kalshi exposure: one trade, counted twice. The Polymarket market id is the shared leg."""
        p = self.legs.get(POLYMARKET)
        return f"pm:{p.market_id}" if p is not None else f"pair:{self.pair_id}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Pair":
        legs = {v: Leg(**l) for v, l in d["legs"].items()}
        return Pair(pair_id=d["pair_id"], family=d["family"], key=d["key"], klass=d["klass"],
                    legs=legs, notes=list(d.get("notes", [])), created_ts=d.get("created_ts", 0.0))


@dataclass
class Combo:
    """A hedged combination: buy `side_a` on venue A and the opposite side on venue B."""

    venue_a: str
    side_a: Side
    venue_b: str
    side_b: Side

    @property
    def name(self) -> str:
        return f"{self.venue_a}:{self.side_a}+{self.venue_b}:{self.side_b}"


@dataclass
class Opportunity:
    ts: float
    pair_id: str
    family: str
    klass: str
    combo: str
    qty: float
    cost_a: float            # notional on leg A at the walked prices (per-contract VWAP * qty)
    cost_b: float
    fees_a: float
    fees_b: float
    edge_per_contract: float # net of fees, marginal at the last level taken
    edge_total: float        # sum over filled levels, net of fees
    best_a: float
    best_b: float
    limit_a: float           # worst level price used on A
    limit_b: float
    days_locked: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Intent:
    intent_id: str
    created_ts: float
    created_poll: int
    pair_id: str
    family: str
    klass: str
    combo: str
    venue_a: str
    side_a: str
    venue_b: str
    side_b: str
    qty: float
    limit_a: float            # worst level the detection walked on A (a price reference, not a fill limit)
    limit_b: float
    edge_seen: float          # marginal edge of the last contract the detection walked
    data_cutoff_ts: float     # newest book timestamp that informed the decision
    status: str = "pending"   # pending | filled | partial | missed | unwinding | closed
    model: str = "joint"      # joint | sequenced
    # sequenced only: what the first leg actually did, so the second leg's budget is exact
    stage: int = 1
    first_venue: str = ""
    qty_first: int = 0
    cost_first: float = 0.0
    fees_first: float = 0.0


@dataclass
class Fill:
    ts: float
    intent_id: str
    pair_id: str
    venue: str
    side: str
    action: str               # buy | sell
    qty: float
    vwap: float
    fee: float
    data_cutoff_ts: float
    reason: str


@dataclass
class Position:
    pair_id: str
    venue: str
    side: str
    qty: float = 0.0
    cost: float = 0.0          # notional paid, excluding fees
    fees: float = 0.0
    opened_ts: float = 0.0
    resolved: bool = False
    voided: bool = False
    payout: float = 0.0

    @property
    def avg_price(self) -> float:
        return self.cost / self.qty if self.qty else 0.0
