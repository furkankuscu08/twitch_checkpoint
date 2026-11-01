"""Run Checkpoint's local server and Twitch plumbing."""

import argparse
import logging
import os
from dataclasses import fields

import uvicorn
from dotenv import load_dotenv

from checkpoint.core.config import Config
from checkpoint.core.text import parse_blocklist
from checkpoint.server.app import ROOT, create_app
from checkpoint.server.twitch import factory


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
    # File I/O belongs here, outside the pure core.
    blocklist = parse_blocklist((ROOT / "checkpoint/core/blocklist.txt").read_text())
    logins = {
        account: os.environ.get(f"TWITCH_{account.upper()}_LOGIN", "").strip().lower()
        for account in ("bot", "test", "test2", "test3")
    }
    broadcaster = os.environ.get("TWITCH_BROADCASTER_LOGIN", "").strip().lower()
    client_id = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    twitch_factory = factory(client_id, logins, broadcaster) if client_id else None
    if twitch_factory is None:
        logging.getLogger(__name__).warning("Twitch unavailable: Client ID missing")
    uvicorn.run(
        create_app(
            config=config,
            blocklist=blocklist,
            bot_login=logins["bot"],
            broadcaster_login=broadcaster,
            debug=os.environ.get("CHECKPOINT_DEBUG") == "1",
            twitch_factory=twitch_factory,
        ),
        host="127.0.0.1",
        port=config.APP_PORT,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
