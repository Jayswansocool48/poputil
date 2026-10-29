#!/usr/bin/env python3
"""popclean — the disk-clutter scanner from PopUtil, as a terminal command."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from engine.popclean import main  # noqa: E402

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBye.")
