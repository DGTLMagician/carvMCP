import argparse
import getpass
import json
import os
import shutil
import sys
from pathlib import Path

from .carv import CARV_STATE, SYNC_STATE, CarvClient, CarvError

CLAUDE_CONFIG = {
    "darwin": Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
    "win32": Path(os.environ.get("APPDATA", "")) / "Claude" / "claude_desktop_config.json",
}.get(sys.platform, Path.home() / ".config" / "Claude" / "claude_desktop_config.json")


def cmd_serve(args) -> None:
    from .server import main

    main()


def cmd_login(args) -> None:
    email = args.email or input("Carv e-mail: ")
    password = getpass.getpass("Carv wachtwoord: ")
    c = CarvClient()
    user = c.login(email, password)
    store = args.store_password
    if store is None:
        a = input("Wachtwoord bewaren (bestand met rechten 0600) zodat de server zelf opnieuw kan inloggen? [J/n] ")
        store = not a.strip().lower().startswith("n")
    c.save(store_password=store)
    print(f"Ingelogd bij Carv als {user['first_name']} {user['last_name']}. Opgeslagen in {CARV_STATE}.")


def cmd_status(args) -> None:
    from . import store

    src = CARV_STATE if CARV_STATE.exists() else SYNC_STATE if SYNC_STATE.exists() else None
    print(f"Login: {src or 'geen (draai `carv-mcp login`)'}")
    if not src and not os.environ.get("CARV_EMAIL"):
        return
    ix = store.index(refresh=True)
    print(f"Sessies: {len(ix)}" + (f", {ix[0]['date']} t/m {ix[-1]['date']}" if ix else ""))


def _command() -> list[str]:
    exe = shutil.which("carv-mcp")
    if exe:
        return [exe]
    return [sys.executable, "-m", "carv_mcp"]


def cmd_install_claude(args) -> None:
    path = Path(args.config) if args.config else CLAUDE_CONFIG
    cfg = json.loads(path.read_text()) if path.exists() else {}
    cmd = _command()
    entry = {"command": cmd[0], "args": cmd[1:]}
    if cfg.get("mcpServers", {}).get("carv") == entry:
        print(f"Staat al in {path}.")
        return
    if path.exists():
        shutil.copy2(path, path.with_suffix(".json.bak"))
    cfg.setdefault("mcpServers", {})["carv"] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"Toegevoegd aan {path}:\n{json.dumps({'carv': entry}, indent=2)}\nHerstart Claude Desktop.")


def main() -> None:
    p = argparse.ArgumentParser(prog="carv-mcp", description="MCP server voor je Carv ski-data")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="MCP server starten over stdio (standaard)").set_defaults(func=cmd_serve)
    lp = sub.add_parser("login", help="inloggen bij Carv")
    lp.add_argument("--email")
    lp.add_argument("--store-password", action=argparse.BooleanOptionalAction, default=None)
    lp.set_defaults(func=cmd_login)
    sub.add_parser("status", help="login en aantal sessies tonen").set_defaults(func=cmd_status)
    ip = sub.add_parser("install-claude", help="server toevoegen aan de Claude Desktop-config")
    ip.add_argument("--config", help=f"pad naar claude_desktop_config.json (standaard {CLAUDE_CONFIG})")
    ip.set_defaults(func=cmd_install_claude)
    args = p.parse_args()
    try:
        (args.func if args.cmd else cmd_serve)(args)
    except CarvError as e:
        sys.exit(f"Fout: {e}")
    except KeyboardInterrupt:
        sys.exit(130)
