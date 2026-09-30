"""
Launch the Ptyzer UI:

    python -m ptyzer.ui [--port 8765] [--dir /path/to/data] [--no-browser]

Run from the repository root (or with the repository on PYTHONPATH), in a Python environment
that has py4DSTEM installed.
"""
import argparse
import os
import sys
import threading
import webbrowser


def main():
    parser = argparse.ArgumentParser(prog="python -m ptyzer.ui", description="Ptyzer local web UI")
    parser.add_argument("--host", default="127.0.0.1",
                        help="interface to bind (default 127.0.0.1; anything else exposes your files)")
    parser.add_argument("--port", type=int, default=8765, help="port (0 picks a free one)")
    parser.add_argument("--dir", default=os.getcwd(), help="folder the file browser opens in")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    parser.add_argument("--verbose", action="store_true", help="log every HTTP request")
    parser.add_argument("--quantem-python", default=os.environ.get("PTYZER_QUANTEM_PYTHON"),
                        help="trusted Python interpreter containing Quantem (default: current interpreter)")
    args = parser.parse_args()

    os.environ.setdefault("MPLBACKEND", "Agg")
    from .server import serve

    try:
        httpd, _ = serve(args.host, args.port, args.dir, args.verbose, args.quantem_python)
    except OSError as exc:
        sys.exit(f"Could not start on {args.host}:{args.port}: {exc}. Try --port 0.")

    host, port = httpd.server_address[:2]
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"
    print(f"Ptyzer UI running at {url}  (Ctrl+C to stop)", flush=True)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("Warning: bound to a non-loopback interface; anyone who can reach this port can "
              "browse files and run conversions on this machine.", flush=True)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
