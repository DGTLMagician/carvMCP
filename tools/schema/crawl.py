import json, sys, time
import schema as S

SKIPT = {"proto::data::flow::Flows", "proto::data::flow_section::Flow"}
def crawl(root, max_tag=1200, gap=100000):
    out, todo, seen = {}, [(root, False)], set()
    while todo:
        name, oneof = todo.pop()
        if (name, oneof) in seen or name in SKIPT:
            continue
        seen.add((name, oneof))
        fn, mode = S.find(name, oneof)
        if fn is None:
            out[name] = {"error": "no merge function"}; continue
        fields, last = {}, 0
        for tag in range(1, max_tag + 1):
            if tag - last > gap and tag > 64:
                break
            r = S.schema_one(fn, tag, mode)
            if r is None:
                continue
            last = tag
            fields[tag] = r
            if r["kind"] in ("message", "oneof") and r["type"] and r["type"].startswith("proto::"):
                todo.append((r["type"], r["kind"] == "oneof"))
        out[name] = {"mode": mode, "fields": fields}
        print(f"{name}: {len(fields)} fields", file=sys.stderr)
    return out

if __name__ == "__main__":
    t = time.time()
    res = crawl(sys.argv[1] if len(sys.argv) > 1 else "proto::data::Session")
    json.dump(res, open("schema.json", "w"), indent=1)
    print(len(res), "types", round(time.time() - t), "s", file=sys.stderr)
