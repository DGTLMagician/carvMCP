"""Recover prost message schemas from libandroid.so by emulating merge_field.

For every tag, merge_field is called with an invalid wire type and an empty
buffer. Each known field then fails and prost pushes (struct, field name) onto
the DecodeError; unknown tags go to skip_field. Calls into typed helpers
(uint32::merge, message::merge<T>, ...) reveal the field type.
"""
import bisect, json, re, struct, sys
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_BLOCK, UcError
from unicorn.arm64_const import *

SO, SYMS = "libandroid.so", "syms.txt"
data = open(SO, "rb").read()

addrs, names, by_name = [], [], {}
for line in open(SYMS):
    p = line.rstrip("\n").split(" ", 2)
    if len(p) == 3 and p[0] and p[1] in "tT":
        a = int(p[0], 16)
        addrs.append(a); names.append(p[2])
        by_name.setdefault(p[2], []).append(a)
order = sorted(range(len(addrs)), key=lambda i: addrs[i])
addrs = [addrs[i] for i in order]; names = [names[i] for i in order]
sym_at = {}
for a, n in zip(addrs, names):
    sym_at.setdefault(a, n)

def func_end(a):
    i = bisect.bisect_right(addrs, a)
    while i < len(addrs) and addrs[i] == a:
        i += 1
    return addrs[i] if i < len(addrs) else a + 0x10000

uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
e_phoff = struct.unpack_from("<Q", data, 0x20)[0]
for i in range(struct.unpack_from("<H", data, 0x38)[0]):
    t, fl, off, va, pa, fs, ms, al = struct.unpack_from("<IIQQQQQQ", data, e_phoff + i * 56)
    if t != 1:
        continue
    start, end = va & ~0xFFF, (va + ms + 0xFFF) & ~0xFFF
    uc.mem_map(start, end - start)
    uc.mem_write(va, data[off:off + fs])

# Apply R_AARCH64_RELATIVE relocations so pointer tables (enum names, vtables) are valid.
e_shoff = struct.unpack_from("<Q", data, 0x28)[0]
for i in range(struct.unpack_from("<H", data, 0x3C)[0]):
    sh = struct.unpack_from("<IIQQQQIIQQ", data, e_shoff + i * 64)
    if sh[1] != 4:  # SHT_RELA
        continue
    off, size = sh[4], sh[5]
    buf = bytearray()
    for j in range(0, size, 24):
        r_off, r_info, r_add = struct.unpack_from("<QQq", data, off + j)
        if r_info & 0xFFFFFFFF == 1027:
            try:
                uc.mem_write(r_off, struct.pack("<Q", r_add & (1 << 64) - 1))
            except Exception:
                pass

STACK, OBJ, BUF, SENT = 0x7F000000, 0x70000000, 0x71000000, 0x72000000
uc.mem_map(STACK - 0x100000, 0x200000)
uc.mem_map(OBJ, 0x100000)
uc.mem_map(BUF, 0x1000)
uc.mem_map(SENT, 0x1000)

PUSH = by_name["<prost::error::DecodeError>::push"][0]
FROM = by_name["<prost::error::DecodeError as core::convert::From<prost::error::DecodeErrorKind>>::from"][0]
SKIPS = set(by_name["prost::encoding::skip_field::<&mut &[u8]>"])
FAKE_ERR = 0x73000000

state = {}

def rd_str(p, n):
    return bytes(uc.mem_read(p, n)).decode(errors="replace")

PASS = re.compile(r"prost::encoding::(varint::decode_varint|decode_key|check_wire_type|decode_length_delimiter)")

def containing(addr):
    i = bisect.bisect_right(addrs, addr) - 1
    return names[i] if i >= 0 else ""

def allowed(addr):
    lo, hi = state["range"]
    return lo <= addr < hi or bool(PASS.match(containing(addr)))

def hook_block(uc, addr, size, _):
    if allowed(addr):
        return
    lr = uc.reg_read(UC_ARM64_REG_X30)
    if addr == SENT:
        uc.emu_stop(); return
    if addr == PUSH:
        state["pushes"].append((rd_str(uc.reg_read(UC_ARM64_REG_X1), uc.reg_read(UC_ARM64_REG_X2)),
                                rd_str(uc.reg_read(UC_ARM64_REG_X3), uc.reg_read(UC_ARM64_REG_X4))))
    elif addr == FROM:
        kind = bytes(uc.mem_read(uc.reg_read(UC_ARM64_REG_X0), 16))
        if struct.unpack_from("<Q", kind)[0] == 0x8000000000000006:
            state["wire"].append(kind[9])
        uc.reg_write(UC_ARM64_REG_X0, FAKE_ERR)
    elif addr in SKIPS:
        state["skip"] = True
        uc.emu_stop(); return
    else:
        state["calls"].append(sym_at.get(addr, hex(addr)))
        uc.reg_write(UC_ARM64_REG_X0, FAKE_ERR)
    if lr == SENT or not allowed(lr):
        uc.emu_stop(); return
    uc.reg_write(UC_ARM64_REG_PC, lr)

uc.hook_add(UC_HOOK_BLOCK, hook_block)

def probe(fn, tag, oneof=False, wt=7, payload=b""):
    state.update(range=(fn, func_end(fn)), pushes=[], wire=[], calls=[], skip=False)
    uc.mem_write(OBJ, b"\0" * 0x100000)
    # buf: &mut B with B = &mut &[u8]: BUF -> BUF+0x10 -> (ptr, len)
    uc.mem_write(BUF, b"\0" * 0x100)
    uc.mem_write(BUF, struct.pack("<QQQQ", BUF + 0x10, 0, BUF + 0x40, len(payload)))
    uc.mem_write(BUF + 0x40, payload)
    uc.reg_write(UC_ARM64_REG_SP, STACK)
    uc.reg_write(UC_ARM64_REG_X30, SENT)
    # merge_field(&mut self, tag, wire_type, buf, ctx); Oneof::merge(&mut Option<Self>, tag, ...) alike
    regs = [OBJ, tag, wt, BUF, 100]
    for r, v in zip([UC_ARM64_REG_X0, UC_ARM64_REG_X1, UC_ARM64_REG_X2, UC_ARM64_REG_X3, UC_ARM64_REG_X4], regs):
        uc.reg_write(r, v)
    try:
        uc.emu_start(fn, SENT, count=20000)
    except UcError as e:
        state["calls"].append(f"!{e}")
    return dict(state)


WT = {0: "varint", 1: "fixed64", 2: "len", 5: "fixed32"}

def classify(calls, wire):
    for c in calls:
        m = re.match(r"prost::encoding::(\w+)::(merge_repeated|merge)(?:::<([^,>]+(?:<[^>]*>)?)|)", c)
        if m:
            kind, rep = m.group(1), m.group(2) == "merge_repeated"
            if kind in ("message", "group"):
                t = re.search(r"<(proto::[\w:]+)", c)
                return ("message", t.group(1) if t else c, rep)
            return (kind, None, rep)
        m = re.match(r"prost::encoding::merge_loop::<(proto::[\w:]+),", c)
        if m:
            return ("message", m.group(1), False)
        m = re.match(r"prost::encoding::merge_loop::<\(", c)
        if m:
            return ("map", c, False)
        m = re.match(r"prost::encoding::merge_loop::<", c)
        if m:
            return ("packed", c, True)
        m = re.match(r"<(proto::[\w:]+)>::merge::<", c)
        if m:
            return ("oneof", m.group(1), False)
        m = re.match(r"<(proto::[\w:]+) as prost::message::Message>::merge_field", c)
        if m:
            return ("message", m.group(1), False)
    if wire:
        return ({1: "double", 5: "float", 0: "varint", 2: "bytes"}.get(wire[0], f"wt{wire[0]}"), None, False)
    return ("?", "; ".join(calls[:3]), False)


def varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F; n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def probe_loop(fn, tag):
    """merge_loop(&mut T, buf, ctx): length-delimited body with one key of wire type 3 (start group)."""
    key = varint(tag << 3 | 3)
    state.update(range=(fn, func_end(fn)), pushes=[], wire=[], calls=[], skip=False)
    uc.mem_write(OBJ, b"\0" * 0x100000)
    payload = varint(len(key)) + key
    uc.mem_write(BUF, b"\0" * 0x100)
    uc.mem_write(BUF, struct.pack("<QQQQ", BUF + 0x10, 0, BUF + 0x40, len(payload)))
    uc.mem_write(BUF + 0x40, payload)
    uc.reg_write(UC_ARM64_REG_SP, STACK)
    uc.reg_write(UC_ARM64_REG_X30, SENT)
    for r, v in zip([UC_ARM64_REG_X0, UC_ARM64_REG_X1, UC_ARM64_REG_X2], [OBJ, BUF, 100]):
        uc.reg_write(r, v)
    try:
        uc.emu_start(fn, SENT, count=20000)
    except UcError as e:
        state["calls"].append(f"!{e}")
    st = dict(state)
    st["calls"] = [c for c in st["calls"] if "skip_field" not in c]
    if not st["pushes"]:
        st["skip"] = True
    return st


def schema(fn, max_tag, oneof=False, loop=False, start=1):
    out = {}
    for tag in range(start, max_tag + 1):
        s = probe_loop(fn, tag) if loop else probe(fn, tag, oneof)
        if s["skip"] or not s["pushes"] and not s["calls"] and not s["wire"]:
            continue
        if oneof and s["calls"] == ["core::panicking::panic_fmt"]:
            continue
        name = s["pushes"][-1][1] if s["pushes"] else None
        kind, typ, rep = classify(s["calls"], s["wire"])
        if kind == "bytes" and not loop:
            # Inlined singular message merge: feed a valid length-delimited payload
            # so the nested merge_field (or a bytes/string helper) gets called.
            s2 = probe(fn, tag, oneof, wt=2, payload=b"\x02\x08\x00")
            k2, t2, _ = classify(s2["calls"], [])
            if k2 != "?":
                kind, typ = k2, t2
            elif s2["calls"]:
                typ = "; ".join(s2["calls"][:3])
        out[tag] = {"name": name, "kind": kind, "type": typ, "repeated": rep, "calls": s["calls"][:4], "wire": s["wire"]}
    return out


def schema_one(fn, tag, mode):
    r = schema(fn, tag, mode == "oneof", mode == "loop", start=tag)
    if not r:
        return None
    f = r[tag]
    f.pop("calls", None) if f["kind"] != "?" else None
    return f


def find(name, oneof=False):
    """Returns (address, mode) with mode 'field', 'oneof' or 'loop'."""
    if oneof:
        return by_name.get(f"<{name}>::merge::<&mut &[u8]>", [None])[0], "oneof"
    a = by_name.get(f"<{name} as prost::message::Message>::merge_field::<&mut &[u8]>")
    if a:
        return a[0], "field"
    a = by_name.get(f"prost::encoding::merge_loop::<{name}, prost::encoding::message::merge<{name}, &mut &[u8]>::{{closure#0}}, &mut &[u8]>")
    return (a[0], "loop") if a else (None, None)


def _old_find(name, oneof=False):
    if oneof:
        key = f"<{name}>::merge::<&mut &[u8]>"
    else:
        key = f"<{name} as prost::message::Message>::merge_field::<&mut &[u8]>"
    return by_name.get(key, [None])[0]


if __name__ == "__main__":
    name = sys.argv[1]
    oneof = "--oneof" in sys.argv
    fn, mode = find(name, oneof)
    for tag, f in schema(fn, int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 400, oneof, mode == "loop").items():
        print(tag, f)
