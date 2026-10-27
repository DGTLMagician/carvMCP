"""Field name -> Rust type, from serde Serialize impls (serialize_entry::<str, T> calls)."""
import json, re, subprocess, struct, sys
import schema as S

def rd(va, n):
    return bytes(S.uc.mem_read(va, n))

def fields(tname):
    key = f"<{tname} as serde_core::ser::Serialize>::serialize::<&mut serde_json::ser::Serializer<&mut bytes::buf::writer::Writer<bytes::bytes_mut::BytesMut>>>"
    a = S.by_name.get(key)
    if not a:
        return None
    a = a[0]
    out = subprocess.run(["objdump", "-d", "-C", "--no-show-raw-insn", f"--start-address={a:#x}", f"--stop-address={S.func_end(a):#x}", S.SO], capture_output=True, text=True).stdout
    regs, imm, res = {}, {}, {}
    for line in out.splitlines():
        m = re.match(r"\s*[0-9a-f]+:\s+(\S+)\s+(.*)", line)
        if not m:
            continue
        op, args = m.groups()
        if op == "adrp":
            r = args.split(",")[0].strip(); v = re.search(r"0x([0-9a-f]+)", args)
            regs[r] = int(v.group(1), 16)
        elif op == "add":
            p = [x.strip() for x in args.split(",")]
            if len(p) == 3 and p[1] in regs and p[2].startswith("#"):
                regs[p[0]] = regs[p[1]] + int(p[2][1:].split()[0], 0)
        elif op == "mov":
            p = [x.strip() for x in args.split(",")]
            mm = re.match(r"#(0x[0-9a-f]+|\d+)", p[1]) if len(p) > 1 else None
            if mm:
                imm[p[0].replace("x", "w")] = int(mm.group(1), 0)
        elif op == "bl" and "serialize_entry::<str, " in args:
            t = args.split("serialize_entry::<str, ", 1)[1].rsplit(">>", 1)[0]
            if "x1" in regs and "w2" in imm and 0 < imm["w2"] < 80:
                res[rd(regs["x1"], imm["w2"]).decode(errors="replace")] = t
    return res

if __name__ == "__main__":
    sch = json.load(open("schema.json"))
    allt = {}
    for t in sch:
        f = fields(t)
        if f:
            allt[t] = f
    json.dump(allt, open("sertypes.json", "w"), indent=1)
    print(len(allt), "of", len(sch))
    print(allt.get("proto::data::Segment"))
