"""Session access and the summaries the MCP tools return.

Conventions in the decoded data (see decode.py):
- times are unix seconds (UTC); the session's `config.utc_offset` gives local time
- speeds m/s, distances and altitudes m, durations s
- many metrics exist in versions: `x_f64` (old double) next to `x` (float), and
  model generations such as `ski_iq_25` / `ski_iq_26`; the newest one is used
"""

from __future__ import annotations

import datetime as dt
import json
import re
import statistics
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Any

from .carv import CACHE_DIR, CarvClient
from .decode import decode

INDEX = CACHE_DIR / "index.json"
INDEX_VERSION = 1

# Heavy parts left out when only a summary is needed.
LIGHT = frozenset(
    {"turns", "path", "technique", "turn_context_metrics", "terrain_detection", "sub_segments", "sub_segments_26",
     "turn_indexes", "flows", "timing_info", "states", "left_tracker_motion_calibration",
     "right_tracker_motion_calibration", "airtimes", "debug_commands"}
)
IQ_TYPES = ("carving", "short", "mogul", "powder", "parallel", "snowplough")

_client: CarvClient | None = None
_index_lock = threading.Lock()


def client() -> CarvClient:
    global _client
    if _client is None:
        _client = CarvClient()
    return _client


@lru_cache(maxsize=3)
def session(session_id: str) -> dict:
    """The fully decoded session."""
    return decode(client().session_bytes(session_id), "data::Session")


def light_session(session_id: str) -> dict:
    return decode(client().session_bytes(session_id), "data::Session", LIGHT)


# --- helpers -----------------------------------------------------------------


def rnd(x: Any, n: int = 3) -> Any:
    if isinstance(x, float):
        return round(x, n)
    if isinstance(x, dict):
        return {k: rnd(v, n) for k, v in x.items()}
    if isinstance(x, list):
        return [rnd(v, n) for v in x]
    return x


def pick(m: dict, name: str) -> Any:
    """Float version of a metric, falling back to the old `_f64` one."""
    v = m.get(name)
    return v if v is not None else m.get(name + "_f64")


def latest(m: dict, prefix: str) -> tuple[Any, str | None]:
    """Newest generation of a versioned field, e.g. latest(m, "ski_iq_") -> (m["ski_iq_26"], "26")."""
    best, ver = None, None
    for k, v in m.items():
        g = re.fullmatch(re.escape(prefix) + r"(\d+)", k)
        if g and (ver is None or int(g.group(1)) > int(ver)):
            best, ver = v, g.group(1)
    return best, ver


def ski_iq(m: dict) -> dict:
    """Ski:IQ of a session, run or turn-type: overall value plus per turn type."""
    by_type, ver = latest(m, "ski_iq_")
    out: dict[str, Any] = {}
    if ver:
        gen = m.get(f"ski_iq_{ver}_generation") or {}
        if gen.get("value") is not None:
            out["overall"] = gen["value"]
            out["based_on"] = gen.get("turn_type")
        if isinstance(by_type, dict):
            out["by_turn_type"] = {t: by_type.get(f"{t}_iq_{ver}") for t in IQ_TYPES if by_type.get(f"{t}_iq_{ver}")}
        out["model"] = ver
    elif m.get("phone_iq_22_value"):
        out = {"overall": m["phone_iq_22_value"], "model": "22"}
    return out


def offset(s: dict) -> int:
    return int((s.get("config") or {}).get("utc_offset") or 0)


def local(ts: float | None, off: int) -> str | None:
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(ts, dt.timezone(dt.timedelta(seconds=off))).strftime("%Y-%m-%d %H:%M:%S")


def kmh(v: float | None) -> float | None:
    return None if v is None else v * 3.6


def runs(s: dict) -> list[dict]:
    return [x["run"] for x in s.get("sections", []) if "run" in x]


def lifts(s: dict) -> list[dict]:
    return [x["lift"] for x in s.get("sections", []) if "lift" in x]


def season_of(date: str) -> str:
    y, m = int(date[:4]), int(date[5:7])
    start = y if m >= 7 else y - 1
    return f"{start}/{str(start + 1)[2:]}"


# --- summaries ----------------------------------------------------------------


def run_summary(r: dict, off: int, i: int) -> dict:
    m = r.get("metrics", {})
    route = r.get("route") or {}
    types = Counter(latest(m, "turn_types_")[0] or [])
    out = {
        "run": i,
        "start": local(m.get("start_time"), off),
        "duration_s": pick(m, "duration"),
        "skiing_duration_s": pick(m, "skiing_duration"),
        "distance_m": pick(m, "skiing_distance"),
        "descent_m": pick(m, "descent"),
        "max_altitude_m": pick(m, "max_altitude"),
        "min_altitude_m": pick(m, "min_altitude"),
        "max_speed_kmh": kmh(pick(m, "max_speed")),
        "avg_speed_kmh": kmh(pick(m, "average_speed")),
        "turns": m.get("no_of_turns"),
        "turn_radius_m": pick(m, "turn_radius"),
        "ski_iq": ski_iq(m),
        "turn_types": dict(types.most_common()),
        "route": route.get("name"),
        "piste_difficulty": route.get("difficulty"),
        "skiing_effort": r.get("skiing_effort"),
    }
    return {k: v for k, v in out.items() if v not in (None, {}, [])}


def lift_summary(lf: dict, off: int) -> dict:
    m, info = lf.get("metrics", {}), lf.get("lift") or {}
    out = {
        "name": info.get("name"),
        "type": info.get("lift_type"),
        "start": local(m.get("start_time"), off),
        "duration_s": pick(m, "duration"),
        "ascent_m": pick(m, "ascent"),
        "top_altitude_m": pick(m, "max_altitude"),
    }
    return {k: v for k, v in out.items() if v is not None}


def session_summary(session_id: str, s: dict) -> dict:
    m, off = s.get("metrics", {}), offset(s)
    rs, ls = runs(s), lifts(s)
    names = Counter((lf.get("lift") or {}).get("name") for lf in ls)
    names.pop(None, None)
    routes = Counter((r.get("route") or {}).get("name") for r in rs)
    routes.pop(None, None)
    start = rs[0]["metrics"].get("start") if rs and rs[0].get("metrics") else None
    resort = next(((lf.get("lift") or {}).get("resort_uuid") for lf in ls if (lf.get("lift") or {}).get("resort_uuid")), None)
    out = {
        "id": session_id,
        "date": local(m.get("start_time"), off)[:10] if m.get("start_time") else None,
        "start": local(m.get("start_time"), off),
        "end": local(m.get("end_time"), off),
        "utc_offset_h": off / 3600,
        "duration_h": (pick(m, "duration") or 0) / 3600,
        "skiing_duration_min": (pick(m, "skiing_duration") or 0) / 60,
        "runs": len(rs),
        "lifts": len(ls),
        "turns": m.get("no_of_turns"),
        "skiing_distance_km": (pick(m, "skiing_distance") or 0) / 1000,
        "descent_m": pick(m, "descent"),
        "ascent_m": pick(m, "ascent"),
        "max_speed_kmh": kmh(pick(m, "max_speed")),
        "max_altitude_m": pick(m, "max_altitude"),
        "min_altitude_m": pick(m, "min_altitude"),
        "ski_iq": ski_iq(m),
        "lifts_used": [n for n, _ in names.most_common(6)],
        "routes": [n for n, _ in routes.most_common(6)],
        "resort_uuid": resort,
        "location": start,
    }
    return rnd({k: v for k, v in out.items() if v not in (None, {}, [])})


# --- index of all sessions --------------------------------------------------


def index(refresh: bool = False) -> list[dict]:
    """Summaries of all sessions, oldest first. Downloads and summarises new sessions."""
    with _index_lock:
        listing = client().list_sessions(refresh=refresh)
        cached = {}
        if INDEX.exists():
            data = json.loads(INDEX.read_text())
            if data.get("version") == INDEX_VERSION:
                cached = data["sessions"]
        todo = [sid for sid in listing if sid not in cached]

        def one(sid: str) -> tuple[str, dict]:
            return sid, session_summary(sid, light_session(sid))

        if todo:
            with ThreadPoolExecutor(8) as ex:
                for sid, summ in ex.map(one, todo):
                    cached[sid] = summ
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            INDEX.write_text(json.dumps({"version": INDEX_VERSION, "sessions": cached}))
        return sorted((cached[sid] for sid in listing if sid in cached), key=lambda x: x.get("start") or "")


def resolve(session: str) -> str:
    """Accept a session id, a date (YYYY-MM-DD, first session that day) or 'latest'."""
    if session == "latest":
        return index()[-1]["id"]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", session):
        for s in index():
            if s.get("date") == session:
                return s["id"]
        raise ValueError(f"geen sessie op {session}")
    return session.upper()


# --- turns --------------------------------------------------------------------

TURN_KEYS = ("display_25_to_26", "additional_25_to_26", "iq_impact_26_to_27")


def turn_runs(s: dict) -> list[int | None]:
    """Run number (0-based, session order) for each turn, matched on time."""
    spans = [((r.get("metrics") or {}).get("start_time"), (r.get("metrics") or {}).get("end_time")) for r in runs(s)]
    out = []
    for t in s.get("turns", []):
        ts = t.get("start_time")
        out.append(next((i for i, (a, b) in enumerate(spans) if a and b and ts and a - 1 <= ts <= b + 1), None))
    return out


def turn_row(t: dict, i: int, run: int | None, off: int) -> dict:
    _, ver = latest(t, "turn_iq_")
    row = {
        "turn": i,
        "run": run,
        "time": local(t.get("start_time"), off),
        # 0 (omitted, proto3 default) = left, 1 = right; checked against the GPS heading change.
        "direction": {0: "left", 1: "right"}.get(t.get("direction", 0), t.get("direction")),
        "turn_type": t.get("turn_type_final_2608") or t.get("turn_type_2507"),
        "duration_s": t.get("duration"),
        "turn_radius_m": t.get("turn_radius"),
        "speed_kmh": kmh(t.get("speed_average")),
        "altitude_m": t.get("altitude_m"),
    }
    if ver:
        row["turn_iq"] = t.get(f"turn_iq_{ver}")
        row["turn_iq_by_type"] = {x: t.get(f"{x}_turn_iq_{ver}") for x in IQ_TYPES if t.get(f"{x}_turn_iq_{ver}")}
    for k in TURN_KEYS:
        if t.get(k):
            row[k.rsplit("_", 3)[0] if k != "display_25_to_26" else "display"] = t[k]
    return {k: v for k, v in row.items() if v is not None}


def describe(v: Any, depth: int, max_items: int) -> Any:
    """Truncate a decoded structure for display: nested levels beyond `depth` become a short note."""
    if isinstance(v, dict):
        if depth <= 0:
            return f"{{{len(v)} velden: {', '.join(list(v)[:8])}{', …' if len(v) > 8 else ''}}}"
        return {k: describe(x, depth - 1, max_items) for k, x in v.items()}
    if isinstance(v, list):
        if depth <= 0 and v and isinstance(v[0], (dict, list)):
            return f"[{len(v)} items]"
        head = [describe(x, depth - 1, max_items) for x in v[:max_items]]
        if len(v) > max_items:
            head.append(f"… nog {len(v) - max_items} items (wijs de lijst aan met path en gebruik offset)")
        return head
    return rnd(v, 4)


def mean(xs: list) -> float | None:
    xs = [x for x in xs if isinstance(x, (int, float)) and not isinstance(x, bool)]
    return statistics.fmean(xs) if xs else None
