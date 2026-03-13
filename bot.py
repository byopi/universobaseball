"""
bot.py — Bot principal de Telegram para béisbol
Publica juegos del día y resultados para MLB, LVBP, Serie del Caribe y WBC.
El WBC se detecta automáticamente — no requiere configuración manual.
"""
import os
import asyncio
import logging
import aiohttp
from datetime import datetime, date
from zoneinfo import ZoneInfo

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from telegram.error import TelegramError

import data_fetcher as df
import image_generator as ig

logger = logging.getLogger(__name__)

ET_TZ = ZoneInfo("America/New_York")
VZ_TZ = ZoneInfo("America/Caracas")

BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
ADMIN_ID    = os.environ.get("ADMIN_ID", "")
CHANNEL_ID  = os.environ.get("CHANNEL_ID", "")
CHANNEL_MLB    = os.environ.get("CHANNEL_MLB",    CHANNEL_ID)
CHANNEL_LVBP   = os.environ.get("CHANNEL_LVBP",   CHANNEL_ID)
CHANNEL_CARIBE = os.environ.get("CHANNEL_CARIBE",  CHANNEL_ID)
CHANNEL_WBC    = os.environ.get("CHANNEL_WBC",    CHANNEL_ID)

_sent_results: set = set()
_sent_lineups: set = set()


def _channel_for(league: str) -> str:
    ch = {
        "mlb":    CHANNEL_MLB,
        "lvbp":   CHANNEL_LVBP,
        "caribe": CHANNEL_CARIBE,
        "wbc":    CHANNEL_WBC,
    }.get(league, CHANNEL_ID)
    return ch or ADMIN_ID


def _game_key(game: dict, prefix: str) -> str:
    return f"{prefix}_{game.get('gamePk', '')}"


def _parse_games_for_display(games: list, league: str) -> list:
    tz = VZ_TZ if league in ("lvbp", "caribe") else ET_TZ
    result = []
    for game in games:
        away = df.get_team_info(game, "away")
        home = df.get_team_info(game, "home")
        result.append({
            "game_pk":   game.get("gamePk"),
            "away_name": away["full_name"],
            "away_abbr": away["abbreviation"],
            "away_id":   away["id"],
            "home_name": home["full_name"],
            "home_abbr": home["abbreviation"],
            "home_id":   home["id"],
            "time":      df.get_game_time_str(game, tz),
            "status":    game.get("status", {}).get("abstractGameState", ""),
            "game_type": game.get("gameType", "R"),
        })
    return result


async def _send_photo(bot: Bot, chat_id: str, image_bytes: bytes, caption: str = ""):
    import io
    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=io.BytesIO(image_bytes),
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
        return True
    except TelegramError as e:
        logger.error(f"Error enviando foto a {chat_id}: {e}")
        return False


async def _send_message(bot: Bot, chat_id: str, text: str):
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
    except TelegramError as e:
        logger.error(f"Error enviando mensaje a {chat_id}: {e}")


# ─────────────────────────────────────────────────────────────
#  PUBLICAR: JUEGOS DEL DÍA
# ─────────────────────────────────────────────────────────────
async def publish_games_today(bot: Bot, league: str, games: list,
                               date_str: str, league_logo_url: str = None):
    if not games:
        return
    display = _parse_games_for_display(games, league)
    image_bytes = await ig.generate_games_today_image(display, league, date_str, league_logo_url)

    headers = {
        "mlb":    "⚾ <b>MLB — JUEGOS DE HOY</b>",
        "lvbp":   "⚾ <b>LVBP Venezuela — JUEGOS DE HOY</b>",
        "caribe": "⚾ <b>SERIE DEL CARIBE — JUEGOS DE HOY</b>",
        "wbc":    "⚾ <b>WORLD BASEBALL CLASSIC — JUEGOS DE HOY</b>",
    }
    lines = [headers.get(league, f"⚾ {league.upper()}"), f"📅 {date_str}", ""]
    for g in display:
        lines.append(
            f"🔸 <b>{g['away_abbr'] or g['away_name']}</b> vs "
            f"<b>{g['home_abbr'] or g['home_name']}</b> — {g['time']}"
        )
    lines.append(f"\n#Baseball #Beisbol #{league.upper()}")

    await _send_photo(bot, _channel_for(league), image_bytes, "\n".join(lines))
    logger.info(f"[{league.upper()}] Juegos del día: {len(display)} juegos")


# ─────────────────────────────────────────────────────────────
#  PUBLICAR: RESULTADO FINAL
# ─────────────────────────────────────────────────────────────
async def publish_final_result(bot: Bot, game: dict, league: str,
                                session: aiohttp.ClientSession):
    key = _game_key(game, f"result_{league}")
    if key in _sent_results:
        return
    _sent_results.add(key)

    game_pk = game.get("gamePk")
    away = df.get_team_info(game, "away")
    home = df.get_team_info(game, "home")

    box = await df.get_game_boxscore(session, game_pk)
    away_hits   = box.get("teams", {}).get("away", {}).get("teamStats", {}).get("batting",  {}).get("hits",   "-")
    home_hits   = box.get("teams", {}).get("home", {}).get("teamStats", {}).get("batting",  {}).get("hits",   "-")
    away_errors = box.get("teams", {}).get("away", {}).get("teamStats", {}).get("fielding", {}).get("errors", "-")
    home_errors = box.get("teams", {}).get("home", {}).get("teamStats", {}).get("fielding", {}).get("errors", "-")

    linescore = await df.get_game_linescore(session, game_pk)
    innings = linescore.get("currentInning", 9)

    decisions = game.get("decisions", {})
    winner_p = decisions.get("winner", {}).get("fullName", "")
    loser_p  = decisions.get("loser",  {}).get("fullName", "")
    save_p   = decisions.get("save",   {}).get("fullName", "")

    away_country = _country_from_team(away["name"])
    home_country = _country_from_team(home["name"])

    game_data = {
        "away_name":      away["name"],
        "home_name":      home["name"],
        "away_id":        away["id"] if not away_country else None,
        "home_id":        home["id"] if not home_country else None,
        "away_country":   away_country,
        "home_country":   home_country,
        "away_score":     away["score"],
        "home_score":     home["score"],
        "innings":        innings,
        "away_hits":      away_hits,
        "home_hits":      home_hits,
        "away_errors":    away_errors,
        "home_errors":    home_errors,
        "winner_pitcher": winner_p,
        "loser_pitcher":  loser_p,
        "save_pitcher":   save_p,
    }

    image_bytes = await ig.generate_final_result_image(game_data, league)
    winner_name = away["name"] if away["score"] > home["score"] else home["name"]
    caption = (
        f"⚾ <b>RESULTADO FINAL</b>\n"
        f"🏆 {away['abbreviation'] or away['name']} <b>{away['score']}</b> "
        f"— <b>{home['score']}</b> {home['abbreviation'] or home['name']}\n"
        f"🥇 Ganador: <b>{winner_name}</b>\n"
    )
    if winner_p: caption += f"✅ W: {winner_p}\n"
    if loser_p:  caption += f"❌ L: {loser_p}\n"
    if save_p:   caption += f"💾 S: {save_p}\n"
    caption += f"\n#Baseball #Beisbol #{league.upper()}"

    await _send_photo(bot, _channel_for(league), image_bytes, caption)
    logger.info(f"[{league.upper()}] Resultado: {away['name']} {away['score']}-{home['score']} {home['name']}")


# ─────────────────────────────────────────────────────────────
#  PUBLICAR: ALINEACIONES
# ─────────────────────────────────────────────────────────────
async def publish_lineup(bot: Bot, game: dict, league: str,
                          session: aiohttp.ClientSession):
    key = _game_key(game, f"lineup_{league}")
    if key in _sent_lineups:
        return

    game_pk = game.get("gamePk")
    away = df.get_team_info(game, "away")
    home = df.get_team_info(game, "home")

    lineups = df.get_lineup_from_game(game)
    away_lineup = lineups.get("away", [])
    home_lineup = lineups.get("home", [])

    if not away_lineup and not home_lineup:
        box = await df.get_game_boxscore(session, game_pk)
        away_batters = box.get("teams", {}).get("away", {}).get("batters", [])
        home_batters = box.get("teams", {}).get("home", {}).get("batters", [])
        away_players = box.get("teams", {}).get("away", {}).get("players", {})
        home_players = box.get("teams", {}).get("home", {}).get("players", {})
        away_lineup = [away_players.get(f"ID{pid}", {}).get("person", {})
                       for pid in away_batters[:9] if f"ID{pid}" in away_players]
        home_lineup = [home_players.get(f"ID{pid}", {}).get("person", {})
                       for pid in home_batters[:9] if f"ID{pid}" in home_players]

    if not away_lineup and not home_lineup:
        return

    _sent_lineups.add(key)

    away_country = _country_from_team(away["name"])
    home_country = _country_from_team(home["name"])

    game_data = {
        "away_name":    away["name"],
        "home_name":    home["name"],
        "away_id":      away["id"] if not away_country else None,
        "home_id":      home["id"] if not home_country else None,
        "away_country": away_country,
        "home_country": home_country,
    }

    image_bytes = await ig.generate_lineup_image(game_data, league, away_lineup, home_lineup)
    caption = (
        f"⚾ <b>ALINEACIONES</b>\n"
        f"🔸 {away['name']} vs {home['name']}\n"
        f"#{league.upper()} #Beisbol #Lineup"
    )
    await _send_photo(bot, _channel_for(league), image_bytes, caption)
    logger.info(f"[{league.upper()}] Alineación: {away['name']} vs {home['name']}")


# ─────────────────────────────────────────────────────────────
#  UTILIDADES
# ─────────────────────────────────────────────────────────────
_COUNTRY_MAP = {
    "dominican": "DOM", "república dominicana": "DOM", "d.r.": "DOM",
    "venezuela": "VEN", "venezuelan": "VEN",
    "puerto rico": "PUR", "puerto rican": "PUR",
    "cuba": "CUB",
    "mexico": "MEX", "méxico": "MEX",
    "panama": "PAN", "panamá": "PAN",
    "united states": "USA", "usa": "USA",
    "japan": "JPN", "japón": "JPN",
    "korea": "KOR", "south korea": "KOR",
    "australia": "AUS",
    "italy": "ITA", "italia": "ITA",
    "netherlands": "NED", "países bajos": "NED",
}


def _country_from_team(team_name: str) -> str | None:
    name_lower = team_name.lower()
    for keyword, code in _COUNTRY_MAP.items():
        if keyword in name_lower:
            return code
    return None


# ─────────────────────────────────────────────────────────────
#  SCHEDULER PRINCIPAL
# ─────────────────────────────────────────────────────────────
async def run_scheduler(bot: Bot):
    last_date_published = None
    logger.info("Scheduler iniciado ✅")

    while True:
        try:
            now_et       = datetime.now(ET_TZ)
            today_str    = now_et.strftime("%Y-%m-%d")
            date_display = now_et.strftime("%d de %B de %Y")

            async with aiohttp.ClientSession() as session:

                # ── 1. Juegos del día (una sola vez por día) ─────────
                if last_date_published != today_str:
                    mlb_games = await df.get_mlb_games(session, today_str)
                    if mlb_games:
                        await publish_games_today(
                            bot, "mlb", mlb_games, date_display,
                            "https://www.mlbstatic.com/team-logos/league-on-dark/1.svg"
                        )
                        await asyncio.sleep(3)

                    lvbp_games = await df.get_lvbp_games(session, today_str)
                    if lvbp_games:
                        await publish_games_today(bot, "lvbp", lvbp_games, date_display)
                        await asyncio.sleep(3)

                    caribe_games = await df.get_caribe_games(session, today_str)
                    if caribe_games:
                        await publish_games_today(bot, "caribe", caribe_games, date_display)
                        await asyncio.sleep(3)

                    # WBC completamente automático
                    wbc_games = await df.get_wbc_games_auto(session, today_str)
                    if wbc_games:
                        await publish_games_today(bot, "wbc", wbc_games, date_display)
                        await asyncio.sleep(3)

                    last_date_published = today_str
                    _sent_results.clear()
                    _sent_lineups.clear()
                    logger.info(f"✅ Publicación diaria completada: {today_str}")

                # ── 2. Resultados y alineaciones ─────────────────────
                mlb_games = await df.get_mlb_games(session, today_str)
                for game in mlb_games:
                    if df.is_game_final(game):
                        await publish_final_result(bot, game, "mlb", session)
                    if df.is_postseason(game):  # Alineaciones MLB solo en postemporada
                        await publish_lineup(bot, game, "mlb", session)
                await asyncio.sleep(5)

                lvbp_games = await df.get_lvbp_games(session, today_str)
                for game in lvbp_games:
                    if df.is_game_final(game):
                        await publish_final_result(bot, game, "lvbp", session)
                    await publish_lineup(bot, game, "lvbp", session)
                await asyncio.sleep(5)

                caribe_games = await df.get_caribe_games(session, today_str)
                for game in caribe_games:
                    if df.is_game_final(game):
                        await publish_final_result(bot, game, "caribe", session)
                    await publish_lineup(bot, game, "caribe", session)
                await asyncio.sleep(5)

                wbc_games = await df.get_wbc_games_auto(session, today_str)
                for game in wbc_games:
                    if df.is_game_final(game):
                        await publish_final_result(bot, game, "wbc", session)
                    await publish_lineup(bot, game, "wbc", session)

        except Exception as e:
            logger.error(f"Error en scheduler: {e}", exc_info=True)

        await asyncio.sleep(300)  # ciclo cada 5 minutos


# ─────────────────────────────────────────────────────────────
#  COMANDOS DEL BOT
# ─────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "⚾ <b>Baseball Bot</b>\n\n"
        "Publicación automática de béisbol:\n"
        "• <b>MLB</b> — Juegos del día + resultados (alineaciones en postseason)\n"
        "• <b>LVBP</b> — Juegos + alineaciones + resultados\n"
        "• <b>Serie del Caribe</b> — Juegos + alineaciones + resultados\n"
        "• <b>WBC</b> — Detectado automáticamente cuando hay torneo\n\n"
        "<b>Comandos:</b>\n"
        "/hoy — Todos los juegos de hoy\n"
        "/hoymlb — Juegos MLB con imagen\n"
        "/hoylvbp — Juegos LVBP con imagen\n"
        "/resultados — Resultados del día\n"
        "/status — Estado del bot\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_hoy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_str    = date.today().strftime("%Y-%m-%d")
    date_display = date.today().strftime("%d/%m/%Y")
    lines = [f"⚾ <b>JUEGOS DE HOY — {date_display}</b>\n"]

    async with aiohttp.ClientSession() as session:
        for label, fetcher, emoji, tz in [
            ("MLB",          df.get_mlb_games,    "🇺🇸", ET_TZ),
            ("LVBP",         df.get_lvbp_games,   "🇻🇪", VZ_TZ),
            ("Serie Caribe", df.get_caribe_games, "🌊",  VZ_TZ),
        ]:
            games = await fetcher(session, today_str)
            if not games:
                continue
            lines.append(f"{emoji} <b>{label}</b>")
            for g in games:
                away  = df.get_team_info(g, "away")
                home  = df.get_team_info(g, "home")
                t     = df.get_game_time_str(g, tz)
                state = g.get("status", {}).get("abstractGameState", "")
                if state == "Final":
                    lines.append(f"  ✅ {away['abbreviation']} <b>{away['score']}</b> — <b>{home['score']}</b> {home['abbreviation']}")
                elif state == "Live":
                    lines.append(f"  🔴 {away['abbreviation']} {away['score']} — {home['score']} {home['abbreviation']} (VIVO)")
                else:
                    lines.append(f"  🔸 {away['abbreviation']} vs {home['abbreviation']} — {t}")

        wbc = await df.get_wbc_games_auto(session, today_str)
        if wbc:
            lines.append("🌍 <b>World Baseball Classic</b>")
            for g in wbc:
                away = df.get_team_info(g, "away")
                home = df.get_team_info(g, "home")
                t    = df.get_game_time_str(g, ET_TZ)
                lines.append(f"  🔸 {away['name']} vs {home['name']} — {t}")

    if len(lines) == 1:
        lines.append("No hay juegos programados para hoy.")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_hoy_mlb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_str    = date.today().strftime("%Y-%m-%d")
    date_display = date.today().strftime("%d/%m/%Y")
    async with aiohttp.ClientSession() as session:
        games = await df.get_mlb_games(session, today_str)
    if games:
        display = _parse_games_for_display(games, "mlb")
        img = await ig.generate_games_today_image(display, "mlb", date_display)
        import io
        await update.message.reply_photo(io.BytesIO(img))
    else:
        await update.message.reply_text("No hay juegos MLB hoy.")


async def cmd_hoy_lvbp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_str    = date.today().strftime("%Y-%m-%d")
    date_display = date.today().strftime("%d/%m/%Y")
    async with aiohttp.ClientSession() as session:
        games = await df.get_lvbp_games(session, today_str)
    if games:
        display = _parse_games_for_display(games, "lvbp")
        img = await ig.generate_games_today_image(display, "lvbp", date_display)
        import io
        await update.message.reply_photo(io.BytesIO(img))
    else:
        await update.message.reply_text("No hay juegos LVBP hoy.")


async def cmd_resultados(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_str = date.today().strftime("%Y-%m-%d")
    lines = ["⚾ <b>RESULTADOS DE HOY</b>\n"]
    async with aiohttp.ClientSession() as session:
        mlb    = await df.get_mlb_games(session, today_str)
        finals = [g for g in mlb if df.is_game_final(g)]
    if finals:
        lines.append("🇺🇸 <b>MLB</b>")
        for g in finals:
            away   = df.get_team_info(g, "away")
            home   = df.get_team_info(g, "home")
            winner = away["name"] if away["score"] > home["score"] else home["name"]
            lines.append(
                f"  ✅ {away['abbreviation']} <b>{away['score']}</b> — "
                f"<b>{home['score']}</b> {home['abbreviation']}  🏆 {winner}"
            )
    else:
        lines.append("Aún no hay resultados finales.")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"✅ <b>Bot activo</b>\n"
        f"📅 Fecha: {date.today()}\n"
        f"📊 Resultados enviados: {len(_sent_results)}\n"
        f"📋 Alineaciones enviadas: {len(_sent_lineups)}\n"
        f"🔄 WBC: detección automática\n"
        f"⚾ Canal principal: {CHANNEL_ID or 'no configurado'}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────────────────────────
#  CLASE PRINCIPAL
# ─────────────────────────────────────────────────────────────
class BaseballBot:
    def __init__(self):
        self.app = Application.builder().token(BOT_TOKEN).build()
        self.app.add_handler(CommandHandler("start",      cmd_start))
        self.app.add_handler(CommandHandler("hoy",        cmd_hoy))
        self.app.add_handler(CommandHandler("hoymlb",     cmd_hoy_mlb))
        self.app.add_handler(CommandHandler("hoylvbp",    cmd_hoy_lvbp))
        self.app.add_handler(CommandHandler("resultados", cmd_resultados))
        self.app.add_handler(CommandHandler("status",     cmd_status))

    async def run(self):
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info("Bot iniciado ✅")
        await run_scheduler(self.app.bot)
