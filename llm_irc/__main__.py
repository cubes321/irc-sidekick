"""Entry point: python -m llm_irc [--config config.toml]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load


def main() -> int:
    parser = argparse.ArgumentParser(prog="llm_irc", description="IRC client with a local LLM sidekick")
    parser.add_argument("-c", "--config", default="config.toml", help="path to config TOML")
    args = parser.parse_args()

    path = Path(args.config)
    if not path.exists():
        print(f"Config not found: {path}\nCopy config.example.toml to config.toml and edit it.",
              file=sys.stderr)
        return 2
    try:
        cfg = load(path)
    except (ValueError, TypeError) as exc:
        print(f"Bad config: {exc}", file=sys.stderr)
        return 2

    from .ui.app import IRCApp  # imported late so config errors print fast
    IRCApp(cfg).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
