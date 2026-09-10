"""A local web server for the graph explorer.

Why a server at all, when `viz` already wrote a self-contained page: because a
file has to decide everything up front. Every node, every edge, every symbol's
source has to be inlined before the browser opens, which puts a hard ceiling on
how much of a repository the page can hold and makes the interesting operations
- search the whole index, expand a neighbourhood, read a function's body -
impossible once you are past that ceiling. A server answers those on demand and
only ships what the viewer is actually looking at.

The static export is still there, and is still the right tool for emailing a
picture of a codebase to someone. This is the tool for exploring one.

Bound to 127.0.0.1, always. The index holds your source, the detail endpoint
serves it verbatim, and none of that should be reachable from the network.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db, query, viz

WEB = Path(__file__).with_name("web")

# Tried in order. A short list rather than "any free port" so the URL is
# predictable across restarts, which matters when it is sitting in a browser
# tab that gets reloaded.
PORTS = (7373, 7374, 7375, 7376, 7377, 8973, 8974)

# The whole file set the page may request. An allowlist rather than a directory
# walk: this process can read the user's source, and a path that escapes WEB
# must not be serveable however it is spelled.
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/layout.worker.js": ("layout.worker.js", "text/javascript; charset=utf-8"),
}

# One graph can be a lot bigger than a static file could hold, but not
# unbounded: past this a force layout is a cloud, not a picture.
MAX_NODES = 3000


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    # On Windows SO_REUSEADDR does not mean what it means on POSIX: it lets a
    # second socket bind a port another process is already LISTENing on, and
    # connections then land on whichever the OS feels like. That turns "try the
    # next port" into "silently fight over this one" - bind appears to succeed
    # and the browser reaches the other program. Asking for exclusive use makes
    # a taken port fail, which is what the fallthrough below is built on.
    # On POSIX SO_REUSEADDR only skips TIME_WAIT, which is worth keeping.
    allow_reuse_address = os.name != "nt"


class _State:
    """Per-server state, so the handler does not reach for globals."""

    def __init__(self, root: Path):
        self.root = root
        self._local = threading.local()

    def conn(self):
        """A connection per thread. SQLite objects are not shareable, and
        ThreadingHTTPServer answers each request on its own thread."""
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = db.connect(self.root)
        return c


class Handler(BaseHTTPRequestHandler):
    server_version = "CodeOrbit"
    sys_version = ""
    state: _State = None          # set by serve()

    # The default logs a line per request to stderr, which buries the one
    # message the user needs (the URL) under asset noise.
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        try:
            if path in ASSETS:
                return self._asset(path)
            if path == "/api/meta":
                return self._send(self._meta())
            if path == "/api/graph":
                return self._send(self._graph(params))
            if path == "/api/search":
                return self._send(self._search(params))
            if path.startswith("/api/node/"):
                return self._send(self._node(path.rsplit("/", 1)[-1]))
        except FileNotFoundError as e:
            return self._error(404, str(e))
        except ValueError as e:
            return self._error(400, str(e))
        except Exception as e:                      # noqa: BLE001
            # A stack trace belongs in the terminal, not in the browser, but
            # the page still needs something it can render.
            import traceback
            traceback.print_exc()
            return self._error(500, f"{type(e).__name__}: {e}")

        self._error(404, "No such path")

    # ---------------------------------------------------------- transport

    def _asset(self, path):
        name, ctype = ASSETS[path]
        body = (WEB / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Assets change whenever CodeOrbit is upgraded underneath a tab that is
        # still open, and a stale app.js against a new API is a confusing bug.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message):
        self._send({"error": message}, status)

    # --------------------------------------------------------------- api

    def _meta(self):
        root = self.state.root
        if not db.db_path(root).exists():
            return {"name": root.name, "root": str(root), "indexed": False,
                    "stats": {}}
        conn = self.state.conn()
        one = lambda sql: conn.execute(sql).fetchone()[0]      # noqa: E731
        return {
            "name": root.name,
            "root": str(root),
            "indexed": True,
            "stats": {
                "files": one("SELECT count(*) FROM files"),
                "symbols": one("SELECT count(*) FROM nodes WHERE kind != 'module'"),
                "calls": one("SELECT count(*) FROM edges WHERE kind = 'calls'"),
                "imports": one("SELECT count(*) FROM edges WHERE kind = 'imports'"),
            },
        }

    def _graph(self, params):
        view = (params.get("view") or ["modules"])[0]
        if view not in ("modules", "symbols"):
            raise ValueError("view must be 'modules' or 'symbols'")
        conn = self.state.conn()

        if view == "modules":
            data = viz.module_graph(conn, limit=MAX_NODES)
            total = conn.execute(
                "SELECT count(*) FROM nodes WHERE kind = 'module'").fetchone()[0]
        else:
            focus = (params.get("focus") or [None])[0]
            focus_id = int(focus) if focus and focus.isdigit() else None
            depth = int((params.get("depth") or ["2"])[0])
            data = viz.symbol_graph(conn, focus_id, depth, limit=MAX_NODES)
            total = conn.execute(
                "SELECT count(*) FROM nodes WHERE kind IN "
                "('function','method','class')").fetchone()[0]

        data["truncated"] = max(0, total - len(data["nodes"]))
        return data

    def _search(self, params):
        term = (params.get("q") or [""])[0].strip()
        if len(term) < 2:
            return []
        conn = self.state.conn()
        return [
            {"id": r["id"], "name": r["name"], "kind": r["kind"],
             "path": r["path"]}
            for r in query.search(conn, term, limit=25)
        ]

    def _node(self, raw):
        if not raw.isdigit():
            raise ValueError("node id must be a number")
        conn = self.state.conn()
        row = query.get_node(conn, int(raw))
        if row is None:
            raise FileNotFoundError(f"No node {raw}")

        def brief(rows, uncertain_key=None):
            out = []
            for r in rows:
                keys = r.keys()
                out.append({
                    "id": r["id"],
                    "name": r["name"],
                    "kind": r["kind"] if "kind" in keys else None,
                    "uncertain": bool(
                        uncertain_key and uncertain_key in keys
                        and r[uncertain_key] != "exact"),
                })
            return out

        keys = row.keys()
        source = None
        try:
            source = query.source_of(self.state.root, row, max_lines=140)
        except (OSError, KeyError):
            # The file moved or was deleted since indexing. Everything else
            # about the node is still true and still worth showing.
            source = None

        return {
            "id": row["id"],
            "name": row["name"] if "name" in keys else row["qname"],
            "qname": row["qname"] if "qname" in keys else None,
            "kind": row["kind"] if "kind" in keys else None,
            "lang": row["lang"] if "lang" in keys else None,
            "path": row["path"] if "path" in keys else "",
            "line": row["line"] if "line" in keys else None,
            "loc": row["loc"] if "loc" in keys else None,
            "callers": brief(query.callers(conn, row["id"], limit=40), "resolution"),
            "callees": brief(query.callees(conn, row["id"], limit=40), "resolution"),
            "members": brief(query.members(conn, row["id"])),
            "source": source,
        }


def bind(preferred: int | None = None) -> ThreadingHTTPServer:
    """First port that will take us, from a small predictable set.

    An explicit --port is a request, not a suggestion: if it is taken, say so
    rather than silently serving somewhere else and printing a URL the user did
    not ask for.
    """
    import socket

    # `is not None`, not truthiness: port 0 is a real request, meaning "any
    # free port the OS will give me", and it is how the tests bind.
    candidates = (preferred,) if preferred is not None else PORTS
    last = None
    for port in candidates:
        try:
            return _Server(("127.0.0.1", port), Handler)
        except OSError as e:
            last = e
            continue

    if preferred:
        raise OSError(f"Port {preferred} is not available ({last}).")
    raise OSError(
        "None of the ports CodeOrbit tries were free "
        f"({', '.join(str(p) for p in PORTS)}). "
        "Pass --port to choose one, or stop whatever is holding them.")


def serve(root: Path, port: int | None = None, on_ready=None) -> None:
    """Run until interrupted. `on_ready` is called with the URL once bound."""
    httpd = bind(port)
    Handler.state = _State(root)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"

    if on_ready:
        on_ready(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
