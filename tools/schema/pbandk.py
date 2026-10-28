"""Message schemas from the Kotlin side of the Carv app (pbandk descriptors in jadx output).

The Kotlin shell decodes the view models the Rust core sends it (proto ui.*) and
some data.*/net.* messages with pbandk. R8 renamed the classes, but each
descriptor keeps the full message name, every field's name and tag, and a field
type object. Enum classes keep their value names in toString/name fields.

usage: python pbandk.py <jadx sources dir> > pbandk.json
"""
import json
import re
import sys
from pathlib import Path

ZDC = re.compile(r'new zdc\("([\w.]+)", apf\.a\.b\((\w+)\.class\)')
# The getter is either a reference object or an anonymous class; only the tail is stable.
FIELD = re.compile(r'[)}], "(\w+)", (\d+), (new \w+\([^;]*?\)), \w+\.\w+, \d+\)\);')
# Type objects seen in field descriptors (obfuscated class -> pbandk FieldDescriptor.Type).
ENUM = "pi7"
REPEATED = re.compile(r"^new cj7\((.*), (true|false)\)$")
REF = re.compile(r"^new (\w+)\((\w+?)(?:Var|\.\w+)(?:, .*)?\)$")


def main(root: str) -> dict:
    files = list(Path(root).rglob("*.java"))
    cls2name: dict[str, str] = {}
    msgs: dict[str, dict] = {}
    for f in files:
        text = f.read_text(errors="replace")
        if "pbandk" not in text:
            continue
        pos = 0
        for m in ZDC.finditer(text):
            cls2name[m.group(2)] = m.group(1)
            # fields are added right before the descriptor that uses them
            chunk = text[pos : m.start()]
            fields = []
            for fm in FIELD.finditer(chunk):
                fields.append({"name": fm.group(1), "tag": int(fm.group(2)), "type": fm.group(3)})
            msgs[m.group(1)] = {"class": m.group(2), "fields": fields}
            pos = m.end()
    # resolve message/enum references to proto names
    for msg in msgs.values():
        for fd in msg["fields"]:
            t = fd["type"]
            rep = REPEATED.match(t)
            if rep:
                fd["repeated"] = True
                t = rep.group(1)
            ref = REF.match(t)
            if ref and ref.group(2) in cls2name:
                fd["ref"] = cls2name[ref.group(2)]
            elif ref and ref.group(1) == ENUM:
                fd["enum_class"] = ref.group(2)
            fd["type"] = t.split("(")[0].removeprefix("new ")
    enums = enum_values(files)
    for msg in msgs.values():
        for fd in msg["fields"]:
            e = enums.get(fd.get("enum_class"))
            if e:
                fd["enum"], fd["enum_values"] = e["name"], e["values"]
    return msgs


def enum_values(files) -> dict:
    """Enum class -> {name, values}: base classes name themselves in toString,
    subclasses pass (value, "NAME") to the base constructor."""
    base, values = {}, {}
    for f in files:
        text = f.read_text(errors="replace")
        m = re.search(r"public abstract class (\w+) implements jdc", text)
        if m:
            n = re.search(r'"([\w.]+)\.", str, "\(value="', text)
            if n:
                base[m.group(1)] = n.group(1)
        sub = re.search(r"class (\w+) extends (\w+) \{", text)
        if sub:
            for v in re.finditer(rf"new {sub.group(1)}\((-?\d+), \"(\w+)\"\)", text):
                values.setdefault(sub.group(2), {})[int(v.group(1))] = v.group(2)
    return {c: {"name": n, "values": dict(sorted(values.get(c, {}).items()))} for c, n in base.items()}


if __name__ == "__main__":
    json.dump(main(sys.argv[1]), sys.stdout, indent=1)
