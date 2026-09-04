"""Command line entry points (`make data`, `make close`, `make eval`)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Config


def _cfg(args: argparse.Namespace) -> Config:
    return Config.load(Path(args.config) if args.config else None)


def cmd_generate(args: argparse.Namespace) -> int:
    from .generate import generate

    cfg = _cfg(args)
    out = Path(args.out) if args.out else (cfg.root / "data" / "synthetic")
    generate(cfg, out)
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    print("close: not implemented yet — see SPEC.md §5-§7 (loader, invariants, engine).")
    return 1


def cmd_evaluate(args: argparse.Namespace) -> int:
    print("evaluate: not implemented yet — see SPEC.md §8.")
    return 1


def main(argv: list[str] | None = None) -> int:
    # --config is accepted either before or after the subcommand. The shared
    # parent uses SUPPRESS so that omitting it on the subcommand does not
    # overwrite a value already parsed at the top level.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS, help="path to config.yaml")

    parser = argparse.ArgumentParser(prog="rlc", description="Refund Lifecycle Closer")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser(
        "generate", parents=[common], help="write synthetic data and ground truth"
    )
    p_gen.add_argument("--out", default=None, help="output directory")
    p_gen.set_defaults(func=cmd_generate)

    sub.add_parser("close", parents=[common], help="run the closure engine").set_defaults(
        func=cmd_close
    )
    sub.add_parser("evaluate", parents=[common], help="score against ground truth").set_defaults(
        func=cmd_evaluate
    )

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
