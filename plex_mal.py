#!/usr/bin/env python3
"""plex-mal-sync entry point.

  python plex_mal.py status
  python plex_mal.py sections
  python plex_mal.py scan
  python plex_mal.py resolve
  python plex_mal.py plan
  python plex_mal.py apply --confirm <hash>
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from plexmal.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
