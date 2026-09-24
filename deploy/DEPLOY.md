# Deploying pm-xarb on the existing Hetzner CX22

Assumptions: the VPS already runs Docker and Docker Compose (it runs updown-desk), you connect
from Windows PowerShell with an SSH key, and the VPS address is `203.0.113.10` (replace it in
every command below; the commands are otherwise complete).

## 1. Copy the project to the VPS (PowerShell, on your PC)

```powershell
cd C:\Users\PA\Downloads
Expand-Archive -Path .\pm-xarb.zip -DestinationPath .\pm-xarb-src -Force
scp -r .\pm-xarb-src\pm-xarb root@203.0.113.10:/opt/pm-xarb
ssh root@203.0.113.10
```

Everything below runs on the VPS (bash).

## 2. Configure

```bash
cd /opt/pm-xarb
cp .env.example .env
nano .env
```

Fill `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (same bot as updown-desk is fine). Save with
Ctrl+O, Enter, Ctrl+X.

## 3. Build and run the diagnostics before anything else

```bash
docker compose build
docker compose run --rm desk pmx doctor
```

`doctor` calls every endpoint once and prints parsed samples. Two things can fail here and
both are configuration, not code:

* A Kalshi series ticker in `config.yaml` returns 404. Find the right one and edit the file:

```bash
docker compose run --rm desk pmx series --grep BTC
docker compose run --rm desk pmx series --grep fed
docker compose run --rm desk pmx series --grep NFL
nano config.yaml
```

* Kalshi is unreachable from the VPS (HTTP 403 or a connection error on every call). Kalshi
  serves market data publicly, but if it geo-blocks the datacenter the desk cannot run there.
  Tell me the exact error line and we route around it.

The last line of `doctor` must read `RESULT: OK`.

## 4. Build the universe once by hand and read it

```bash
docker compose run --rm desk pmx universe
less data/state/pairs.json
less data/universe/unmatched_$(date -u +%Y%m%d).json
```

The unmatched file lists every canonical key each venue produced that found no partner, with
titles. That is the file to read when coverage looks thin: it tells you whether the parser
missed the market or whether the other venue simply does not list it.

If a pair is wrong, add it to `config/pair_overrides.yaml` (by `pair_id` or `key_regex`) and
rerun `pmx universe`.

## 5. Start the desk

```bash
docker compose up -d
docker compose logs -f --tail 100
```

Ctrl+C leaves it running. Health checks:

```bash
docker compose exec desk pmx status
docker stats --no-stream
du -sh data/snapshots data/blotter
```

Expected: a few hundred megabytes of RAM, a poll every 3 to 5 seconds, `books_k` and `books_p`
close to the number of pairs in `data/blotter/polls.jsonl`.

## 6. Reports

The daily report is written at 06:00 UTC to `data/reports/YYYY-MM-DD.md` and `latest.md`, and a
digest goes to Telegram. To build one on demand:

```bash
docker compose exec desk pmx report
docker compose exec desk pmx report --date 2026-09-19
cat data/reports/latest.md
```

The historical screen (upper bound, not executable) runs Mondays 07:00 UTC or on demand:

```bash
docker compose exec desk pmx history
cat data/history/screen_$(date -u +%Y-%m-%d).md
```

## 7. Optional: push reports to GitHub

Same pattern as updown-desk: a private repository with a deploy key that has write access.

```bash
mkdir -p data && cd data
git clone git@github.com:paandrighetti/pm-xarb-reports.git reports_repo
cd reports_repo && git config user.email "bot@pm-xarb" && git config user.name "pm-xarb" && cd /opt/pm-xarb
```

Mount the key in `docker-compose.yml` (add under `volumes:` the line
`- /root/.ssh:/root/.ssh:ro`), set `report.git_push: true` in `config.yaml`, then
`docker compose up -d --force-recreate`.

## 8. Updating the code later

```powershell
scp -r .\pm-xarb-src\pm-xarb\src root@203.0.113.10:/opt/pm-xarb/
scp .\pm-xarb-src\pm-xarb\config.yaml root@203.0.113.10:/opt/pm-xarb/config.yaml
```

```bash
cd /opt/pm-xarb && docker compose build && docker compose up -d
```

State (`data/state/paper_state.json`), the blotter and the snapshots survive rebuilds; only the
image changes.

## 9. Stopping

```bash
docker compose down
```

The container flushes the blotter and saves the paper state on SIGTERM (30 s grace period).
