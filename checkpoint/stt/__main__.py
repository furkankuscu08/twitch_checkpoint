"""Stream the Windows mic page or a PCM WAV through Deepgram."""

import argparse
import asyncio
import logging
import os
from contextlib import ExitStack
from pathlib import Path

import httpx
import websockets
from dotenv import load_dotenv

from minefield.server.__main__ import config_from_env
from minefield.server.app import ROOT

from .engine import Engine


def log_file(stack, path):
    if path is None:
        return None
    target = Path(path).resolve()
    if not target.is_relative_to(ROOT / "data"):
        raise ValueError("Engine logs must be saved under data/")
    target.parent.mkdir(parents=True, exist_ok=True)
    return stack.enter_context(target.open("w", encoding="utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--mic", action="store_true")
    source.add_argument("--wav", type=Path)
    parser.add_argument("--backend", choices=("flux", "nova3"))
    parser.add_argument("--save-raw", type=Path)
    parser.add_argument("--save-hyps", type=Path)
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env")
    environ = dict(os.environ)
    if args.backend:
        environ["STT_BACKEND"] = args.backend
    try:
        config = config_from_env(environ)
        key = environ.get("DEEPGRAM_API_KEY")
        if not key:
            raise ValueError("DEEPGRAM_API_KEY is missing")
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
        # httpx INFO contains request URLs; no bodies, but avoid needless hot-path logs.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        with ExitStack() as stack:
            raw = log_file(stack, args.save_raw)
            hyps = log_file(stack, args.save_hyps)

            async def run():
                async with httpx.AsyncClient(timeout=config.STT_HTTP_TIMEOUT_S) as http:
                    engine = Engine(key, config, http, raw_log=raw, hyp_log=hyps)
                    if args.mic:
                        async with websockets.serve(
                            engine.mic,
                            "127.0.0.1",
                            config.STT_MIC_PORT,
                            max_size=1024,
                            max_queue=config.STT_AUDIO_QUEUE_SIZE,
                        ):
                            await engine.run()
                    else:
                        await engine.run(args.wav)

            asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError):
        # Never render arbitrary exception strings: input and network errors can
        # contain credentials, audio transcripts or private request data.
        parser.exit(
            1, "STT could not start or continue; check configuration and audio input.\n"
        )


if __name__ == "__main__":
    main()
