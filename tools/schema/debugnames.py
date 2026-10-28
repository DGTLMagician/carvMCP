"""Field names and Rust types of prost messages from their Debug impls.

Works for messages that are only ever encoded (most proto::ui view models and
proto::net requests), where schema.py's merge_field emulation has nothing to run.
Each `DebugStruct::field(name, len, value, vtable)` call gives the field name;
the vtable's fmt entry is the Debug impl of the field's type, whose symbol names
the type. No protobuf tags: Debug does not know them.

usage: objdump -d -C --no-show-raw-insn --start-address=.. --stop-address=.. libandroid.so > dbg.dis
       python debugnames.py dbg.dis 'proto::(ui|net)::' > debugnames.json
"""
import json
import re
import struct
import sys

import schema as S

FIELD = "<core::fmt::builders::DebugStruct>::field"
FN = re.compile(r"^[0-9a-f]+ <<(&?)(proto::[\w:]+) as core::fmt::Debug>::fmt>:$")


def type_of(vtable: int) -> str:
    try:
        fmt = struct.unpack("<Q", bytes(S.uc.mem_read(vtable + 0x18, 8)))[0]
    except Exception:
        return "?"
    name = S.containing(fmt)
    m = re.match(r"<(.*) as core::fmt::Debug>::fmt", name)
    t = m.group(1) if m else name
    t = re.sub(r"^&", "", t)
    t = re.sub(r"<<(proto::[\w:]+) as core::fmt::Debug>::fmt::ScalarWrapper", r"scalar<\1", t)
    return t.replace("alloc::vec::", "").replace("core::option::", "").replace("alloc::string::", "")


def main(path: str, pattern: str) -> dict:
    pat = re.compile(pattern)
    out: dict[str, list] = {}
    cur, regs, imm = None, {}, {}
    for line in open(path, errors="replace"):
        m = FN.match(line)
        if m:
            cur = m.group(2) if pat.match(m.group(2)) and not out.get(m.group(2)) else None
            if cur:
                out[cur] = []
            regs, imm = {}, {}
            continue
        if cur is None:
            continue
        m = re.match(r"\s*[0-9a-f]+:\s+(\S+)\s+(.*)", line)
        if not m:
            continue
        op, args = m.groups()
        p = [x.strip() for x in args.split(",")]
        if op == "adrp":
            v = re.search(r"0x([0-9a-f]+)", args)
            regs[p[0]] = int(v.group(1), 16)
        elif op == "add" and len(p) == 3 and p[1] in regs and p[2].startswith("#"):
            regs[p[0]] = regs[p[1]] + int(p[2][1:].split()[0], 0)
        elif op == "mov" and len(p) > 1:
            mm = re.match(r"#(0x[0-9a-f]+|\d+)", p[1])
            if mm:
                imm[p[0].replace("x", "w")] = int(mm.group(1), 0)
        elif op in ("bl", "b") and FIELD in args:
            if "x1" in regs and 0 < imm.get("w2", 0) < 80:
                name = bytes(S.uc.mem_read(regs["x1"], imm["w2"])).decode(errors="replace")
                out[cur].append([name, type_of(regs["x4"]) if "x4" in regs else "?"])
    return {k: v for k, v in out.items() if v}


if __name__ == "__main__":
    json.dump(main(sys.argv[1], sys.argv[2]), sys.stdout, indent=1)
