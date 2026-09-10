"""The graph explorer's HTTP surface.

Two things here are worth more than the routing checks: the server must never
serve a path outside its own asset set, and it must never listen anywhere but
loopback. The index holds the user's source and the node endpoint returns it
verbatim, so both are security properties, not preferences.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from codeorbit import server
from codeorbit.indexer import index_project
from codeorbit.resolve import resolve_project


@pytest.fixture
def project(tmp_path):
    """A tiny real project, indexed."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "core.py").write_text(
        "def helper(v):\n"
        "    return v + 1\n"
        "\n"
        "def entry(v):\n"
        "    return helper(v)\n",
        encoding="utf-8")
    (tmp_path / "pkg" / "edge.py").write_text(
        "from pkg.core import entry\n"
        "\n"
        "def outer(v):\n"
        "    return entry(v)\n",
        encoding="utf-8")
    index_project(tmp_path)
    resolve_project(tmp_path)
    return tmp_path


@pytest.fixture
def live(project):
    """The real server on an ephemeral port, torn down after the test."""
    httpd = server.bind(0)
    server.Handler.state = server._State(project)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.read(), r.headers


def get_json(url):
    status, body, _ = get(url)
    return status, json.loads(body)


# ------------------------------------------------------------------ binding

def test_binds_loopback_only():
    """Never 0.0.0.0. This process can read the user's source."""
    httpd = server.bind(0)
    try:
        assert httpd.server_address[0] == "127.0.0.1"
    finally:
        httpd.server_close()


def test_explicit_port_in_use_is_an_error_not_a_silent_move():
    """A --port the user typed is a request. Serving somewhere else and
    printing a different URL would be worse than failing."""
    first = server.bind(0)
    taken = first.server_address[1]
    try:
        with pytest.raises(OSError, match=str(taken)):
            server.bind(taken)
    finally:
        first.server_close()


def test_falls_through_to_the_next_default_port(monkeypatch):
    held = server.bind(0)
    try:
        monkeypatch.setattr(server, "PORTS", (held.server_address[1], 0))
        httpd = server.bind()
        try:
            assert httpd.server_address[1] != held.server_address[1]
        finally:
            httpd.server_close()
    finally:
        held.server_close()


# ------------------------------------------------------------------- assets

def test_every_advertised_asset_exists():
    for name, _ctype in server.ASSETS.values():
        assert (server.WEB / name).is_file(), f"{name} is advertised but missing"


def test_serves_the_app_shell(live):
    status, body, headers = get(live + "/")
    assert status == 200
    assert b"<canvas" in body
    assert headers["Content-Type"].startswith("text/html")


@pytest.mark.parametrize("path", [
    "/../server.py",
    "/../../etc/passwd",
    "/app.js/../../server.py",
    "/%2e%2e/server.py",
    "/web/app.js",
    "/schema.sql",
])
def test_refuses_anything_outside_the_asset_allowlist(live, path):
    """The allowlist is the whole defence. No spelling of a path may reach a
    file that is not one of the five the page asks for."""
    with pytest.raises(urllib.error.HTTPError) as e:
        get(live + path)
    assert e.value.code == 404


# --------------------------------------------------------------------- api

def test_meta_describes_the_project(live, project):
    status, data = get_json(live + "/api/meta")
    assert status == 200
    assert data["indexed"] is True
    assert data["name"] == project.name
    assert data["stats"]["files"] >= 3
    assert data["stats"]["symbols"] >= 3


def test_meta_reports_an_unindexed_project_rather_than_failing(tmp_path):
    """The page renders guidance for this. A 500 would render nothing."""
    httpd = server.bind(0)
    server.Handler.state = server._State(tmp_path)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        _, data = get_json(f"http://127.0.0.1:{httpd.server_address[1]}/api/meta")
        assert data["indexed"] is False
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_module_graph(live):
    status, data = get_json(live + "/api/graph?view=modules")
    assert status == 200
    assert data["view"] == "modules"
    assert len(data["nodes"]) >= 3
    assert "truncated" in data
    ids = {n["id"] for n in data["nodes"]}
    for e in data["edges"]:
        assert e["source"] in ids and e["target"] in ids


def test_symbol_graph(live):
    _, data = get_json(live + "/api/graph?view=symbols")
    assert data["view"] == "symbols"
    names = {n["label"] for n in data["nodes"]}
    assert {"helper", "entry", "outer"} <= names


def test_unknown_view_is_a_client_error(live):
    with pytest.raises(urllib.error.HTTPError) as e:
        get(live + "/api/graph?view=nonsense")
    assert e.value.code == 400


def test_search(live):
    _, hits = get_json(live + "/api/search?q=helper")
    assert any(h["name"] == "helper" for h in hits)
    assert all({"id", "name", "path"} <= set(h) for h in hits)


def test_search_ignores_a_term_too_short_to_mean_anything(live):
    _, hits = get_json(live + "/api/search?q=h")
    assert hits == []


def test_node_detail_carries_source_and_both_directions(live):
    _, hits = get_json(live + "/api/search?q=entry")
    node_id = next(h["id"] for h in hits if h["name"] == "entry")
    _, d = get_json(live + f"/api/node/{node_id}")
    assert d["name"] == "entry"
    assert "helper" in d["source"]
    assert any(c["name"] == "helper" for c in d["callees"])
    assert any(c["name"] == "outer" for c in d["callers"])


def test_missing_node_is_404_and_a_bad_id_is_400(live):
    for path, code in [("/api/node/999999", 404), ("/api/node/abc", 400)]:
        with pytest.raises(urllib.error.HTTPError) as e:
            get(live + path)
        assert e.value.code == code


def test_errors_come_back_as_json_the_page_can_render(live):
    try:
        get(live + "/api/node/999999")
    except urllib.error.HTTPError as e:
        assert json.loads(e.read())["error"]
    else:
        pytest.fail("expected a 404")
