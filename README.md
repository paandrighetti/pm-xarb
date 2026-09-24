# pm-xarb

Cross-venue prediction-market arbitrage desk for Kalshi and Polymarket. It matches markets
deterministically, records both order books side by side, detects hedged combinations that cost
less than one dollar after fees, executes them on paper with leg risk and per-venue capital, and
reports daily. It runs unattended in one container.

It is a measurement instrument, not a trading bot. Nothing here signs an order.

## What question it answers

When Kalshi and Polymarket quote the same event at prices that sum to less than one, how much of
that gap is capturable by a taker who has to (1) be right that the two contracts settle on the same
fact, (2) cross two spreads on two venues that do not share capital, (3) pay two fee curves, and
(4) wait for settlement with the money locked?

The published literature answers parts of this with hourly or trade-level data and finds
persistent cross-venue disparities (Zhou, SSRN 5331995) and no-arbitrage violations that are not
executable before settlement (arXiv 2608.00666). This desk measures the executable part with
synchronized quotes and an explicit settlement-rule classification.

## Design

```
universe (daily)   Kalshi series + Gamma events -> family parsers -> canonical keys -> pairs.json
                   each pair carries a class: exact (same fact, same source, same instant) or basis
recorder (3 s)     GET /markets/orderbooks (Kalshi, batch) + POST /books (Polymarket, batch), concurrently
                   -> canonical two-sided books -> JSONL snapshots (written on change only)
scanner            two-ladder walk: take contracts while ask_A + fee_A + ask_B + fee_B < 1 - min_edge
paper executor     intent at poll n, fills against poll n+1 up to limits, naked excess sold at poll n+2
settlement         values read from each venue's API; basis pairs can pay 0 or 2; divergence is logged
report (daily)     DuckDB over the JSONL streams -> markdown + Telegram digest
history (weekly)   Kalshi 1-minute bid/ask candles vs Polymarket price series: UPPER BOUND screen only
```

### Families (v1, deterministic)

| family | Kalshi input | Polymarket input | canonical key | default class |
|---|---|---|---|---|
| crypto | `strike_type`, `floor_strike`/`cap_strike`, `close_time`, series settlement source | question regex (asset, above/below, threshold, date) + description clock and source | asset, comparator, threshold, reference instant | exact only if source and instant agree, otherwise basis |
| macro | FOMC decision wording per meeting month; CPI threshold via strike fields | question regex | indicator, period, decision kind or threshold | exact |
| sports | event title (two teams) + market `yes_sub_title`; game day from ticker | two-outcome moneyline, team names, `gameStartTime` | league, game day, winning team | exact, with tie and postponement rules flagged as unverified |

Anything a parser is unsure about is dropped and listed in `data/universe/unmatched_*.json`.
Manual control lives in `config/pair_overrides.yaml` (exclude, reclass). No fuzzy text matching:
the false-positive cost of a wrong pair is a fake arbitrage that the report would count as real.

### Fees

Kalshi taker: round up(M × 0.07 × C × P × (1 − P)) per order, M from `GET /series`, rounding to a
centicent (schedule of 7 July 2026). Polymarket taker: C × rate × P × (1 − P) with a per-category
rate (crypto 0.07, sports 0.05, economics/culture/weather 0.05, politics/finance/tech 0.04,
geopolitics 0), makers 0. Rates are in `config.yaml`. Fees are charged on both buys and sells in
the paper model; if Polymarket does not charge taker fees on sells, unwind costs are overstated.

### What gets capital, and why

Only `exact` pairs are traded. A `basis` pair is not an arbitrage: it is a bet that two different
measurements of the same event agree, which is short volatility on the window between the two
settlement instants. It pays a steady premium and loses the whole stake when the venues disagree.
Trading it consumes the budget, carries leg risk, and answers a different question from the one
this desk was built for. So `basis` pairs are detected, recorded, and their outcome swept exactly
as if they were held; the report prices what a hedge would have returned, entered at the **first**
opportunity seen on each pair rather than the best, with each leg paid by its own venue. That
gives the divergence rate and the return distribution at zero capital and zero execution risk.
`paper.execute_classes` controls this; set it to `[exact, basis]` to trade both.

One hedge per pair at a time. Without that rule the desk stacks opposing hedges on the same pair
as the market moves through a threshold: not wrong, but the position stops being one experiment
with one outcome, and the per-pair numbers stop meaning anything.

### The unit of result is the pair, not the leg

Venues settle hours apart: Kalshi finalises in minutes, Polymarket's oracle takes longer. A payout
moves cash the moment the venue pays, but the **result** of a hedge is only known once both legs
have reported. Booking a leg on its own shows the losing half of a hedge with no counterpart, which
reads as a loss and is not one. So a settled leg's PnL waits in escrow keyed by pair, and moves to
realized PnL only when the pair is whole. `resolution.escrow_max_days` (default 7) books a pair
that stays half-settled, flagged, so a venue that never reports cannot hold a result for ever.
`pmx peek` and the daily mark print realized, escrow and their total side by side.

### Paper execution model

* Capital is per venue (`paper.capital_per_venue_usd`) and never moves between venues.
* An intent is sized by the two-ladder walk, capped by `paper.max_notional_per_pair_usd` and by
  cash on each venue.
* Both legs are evaluated against the next observed books with a limit of the seen level plus
  `paper.limit_tolerance`. Partial fills are allowed. The unhedged excess is sold at the bid on the
  following poll (`on_leg_failure: unwind`), and the pair enters a cooldown.
* Positions lock cash until the venue reports settlement. Realized PnL uses the venue's own
  settlement value for each leg. Unrealized PnL marks open legs at mid once a day.
* Latency is one poll interval by construction. The data cutoff timestamp is stored on every
  intent and fill.

## Running

```
pip install -e ".[dev]"
pytest
pmx doctor          # every endpoint once, parsed samples, fee sanity, Telegram
pmx series --grep BTC
pmx universe
pmx run
pmx report [--date 2026-09-19]
pmx history
pmx status
```

Deployment on the VPS: `deploy/DEPLOY.md`.

## Data layout

```
data/
  state/pairs.json              current whitelist with stats and notes
  state/paper_state.json        cash, positions, pending intents (atomic writes, restart-safe)
  universe/pairs_YYYYMMDD.json  daily universe snapshots
  universe/unmatched_YYYYMMDD.json
  snapshots/YYYY-MM-DD/HHMM.jsonl[.gz]   both books per pair, on change
  blotter/polls.jsonl           one line per poll: coverage, skew between venues, poll latency
  blotter/detections.jsonl      every opportunity per poll (fees and depth applied)
  blotter/episodes.jsonl        contiguous opportunities with lifetime and peak size
  blotter/intents.jsonl, intent_outcomes.jsonl, fills.jsonl, unwinds.jsonl
  blotter/resolutions.jsonl     per pair: settlement value per venue, divergent flag, PnL per leg
  blotter/mtm.jsonl
  reports/YYYY-MM-DD.md, latest.md
  history/screen_YYYY-MM-DD.{md,parquet}
```

## Incidents

A pair whose definition is lost cannot be settled: its legs would sit for ever and any leg already
settled would leave a loss with no possible offset. `pmx recover` lists them; `pmx recover --void`
reverses whatever was booked on the pair, returns the notional, keeps only the fees actually paid,
and excludes the pair from the study. Pair definitions are persisted for everything held or
watched, and the universe is archived once per run, so this should not recur.

## What the numbers do not mean

* A detection is not a fill. Fill rates and leg failures are reported separately.
* A basis pair's edge is not an arbitrage. The report keeps classes apart and counts divergent
  settlements.
* The historical screen uses a Polymarket price series with no side and no depth; it is labelled
  as an upper bound wherever it is printed and is used only to choose which families to record.
* Return on locked capital is computed on resolved legs only; open positions are marked at mid.

## Known unknowns (verified on first VPS run by `pmx doctor`)

1. Exact Kalshi series tickers for crypto, FOMC, CPI and game winners. `pmx series --grep` lists them.
2. Whether `GET /markets/orderbooks` wants `tickers=A,B` or `tickers=A&tickers=B`; the client tries
   both once.
3. Kalshi's per-request ceiling on 1-minute candles (chunked at 4000 periods, configurable).
4. Polymarket sports event tags and `sportsMarketType` values; the parser also accepts untagged
   two-outcome team markets and rejects anything that looks like a spread or total.
5. Whether Kalshi serves market data to a Hetzner datacenter address.

## Next steps once data accumulates

* Replay backtester over recorded snapshots (varying latency, tolerance and size).
* Candidate pairs from text similarity with Telegram approval, for families without structure.
* Weather family (NYC daily high on both venues).
* Maker-side variant: rest one leg, take the other when it becomes hedgeable.

## Tests

`pytest` covers the fee curves against the published schedules, the Kalshi bids-only to
two-sided conversion including polarity, the two-ladder walk, the three parser families with
polarity and disambiguation, the matcher classes and overrides, the paper state machine (hedged
fill, leg failure and unwind, basis divergence, caps, state round trip) and an offline
end-to-end run of the desk plus report with fake venues.
