from __future__ import annotations

import logging
import signal
import sys

from app.config import Config, ConfigError
from app.service import GroupManagerService


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )


def main() -> int:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration invalide : {exc}", file=sys.stderr)
        return 2

    configure_logging(config.log_level)
    service = GroupManagerService(config)

    def request_stop(signum: int, frame: object) -> None:
        logging.getLogger(__name__).info("Arrêt demandé (signal %s)", signum)
        service.stop()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    try:
        service.run()
    except Exception:
        logging.getLogger(__name__).exception("Le bot s’est arrêté à cause d’une erreur fatale")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

