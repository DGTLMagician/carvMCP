"""Schema-driven protobuf decoder for Carv session files.

Carv ships no .proto files. `schema.json` was recovered from the Android app's
Rust library (libandroid.so, Carv 10.0.0) by emulating each message's prost
`merge_field`: for every tag prost reports the field name in its DecodeError,
and the helper it calls gives the type. Repeated-ness comes from the serde
Serialize impls (`Vec<T>`). Format per field:

    tag: [name, kind, message_type?, repeated?]

kind is one of double, float, int, bool, string, bytes, message, oneof, map.
A oneof field points at a pseudo message whose tags are the oneof variants.
Enum names (`__enums__`) come from the enums' Debug impls; which int fields use
them is not visible in the binary and is mapped by name in ENUM_FIELDS.
"""

from __future__ import annotations

import json
import struct
from functools import cache
from importlib import resources
from typing import Any


TURN_TYPE_FIELDS = (
    "turn_type",
    "turn_type_2507",
    "turn_type_2608",
    "turn_types_2507",
    "turn_types_2608",
    "turn_type_observation_2507",
    "turn_type_mlp_2608",
    "turn_type_mlp_smoothed_2608",
    "turn_type_final_2608",
)
ENUM_FIELDS = {
    **dict.fromkeys(TURN_TYPE_FIELDS, "TurnType"),
    "lift_type": "LiftType",
    "difficulty": "PisteDifficulty",
    "state": "Activity",
    "current_metric": "CoachingMetric",
}


@cache
def _raw() -> dict:
    return json.loads(resources.files(__package__).joinpath("schema.json").read_text())


@cache
def schema() -> dict[str, dict[int, list]]:
    return {t: {int(k): v for k, v in fs.items()} for t, fs in _raw().items() if not t.startswith("__")}


@cache
def enums() -> dict[str, dict[int, str]]:
    return {e: {int(k): v for k, v in vals.items()} for e, vals in _raw()["__enums__"].items()}


def _enum(name: str, vals: list) -> list:
    table = enums().get(ENUM_FIELDS.get(name, ""))
    return [table.get(v, v) for v in vals] if table else vals


def _varint(b: bytes, i: int) -> tuple[int, int]:
    shift = n = 0
    while True:
        c = b[i]
        i += 1
        n |= (c & 0x7F) << shift
        if not c & 0x80:
            return n, i
        shift += 7


def _fields(b: bytes):
    """Yield (tag, wire_type, value) with value int / bytes(8|4) / bytes."""
    i, n = 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        tag, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 1:
            v, i = b[i : i + 8], i + 8
        elif wt == 2:
            ln, i = _varint(b, i)
            v, i = b[i : i + ln], i + ln
        elif wt == 5:
            v, i = b[i : i + 4], i + 4
        else:
            raise ValueError(f"unsupported wire type {wt}")
        yield tag, wt, v


def _signed(v: int) -> int:
    return v - (1 << 64) if v >= 1 << 63 else v


def _scalar(kind: str, wt: int, v) -> list:
    """Decode a scalar; a length-delimited value for a numeric kind is a packed list."""
    if wt == 2 and kind in ("double", "float", "int", "bool"):
        if kind == "double":
            return [x[0] for x in struct.iter_unpack("<d", v)]
        if kind == "float":
            return [round(x[0], 6) for x in struct.iter_unpack("<f", v)]
        out, i = [], 0
        while i < len(v):
            n, i = _varint(v, i)
            out.append(bool(n) if kind == "bool" else _signed(n))
        return out
    if wt == 1:
        return [struct.unpack("<d", v)[0]]
    if wt == 5:
        return [round(struct.unpack("<f", v)[0], 6)]
    if wt == 0:
        return [bool(v) if kind == "bool" else _signed(v)]
    if kind == "string":
        return [v.decode(errors="replace")]
    return [v.hex()]


def decode(b: bytes, type_name: str, skip: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Decode a message into a dict keyed by field name. Fields named in `skip` are left out."""
    fs = schema().get(type_name, {})
    out: dict[str, Any] = {}
    reps: set[str] = set()
    for tag, wt, v in _fields(b):
        f = fs.get(tag)
        if f is None:
            name, vals = f"_{tag}", _scalar("bytes" if wt == 2 else "int", wt, v)
        else:
            name, kind = f[0], f[1]
            if name in skip:
                continue
            if len(f) > 3 and f[3]:
                reps.add(name)
            if kind == "message":
                vals = [decode(v, f[2], skip)] if wt == 2 else []
            elif kind == "oneof":
                variant = schema().get(f[2], {}).get(tag)
                if variant and wt == 2:
                    name = variant[0]
                    vals = [decode(v, variant[2], skip)] if variant[1] == "message" else _scalar(variant[1], wt, v)
                else:
                    vals = _scalar("bytes", wt, v)
            elif kind == "map":
                e = dict(_entry(v))
                vals = [{"key": e.get(1), "value": e.get(2)}]
            else:
                vals = _scalar(kind, wt, v)
                if kind == "int" and name in ENUM_FIELDS:
                    vals = _enum(name, vals)
                if wt == 2 and kind in ("double", "float", "int", "bool"):
                    reps.add(name)
        if name in out:
            prev = out[name]
            out[name] = (prev if isinstance(prev, list) and name in reps else [prev]) + vals
            reps.add(name)
        else:
            out[name] = vals if name in reps else (vals[0] if len(vals) == 1 else vals)
    return out


def _entry(b: bytes):
    for tag, wt, v in _fields(b):
        yield tag, (_signed(v) if wt == 0 else _scalar("bytes", wt, v)[0])
