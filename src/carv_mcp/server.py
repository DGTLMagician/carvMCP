"""MCP server exposing Carv ski data (sessions, runs, turns, Ski:IQ, technique metrics)."""

from __future__ import annotations

import functools
from collections import Counter, defaultdict
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from . import store
from .carv import CarvError
from .decode import ENUM_FIELDS, enums, schema
from .store import IQ_TYPES, mean, offset, pick, rnd, runs

INSTRUCTIONS = """\
Carv is a digital ski coach: sensors under the boot insoles record every turn.
This server reads the user's own Carv sessions (one session = one ski day).

Start with list_sessions to find session ids and dates; tools that take a
`session` also accept a date (YYYY-MM-DD) or "latest". Drill down with
get_session -> get_run -> get_turns. Use metric_trend and season_summary for
progress over time, and explore_session_data / describe_message to reach any
other field Carv stores (sensor calibration, sub-segments, GPS path, weather,
equipment, coaching state...).

Units: speeds in km/h where the key says so (otherwise m/s), distances and
altitudes in m, durations in s, times local to the resort. Ski:IQ is Carv's
skill score (~100 = average Carv skier); `model` is the Ski:IQ generation.
Technique metrics under `display` / `display_metrics` are Carv's normalised
scores (mostly 0..1, higher is better) except tempo, turn_speed (m/s),
turn_radius and turn_corridor (m). `iq_impact` shows how many Ski:IQ points a
metric contributes.
"""

mcp = MCPServer("carv", instructions=INSTRUCTIONS)
RO = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def tool(fn):
    """Register a read-only tool; expected failures reach Claude with their message."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, IndexError, CarvError) as e:
            raise ToolError(str(e)) from e

    return mcp.tool(annotations=RO)(wrapper)

TECHNIQUE_GROUPS = ("display_metrics_25_to_26", "additional_metrics_25_to_26", "iq_impact_26_to_27")


def _technique(m: dict) -> dict:
    out = {}
    for g in TECHNIQUE_GROUPS:
        if m.get(g):
            out[g.split("_25")[0].split("_26")[0]] = m[g]
    return out


@tool
def list_sessions(since: str | None = None, until: str | None = None, refresh: bool = False) -> dict[str, Any]:
    """List Carv ski sessions (one per ski day) with headline stats and Ski:IQ.

    Args:
        since: only sessions on or after this date (YYYY-MM-DD)
        until: only sessions on or before this date (YYYY-MM-DD)
        refresh: ask Carv for new sessions instead of using the cached list
    """
    rows = [
        s for s in store.index(refresh)
        if (not since or s.get("date", "") >= since) and (not until or s.get("date", "") <= until)
    ]
    return {"count": len(rows), "sessions": rows}


@tool
def get_session(session: str) -> dict[str, Any]:
    """One ski day in detail: totals, Ski:IQ, every run and lift ride, skier profile and equipment.

    Args:
        session: session id, a date (YYYY-MM-DD) or "latest"
    """
    sid = store.resolve(session)
    s = store.light_session(sid)
    off = offset(s)
    boot_room = s.get("boot_room") or {}
    cfg = s.get("config") or {}
    out = {
        "summary": store.session_summary(sid, s),
        "runs": [store.run_summary(r, off, i) for i, r in enumerate(runs(s))],
        "lifts": [store.lift_summary(lf, off) for lf in store.lifts(s)],
        "skier": boot_room.get("skier"),
        "equipment": {k: boot_room.get(k) for k in ("boots", "skis") if boot_room.get(k)},
        "app_version": cfg.get("app_version"),
        "weather": s.get("recorded_weather") or s.get("weather"),
    }
    return rnd({k: v for k, v in out.items() if v})


@tool
def get_run(session: str, run: int) -> dict[str, Any]:
    """Everything Carv measured for one run (descent): stats, Ski:IQ per turn type, all technique
    metrics, snow/terrain conditions, sub-segments, and per-metric averages over its turns.

    Args:
        session: session id, a date (YYYY-MM-DD) or "latest"
        run: run number (0-based, as in get_session)
    """
    sid = store.resolve(session)
    s = store.session(sid)
    rs, off = runs(s), offset(s)
    if not 0 <= run < len(rs):
        raise ValueError(f"run {run} bestaat niet; deze sessie heeft {len(rs)} afdalingen (0..{len(rs) - 1})")
    r = rs[run]
    m = r.get("metrics", {})
    turns = [t for t, ri in zip(s.get("turns", []), store.turn_runs(s)) if ri == run]
    stats: dict[str, dict] = defaultdict(dict)
    for g in store.TURN_KEYS:
        keys = {k for t in turns for k in (t.get(g) or {})}
        for k in sorted(keys):
            vals = [(t.get(g) or {}).get(k) for t in turns]
            vals = [v for v in vals if isinstance(v, (int, float))]
            if vals:
                stats[g.split("_25")[0].split("_26")[0]][k] = {"mean": mean(vals), "min": min(vals), "max": max(vals)}
    scores = {
        k: r[k] for k in r
        if k.endswith(("_score", "_avg")) or k in ("cop_y_score", "skiing_effort")
    }
    subs = r.get("sub_segments_26") or r.get("sub_segments") or []
    out = {
        "summary": store.run_summary(r, off, run),
        "technique": _technique(m),
        "turn_metric_stats": stats,
        "segment_scores": scores,
        "skiing_conditions": r.get("skiing_conditions"),
        "terrain_detection": m.get("terrain_detection"),
        "sub_segments": [{k: v for k, v in x.items() if k not in ("turn_indexes", "technique")} for x in subs],
        "route": r.get("route"),
        "turn_count_by_type": dict(Counter(t.get("turn_type_final_2608") or t.get("turn_type_2507") for t in turns)),
    }
    return rnd({k: v for k, v in out.items() if v})


@tool
def get_turns(session: str, run: int | None = None, offset: int = 0, limit: int = 40) -> dict[str, Any]:
    """Per-turn measurements: turn type, Ski:IQ per turn, radius, speed, and every technique metric
    (edge angle, edge build, edging similarity, early edging, g-force, weight release, turn shape,
    parallel skis, rotation, balance, ...) plus how much each metric adds to Ski:IQ.

    Args:
        session: session id, a date (YYYY-MM-DD) or "latest"
        run: only turns of this run (0-based); all turns of the day when omitted
        offset: skip this many turns (paging)
        limit: max turns to return (max 200)
    """
    sid = store.resolve(session)
    s = store.session(sid)
    off = store.offset(s)
    rows = [
        store.turn_row(t, i, ri, off)
        for i, (t, ri) in enumerate(zip(s.get("turns", []), store.turn_runs(s)))
        if run is None or ri == run
    ]
    limit = max(1, min(limit, 200))
    return rnd({"total": len(rows), "offset": offset, "turns": rows[offset : offset + limit]})


def _session_metric(sid: str, metric: str) -> list[tuple[str, int | None, Any]]:
    """(date, run, value) for a metric: per session, plus per run for run-level metrics."""
    s = store.light_session(sid)
    summ = store.session_summary(sid, s)
    date = summ.get("date")
    if metric in summ and not isinstance(summ[metric], (dict, list)):
        return [(date, None, summ[metric])]
    if metric.startswith("ski_iq"):
        iq = summ.get("ski_iq", {})
        part = metric.split(".", 1)[1] if "." in metric else "overall"
        val = iq.get("by_turn_type", {}).get(part) if part in IQ_TYPES else iq.get(part)
        return [(date, None, val)]
    # Technique metric: per run, and a turn-weighted mean for the session.
    rows = []
    for i, r in enumerate(runs(s)):
        m = r.get("metrics", {})
        for g in TECHNIQUE_GROUPS:
            v = (m.get(g) or {}).get(metric)
            if v is not None:
                rows.append((date, i, v, m.get("no_of_turns") or 1))
                break
        else:
            v = pick(m, metric)
            if isinstance(v, (int, float)):
                rows.append((date, i, v, m.get("no_of_turns") or 1))
    if not rows:
        return []
    total = sum(w for *_, w in rows)
    return [(date, None, sum(v * w for _, _, v, w in rows) / total)] + [(d, i, v) for d, i, v, _ in rows]


@tool
def metric_trend(metric: str, since: str | None = None, until: str | None = None, per_run: bool = False) -> dict[str, Any]:
    """How a metric develops over time, one value per ski day (or per run).

    Args:
        metric: a list_sessions field (e.g. max_speed_kmh, descent_m, skiing_distance_km, turns, runs),
            "ski_iq" or "ski_iq.<carving|short|mogul|powder|parallel|snowplough>", or a technique metric
            name such as edge_angle, early_edging, edge_build, edging_similarity, edging_smoothness,
            parallel_skis, g_force, weight_release, turn_closure, rotation_rate, turn_shape_25_short_carving,
            turn_radius, turn_corridor, early_forward_movement, mid_turn_balance, tempo, turn_speed
            (technique metrics are turn-weighted means over the day's runs)
        since: from this date (YYYY-MM-DD)
        until: up to this date (YYYY-MM-DD)
        per_run: also return the value of every run
    """
    points = []
    for summ in store.index():
        d = summ.get("date", "")
        if (since and d < since) or (until and d > until):
            continue
        for date, run, v in _session_metric(summ["id"], metric):
            if v is None or (run is not None and not per_run):
                continue
            p = {"date": date, "session": summ["id"], "value": v}
            if run is not None:
                p["run"] = run
            points.append(p)
    vals = [p["value"] for p in points if "run" not in p and isinstance(p["value"], (int, float))]
    out: dict[str, Any] = {"metric": metric, "points": points}
    if vals:
        out["stats"] = {"days": len(vals), "first": vals[0], "last": vals[-1], "min": min(vals), "max": max(vals), "mean": mean(vals)}
    elif not points:
        out["note"] = "metric niet gevonden; zie de beschrijving van deze tool voor geldige namen"
    return rnd(out)


@tool
def season_summary(season: str | None = None) -> dict[str, Any]:
    """Totals and bests per ski season (July-June), e.g. "2025/26". All seasons when omitted.

    Args:
        season: season label like "2025/26"
    """
    by: dict[str, list[dict]] = defaultdict(list)
    for s in store.index():
        if s.get("date"):
            by[store.season_of(s["date"])].append(s)
    out = []
    for name, ss in sorted(by.items()):
        if season and name != season:
            continue
        iq = [(s["ski_iq"]["overall"], s["date"]) for s in ss if s.get("ski_iq", {}).get("overall")]
        fastest = max(ss, key=lambda s: s.get("max_speed_kmh") or 0)
        lifts = Counter(n for s in ss for n in s.get("lifts_used", []))
        out.append({
            "season": name,
            "days": len(ss),
            "first_day": ss[0]["date"],
            "last_day": ss[-1]["date"],
            "runs": sum(s.get("runs", 0) for s in ss),
            "turns": sum(s.get("turns") or 0 for s in ss),
            "skiing_hours": sum(s.get("skiing_duration_min", 0) for s in ss) / 60,
            "skiing_distance_km": sum(s.get("skiing_distance_km", 0) for s in ss),
            "descent_m": sum(s.get("descent_m") or 0 for s in ss),
            "max_speed_kmh": fastest.get("max_speed_kmh"),
            "max_speed_date": fastest["date"],
            "ski_iq_best": dict(zip(("value", "date"), max(iq))) if iq else None,
            "ski_iq_first": dict(zip(("value", "date"), iq[0])) if iq else None,
            "ski_iq_last": dict(zip(("value", "date"), iq[-1])) if iq else None,
            "ski_iq_mean": mean([v for v, _ in iq]),
            "most_used_lifts": [n for n, _ in lifts.most_common(5)],
        })
    return rnd({"seasons": out})


@tool
def explore_session_data(session: str, path: str = "", depth: int = 2, offset: int = 0, max_items: int = 20) -> dict[str, Any]:
    """Raw access to everything Carv stores for a session, as decoded from its data file.

    Navigate with a dotted path; list elements by index. Examples: "" (top level), "metrics",
    "runs.3.metrics" (run numbers as in get_session), "lifts.0", "runs.3.metrics.technique",
    "turns.10", "turns.10.left", "sections" (runs, lifts and segments in recorded order),
    "config", "boot_room", "battery", "states".

    Args:
        session: session id, a date (YYYY-MM-DD) or "latest"
        path: dotted path into the session
        depth: how many levels to expand below the path
        offset: first list element to show (when the path points at a list)
        max_items: max list elements to show
    """
    sid = store.resolve(session)
    s = store.session(sid)
    v: Any = {"runs": runs(s), "lifts": store.lifts(s), **s}
    for part in [p for p in path.split(".") if p]:
        if isinstance(v, list):
            if not part.lstrip("-").isdigit() or not -len(v) <= int(part) < len(v):
                raise ValueError(f"'{part}': geen geldige index; deze lijst heeft {len(v)} items")
            v = v[int(part)]
        elif isinstance(v, dict):
            if part not in v:
                raise ValueError(f"'{part}' niet gevonden; beschikbaar: {', '.join(v)}")
            v = v[part]
        else:
            raise ValueError(f"'{part}': waarde is geen object of lijst")
    if isinstance(v, list):
        total = len(v)
        v = v[offset:]
        return {"path": path, "total_items": total, "offset": offset, "value": store.describe(v, depth, max_items)}
    return {"path": path, "value": store.describe(v, depth, max_items)}


@tool
def describe_message(type_name: str = "data::Session") -> dict[str, Any]:
    """The fields Carv's data format defines for a message type (recovered from the Carv app),
    with protobuf tag, kind, nested type and enum values. Not every field is filled in every session.

    Args:
        type_name: e.g. data::Session, data::Segment (a run), data::Metrics, data::Turn, data::SkiIq26
    """
    fs = schema().get(type_name)
    if fs is None:
        close = [t for t in schema() if type_name.lower().split("::")[-1] in t.lower()]
        raise ValueError(f"onbekend type {type_name}; bedoelde je: {', '.join(close[:10]) or '-'}")
    fields = []
    for tag, f in sorted(fs.items()):
        e: dict[str, Any] = {"tag": tag, "name": f[0], "kind": f[1]}
        if len(f) > 2 and f[2]:
            e["type"] = f[2]
        if len(f) > 3 and f[3]:
            e["repeated"] = True
        if f[0] in ENUM_FIELDS:
            e["enum"] = enums().get(ENUM_FIELDS[f[0]])
        fields.append(e)
    return {"type": type_name, "fields": fields}


def main() -> None:
    mcp.run()
