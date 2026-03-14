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
    missing = []
    for var in ("BOT_TOKEN", "ADMIN_ID"):
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        logger.error(f"❌ Faltan variables de entorno: {', '.join(missing)}")
        sys.exit(1)
    if not (os.environ.get("CHANNEL_ID") or os.environ.get("CHANNEL_MLB")):
        logger.warning("⚠️  No hay CHANNEL_ID — mensajes irán al ADMIN_ID")


async def main():
    check_env()
    logger.info("⚾ Baseball Bot arrancando...")

    from bot import BaseballBot
    from server import run_server
    import image_generator as ig

    # Pre-cargar logos en background (no bloquea el arranque)
    asyncio.create_task(ig.warm_logo_cache())

    bot = BaseballBot()
    await asyncio.gather(
        run_server(),
        bot.run(),
    )


if __name__ == "__main__":
    asyncio.run(main())
