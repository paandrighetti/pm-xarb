"""Append-only JSONL writers with hourly rotation and deferred gzip. One writer per stream.
Snapshots are written only when a pair's top-N ladders changed (record_mode: changes), which is
what keeps a 3-second poll on a few hundred pairs under a few hundred megabytes a day."""
from __future__ import annotations

import gzip
import json
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class JsonlWriter:
    """Rotating writer: <root>/<YYYY-MM-DD>/<HH>.jsonl, flushed on every write."""

    def __init__(self, root: Path, rotate_minutes: int = 60):
        self.root = root
        self.rotate_s = max(1, rotate_minutes) * 60
        self._fh = None
        self._path: Path | None = None
        self._bucket: int | None = None

    def _target(self, ts: float) -> tuple[int, Path]:
        bucket = int(ts // self.rotate_s)
        d = datetime.fromtimestamp(bucket * self.rotate_s, tz=timezone.utc)
        return bucket, self.root / d.strftime("%Y-%m-%d") / (d.strftime("%H%M") + ".jsonl")

    def write(self, record: dict[str, Any], ts: float | None = None) -> None:
        ts = ts or time.time()
        bucket, path = self._target(ts)
        if bucket != self._bucket or self._fh is None:
            self.close()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8")
            self._path, self._bucket = path, bucket
        self._fh.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    def compress_closed(self, older_than_s: float) -> int:
        """gzip .jsonl files not written to for `older_than_s` seconds, except the live one."""
        n = 0
        now = time.time()
        for p in self.root.glob("*/*.jsonl"):
            if p == self._path:
                continue
            try:
                if now - p.stat().st_mtime < older_than_s:
                    continue
                gz = p.with_suffix(".jsonl.gz")
                with open(p, "rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
                    shutil.copyfileobj(src, dst)
                p.unlink()
                n += 1
            except OSError as exc:
                log.warning("compress %s failed: %s", p, exc)
        return n


class Blotter:
    """Flat append-only streams under <data>/blotter/<name>.jsonl. Never rewritten."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def append(self, stream: str, record: dict[str, Any]) -> None:
        with open(self.root / f"{stream}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
    tmp.replace(path)
