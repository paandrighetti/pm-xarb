"""Join canonical legs across venues and classify each pair as exact or basis.

exact : same event, same resolution source and instant; a hedged position pays exactly 1.
basis : same event in plain language but a different reference (source, timestamp, date);
        the two legs can resolve differently, so the "arbitrage" carries settlement risk.
Everything the classifier saw is written into Pair.notes so a human can overrule it in
config/pair_overrides.yaml.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..config import Config
from ..models import KALSHI, POLYMARKET, Pair
from .families import CanonLeg

# Loose-join tolerance on the reference instant. Kalshi lists hourly above/below crypto series, so
# "same threshold, same ET day" alone would pair a noon Polymarket market with a 23:00 Kalshi one.
LOOSE_TOLERANCE_S = {"sports": 36 * 3600, "crypto": 6 * 3600, "macro": 0}


def _pair_id(family: str, key: str, k_id: str, p_id: str) -> str:
    h = hashlib.sha1(f"{key}|{k_id}|{p_id}".encode()).hexdigest()[:10]
    return f"{family}-{h}"


def classify(k: CanonLeg, p: CanonLeg, exact_key: bool) -> tuple[str, list[str]]:
    notes: list[str] = []
    fam = k.family
    ks, ps = k.sources, p.sources
    if fam == "sports":
        if not exact_key:
            notes.append(f"game day differs: kalshi {k.meta.get('day')} vs polymarket {p.meta.get('day')} (timezone or listing)")
        notes.append("official result on both venues; tie and postponement rules not verified")
        return "exact", notes
    if fam == "macro":
        if exact_key:
            return "exact", notes
        return "basis", notes + ["loose macro join"]
    # crypto
    if not exact_key:
        notes.append(f"reference instants differ: kalshi {k.meta.get('instant')} vs polymarket {p.meta.get('instant')}")
    if ks and ps:
        if ks == ps:
            src_ok = True
        else:
            src_ok = False
            notes.append(f"reference sources differ: kalshi {sorted(ks)} vs polymarket {sorted(ps)}")
    else:
        src_ok = False
        notes.append(f"reference source missing on one side: kalshi {sorted(ks)} vs polymarket {sorted(ps)}")
    return ("exact" if exact_key and src_ok else "basis"), notes


class Overrides:
    def __init__(self, raw: dict | None):
        raw = raw or {}
        self.exclude = raw.get("exclude") or []
        self.reclass = raw.get("reclass") or []

    @classmethod
    def load(cls, path: str | Path) -> "Overrides":
        p = Path(path)
        if not p.exists():
            return cls({})
        with open(p, "r", encoding="utf-8") as fh:
            return cls(yaml.safe_load(fh) or {})

    @staticmethod
    def _hit(rule: dict, pair: Pair) -> bool:
        if rule.get("pair_id") and rule["pair_id"] == pair.pair_id:
            return True
        if rule.get("key_regex") and re.search(rule["key_regex"], pair.key):
            return True
        return False

    def apply(self, pairs: list[Pair]) -> tuple[list[Pair], int]:
        kept: list[Pair] = []
        excluded = 0
        for pair in pairs:
            if any(self._hit(r, pair) for r in self.exclude):
                excluded += 1
                continue
            for r in self.reclass:
                if self._hit(r, pair) and r.get("klass") in ("exact", "basis"):
                    pair.klass = r["klass"]
                    pair.notes.append(f"manual reclass: {r.get('note', '')}".strip())
            kept.append(pair)
        return kept, excluded


def build_pairs(k_legs: list[CanonLeg], p_legs: list[CanonLeg], cfg: Config,
                overrides: Overrides) -> tuple[list[Pair], dict[str, Any]]:
    now = time.time()
    horizon = now + cfg.universe.max_days_to_close * 86400
    by_key: dict[str, list[CanonLeg]] = defaultdict(list)
    by_block: dict[str, list[CanonLeg]] = defaultdict(list)
    for p in p_legs:
        by_key[p.key].append(p)
        by_block[p.block].append(p)
    used: set[str] = set()
    pairs: list[Pair] = []
    stats: dict[str, Any] = defaultdict(lambda: defaultdict(int))
    for leg in k_legs:
        stats[leg.family]["kalshi_legs"] += 1
    for leg in p_legs:
        stats[leg.family]["polymarket_legs"] += 1

    def make(k: CanonLeg, p: CanonLeg, exact_key: bool) -> None:
        klass, notes = classify(k, p, exact_key)
        if k.leg.close_ts > horizon or p.leg.close_ts > horizon:
            stats[k.family]["beyond_horizon"] += 1
            return
        if abs(k.leg.close_ts - p.leg.close_ts) > 3 * 86400:
            notes.append(f"close times differ by {abs(k.leg.close_ts - p.leg.close_ts) / 86400:.1f} days")
        pair = Pair(pair_id=_pair_id(k.family, k.key, k.leg.market_id, p.leg.market_id), family=k.family,
                    key=k.key, klass=klass, legs={KALSHI: k.leg, POLYMARKET: p.leg}, notes=notes, created_ts=now)
        pairs.append(pair)
        used.add(p.leg.leg_id)
        stats[k.family][klass] += 1

    # pass 1: exact keys
    pending: list[CanonLeg] = []
    for k in k_legs:
        cands = [p for p in by_key.get(k.key, []) if p.leg.leg_id not in used]
        if cands:
            make(k, max(cands, key=lambda p: p.leg.volume), True)
        else:
            pending.append(k)
    # pass 2: loose blocks within tolerance
    for k in pending:
        tol = LOOSE_TOLERANCE_S.get(k.family, 0)
        cands = [p for p in by_block.get(k.block, []) if p.leg.leg_id not in used]
        if tol > 0 and k.when is not None:
            cands = [p for p in cands if p.when is not None and abs((p.when - k.when).total_seconds()) <= tol]
            cands.sort(key=lambda p: (abs((p.when - k.when).total_seconds()), -p.leg.volume))
        else:
            cands = []          # families with no loose join (macro)
        if cands:
            make(k, cands[0], False)
        else:
            stats[k.family]["kalshi_unmatched"] += 1
    kept, excluded = overrides.apply(pairs)
    stats["_total"]["pairs"] = len(kept)
    stats["_total"]["excluded_by_override"] = excluded
    return kept, {f: dict(v) for f, v in stats.items()}


# ------------------------------------------------------------------------------------------------
# persistence
# ------------------------------------------------------------------------------------------------

def save_pairs(cfg: Config, pairs: list[Pair], stats: dict, unmatched: dict[str, list[dict]]) -> Path:
    payload = {"generated_ts": time.time(), "generated": datetime.now(timezone.utc).isoformat(),
               "stats": stats, "pairs": [p.to_dict() for p in pairs]}
    live = cfg.path("state", "pairs.json")
    tmp = live.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    tmp.replace(live)
    # One archive per run, not per day: an intraday rebuild must not erase the definitions of the
    # pairs that just left the universe, since positions may still be open on them.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    cfg.path("universe", f"pairs_{stamp}.json").write_text(json.dumps(payload, default=str), encoding="utf-8")
    cfg.path("universe", f"unmatched_{stamp}.json").write_text(json.dumps(unmatched, default=str), encoding="utf-8")
    return live


def load_pairs(cfg: Config) -> tuple[list[Pair], float]:
    p = cfg.path("state", "pairs.json")
    if not p.exists():
        return [], 0.0
    payload = json.loads(p.read_text(encoding="utf-8"))
    return [Pair.from_dict(d) for d in payload.get("pairs", [])], float(payload.get("generated_ts", 0.0))
