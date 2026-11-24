#!/usr/bin/env python3
"""Generate the sample content served by the content servers.

Deterministic on purpose: every run produces identical files, so throughput
measurements are comparable between runs and between servers.

    small.txt   4 KB    used for latency and selection measurements
    large.bin   4 MB    used for throughput measurements
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

SMALL_SIZE = 4096
LARGE_SIZE = 4 * 1024 * 1024


def build_pattern(size: int) -> bytes:
    """A repeating, non-compressible-ish block built from a fixed seed phrase."""
    seed = b"smart content delivery system - sample payload 0123456789 abcdefghijklmnopqrstuvwxyz\n"
    repeats = size // len(seed) + 1
    return (seed * repeats)[:size]


def main() -> int:
    config.ensure_dirs()
    small_path = os.path.join(config.CONTENT_DIR, config.SMALL_FILE)
    large_path = os.path.join(config.CONTENT_DIR, config.LARGE_FILE)

    for path, size in ((small_path, SMALL_SIZE), (large_path, LARGE_SIZE)):
        if os.path.exists(path) and os.path.getsize(path) == size:
            print("keeping %s (%d bytes)" % (path, size))
            continue
        with open(path, "wb") as handle:
            handle.write(build_pattern(size))
        print("wrote %s (%d bytes)" % (path, size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
