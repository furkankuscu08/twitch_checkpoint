"""Run Checkpoint's local microphone and STT intake server."""

import argparse
import logging
import os
from dataclasses import fields

import uvicorn
from dotenv import load_dotenv

from checkpoint.core.config import Config
from checkpoint.server.app import ROOT, create_app


def config_from_env(environ):
    return Config(
        **{
            item.name: item.type(environ[item.name])
            for item in fields(Config)
            if item.name in environ
        }
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log-level", choices=("debug", "info", "warning"), default="info"
    )
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env")
    try:
        config = config_from_env(os.environ)
    except ValueError:
        parser.exit(
            1, "Invalid Checkpoint configuration; check environment overrides.\n"
        )
    logging.basicConfig(
        level=args.log_level.upper(), format="%(levelname)s %(message)s"
    )
    uvicorn.run(
        create_app(config=config),
        host="127.0.0.1",
        port=config.APP_PORT,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
