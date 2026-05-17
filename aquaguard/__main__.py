"""Entry point: python -m aquaguard [--config path/to/config.yaml]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from aquaguard.app import AquaGuardApp
from aquaguard.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="AquaGuard Water Management System")
    parser.add_argument(
        "--config", "-c",
        default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = load_config(args.config)
    app = AquaGuardApp(config)

    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
