"""Enum value names from the Debug impls of proto enums (tail call to Formatter::write_str)."""
import json, re, struct
import schema as S
from unicorn import UcError
from unicorn.arm64_const import *

WS = set(S.by_name.get("<core::fmt::Formatter as core::fmt::Write>::write_str", []) + S.by_name.get("<core::fmt::Formatter>::write_str", []))
uc = S.uc
res = {}

def run(fn, ref, val):
    S.state.update(range=(0, 1 << 64))  # let schema's hook ignore everything
    lo, hi = fn, S.func_end(fn)
    got = []
    S.uc.mem_write(S.OBJ, struct.pack("<Qi", S.OBJ + 8, val) if ref else struct.pack("<i", val))
    def hook(uc, addr, size, _):
        if lo <= addr < hi: return
        if addr in WS:
            got.append(bytes(uc.mem_read(uc.reg_read(UC_ARM64_REG_X1), uc.reg_read(UC_ARM64_REG_X2))).decode(errors="replace"))
        uc.emu_stop()
    h = uc.hook_add(1 << 3, hook)  # UC_HOOK_BLOCK
    uc.reg_write(UC_ARM64_REG_SP, S.STACK); uc.reg_write(UC_ARM64_REG_X30, S.SENT)
    uc.reg_write(UC_ARM64_REG_X0, S.OBJ); uc.reg_write(UC_ARM64_REG_X1, S.OBJ + 0x1000)
    try: uc.emu_start(fn, S.SENT, count=500)
    except UcError: pass
    uc.hook_del(h)
    return got[0] if got else None

# disable the schema hook while probing enums
S.state["range"] = (0, 1 << 64)
import sys
for name, addrs in list(S.by_name.items()):
    m = re.fullmatch(r"<(&?)(proto::[\w:]+) as core::fmt::Debug>::fmt", name)
    if not m or m.group(2) in res: continue
    vals = {}
    for v in range(0, 64):
        s = run(addrs[0], bool(m.group(1)), v)
        if not s or not re.fullmatch(r"[A-Z][A-Za-z0-9_]*", s): 
            if v > 0: break
            continue
        vals[v] = s
    if len(vals) >= 2:
        res[m.group(2)] = vals
json.dump(res, open("enums.json", "w"), indent=1)
print(len(res))
for k in sorted(res):
    if k.startswith("proto::data::") and k.count("::") == 2: print(k, res[k])
