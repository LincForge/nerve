"""MCP usage-stats reader — the analysis layer over ``~/.linc/mcp-calls.jsonl``.

The ``log_mcp_call`` writer (``observability.py``) is write-only: every nerve/
crucible/spectra tool call lands in the shared JSONL log, but nothing reads it
back (linc-index is the only LINC MCP with a usage-stats tool). This module is
the missing reader — it aggregates the JSONL into per-tool call counts, success
rates, and latency percentiles, flags dormant (registered-but-never-called)
tools, and counts test-fixture junk that leaked into the production log.

The log is shared across all three servers on a host, so this reader is
server-agnostic: pass ``server=`` to scope per-tool stats to one server, and
``registered_tools=`` (that server's tool registry) to compute its dormant set.

Pure stdlib + read-only — safe to copy verbatim into linc-crucible and
linc-spectra (only the thin MCP wrapper that supplies ``registered_tools``
differs per repo).
"""

from __future__ import annotations

import glob
import json
import time
from pathlib import Path
from typing import Any

from linc_nerve.core.observability import linc_home

# Tool/server names that only ever appear from test fixtures — never real
# traffic. Used to quarantine junk records (see imp 01606251: the observability
# test suite leaked these into the prod log when LINC_HOME wasn't isolated).
_JUNK_TOOL_SUBSTRINGS = ("dummy", "fake", "magicmock", "_bad")


def _default_log_path() -> Path:
    """Resolve the mcp-calls log path; honors LINC_HOME for tests."""
    return linc_home() / "mcp-calls.jsonl"


def _is_junk(rec: dict) -> bool:
    """A record is junk if it has no server, or names a test-fixture tool."""
    server = rec.get("server")
    tool = rec.get("tool")
    if not server or not tool:
        return True
    low = str(tool).lower()
    return any(sub in low for sub in _JUNK_TOOL_SUBSTRINGS)


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Nearest-rank percentile of a pre-sorted list (empty -> 0.0)."""
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, round(pct / 100.0 * (len(sorted_vals) - 1))))
    return sorted_vals[k]


def _iter_records(log_path: Path, include_rotated: bool) -> list[dict]:
    """Load JSONL records from the log (and rotated siblings if requested)."""
    paths: list[Path] = []
    if include_rotated:
        # Rotated files are siblings: mcp-calls.<epoch>.jsonl
        pattern = str(log_path.with_suffix("")) + "*.jsonl"
        paths = [Path(p) for p in sorted(glob.glob(pattern))]
    if log_path not in paths and log_path.exists():
        paths.append(log_path)
    records: list[dict] = []
    for p in paths:
        try:
            with p.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
    return records


def _cutoff_iso(since_hours: float | None) -> str | None:
    """ISO cutoff timestamp for a rolling window, or None for all-history."""
    if since_hours is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - since_hours * 3600))


def _accumulate(agg: dict[str, dict[str, Any]], rec: dict, ts: str) -> None:
    """Fold one real record into its per-tool aggregate entry."""
    entry = agg.setdefault(
        rec.get("tool"),
        {"server": rec.get("server"), "durations": [], "ok": 0, "total": 0, "last_seen": ""},
    )
    entry["total"] += 1
    entry["ok"] += 1 if rec.get("success") else 0
    dur = rec.get("duration_ms")
    if isinstance(dur, (int, float)):
        entry["durations"].append(float(dur))
    if ts > entry["last_seen"]:
        entry["last_seen"] = ts


def _aggregate_records(
    records: list[dict], cutoff_iso: str | None, server: str | None
) -> dict[str, Any]:
    """Partition records into real (per-tool aggregate) vs junk, in one pass."""
    junk_breakdown: dict[str, int] = {}
    agg: dict[str, dict[str, Any]] = {}
    real_total = 0
    real_ok = 0
    window_min: str | None = None
    window_max: str | None = None

    for rec in records:
        ts = rec.get("timestamp") or ""
        if cutoff_iso and ts and ts < cutoff_iso:
            continue
        if _is_junk(rec):
            key = f"{rec.get('server')}/{rec.get('tool')}"
            junk_breakdown[key] = junk_breakdown.get(key, 0) + 1
            continue
        if server is not None and rec.get("server") != server:
            continue
        if ts:
            window_min = ts if window_min is None or ts < window_min else window_min
            window_max = ts if window_max is None or ts > window_max else window_max
        real_total += 1
        real_ok += 1 if rec.get("success") else 0
        _accumulate(agg, rec, ts)

    return {
        "agg": agg,
        "junk_breakdown": junk_breakdown,
        "real_total": real_total,
        "real_ok": real_ok,
        "window_min": window_min,
        "window_max": window_max,
    }


def _build_per_tool(agg: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn the per-tool aggregate into a sorted, percentile-annotated list."""
    per_tool: list[dict[str, Any]] = []
    for tool, e in agg.items():
        durs = sorted(e["durations"])
        n = e["total"]
        per_tool.append(
            {
                "tool": tool,
                "server": e["server"],
                "calls": n,
                "success_pct": round(100.0 * e["ok"] / n, 1) if n else 0.0,
                "p50_ms": round(_percentile(durs, 50), 1),
                "p95_ms": round(_percentile(durs, 95), 1),
                "avg_ms": round(sum(durs) / len(durs), 1) if durs else 0.0,
                "last_seen": e["last_seen"],
            }
        )
    per_tool.sort(key=lambda t: t["calls"], reverse=True)
    return per_tool


def compute_usage_stats(
    log_path: Path | None = None,
    server: str | None = None,
    registered_tools: list[str] | set[str] | None = None,
    since_hours: float | None = None,
    include_rotated: bool = False,
    top_n: int = 50,
) -> dict[str, Any]:
    """Aggregate the mcp-calls JSONL into a usage-stats report.

    Args:
        log_path: JSONL log path (defaults to ``$LINC_HOME/mcp-calls.jsonl``).
        server: If set, per-tool stats cover only this server's records.
        registered_tools: This server's registered tool names — enables the
            ``dormant`` list (registered but 0 real calls). Omit to skip.
        since_hours: Only count records newer than this many hours (by the
            record's ``timestamp``). None = all history in the log.
        include_rotated: Also read rotated ``mcp-calls.<epoch>.jsonl`` siblings.
        top_n: Cap the ``per_tool`` list to the N busiest tools.

    Returns:
        A JSON-serializable dict: overall counts, ``per_tool`` (calls,
        success_pct, p50/p95/avg latency, last_seen), ``dormant``, and a
        ``junk`` breakdown.
    """
    if log_path is None:
        log_path = _default_log_path()
    records = _iter_records(log_path, include_rotated)

    # Partition into real vs junk (applying the time window), then shape output.
    parts = _aggregate_records(records, _cutoff_iso(since_hours), server)
    agg = parts["agg"]
    junk_breakdown = parts["junk_breakdown"]
    real_total = parts["real_total"]
    real_ok = parts["real_ok"]
    per_tool = _build_per_tool(agg)

    result: dict[str, Any] = {
        "log_path": str(log_path),
        "server": server,
        "window_hours": since_hours,
        "window": {"first": parts["window_min"], "last": parts["window_max"]},
        "total_calls": real_total,
        "junk_calls": sum(junk_breakdown.values()),
        "overall_success_pct": round(100.0 * real_ok / real_total, 1) if real_total else 0.0,
        "distinct_tools": len(agg),
        "per_tool": per_tool[:top_n],
        "junk": dict(sorted(junk_breakdown.items(), key=lambda kv: kv[1], reverse=True)),
    }

    if registered_tools is not None:
        used = set(agg.keys())
        dormant = sorted(t for t in registered_tools if t not in used)
        result["registered_count"] = len(set(registered_tools))
        result["dormant"] = dormant
        result["dormant_count"] = len(dormant)

    return result
