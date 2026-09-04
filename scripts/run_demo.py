#!/usr/bin/env python3
"""Run the host (FastAPI, :8010) and the web app (Next.js, :3110) with one command.

    python scripts/run_demo.py

Boots both as subprocesses, pipes their output with a label, and stops both on Ctrl-C.
Process management (spawn/signal/wait) follows refs/commerce-agents/scripts/run_demo.py;
only the two commands differ.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

REPO_ROOT = Path(__file__).resolve().parents[1]

WINDOWS = os.name == "nt"
GREEN, YELLOW, DIM, RESET = "\033[32m", "\033[33m", "\033[2m", "\033[0m"


def pipe_output(process: subprocess.Popen, label: str) -> None:
    def run() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(f"{DIM}[{label}]{RESET} {line.decode(errors='replace')}")

    threading.Thread(target=run, daemon=True).start()


def spawn(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.Popen:
    # Each child owns its own process group so shutdown takes its whole tree: on POSIX via
    # start_new_session, on Windows via CREATE_NEW_PROCESS_GROUP (os.killpg doesn't exist there).
    kwargs: dict[str, object] = {}
    if WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **kwargs,
    )


def stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if WINDOWS:
        with contextlib.suppress(Exception):
            process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)


def kill(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if WINDOWS:
        with contextlib.suppress(Exception):
            process.kill()
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)


def start_host() -> subprocess.Popen:
    command = [sys.executable, "-m", "atworks_host.main"]
    return spawn(command, REPO_ROOT)


def start_web() -> subprocess.Popen:
    # npm is npm.cmd on Windows; shutil.which resolves either name to the right executable.
    npm = shutil.which("npm")
    if npm is None:
        sys.exit("npm not found on PATH — install Node.js first.")
    command = [npm, "run", "dev", "--prefix", "web/atworks-web"]
    return spawn(command, REPO_ROOT)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").partition("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(__doc__ or "").partition("\n")[2],
    )
    parser.add_argument("--host-only", action="store_true", help="start only the host")
    parser.add_argument("--web-only", action="store_true", help="start only the web app")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.host_only and args.web_only:
        sys.exit("--host-only and --web-only are mutually exclusive.")

    def terminate(signum: int, frame: object) -> None:
        del signum, frame
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)

    processes: list[tuple[str, subprocess.Popen]] = []
    try:
        if not args.web_only:
            host = start_host()
            processes.append(("host", host))
            pipe_output(host, "host")
        if not args.host_only:
            web = start_web()
            processes.append(("web", web))
            pipe_output(web, "web")

        if not processes:
            return 0
        print(f"\n{GREEN}atworks-ai demo starting{RESET}")
        print(f"  {'host':<8} http://127.0.0.1:{os.environ.get('ATWORKS_PORT', '8010')}")
        print("  web      http://localhost:3110")
        print(f"{DIM}Ctrl-C stops everything.{RESET}\n")
        while all(process.poll() is None for _, process in processes):
            time.sleep(1)
        for name, process in processes:
            if process.poll() is not None:
                print(f"{YELLOW}{name} exited with code {process.returncode}.{RESET}")
                return process.returncode or 1
        return 0
    except KeyboardInterrupt:
        print("\nShutting down…")
        return 0
    finally:
        for _, process in processes:
            stop(process)
        deadline = time.monotonic() + 10
        for _, process in processes:
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                kill(process)


if __name__ == "__main__":
    raise SystemExit(main())
