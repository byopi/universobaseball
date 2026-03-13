import asyncio
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


def check_env():
    """Verifica variables obligatorias antes de arrancar."""
    missing = []
    for var in ("BOT_TOKEN", "ADMIN_ID"):
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        logger.error(f"❌ Faltan variables de entorno obligatorias: {', '.join(missing)}")
        logger.error("   Configúralas en Render → Environment antes de deployar.")
        sys.exit(1)

    channel = (
        os.environ.get("CHANNEL_ID") or
        os.environ.get("CHANNEL_MLB") or
        os.environ.get("CHANNEL_LVBP")
    )
    if not channel:
        logger.warning("⚠️  No hay CHANNEL_ID configurado — los mensajes irán al ADMIN_ID")


async def main():
    check_env()
    logger.info("⚾ Baseball Bot arrancando...")

    # Importar aquí para que check_env() falle primero si faltan vars
    from bot import BaseballBot
    from server import run_server

    bot = BaseballBot()
    await asyncio.gather(
        run_server(),
        bot.run(),
    )


if __name__ == "__main__":
    asyncio.run(main())
