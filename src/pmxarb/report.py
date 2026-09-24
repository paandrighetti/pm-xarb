"""Daily report built with DuckDB straight from the append-only JSONL streams.
No number in the markdown is typed by hand; every table is a query result."""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb

from .config import Config

log = logging.getLogger(__name__)


def _md_table(rows: list[dict[str, Any]], cols: list[str] | None = None) -> str:
    if not rows:
        return "_none_\n"
    cols = cols or list(rows[0].keys())
    out = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for r in rows:
        cells = []
        for c in cols:
            v = r.get(c)
            if isinstance(v, float):
                cells.append(f"{v:.4f}" if abs(v) < 10 else f"{v:,.2f}")
            else:
                cells.append("" if v is None else str(v))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


class Report:
    def __init__(self, cfg: Config, day: datetime | None = None):
        self.cfg = cfg
        now = datetime.now(timezone.utc)
        self.day = (day or (now - timedelta(days=1))).replace(hour=0, minute=0, second=0, microsecond=0)
        self.t0 = self.day.timestamp()
        self.t1 = self.t0 + 86400
        self.con = duckdb.connect()
        self.blotter = cfg.data / "blotter"
        self._views()

    def _views(self) -> None:
        for name in ("polls", "detections", "episodes", "intents", "intent_outcomes", "fills", "unwinds",
                     "resolutions", "observations", "voids", "escrow_forced", "mtm"):
            p = self.blotter / f"{name}.jsonl"
            if p.exists() and p.stat().st_size > 0:
                self.con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_ndjson_auto('{p.as_posix()}', ignore_errors=true, "
                                 f"maximum_object_size=4000000)")
            else:
                self.con.execute(f"CREATE VIEW {name} AS SELECT NULL::DOUBLE AS ts WHERE 1=0")

    def cols(self, view: str) -> set[str]:
        try:
            cur = self.con.execute(f"SELECT * FROM {view} LIMIT 0")
            return {d[0] for d in cur.description}
        except duckdb.Error:
            return set()

    def q(self, sql: str) -> list[dict[str, Any]]:
        try:
            cur = self.con.execute(sql, [self.t0, self.t1] if "?" in sql else [])
        except duckdb.Error as exc:
            log.warning("query failed: %s :: %s", exc, sql[:120])
            return []
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def universe(self) -> dict[str, Any]:
        p = self.cfg.path("state", "pairs.json")
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"pairs": [], "stats": {}}

    def build(self) -> tuple[str, str]:
        uni = self.universe()
        pairs = {d["pair_id"]: d for d in uni.get("pairs", [])}
        stats = uni.get("stats", {})
        day_s = self.day.strftime("%Y-%m-%d")
        md = [f"# pm-xarb daily report, {day_s} (UTC)\n",
              "Paper desk. Two-venue quotes are quasi-synchronous (one poll apart at most); fills are simulated one poll after "
              "detection against the next observed books; settlement values come from each venue's own API.\n"]

        # universe
        fam_rows = [{"family": f, **{k: v for k, v in d.items()}} for f, d in stats.items() if not f.startswith("_")]
        md += ["## Universe\n", f"Pairs whitelisted: **{len(pairs)}** (generated {uni.get('generated', 'n/a')})\n",
               _md_table(fam_rows, ["family", "kalshi_legs", "polymarket_legs", "exact", "basis", "kalshi_unmatched"])]

        # data quality
        dq = self.q("SELECT count(*) AS polls, round(avg(skew_s),3) AS mean_skew_s, round(max(skew_s),3) AS max_skew_s, "
                    "round(avg(latency_s),3) AS mean_poll_s, round(avg(books_k*1.0/nullif(pairs,0)),3) AS kalshi_coverage, "
                    "round(avg(books_p*1.0/nullif(pairs,0)),3) AS poly_coverage FROM polls WHERE ts>=? AND ts<?")
        md += ["## Data quality\n", _md_table(dq)]

        # detections
        det = self.q("SELECT family, klass, combo, count(*) AS n, round(avg(edge_per_contract),4) AS mean_edge, "
                     "round(max(edge_per_contract),4) AS max_edge, round(avg(qty),1) AS mean_qty, "
                     "round(sum(edge_total),2) AS sum_edge_usd, round(avg(days_locked),1) AS mean_days_locked "
                     "FROM detections WHERE ts>=? AND ts<? GROUP BY 1,2,3 ORDER BY 1,2,3")
        eps = self.q("SELECT family, klass, count(*) AS episodes, round(median(lifetime_s),1) AS median_life_s, "
                     "round(quantile_cont(lifetime_s,0.9),1) AS p90_life_s, round(max(lifetime_s),1) AS max_life_s, "
                     "round(avg(max_edge),4) AS mean_peak_edge, round(sum(max_edge_total),2) AS sum_peak_edge_usd "
                     "FROM episodes WHERE last_ts>=? AND last_ts<? GROUP BY 1,2 ORDER BY 1,2")
        md += ["## Detections (net of fees, depth-limited)\n", _md_table(det),
               "### Episodes (contiguous polls with the same opportunity)\n", _md_table(eps)]

        # top episodes with titles
        top = self.q("SELECT pair_id, combo, klass, round(max_edge,4) AS peak_edge, max_qty, round(max_edge_total,2) AS peak_usd, "
                     "round(lifetime_s,1) AS life_s FROM episodes WHERE last_ts>=? AND last_ts<? ORDER BY max_edge_total DESC LIMIT 10")
        for r in top:
            pd = pairs.get(r["pair_id"], {})
            r["kalshi"] = (pd.get("legs", {}).get("kalshi", {}).get("title") or "")[:60]
            r["polymarket"] = (pd.get("legs", {}).get("polymarket", {}).get("title") or "")[:60]
        md += ["### Largest episodes\n", _md_table(top, ["pair_id", "klass", "combo", "peak_edge", "max_qty", "peak_usd", "life_s", "kalshi", "polymarket"])]

        # paper execution
        oc = self.cols("intent_outcomes")
        # hedge_cost_per_contract (0.3) prices the hedge alone; locked_cost_per_hedged also counts the
        # naked excess later sold back, which made a leg failure read as a fill far above par.
        cost = ("coalesce(hedge_cost_per_contract, locked_cost_per_hedged)" if "hedge_cost_per_contract" in oc
                else "locked_cost_per_hedged")
        model = "model" if "model" in oc else "'legacy' AS model"
        outc = self.q(f"SELECT {model}, status, count(*) AS n, sum(hedged) AS hedged_contracts, round(sum(cost_a+cost_b),2) AS notional, "
                      "round(sum(fees),2) AS fees, round(avg(edge_seen),4) AS mean_edge_seen, "
                      f"round(avg({cost}),4) AS mean_hedge_cost FROM intent_outcomes WHERE ts>=? AND ts<? GROUP BY 1, 2 ORDER BY 1, 2")
        # The variable that decided the weekend: how often the second leg was gone by the time the
        # first had filled. Printed next to the fill rate because one without the other misleads.
        legf = self.q("SELECT klass, family, count(*) AS intents, "
                      "sum(CASE WHEN status='filled' THEN 1 ELSE 0 END) AS filled, "
                      "sum(CASE WHEN status IN ('unwinding','partial') THEN 1 ELSE 0 END) AS leg_failures, "
                      "sum(CASE WHEN status='missed' THEN 1 ELSE 0 END) AS missed, "
                      "round(sum(CASE WHEN status IN ('unwinding','partial') THEN 1 ELSE 0 END) * 1.0 / count(*), 3) AS leg_failure_rate, "
                      "sum(hedged) AS hedged, round(sum(hedged) * 1.0 / nullif(sum(qty_intended), 0), 3) AS fill_ratio "
                      "FROM intent_outcomes WHERE ts>=? AND ts<? AND klass IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2")
        unw = self.q("SELECT count(*) AS unwinds, round(sum(pnl),2) AS pnl, round(avg(polls),1) AS mean_polls FROM unwinds WHERE ts>=? AND ts<? AND pnl IS NOT NULL")
        # A hedged pair settles one leg at a time: Kalshi finalises in minutes, Polymarket's oracle
        # takes hours. Until both legs report, the PnL booked on the pair is the losing leg alone,
        # so the one-sided count must be printed next to it or the total reads as a loss.
        rc = self.cols("resolutions")
        one_sided = (", count(DISTINCT CASE WHEN NOT coalesce(complete, true) THEN pair_id END) AS one_sided "
                     if "complete" in rc else ", NULL AS one_sided ")
        # A naked pair is the residue of a leg failure held to resolution: its result is a leg
        # risk cost, not an arbitrage outcome, and is counted apart so the hedged result stays legible.
        naked = (", count(DISTINCT CASE WHEN naked THEN pair_id END) AS naked_pairs, "
                 "round(sum(CASE WHEN coalesce(naked, false) THEN pnl END),2) AS pnl_naked, "
                 "round(sum(CASE WHEN NOT coalesce(naked, false) THEN pnl END),2) AS pnl_hedged "
                 if "naked" in rc else ", NULL AS naked_pairs, NULL AS pnl_naked, round(sum(pnl),2) AS pnl_hedged ")
        res = self.q("SELECT family, klass, count(DISTINCT pair_id) AS resolved_pairs, "
                     "count(DISTINCT CASE WHEN divergent THEN pair_id END) AS divergent" + one_sided + naked +
                     ", round(sum(pnl),2) AS pnl FROM resolutions WHERE ts>=? AND ts<? GROUP BY 1,2 ORDER BY 1,2")
        cum = self.q("SELECT family, klass, count(DISTINCT pair_id) AS resolved_pairs, "
                     "count(DISTINCT CASE WHEN divergent THEN pair_id END) AS divergent" + one_sided + naked +
                     ", round(sum(pnl),2) AS pnl FROM resolutions GROUP BY 1,2 ORDER BY 1,2")
        rpd = self.q("SELECT round(sum(l.pnl),2) AS pnl, round(sum((l.cost + l.fees) * l.days_locked),2) AS dollar_days, "
                     "round(sum(l.pnl)/nullif(sum((l.cost + l.fees) * l.days_locked),0)*365,4) AS annualized_return_on_locked "
                     "FROM (SELECT unnest(legs) AS l FROM resolutions)")
        mtm = self.q("SELECT strftime(to_timestamp(ts), '%Y-%m-%d %H:%M') AS at, open_positions, locked, unrealized, realized, "
                     "cash FROM mtm ORDER BY ts DESC LIMIT 1")
        md += ["## Paper execution\n", "### Intent outcomes\n", _md_table(outc),
               "### Leg failures (second leg gone once the first is on; the cost sits in the unwinds)\n", _md_table(legf),
               "### Unwinds of naked legs\n", _md_table(unw),
               "### Resolutions today (one_sided: pairs where only one venue has reported so far)\n", _md_table(res),
               "### Resolutions since inception\n", _md_table(cum),
               "### Return on locked capital (resolved legs, since inception)\n", _md_table(rpd),
               "### Latest mark-to-market\n", _md_table(mtm)]

        md += ["## Classes not traded: counterfactual\n", self._counterfactual(),
               "## Reading guide\n",
               "* `exact` pairs share resolution source and instant; a hedged pair pays 1 per contract. `basis` pairs do not, "
               "and `divergent` counts resolutions where the two venues disagreed.\n",
               "* Edges are net of taker fees on both legs at the walked prices. They are not net of the capital cost of waiting.\n",
               "* Realized PnL books each leg at its own venue's settlement. A pair whose `one_sided` flag is set has had only "
               "its losing or winning leg booked; the total is meaningful once `one_sided` returns to zero.\n",
               "* Latency is one poll interval by construction; a real taker would be faster, but would also face size that "
               "vanished between quote and order, which is what the `missed` and `partial` rows measure.\n"]
        text = "\n".join(md)

        n_det = sum(int(r["n"]) for r in det) if det else 0
        n_eps = sum(int(r["episodes"]) for r in eps) if eps else 0
        n_filled = sum(int(r["n"]) for r in outc if r["status"] == "filled")
        n_hedged = sum(int(r["hedged_contracts"] or 0) for r in outc if r["status"] == "filled")
        n_int = sum(int(r["intents"]) for r in legf) if legf else 0
        n_lf = sum(int(r["leg_failures"]) for r in legf) if legf else 0
        digest = (f"pm-xarb {day_s}\n"
                  f"pairs {len(pairs)} | polls {dq[0]['polls'] if dq else 0} | detections {n_det} | episodes {n_eps}\n"
                  f"intents {n_int} | filled {n_filled} | leg failures {n_lf}"
                  + (f" ({n_lf / n_int:.0%})" if n_int else "") + f" | hedged {n_hedged}\n"
                  f"resolved today: " + (", ".join(f"{r['family']}/{r['klass']} {r['resolved_pairs']} (div {r['divergent']}) pnl {r['pnl']}" for r in res) or "none") + "\n"
                  f"realized total {mtm[0]['realized'] if mtm else 0} | unrealized {mtm[0]['unrealized'] if mtm else 0} | cash {mtm[0]['cash'] if mtm else 'n/a'}")
        return text, digest

    @staticmethod
    def _side_value(yes_value: float, side: str) -> float:
        return yes_value if side == "yes" else 1.0 - yes_value

    def _counterfactual(self) -> str:
        """What a hedge would have returned on the pairs we watched without trading. Priced at the
        FIRST opportunity seen on each pair, not the best: taking the peak would be choosing the
        entry after seeing the whole day. Payout is each venue's own settlement, so a divergence
        pays 0 or 2 and shows up as the tail this class actually carries."""
        det = self.q("SELECT pair_id, klass, combo, edge_per_contract, qty, ts FROM detections ORDER BY ts")
        obs = self.q("SELECT pair_id, klass, yes_value, divergent FROM observations WHERE complete")
        if not det or not obs:
            return "_no complete observation yet_\n"
        first: dict[str, dict[str, Any]] = {}
        for d in det:
            first.setdefault(d["pair_id"], d)
        rows: list[dict[str, Any]] = []
        for o in obs:
            d = first.get(o["pair_id"])
            vals = o.get("yes_value") or {}
            if not d or len(vals) != 2:
                continue
            try:
                va, sa = d["combo"].split("+")[0].split(":")
                vb, sb = d["combo"].split("+")[1].split(":")
                payout = self._side_value(float(vals[va]), sa) + self._side_value(float(vals[vb]), sb)
            except (KeyError, ValueError, TypeError):
                continue
            cost = 1.0 - float(d["edge_per_contract"])           # per contract, fees included
            rows.append({"klass": o["klass"], "divergent": bool(o["divergent"]),
                         "edge": float(d["edge_per_contract"]), "pnl": payout - cost,
                         "qty": float(d["qty"] or 0)})
        if not rows:
            return "_no complete observation matched a detection_\n"
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            a = out.setdefault(r["klass"], {"klass": r["klass"], "pairs": 0, "divergent": 0, "sum_edge": 0.0,
                                            "sum_pnl": 0.0, "worst": 0.0, "sum_usd": 0.0})
            a["pairs"] += 1
            a["divergent"] += int(r["divergent"])
            a["sum_edge"] += r["edge"]
            a["sum_pnl"] += r["pnl"]
            a["sum_usd"] += r["pnl"] * min(r["qty"], self.cfg.paper.max_notional_per_pair_usd)
            a["worst"] = min(a["worst"], r["pnl"])
        table = [{"klass": a["klass"], "pairs": a["pairs"], "divergent": a["divergent"],
                  "divergence_rate": round(a["divergent"] / a["pairs"], 3),
                  "mean_edge_at_entry": round(a["sum_edge"] / a["pairs"], 4),
                  "mean_pnl_per_contract": round(a["sum_pnl"] / a["pairs"], 4),
                  "worst_pair_per_contract": round(a["worst"], 4)} for a in out.values()]
        return (_md_table(table) +
                "\nPriced at the first opportunity seen on each pair. `mean_pnl_per_contract` is what a hedge "
                "held to settlement would have returned per contract, each leg paid by its own venue; a divergent "
                "pair pays 0 or 2, which is the tail the entry edge is being paid for.\n")

    def write(self) -> tuple[Path, str]:
        text, digest = self.build()
        out = self.cfg.path("reports", f"{self.day.strftime('%Y-%m-%d')}.md")
        out.write_text(text, encoding="utf-8")
        self.cfg.path("reports", "latest.md").write_text(text, encoding="utf-8")
        if self.cfg.report.git_push:
            self._push(out)
        return out, digest

    def _push(self, report: Path) -> None:
        gd = Path(self.cfg.report.git_dir)
        if not (gd / ".git").exists():
            log.warning("git_push enabled but %s is not a repository", gd)
            return
        dst = gd / "reports" / report.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(report.read_bytes())
        (gd / "reports" / "latest.md").write_bytes(report.read_bytes())
        cmds = [["git", "add", "reports"], ["git", "commit", "-m", f"report {report.stem}"], ["git", "push"]]
        for c in cmds:
            r = subprocess.run(c, cwd=gd, capture_output=True, text=True)
            if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
                log.warning("git %s failed: %s", c[1], (r.stderr or r.stdout)[:200])
                return
