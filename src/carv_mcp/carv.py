"""Client for Carv's gRPC backend (rpc.carv.ai), reconstructed from the Android app (10.0.0).

Only three steps are needed:
  /net.Account/UserLogin   {1 email, 2 password} -> {1 User{..., 8 "sessionid=..."}}
  /net.SessionApi/Sync     {} -> {1 {1 repeated {1 session id, 2 signed GCS URL}}}
  GET <signed URL>         zstd-compressed proto::data::Session (data version 14.0.1)
"""

import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

import grpc
import zstandard

from .decode import _fields

HOST = "rpc.carv.ai:443"
APP_VERSION = "10.0.0"
DATA_VERSION = "14.0.1"  # x-server-data-version reported by the server

STATE_DIR = Path(os.environ.get("CARV_MCP_HOME", Path.home() / ".config" / "carv-mcp"))
CACHE_DIR = Path(os.environ.get("CARV_MCP_CACHE", Path.home() / ".cache" / "carv-mcp"))
CARV_STATE = STATE_DIR / "carv.json"
LISTING = CACHE_DIR / "listing.json"
# Login and downloads from carv-garmin-sync are reused when present.
SYNC_STATE = Path.home() / ".config" / "carv-garmin-sync" / "carv.json"
SYNC_CACHE = Path.home() / ".cache" / "carv-garmin-sync"


class CarvError(Exception):
    pass


class NotLoggedIn(CarvError):
    pass


def _encode_strings(fields: list[tuple[int, str]]) -> bytes:
    out = bytearray()
    for num, val in fields:
        b = val.encode()
        out += bytes([num << 3 | 2]) + _varint(len(b)) + b
    return bytes(out)


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


class CarvClient:
    def __init__(self):
        self.cookie: str | None = None
        self.email: str | None = None
        self.password: str | None = None
        self._channel = None
        self._lock = threading.Lock()
        self._load()

    # --- credentials -----------------------------------------------------

    def _load(self) -> None:
        for path in (CARV_STATE, SYNC_STATE):
            if path.exists():
                s = json.loads(path.read_text())
                self.cookie, self.email, self.password = s.get("cookie"), s.get("email"), s.get("password")
                break
        # Credentials in the environment (e.g. Claude Desktop config) take precedence for re-login.
        if os.environ.get("CARV_EMAIL") and os.environ.get("CARV_PASSWORD"):
            self.email, self.password = os.environ["CARV_EMAIL"], os.environ["CARV_PASSWORD"]

    def save(self, store_password: bool) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        state = {"email": self.email, "cookie": self.cookie, "password": self.password if store_password else None}
        fd = os.open(CARV_STATE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({k: v for k, v in state.items() if v is not None}, f, indent=2)
        CARV_STATE.chmod(0o600)

    # --- transport -------------------------------------------------------

    def _call(self, method: str, request: bytes = b"") -> bytes:
        if self._channel is None:
            self._channel = grpc.secure_channel(
                HOST, grpc.ssl_channel_credentials(), options=[("grpc.max_receive_message_length", 64 * 1024 * 1024)]
            )
        md = [
            ("app", APP_VERSION),
            ("os", "android"),
            ("platform", "android"),
            ("data", DATA_VERSION),
            ("locale", "en-US"),
            ("phone-make", "carv-mcp"),
            ("phone-model", "mcp"),
        ]
        if self.cookie:
            md.append(("cookie", self.cookie))
        rpc = self._channel.unary_stream(method, request_serializer=lambda b: b, response_deserializer=lambda b: b)
        try:
            msgs = list(rpc(request, metadata=md, timeout=120))
        except grpc.RpcError as e:
            raise CarvError(f"{method}: {e.code().name}: {e.details()}") from e
        if not msgs:
            raise CarvError(f"{method}: empty response")
        return msgs[0]

    # --- API -------------------------------------------------------------

    def login(self, email: str, password: str) -> dict:
        """Returns the user's first and last name; sets the session cookie."""
        res = self._call("/net.Account/UserLogin", _encode_strings([(1, email), (2, password)]))
        user = next((v for tag, _, v in _fields(res) if tag == 1), b"")
        u = {tag: v for tag, wt, v in _fields(user) if wt == 2}
        if 8 not in u:
            raise CarvError("UserLogin: no session cookie in response")
        self.cookie, self.email, self.password = u[8].decode(), email, password
        return {"first_name": u.get(2, b"").decode(), "last_name": u.get(3, b"").decode()}

    def _relogin(self) -> None:
        if not (self.email and self.password):
            raise NotLoggedIn("Carv-sessie verlopen of niet ingelogd. Draai `carv-mcp login` in een terminal.")
        self.login(self.email, self.password)
        if CARV_STATE.exists():
            self.save(store_password="password" in json.loads(CARV_STATE.read_text()))

    def list_sessions(self, refresh: bool = False) -> dict[str, str]:
        """Session id -> signed download URL. Cached on disk; refresh asks the server again."""
        with self._lock:
            if not refresh and LISTING.exists():
                return json.loads(LISTING.read_text())
            if not self.cookie:
                self._relogin()
            try:
                res = self._call("/net.SessionApi/Sync")
            except CarvError as e:
                if "UNAUTHENTICATED" not in str(e):
                    raise
                self._relogin()
                res = self._call("/net.SessionApi/Sync")
            listing = {}
            for tag, _, v in _fields(res):
                if tag != 1:
                    continue
                for t2, _, entry in _fields(v):
                    if t2 == 1:
                        e = {t: x.decode() for t, wt, x in _fields(entry) if wt == 2}
                        listing[e[1]] = e.get(2, "")
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            LISTING.write_text(json.dumps(listing))
            return listing

    def session_bytes(self, session_id: str) -> bytes:
        """A session's decompressed proto::data::Session, cached on disk."""
        name = f"{session_id}.{DATA_VERSION}.pb"
        for path in (CACHE_DIR / name, SYNC_CACHE / name):
            if path.exists():
                return path.read_bytes()
        listing = self.list_sessions()
        if session_id not in listing:
            listing = self.list_sessions(refresh=True)
        if session_id not in listing:
            raise CarvError(f"onbekende sessie: {session_id}")
        try:
            raw = _get(listing[session_id])
        except urllib.error.HTTPError:  # signed URL expired
            raw = _get(self.list_sessions(refresh=True)[session_id])
        data = zstandard.ZstdDecompressor().stream_reader(raw).read()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_DIR / (name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(CACHE_DIR / name)
        return data


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()
