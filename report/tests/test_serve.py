"""report/serve.py: the static file server behind the Serverless Endpoint. Real sockets on
127.0.0.1, an ephemeral port and a temporary directory; no model, no sandbox, no cloud."""

import http.client
import threading
from pathlib import Path

import pytest

from report import serve, site

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def server(tmp_path):
    """A server over a real page built by report.site from the rich fixture."""
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    out = site_dir / "index.html"
    assert site.main(["--results", str(FIXTURES / "results_rich.json"), "--out", str(out)]) == 0
    (tmp_path / "secret.txt").write_text("outside the served directory", encoding="utf-8")
    (site_dir / "empty").mkdir()
    httpd = serve.make_server(site_dir, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, out
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def fetch(httpd, path: str, method: str = "GET"):
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def test_root_serves_the_built_page_byte_for_byte(server):
    # Catches a handler pointed at the wrong directory or one that mangles the page.
    httpd, out = server
    status, headers, body = fetch(httpd, "/")
    assert status == 200
    assert headers["content-type"] == "text/html"
    assert body == out.read_bytes()
    assert headers["x-content-type-options"] == "nosniff"


def test_healthz_is_200_for_get_and_head_without_a_body_on_head(server):
    # Catches a deploy whose probe (or a human's curl) gets a 404 from the page server.
    httpd, _ = server
    status, _, body = fetch(httpd, "/healthz")
    assert (status, body) == (200, b"ok\n")
    status, _, body = fetch(httpd, "/healthz", "HEAD")
    assert (status, body) == (200, b"")


def test_directory_without_index_is_404_not_a_listing(server):
    # Catches stdlib's default directory listing leaking file names.
    httpd, _ = server
    status, _, body = fetch(httpd, "/empty/")
    assert status == 404
    assert b"Directory listing" not in body


def test_path_traversal_cannot_reach_files_outside_the_directory(server):
    # Catches a handler that serves from a root other than the page directory.
    httpd, _ = server
    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt"):
        status, _, body = fetch(httpd, path)
        assert status == 404, path
        assert b"outside the served directory" not in body


def test_post_is_refused(server):
    # The page is read-only; stdlib answers 501 for verbs with no handler.
    httpd, _ = server
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
    conn.request("POST", "/", body=b"x")
    assert conn.getresponse().status == 501
    conn.close()


def test_main_refuses_a_directory_without_index_html(tmp_path, capsys):
    # Catches a container that starts, passes a probe and serves 404 because the page was never
    # copied in.
    with pytest.raises(SystemExit) as exc:
        serve.main(["--dir", str(tmp_path), "--port", "0"])
    assert exc.value.code == 2
    assert "index.html not found" in capsys.readouterr().err
