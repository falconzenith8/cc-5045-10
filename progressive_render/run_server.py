#!/usr/bin/env python3
"""Start the progressive render service (web panel + kernel).

Usage:
    python3 run_server.py [--host 127.0.0.1] [--port 8000] [--workers 6]

Then open http://127.0.0.1:8000/ in a browser. The panel is driven entirely
by the preview contention kernel running in this process.
"""

import argparse

from engine.server import serve

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--workers", type=int, default=None,
                    help="bounded render pool size (default: cpu-2, max 8)")
    args = ap.parse_args()
    serve(args.host, args.port, args.workers)
