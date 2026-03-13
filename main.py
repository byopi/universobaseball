import asyncio
import logging
from bot import BaseballBot
from server import run_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


async def main():
    bot = BaseballBot()
    server_task = asyncio.create_task(run_server())
    bot_task = asyncio.create_task(bot.run())
    await asyncio.gather(server_task, bot_task)


if __name__ == "__main__":
    asyncio.run(main())
