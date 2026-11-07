"""Serve a mock Polarion on localhost for manual UI work.

    python -m mock_polarion.serve 2410-rest-off --port 8810
Then add an environment with base URL http://127.0.0.1:8810/polarion
(test token: test-token-admin; test login: admin / admin-test-pw).
"""
from __future__ import annotations

import argparse
from wsgiref.simple_server import make_server

from .app import MockPolarion
from .profiles import PROFILES


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("profile", choices=sorted(PROFILES))
    ap.add_argument("--port", type=int, default=8810)
    args = ap.parse_args()
    app = MockPolarion(PROFILES[args.profile])
    with make_server("127.0.0.1", args.port, app) as httpd:
        print(f"Mock Polarion {args.profile} on http://127.0.0.1:{args.port}/polarion (Ctrl+C to stop)")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
