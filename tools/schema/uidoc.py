"""Markdown reference of the app's view models (ui.*) and the messages they use.

usage: python uidoc.py pbandk.json > ../../docs/carv-app/ui-schema.md
"""
import json
import sys

KINDS = {  # pbandk FieldDescriptor.Type classes, matched against known data.* field types
    "ui7": "double", "vi7": "float", "yi7": "string", "si7": "bool", "wi7": "int32",
    "zi7": "int64", "xi7": "uint32", "pi7": "enum", "ri7": "message", "qi7": "map",
}


def kind(f: dict) -> str:
    if f.get("ref"):
        k = f["ref"]
    elif f.get("enum"):
        k = f"enum {f['enum']}"
    else:
        k = KINDS.get(f["type"], f["type"])
    return k + ("[]" if f.get("repeated") else "")


def main(path: str) -> None:
    pb = json.load(open(path))
    todo = sorted(t for t in pb if t.startswith("ui."))
    seen, order = set(), []
    while todo:  # ui.* plus every non-ui message they reference
        t = todo.pop(0)
        if t in seen or t not in pb:
            continue
        seen.add(t)
        order.append(t)
        todo += [f["ref"] for f in pb[t]["fields"] if f.get("ref") and f["ref"] not in seen]
    enums = {}
    print("# Carv-app: view models (`ui.*`) en gebruikte berichten\n")
    print("Gegenereerd door `tools/schema/uidoc.py` uit de pbandk-descriptors in de Kotlin-code van Carv 10.0.0")
    print("(Android). Dit zijn de berichten die de Rust-kern per scherm aan de UI geeft: ze beschrijven wat")
    print("de app toont. Tags zijn protobuf-veldnummers. Velden zonder descriptor-match ontbreken mogelijk.\n")
    for group in ("ui.", ""):
        print(f"## {'UI view models' if group else 'Gerefereerde berichten (data.*, geo.*, net.*)'}\n")
        for t in order:
            if group and not t.startswith(group) or not group and t.startswith("ui."):
                continue
            fs = pb[t]["fields"]
            print(f"### `{t}`\n")
            if not fs:
                print("_(geen velden gevonden; vaak een oneof-wrapper)_\n")
                continue
            print("| tag | veld | type |\n|---:|---|---|")
            for f in sorted(fs, key=lambda f: f["tag"]):
                print(f"| {f['tag']} | `{f['name']}` | {kind(f)} |")
                if f.get("enum_values"):
                    enums[f["enum"]] = f["enum_values"]
            print()
    print("## Enums\n")
    for name, vals in sorted(enums.items()):
        print(f"- **{name}**: " + ", ".join(f"{k} `{v}`" for k, v in sorted(vals.items(), key=lambda kv: int(kv[0]))))


if __name__ == "__main__":
    main(sys.argv[1])
