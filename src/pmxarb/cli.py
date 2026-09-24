"""pmx command line.

  pmx doctor              hit every endpoint once, print parsed samples (first thing to run on the VPS)
  pmx series --grep BTC   list Kalshi series whose ticker/title matches (to fix config.yaml tickers)
  pmx universe            rebuild pairs now and print the match summary
  pmx run                 the desk (poll loop + scheduler); what docker-compose runs
  pmx report [--date D]   build the daily report for a UTC day (default yesterday)
  pmx history             run the upper-bound historical screen now
  pmx status              print paper state and health
  pmx peek [--hours 3]    what the desk saw recently: detections per pair with titles, intents, positions
  pmx recover [--void]    list, and optionally void, positions whose pair definition was lost
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone

from .config import Config, load_config
from .fees import FeeModel
from .matching.matcher import load_pairs
from .matching.normalize import TeamBook
from .venues import HttpError, KalshiClient, PolymarketClient


def _log(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def cmd_doctor(cfg: Config) -> int:
    from .books import kalshi_book, polymarket_book
    from .models import KALSHI, POLYMARKET, Leg

    ok = True
    k = KalshiClient(cfg.venues.kalshi)
    p = PolymarketClient(cfg.venues.polymarket)
    fees = FeeModel(cfg.venues.kalshi, cfg.venues.polymarket)
    print("== Kalshi")
    try:
        ms = await k.request("GET", "/markets", params={"limit": 3, "status": "open"})
        m = (ms or {}).get("markets", [{}])[0]
        print("  GET /markets ok; sample keys:", sorted(m.keys())[:40])
        print("  sample:", {kk: m.get(kk) for kk in ("ticker", "series_ticker", "event_ticker", "title", "yes_sub_title", "strike_type",
                                                    "floor_strike", "cap_strike", "close_time", "yes_bid_dollars", "yes_ask_dollars", "status")})
        obs = await k.get_orderbooks([m["ticker"]])
        raw = obs.get(m["ticker"], {})
        print("  GET /markets/orderbooks ok; keys:", list(raw.keys()))
        leg = Leg(KALSHI, m["ticker"], m.get("title", ""), "Yes", m.get("series_ticker", ""), 1.0, 0.0, "", None)
        b = kalshi_book(leg, raw)
        print(f"  parsed book: yes_bid {b.yes_bids[:1]} yes_ask {b.yes_asks[:1]} no_ask {b.no_asks[:1]}")
        for fam, tickers in cfg.venues.kalshi.series.items():
            for st in tickers:
                try:
                    s = await k.get_series(st)
                    print(f"  series {st:16s} ok  fee_type={s.get('fee_type')} mult={s.get('fee_multiplier')} "
                          f"settlement={[x.get('name') for x in (s.get('settlement_sources') or []) if isinstance(x, dict)]}")
                except HttpError as exc:
                    ok = False
                    print(f"  series {st:16s} FAIL {exc.status}  -> fix config.venues.kalshi.series.{fam} (use `pmx series --grep`)")
    except HttpError as exc:
        ok = False
        print("  Kalshi FAILED:", exc)
    print("== Polymarket")
    try:
        data = await p.request("GET", "/events/keyset", params={"closed": "false", "limit": 2, "order": "volume24hr", "ascending": "false"}, base=p.gamma)
        evs = (data or {}).get("events", []) if isinstance(data, dict) else (data or [])
        ev = (evs or [{}])[0]
        print("  Gamma /events/keyset ok; next_cursor present:", bool(isinstance(data, dict) and data.get("next_cursor")))
        print("  event keys:", sorted(ev.keys())[:40])
        print("  tags:", [t.get("slug") for t in ev.get("tags") or []][:10])
        m = (ev.get("markets") or [{}])[0]
        print("  market keys:", sorted(m.keys())[:50])
        print("  sample:", {kk: m.get(kk) for kk in ("question", "conditionId", "outcomes", "clobTokenIds", "endDate", "gameStartTime",
                                                    "sportsMarketType", "volume24hr", "bestBid", "bestAsk", "negRisk")})
        from .venues.polymarket import jlist
        toks = [str(t) for t in jlist(m.get("clobTokenIds"))]
        if toks:
            books = await p.get_books(toks)
            print("  CLOB POST /books ok; assets returned:", list(books.keys())[:2])
            leg = Leg(POLYMARKET, str(m.get("conditionId")), m.get("question", ""), "Yes", "other", 1.0, 0.0, "", None,
                      yes_token=toks[0], no_token=toks[1] if len(toks) > 1 else None)
            b = polymarket_book(leg, books.get(toks[0]), books.get(toks[1]) if len(toks) > 1 else None)
            print(f"  parsed book: yes_bid {b.yes_bids[:1]} yes_ask {b.yes_asks[:1]} no_ask {b.no_asks[:1]} ts_src {b.ts_src}")
            now = int(datetime.now(timezone.utc).timestamp())
            h = await p.prices_history(toks[0], now - 3600, now, fidelity_min=1)
            print(f"  CLOB /prices-history ok; {len(h)} points last hour; sample {h[:1]}")
            cm = await p.clob_market(str(m.get("conditionId")))
            print("  CLOB /markets/{id} ok; keys:", sorted(cm.keys())[:30])
    except HttpError as exc:
        ok = False
        print("  Polymarket FAILED:", exc)
    print("== Fees sanity")
    from .models import Leg as L
    lk = L(KALSHI, "X", "", "Yes", "KXBTC", 1.0, 0.0, "", None)
    lp = L(POLYMARKET, "Y", "", "Yes", "crypto", 1.0, 0.0, "", None)
    print(f"  kalshi taker @0.50 x100 = {fees.order_fee(lk, 0.5, 100):.4f} (expect 1.75)  poly crypto @0.50 x100 = {fees.order_fee(lp, 0.5, 100):.4f} (expect 1.75)")
    print("== Telegram")
    from . import telegram
    sent = await telegram.send("pm-xarb doctor: hello", cfg.telegram.enabled)
    print("  sent" if sent else "  not sent (token/chat missing or disabled)")
    await k.aclose()
    await p.aclose()
    print("== RESULT:", "OK" if ok else "PROBLEMS FOUND")
    return 0 if ok else 1


async def cmd_series(cfg: Config, grep: str) -> int:
    k = KalshiClient(cfg.venues.kalshi)
    try:
        series = await k.list_series()
    finally:
        await k.aclose()
    g = grep.lower()
    rows = [s for s in series if g in str(s.get("ticker", "")).lower() or g in str(s.get("title", "")).lower()
            or g in str(s.get("category", "")).lower()]
    for s in sorted(rows, key=lambda s: s.get("ticker", "")):
        print(f"{s.get('ticker'):24s} {s.get('frequency') or '':10s} {s.get('category') or '':14s} {s.get('title')}")
    print(f"{len(rows)} of {len(series)} series match '{grep}'")
    return 0


async def cmd_universe(cfg: Config) -> int:
    from .universe import build_universe, summarize
    k, p = KalshiClient(cfg.venues.kalshi), PolymarketClient(cfg.venues.polymarket)
    fees = FeeModel(cfg.venues.kalshi, cfg.venues.polymarket)
    try:
        pairs, stats = await build_universe(cfg, k, p, fees, TeamBook.load(cfg.universe.teams_file))
    finally:
        await k.aclose()
        await p.aclose()
    print(json.dumps(stats, indent=1))
    print(summarize(stats))
    for pr in pairs[:25]:
        print(f"  [{pr.klass:5s}] {pr.key}\n      K: {pr.legs['kalshi'].title[:80]}\n      P: {pr.legs['polymarket'].title[:80]}"
              + (f"\n      notes: {'; '.join(pr.notes)}" if pr.notes else ""))
    print(f"{len(pairs)} pairs written to {cfg.path('state', 'pairs.json')}")
    return 0


async def cmd_history(cfg: Config) -> int:
    from .history import run_screen
    pairs, _ = load_pairs(cfg)
    if not pairs:
        print("no pairs; run `pmx universe` first")
        return 1
    k, p = KalshiClient(cfg.venues.kalshi), PolymarketClient(cfg.venues.polymarket)
    fees = FeeModel(cfg.venues.kalshi, cfg.venues.polymarket)
    try:
        text, rows = await run_screen(cfg, k, p, fees, pairs)
    finally:
        await k.aclose()
        await p.aclose()
    print(text)
    return 0


def cmd_report(cfg: Config, date: str | None) -> int:
    from .report import Report
    day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc) if date else None
    path, digest = Report(cfg, day).write()
    print(path)
    print(digest)
    return 0


def cmd_peek(cfg: Config, hours: float) -> int:
    """What the desk saw recently: detections per pair with both titles and prices, intent outcomes,
    open positions. Reads the blotter with DuckDB; nothing is recomputed by hand."""
    import duckdb

    pairs_p = cfg.path("state", "pairs.json")
    pairs = {d["pair_id"]: d for d in (json.loads(pairs_p.read_text(encoding="utf-8")).get("pairs", []) if pairs_p.exists() else [])}
    state_p = cfg.path("state", "paper_state.json")
    # Pairs the desk carries after they left the universe live in the state under `carried`
    # (`retired` before 0.2). Reading the wrong key flagged every carried pair as an orphan.
    st = json.loads(state_p.read_text(encoding="utf-8")) if state_p.exists() else {}
    for pd in st.get("carried", st.get("retired", [])):
        pairs.setdefault(pd["pair_id"], pd)
    # Pairs that are done (settled, nothing carried) are in neither file; their titles and classes
    # live in the per-run universe archives. Newest first, so a redefinition wins.
    for f in sorted((cfg.data / "universe").glob("pairs_*.json"), reverse=True):
        try:
            for pd in json.loads(f.read_text(encoding="utf-8")).get("pairs", []):
                pairs.setdefault(pd["pair_id"], pd)
        except (OSError, ValueError):
            continue

    def title(pid: str, venue: str) -> str:
        return (pairs.get(pid, {}).get("legs", {}).get(venue, {}).get("title") or "?")[:58]

    def klass(pid: str) -> str:
        return pairs.get(pid, {}).get("klass", "?")

    bl = cfg.data / "blotter"
    since = datetime.now(timezone.utc).timestamp() - hours * 3600
    con = duckdb.connect()

    def q(sql: str, path: str) -> list[tuple]:
        p = bl / path
        if not p.exists() or p.stat().st_size == 0:
            return []
        try:
            return con.execute(sql.replace("$F", f"read_ndjson_auto('{p.as_posix()}', ignore_errors=true)"), [since]).fetchall()
        except duckdb.Error as exc:
            print(f"  ({path}: {exc})")
            return []

    def cols(path: str) -> set[str]:
        p = bl / path
        if not p.exists() or p.stat().st_size == 0:
            return set()
        try:
            cur = con.execute(f"SELECT * FROM read_ndjson_auto('{p.as_posix()}', ignore_errors=true) LIMIT 0")
            return {d[0] for d in cur.description}
        except duckdb.Error:
            return set()

    print(f"== polls (last {hours:g} h)")
    for r in q("SELECT count(*), round(avg(latency_s),3), round(avg(skew_s),3), round(max(skew_s),3), "
               "round(avg(books_k*1.0/nullif(pairs,0)),3), round(avg(books_p*1.0/nullif(pairs,0)),3), round(avg(detections),2) "
               "FROM $F WHERE ts>=?", "polls.jsonl"):
        print(f"  polls={r[0]} mean_poll_s={r[1]} mean_skew_s={r[2]} max_skew_s={r[3]} coverage_k={r[4]} coverage_p={r[5]} mean_detections={r[6]}")

    print(f"== detections by pair (last {hours:g} h)")
    rows = q("SELECT pair_id, combo, count(*), round(avg(edge_per_contract),4), round(max(edge_per_contract),4), round(avg(qty),0), "
             "round(avg(best_a),3), round(avg(best_b),3), round(avg(limit_a),3), round(avg(limit_b),3), round(avg(days_locked),1) "
             "FROM $F WHERE ts>=? GROUP BY 1,2 ORDER BY 3 DESC", "detections.jsonl")
    if not rows:
        print("  none")
    for r in rows:
        print(f"  {r[0]} [{klass(r[0])}] {r[1]}: n={r[2]} edge mean={r[3]} max={r[4]} qty~{r[5]:.0f} best_a={r[6]} best_b={r[7]} "
              f"limit_a={r[8]} limit_b={r[9]} days_locked~{r[10]}")
        print(f"      K: {title(r[0], 'kalshi')}\n      P: {title(r[0], 'polymarket')}")

    print(f"== intent outcomes (last {hours:g} h)")
    oc = cols("intent_outcomes.jsonl")
    reason = "reason" if "reason" in oc else "NULL"
    model = "model" if "model" in oc else "NULL"
    # hedge_cost_per_contract (0.3) prices the hedge alone; the older locked_cost_per_hedged also
    # counts the naked excess later sold back, which made leg failures look like fills above par.
    cost = "coalesce(hedge_cost_per_contract, locked_cost_per_hedged)" if "hedge_cost_per_contract" in oc else "locked_cost_per_hedged"
    rows = q("SELECT ts, pair_id, combo, status, qty_intended, qty_a, qty_b, round(cost_a,2), round(cost_b,2), round(fees,3), "
             f"round(edge_seen,4), round({cost},4), {reason}, {model} FROM $F WHERE ts>=? ORDER BY ts",
             "intent_outcomes.jsonl")
    if not rows:
        print("  none")
    for r in rows:
        when = datetime.fromtimestamp(r[0], tz=timezone.utc).strftime("%H:%M:%S")
        print(f"  {when} {r[1]} [{klass(r[1])}] {r[2]} {r[13] or ''} -> {r[3]} qty={r[4]} a={r[5]} b={r[6]} cost_a={r[7]} cost_b={r[8]} "
              f"fees={r[9]} edge_seen={r[10]} hedge_cost={r[11]} {r[12] or ''}")
    fails = q("SELECT count(*), sum(CASE WHEN status='filled' THEN 1 ELSE 0 END), "
              "sum(CASE WHEN status IN ('unwinding','partial') THEN 1 ELSE 0 END), sum(CASE WHEN status='missed' THEN 1 ELSE 0 END) "
              "FROM $F WHERE ts>=?", "intent_outcomes.jsonl")
    if fails and fails[0][0]:
        n, f, lf, m = fails[0]
        print(f"  intents={n} filled={f} leg_failures={lf} ({lf / n:.0%}) missed={m}")

    rows = q("SELECT pair_id, venue, side, qty, round(vwap,3), round(fee,3), round(pnl,3), polls FROM $F WHERE ts>=? ORDER BY ts", "unwinds.jsonl")
    if rows:
        print("== unwinds")
        for r in rows:
            print(f"  {r[0]} {r[1]} {r[2]} qty={r[3]} vwap={r[4]} fee={r[5]} pnl={r[6]} polls={r[7]}")

    has_pending = "pending_venues" in cols("resolutions.jsonl")
    rows = q("SELECT pair_id, family, klass, yes_value, divergent, round(pnl,3)"
             + (", pending_venues" if has_pending else ", NULL") + " FROM $F WHERE ts>=? ORDER BY ts", "resolutions.jsonl")
    if rows:
        print("== resolutions")
        for r in rows:
            waiting = f" EN ATTENTE DE {r[6]}" if r[6] else ""
            print(f"  {r[0]} {r[1]}/{r[2]} yes_value={r[3]} divergent={r[4]} pnl={r[5]}{waiting}")

    has_obs = bool(cols("observations.jsonl"))
    rows = q("SELECT pair_id, klass, yes_value, divergent, complete FROM $F WHERE ts>=? ORDER BY ts", "observations.jsonl") if has_obs else []
    if rows:
        print(f"== observations (classes non tradees, last {hours:g} h)")
        for r in rows:
            print(f"  {r[0]} [{r[1]}] yes_value={r[2]} divergent={r[3]} complete={r[4]}")

    print("== open positions")
    if state_p.exists():
        paper = json.loads(state_p.read_text(encoding="utf-8")).get("paper", {})
        open_pos = [v for v in paper.get("positions", {}).values() if not v.get("resolved") and v.get("qty", 0) > 0]
        for v in sorted(open_pos, key=lambda x: x["pair_id"]):
            avg = v["cost"] / v["qty"] if v["qty"] else 0.0
            print(f"  {v['pair_id']} [{klass(v['pair_id'])}] {v['venue']} {v['side']} qty={v['qty']:.0f} avg={avg:.3f} fees={v['fees']:.2f}")
        esc = paper.get("escrow", {})
        if esc:
            print("== paires a moitie reglees (resultat en sequestre, non inscrit)")
            for pid, e in sorted(esc.items()):
                waited = (datetime.now(timezone.utc).timestamp() - e["since"]) / 3600
                print(f"  {pid} [{klass(pid)}] sequestre={e['pnl']:+.2f} depuis {waited:.1f} h")
        orphans = sorted({v["pair_id"] for v in open_pos} - set(pairs))
        if orphans:
            print(f"  ATTENTION positions sans definition de paire (ne peuvent pas se regler): {orphans}")
        locked = sum(v["cost"] + v["fees"] for v in open_pos)
        held = sum(e["pnl"] for e in esc.values())
        # Legs whose pair is half settled: what they pay if the two venues agree. A projection,
        # printed apart from realized PnL, because the alternative is reading a hedge half-booked.
        settled_map = paper.get("settled", {})
        congruent = 0.0
        for v in open_pos:
            known = settled_map.get(v["pair_id"], {})
            other = [val for vv, val in known.items() if vv != v["venue"]]
            if other:
                payout = v["qty"] * (other[0] if v["side"] == "yes" else 1.0 - other[0])
                congruent += payout - v["cost"] - v["fees"]
        print(f"  cash={ {k: round(c, 2) for k, c in paper.get('cash', {}).items()} } immobilise={locked:.2f}")
        r = paper.get("realized", 0.0)
        print(f"  realized={r:+.2f} (paires completes)  sequestre={held:+.2f} ({len(esc)} paires)")
        if congruent:
            print(f"  jambes en attente, valorisees si les venues concordent : {congruent:+.2f}")
            print(f"  -> projection {r + held + congruent:+.2f} (hypothese de concordance, pas un resultat)")
    return 0


def cmd_recover(cfg: Config, do_void: bool) -> int:
    """Report positions whose pair definition is missing, and optionally void them.

    A hedge whose counterpart leg can never settle leaves a booked loss with no possible offset.
    Voiding reverses any settlement already booked on the pair, returns the notional and keeps
    only the fees actually paid, so the running PnL stops carrying a loss that does not exist."""
    from .desk import Desk
    from .fees import FeeModel as _FM
    from .matching.matcher import load_pairs as _lp

    k, p = KalshiClient(cfg.venues.kalshi), PolymarketClient(cfg.venues.polymarket)
    desk = Desk(cfg, k, p, _FM(cfg.venues.kalshi, cfg.venues.polymarket))
    pairs, _ = _lp(cfg)
    desk.set_pairs(pairs)
    orph = desk.orphans()
    if not orph:
        print("no orphaned positions: every held pair has a definition")
        return 0
    stuck = 0.0
    for key, pos in desk.paper.positions.items():
        if key.split("|")[0] in orph and not pos.voided and pos.qty > 0:
            stuck += pos.cost + pos.fees
    print(f"{len(orph)} pairs hold positions with no definition, {stuck:,.2f} USD locked:")
    for pid in sorted(orph):
        for key, pos in desk.paper.positions.items():
            if key.split("|")[0] == pid and not pos.voided and pos.qty > 0:
                print(f"  {pid} {pos.venue:11s} {pos.side:3s} qty={pos.qty:.0f} cost={pos.cost:.2f} "
                      f"fees={pos.fees:.2f} settled={pos.resolved}")
    if not do_void:
        print("\nrun again with --void to void them (notional returned, fees kept, pairs excluded from the study)")
        return 0
    recs = desk.void_orphans("pair definition lost before the per-run universe archive was introduced")
    print(f"\nvoided {len(recs)} pairs")
    print(f"  notional returned : {sum(r['notional_returned'] for r in recs):,.2f} USD")
    print(f"  fees kept in PnL  : {sum(r['fees_kept'] for r in recs):,.2f} USD")
    print(f"  realized now      : {desk.paper.realized:,.2f} USD")
    print(f"  cash              : { {a: round(b, 2) for a, b in desk.paper.cash.items()} }")
    return 0


def cmd_status(cfg: Config) -> int:
    p = cfg.path("state", "paper_state.json")
    if not p.exists():
        print("no state yet")
        return 0
    s = json.loads(p.read_text(encoding="utf-8"))
    paper = s.get("paper", {})
    open_pos = [v for v in paper.get("positions", {}).values() if not v.get("resolved") and v.get("qty", 0) > 0]
    print(json.dumps({"poll_idx": s.get("poll_idx"), "saved": datetime.fromtimestamp(s.get("saved_ts", 0), tz=timezone.utc).isoformat(),
                      "cash": paper.get("cash"), "realized": paper.get("realized"), "stats": paper.get("stats"),
                      "open_positions": len(open_pos), "pending_intents": len(paper.get("intents", {})),
                      "health": s.get("health")}, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pmx", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, help="path to config.yaml (default: $PMX_CONFIG or ./config.yaml)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    sp = sub.add_parser("series"); sp.add_argument("--grep", required=True)
    sub.add_parser("universe")
    sub.add_parser("run")
    rp = sub.add_parser("report"); rp.add_argument("--date", default=None, help="UTC day YYYY-MM-DD (default: yesterday)")
    sub.add_parser("history")
    sub.add_parser("status")
    pk = sub.add_parser("peek"); pk.add_argument("--hours", type=float, default=3.0, help="look-back window (default 3)")
    rc = sub.add_parser("recover"); rc.add_argument("--void", action="store_true", help="void positions whose pair definition is lost")
    a = ap.parse_args(argv)
    _log(a.verbose)
    cfg = load_config(a.config)
    if a.cmd == "doctor":
        return asyncio.run(cmd_doctor(cfg))
    if a.cmd == "series":
        return asyncio.run(cmd_series(cfg, a.grep))
    if a.cmd == "universe":
        return asyncio.run(cmd_universe(cfg))
    if a.cmd == "run":
        from .runner import Runner
        asyncio.run(Runner(cfg).run())
        return 0
    if a.cmd == "report":
        return cmd_report(cfg, a.date)
    if a.cmd == "history":
        return asyncio.run(cmd_history(cfg))
    if a.cmd == "status":
        return cmd_status(cfg)
    if a.cmd == "peek":
        return cmd_peek(cfg, a.hours)
    if a.cmd == "recover":
        return cmd_recover(cfg, a.void)
    return 2


if __name__ == "__main__":
    sys.exit(main())
