"""report/serve.py: serve the static results page over HTTP. Stdlib only, read-only, no model.

    python -m report.serve --dir results/site [--port 8080]

Used as the container entrypoint for Nebius Serverless Endpoints (see report/README.md).
GET and HEAD only; `/healthz` answers 200 without touching the disk; a directory with no
index.html is a 404, never a listing.
"""

import argparse
import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HEALTH_PATH = "/healthz"


class PageHandler(SimpleHTTPRequestHandler):
    server_version = "nemogate-report"

    def do_GET(self) -> None:
        if self.path == HEALTH_PATH:
            self._health()
        else:
            super().do_GET()

    def do_HEAD(self) -> None:
        if self.path == HEALTH_PATH:
            self._health(body=False)
        else:
            super().do_HEAD()

    def _health(self, body: bool = True) -> None:
        payload = b"ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if body:
            self.wfile.write(payload)

    def list_directory(self, path):
        self.send_error(404, "Not found")
        return None

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


def make_server(directory: Path, host: str, port: int) -> ThreadingHTTPServer:
    handler = partial(PageHandler, directory=str(directory))
    return ThreadingHTTPServer((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", type=Path, default=Path("results/site"))
    # A container must listen on all interfaces for the endpoint's HTTPS front to reach it.
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args(argv)
    if not (args.dir / "index.html").is_file():
        parser.error(f"{args.dir}/index.html not found; build it with `python -m report.site`")
    server = make_server(args.dir, args.host, args.port)
    print(f"serving {args.dir} on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
