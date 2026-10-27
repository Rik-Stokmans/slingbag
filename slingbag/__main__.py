"""Start de slingbag-service: ``python3 -m slingbag [--simulatie]``."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from .api import create_app
from .service import SlingbagService

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Slingbag-activiteitenhulp")
    parser.add_argument("--data", type=Path, default=Path.home() / "slingbag-data",
                        help="map voor instellingen, opnames, spraak en logboek")
    parser.add_argument("--stemmen", type=Path, default=ROOT / "vendor" / "voices")
    parser.add_argument("--piper", type=Path, default=ROOT / "vendor" / "piper" / "piper")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--poort", type=int, default=8080)
    parser.add_argument("--audio-apparaat", default="plughw:CARD=Headphones,DEV=0")
    parser.add_argument("--simulatie", action="store_true",
                        help="zonder GPIO: knoppen via het testpaneel in de app")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    service = SlingbagService(args.data, args.stemmen, args.piper, simulate=args.simulatie,
                              audio_device=args.audio_apparaat)
    service.start()
    signal.signal(signal.SIGTERM, lambda *_: (service.close(), sys.exit(0)))

    app = create_app(service)
    try:
        from waitress import serve
        serve(app, host=args.host, port=args.poort, threads=6)
    except ImportError:
        app.run(host=args.host, port=args.poort, threaded=True)
    finally:
        service.close()


if __name__ == "__main__":
    main()
