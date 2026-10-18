#!/usr/bin/env python3
"""Refresh built-in account records and the three local tool records."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

# Python cache writes inside a live plugin would trigger needless shell reloads.
sys.dont_write_bytecode = True
from usage import NAMES, PARSER_VERSION, obj, scan


def atomic_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_json(path):
    try:
        return obj(json.loads(path.read_text()))
    except FileNotFoundError:
        return {}


@contextmanager
def lock(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def update_local(args):
    home = Path.home()
    config_dir = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    cache_dir = Path(os.environ.get("XDG_CACHE_HOME", home / ".cache")) / "omarchy/agents-extension"
    state_dir = Path(os.environ.get("XDG_STATE_HOME", home / ".local/state")) / "omarchy/agents/usage"
    settings = load_json(config_dir / "omarchy/agents/local-tools.json")
    with lock(cache_dir / "collector.lock"):
        cache_path = cache_dir / "local-tools.json"
        try:
            cache = load_json(cache_path)
        except (ValueError, OSError):
            cache = {}
        if cache.get("parserVersion") != PARSER_VERSION:
            cache = {"parserVersion": PARSER_VERSION}
        status = 0
        for agent in NAMES:
            if agent in args.exclude or (args.agents and agent not in args.agents):
                continue
            try:
                record = scan(agent, settings, cache, force=args.force)
                atomic_json(state_dir / (agent + ".json"), record)
                if record["collectionErrors"]:
                    print(f"agents-extension: {agent}: " + "; ".join(record["collectionErrors"]), file=sys.stderr)
                    status = 1
            except Exception as exc:
                # Report no raw exception text: a malformed record could contain
                # private data. A broken provider must not block its neighbors.
                print(f"agents-extension: {agent}: {type(exc).__name__}", file=sys.stderr)
                status = 1
        atomic_json(cache_path, cache)
        return status


def update_builtin(args):
    command = Path(os.environ.get("OMARCHY_PATH", "/usr/share/omarchy")) / "bin/omarchy-agent-usage-update"
    flags = []
    if args.force:
        flags.append("--force")
    if args.limits_only:
        flags.append("--limits-only")
    for agent in args.exclude:
        flags.extend(("--except", agent))
    flags.extend(args.agents)
    try:
        return subprocess.run([str(command), *flags], timeout=90, check=False).returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"agents-extension: built-in collectors: {type(exc).__name__}", file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limits-only", action="store_true")
    parser.add_argument("--except", dest="exclude", action="append", default=[])
    parser.add_argument("--local-only", action="store_true", help="Do not run network-backed built-in collectors")
    parser.add_argument("agents", nargs="*")
    args = parser.parse_args()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(update_local, args)]
        if not args.local_only:
            futures.append(pool.submit(update_builtin, args))
        return max(f.result() for f in futures)


if __name__ == "__main__":
    sys.exit(main())
