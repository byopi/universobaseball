"""
bot.py — Bot principal de Telegram para béisbol
MLB, LVBP, Serie del Caribe y WBC.
"""
import os
import io
import asyncio
import logging
import aiohttp
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                           ContextTypes)
from telegram.constants import ParseMode
from telegram.error import TelegramError

import data_fetcher as df
import image_generator as ig

logger = logging.getLogger(__name__)

ET_TZ = ZoneInfo("America/New_York")
VZ_TZ = ZoneInfo("America/Caracas")

BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
ADMIN_ID       = os.environ.get("ADMIN_ID", "")
CHANNEL_ID     = os.environ.get("CHANNEL_ID", "")
CHANNEL_MLB    = os.environ.get("CHANNEL_MLB",    "") or CHANNEL_ID
CHANNEL_LVBP   = os.environ.get("CHANNEL_LVBP",   "") or CHANNEL_ID
CHANNEL_CARIBE = os.environ.get("CHANNEL_CARIBE", "") or CHANNEL_ID
CHANNEL_WBC    = os.environ.get("CHANNEL_WBC",    "") or CHANNEL_ID

SUBSCRIBE_LINE      = "<i>⚾️ Suscribete en t.me/UniversoBaseball</i>"
SUBSCRIBE_LINE_BOLD = "📲 <b>Suscribete en t.me/UniversoBaseball</b>"
VIDEOS_LINE         = "🎦 <b>Todos los videos del juego en: @homerunsmlb / @ubvideos</b>"
GAMES_TODAY_GIF = (
    "https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEjXxAzGsrFLFoVAQz-TbKEUpko0U2QhpGWF0EiksuZ_"
    "sxciC2yS1v6ilCzz9HHvzLfOH9-MKUgASBOAMzVfuduR-Ww_UzYAlNPI9RZju1rmR4DxpURoNkmg8naEHuPABgsQd2jkk3Bq"
    "Tlb3HzbgEDtRJuioYg6R1Vy4Nwiaw-PoUmimdenyfebBnOW17N4/w665-h443/juegos-de-hoy.gif"
)

# ─── Estado en memoria ────────────────────────────────────────
_sent_results:     set  = set()
_sent_lineups:     set  = set()
_bulk_state:       dict = {}       # {league: {"sent": bool, "date": str}}
_games_today_sent: str  = ""

# livescore activo: {game_pk: {"league": str, "last_inning": int,
#                               "chat_id": str, "task": asyncio.Task}}
_livescore_tasks: dict = {}


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


def _tz_for(league: str):
    return VZ_TZ if league in ("lvbp", "caribe") else ET_TZ


# ─────────────────────────────────────────────────────────────
#  BANDERAS Y PAÍSES
# ─────────────────────────────────────────────────────────────
_COUNTRY_MAP = {
    "toronto":          "CAN",
    "montreal":         "CAN",
    "dominican":        "DOM", "república dominicana": "DOM", "d.r.": "DOM",
    "venezuela":        "VEN", "venezuelan": "VEN",
    "puerto rico":      "PUR", "puerto rican": "PUR",
    "cuba":             "CUB",
    "mexico":           "MEX", "méxico": "MEX",
    "panama":           "PAN", "panamá": "PAN",
    "united states":    "USA", "team usa": "USA",
    "japan":            "JPN", "japón": "JPN",
    "korea":            "KOR", "south korea": "KOR",
    "australia":        "AUS",
    "italy":            "ITA", "italia": "ITA",
    "netherlands":      "NED", "países bajos": "NED",
    "nicaragua":        "NIC",
    "colombia":         "COL",
    "brazil":           "BRA", "brasil": "BRA",
    "chinese taipei":   "TPE",
    "china":            "CHN",
    "great britain":    "GBR",
    "israel":           "ISR",
    "new zealand":      "NZL",
    "south africa":     "ZAF",
    "czech republic":   "CZE",
}

_FLAG_EMOJI = {
    "DOM": "🇩🇴", "VEN": "🇻🇪", "PUR": "🇵🇷", "CUB": "🇨🇺",
    "MEX": "🇲🇽", "PAN": "🇵🇦", "USA": "🇺🇸", "JPN": "🇯🇵",
    "KOR": "🇰🇷", "AUS": "🇦🇺", "ITA": "🇮🇹", "NED": "🇳🇱",
    "CAN": "🇨🇦", "NIC": "🇳🇮", "COL": "🇨🇴", "BRA": "🇧🇷",
    "TPE": "🇹🇼", "CHN": "🇨🇳", "GBR": "🇬🇧", "ISR": "🇮🇱",
    "NZL": "🇳🇿", "ZAF": "🇿🇦", "CZE": "🇨🇿",
}


def _country_from_team(team_name: str) -> str | None:
    name_lower = team_name.lower()
    for keyword, code in _COUNTRY_MAP.items():
        if keyword in name_lower:
            return code
    return None


def _flag_emoji(country_code: str | None) -> str:
    if not country_code:
        return "⚾️"
    return _FLAG_EMOJI.get(country_code, "🏳️")


# ─────────────────────────────────────────────────────────────
#  ENVÍO SEGURO
# ─────────────────────────────────────────────────────────────
async def _send_photo(bot: Bot, chat_id: str, image_bytes: bytes, caption: str = ""):
    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=io.BytesIO(image_bytes),
            caption=caption[:1024],
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
#  FORMATO 1: JUEGOS DEL DÍA — todas las ligas en UN SOLO post
# ─────────────────────────────────────────────────────────────
async def publish_all_games_today(bot: Bot, date_str: str,
                                   session: aiohttp.ClientSession):
    global _games_today_sent
    today_str = date.today().strftime("%Y-%m-%d")
    if _games_today_sent == today_str:
        return

    lines     = ["🍿 ¡JUEGOS DE HOY! ⚾️", ""]
    found_any = False

    for league, fetcher, header in [
        ("mlb",    df.get_mlb_games,    "🇺🇸 | <b>MLB</b>"),
        ("lvbp",   df.get_lvbp_games,   "🇻🇪 | <b>LVBP Venezuela</b>"),
        ("caribe", df.get_caribe_games, "🌊 | <b>Serie del Caribe</b>"),
    ]:
        games = await fetcher(session, today_str)
        if not games:
            continue
        found_any = True
        lines.append(header)
        lines.append("")
        for g in games:
            away = df.get_team_info(g, "away")
            home = df.get_team_info(g, "home")
            t    = df.get_game_time_str(g, _tz_for(league))
            lines.append(f"{away['full_name']} - {home['full_name']}  {t}")
        lines.append("")

    wbc = await df.get_wbc_games_auto(session, today_str)
    if wbc:
        found_any = True
        lines.append("🌍 | <b>World Baseball Classic</b>")
        lines.append("")
        for g in wbc:
            away = df.get_team_info(g, "away")
            home = df.get_team_info(g, "home")
            t    = df.get_game_time_str(g, ET_TZ)
            lines.append(f"{away['full_name']} - {home['full_name']}  {t}")
        lines.append("")

    if not found_any:
        return

    lines.append(SUBSCRIBE_LINE)
    channel = CHANNEL_ID or ADMIN_ID
    try:
        await bot.send_animation(
            chat_id=channel,
            animation=GAMES_TODAY_GIF,
            caption="\n".join(lines)[:1024],
            parse_mode=ParseMode.HTML,
        )
        _games_today_sent = today_str
        logger.info("✅ Post juegos del día enviado")
    except TelegramError as e:
        logger.error(f"Error enviando GIF juegos del día: {e}")


# ─────────────────────────────────────────────────────────────
#  FORMATO 2: RESULTADOS BULK
# ─────────────────────────────────────────────────────────────
async def _build_game_data(game: dict, session: aiohttp.ClientSession) -> dict:
    game_pk = game.get("gamePk")
    away    = df.get_team_info(game, "away")
    home    = df.get_team_info(game, "home")
    box       = await df.get_game_boxscore(session, game_pk)
    linescore = await df.get_game_linescore(session, game_pk)
    decisions = game.get("decisions", {})
    away_country = _country_from_team(away["name"])
    home_country = _country_from_team(home["name"])
    return {
        "away_name": away["name"], "home_name": home["name"],
        "away_id":   away["id"] if not away_country else None,
        "home_id":   home["id"] if not home_country else None,
        "away_country": away_country, "home_country": home_country,
        "away_score": away["score"], "home_score": home["score"],
        "innings": linescore.get("currentInning", 9),
        "away_hits":   box.get("teams",{}).get("away",{}).get("teamStats",{}).get("batting", {}).get("hits",   "-"),
        "home_hits":   box.get("teams",{}).get("home",{}).get("teamStats",{}).get("batting", {}).get("hits",   "-"),
        "away_errors": box.get("teams",{}).get("away",{}).get("teamStats",{}).get("fielding",{}).get("errors", "-"),
        "home_errors": box.get("teams",{}).get("home",{}).get("teamStats",{}).get("fielding",{}).get("errors", "-"),
        "winner_pitcher": decisions.get("winner", {}).get("fullName", ""),
        "loser_pitcher":  decisions.get("loser",  {}).get("fullName", ""),
        "save_pitcher":   decisions.get("save",   {}).get("fullName", ""),
    }


async def publish_result_individual(bot: Bot, game: dict, league: str,
                                     session: aiohttp.ClientSession):
    """Publica el resultado de UN juego apenas termina."""
    key = _game_key(game, f"result_{league}")
    if key in _sent_results:
        return
    _sent_results.add(key)

    away = df.get_team_info(game, "away")
    home = df.get_team_info(game, "home")

    game_data   = await _build_game_data(game, session)
    image_bytes = await ig.generate_final_result_image(game_data, league)

    lines = [
        "📢 | <b>FINAL DEL JUEGO</b>",
        "",
        f"↪️ {away['full_name']} {away['score']}-{home['score']} {home['full_name']}",
        "",
        VIDEOS_LINE,
        "",
        SUBSCRIBE_LINE,
    ]
    await _send_photo(bot, _channel_for(league), image_bytes, "\n".join(lines))
    logger.info(f"[{league.upper()}] Resultado: {away['full_name']} {away['score']}-{home['score']} {home['full_name']}")


# ─────────────────────────────────────────────────────────────
#  FORMATO 3: ALINEACIONES
# ─────────────────────────────────────────────────────────────
async def publish_lineup(bot: Bot, game: dict, league: str,
                          session: aiohttp.ClientSession):
    key = _game_key(game, f"lineup_{league}")
    if key in _sent_lineups:
        return
    game_pk = game.get("gamePk")
    away    = df.get_team_info(game, "away")
    home    = df.get_team_info(game, "home")
    lineups     = df.get_lineup_from_game(game)
    away_lineup = lineups.get("away", [])
    home_lineup = lineups.get("home", [])
    if not away_lineup and not home_lineup:
        box          = await df.get_game_boxscore(session, game_pk)
        away_batters = box.get("teams",{}).get("away",{}).get("batters", [])
        home_batters = box.get("teams",{}).get("home",{}).get("batters", [])
        away_players = box.get("teams",{}).get("away",{}).get("players", {})
        home_players = box.get("teams",{}).get("home",{}).get("players", {})
        away_lineup  = [away_players.get(f"ID{p}",{}).get("person",{})
                        for p in away_batters[:9] if f"ID{p}" in away_players]
        home_lineup  = [home_players.get(f"ID{p}",{}).get("person",{})
                        for p in home_batters[:9] if f"ID{p}" in home_players]
    if not away_lineup and not home_lineup:
        return
    _sent_lineups.add(key)
    away_country = _country_from_team(away["name"])
    home_country = _country_from_team(home["name"])
    game_data = {
        "away_name": away["name"], "home_name": home["name"],
        "away_id":   away["id"] if not away_country else None,
        "home_id":   home["id"] if not home_country else None,
        "away_country": away_country, "home_country": home_country,
    }
    image_bytes = await ig.generate_lineup_image(game_data, league, away_lineup, home_lineup)
    def _names(lu):
        return ", ".join(p.get("fullName", "") for p in lu if p.get("fullName"))
    lines = [
        f"👥 <b>ALINEACIONES #{league.upper()} | {away['name']} vs. {home['name']}</b>",
        "",
        f"{_flag_emoji(away_country)} <b>{away['name']}</b>: {_names(away_lineup)}",
        "",
        f"{_flag_emoji(home_country)} <b>{home['name']}</b>: {_names(home_lineup)}",
        "",
        SUBSCRIBE_LINE,
    ]
    await _send_photo(bot, _channel_for(league), image_bytes, "\n".join(lines))
    logger.info(f"[{league.upper()}] Lineup: {away['name']} vs {home['name']}")


# ─────────────────────────────────────────────────────────────
#  LIVESCORE — actualiza por inning, publica al canal
# ─────────────────────────────────────────────────────────────
async def _livescore_loop(bot: Bot, game_pk: int, league: str, chat_id: str):
    """
    Monitorea un juego y publica al canal cada vez que cambia el inning.
    Formato:
        ❗️ | LIVESCORE (bold)

        Equipo X-X Equipo (bold)

        ➡️ Inning N°

        📲 Suscribete... (bold)
    """
    logger.info(f"[LIVESCORE] Iniciando para gamePk={game_pk}")
    last_inning = -1
    consecutive_errors = 0

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                linescore = await df.get_game_linescore(session, game_pk)
                schedule  = await df.fetch_json(
                    session,
                    f"https://statsapi.mlb.com/api/v1/schedule",
                    {"gamePk": game_pk, "hydrate": "linescore,decisions"}
                )

            # Extraer datos del juego
            game_obj = None
            if schedule and schedule.get("dates"):
                for d in schedule["dates"]:
                    for g in d.get("games", []):
                        if g.get("gamePk") == game_pk:
                            game_obj = g
                            break

            if not game_obj:
                consecutive_errors += 1
                if consecutive_errors > 5:
                    logger.warning(f"[LIVESCORE] gamePk={game_pk} no encontrado, cancelando")
                    break
                await asyncio.sleep(60)
                continue

            consecutive_errors = 0
            status = game_obj.get("status", {}).get("abstractGameState", "")

            # Juego terminó — publicar resultado final y parar
            if status == "Final":
                away = df.get_team_info(game_obj, "away")
                home = df.get_team_info(game_obj, "home")
                innings = linescore.get("currentInning", 9)
                text = (
                    "❗️ | <b>LIVESCORE</b>\n\n"
                    f"<b>{away['full_name']} {away['score']}-{home['score']} {home['full_name']}</b>\n\n"
                    f"✅ Juego finalizado — {innings} entradas\n\n"
                    f"{SUBSCRIBE_LINE_BOLD}"
                )
                await _send_message(bot, chat_id, text)
                logger.info(f"[LIVESCORE] gamePk={game_pk} finalizado")
                break

            # Solo publicar si cambió el inning
            if status == "Live":
                current_inning = linescore.get("currentInning", 0)
                inning_half    = linescore.get("inningHalf", "")   # "Top" / "Bottom"
                inning_ordinal = linescore.get("currentInningOrdinal", f"#{current_inning}")

                if current_inning != last_inning and current_inning > 0:
                    last_inning = current_inning
                    away = df.get_team_info(game_obj, "away")
                    home = df.get_team_info(game_obj, "home")

                    half_str = "▲" if inning_half == "Top" else "▼"
                    text = (
                        "❗️ | <b>LIVESCORE</b>\n\n"
                        f"<b>{away['full_name']} {away['score']}-{home['score']} {home['full_name']}</b>\n\n"
                        f"➡️ {half_str} {inning_ordinal}\n\n"
                        f"{SUBSCRIBE_LINE_BOLD}"
                    )
                    await _send_message(bot, chat_id, text)
                    logger.info(f"[LIVESCORE] gamePk={game_pk} inning {inning_ordinal}")

        except asyncio.CancelledError:
            logger.info(f"[LIVESCORE] gamePk={game_pk} cancelado")
            break
        except Exception as e:
            logger.error(f"[LIVESCORE] Error gamePk={game_pk}: {e}")

        await asyncio.sleep(90)   # revisar cada 90 segundos

    # Limpiar del registro
    _livescore_tasks.pop(game_pk, None)


# ─────────────────────────────────────────────────────────────
#  SCHEDULER
#  - Juegos del día: se publica exactamente a las 00:00 UTC-4
#  - Resultados / lineups: se revisan cada 5 minutos
# ─────────────────────────────────────────────────────────────
def _seconds_until_midnight_vz() -> float:
    """Segundos que faltan para las 00:00 en UTC-4 (Venezuela/Caracas)."""
    now_vz    = datetime.now(VZ_TZ)
    tomorrow  = (now_vz + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0)
    return (tomorrow - now_vz).total_seconds()


async def run_scheduler(bot: Bot):
    logger.info("Scheduler iniciado ✅")

    # ── Tarea de juegos del día: publica exactamente a las 00:00 VZ ──
    # NO publica al arrancar — solo en medianoche.
    async def _games_today_loop():
        while True:
            # Dormir hasta la próxima medianoche VZ
            secs = _seconds_until_midnight_vz()
            logger.info(f"[games_today] Próxima publicación en {secs/3600:.1f}h (00:00 VZ)")
            await asyncio.sleep(secs + 5)   # +5s de margen para asegurarse

            try:
                now_vz       = datetime.now(VZ_TZ)
                date_display = now_vz.strftime("%d de %B de %Y")
                async with aiohttp.ClientSession() as s:
                    await publish_all_games_today(bot, date_display, s)
            except Exception as e:
                logger.error(f"[games_today] Error: {e}", exc_info=True)

    # Lanzar la tarea de medianoche en paralelo
    asyncio.create_task(_games_today_loop())

    # ── Loop principal: resultados + lineups cada 5 minutos ──
    while True:
        try:
            today_str = date.today().strftime("%Y-%m-%d")

            async with aiohttp.ClientSession() as session:
                # MLB
                mlb_games = await df.get_mlb_games(session, today_str)
                for game in mlb_games:
                    if df.is_game_final(game):
                        await publish_result_individual(bot, game, "mlb", session)
                    if df.is_postseason(game):
                        await publish_lineup(bot, game, "mlb", session)
                await asyncio.sleep(5)

                # LVBP
                lvbp_games = await df.get_lvbp_games(session, today_str)
                for game in lvbp_games:
                    if df.is_game_final(game):
                        await publish_result_individual(bot, game, "lvbp", session)
                    await publish_lineup(bot, game, "lvbp", session)
                await asyncio.sleep(5)

                # Caribe
                caribe_games = await df.get_caribe_games(session, today_str)
                for game in caribe_games:
                    if df.is_game_final(game):
                        await publish_result_individual(bot, game, "caribe", session)
                    await publish_lineup(bot, game, "caribe", session)
                await asyncio.sleep(5)

                # WBC
                wbc_games = await df.get_wbc_games_auto(session, today_str)
                for game in wbc_games:
                    if df.is_game_final(game):
                        await publish_result_individual(bot, game, "wbc", session)
                    await publish_lineup(bot, game, "wbc", session)

        except Exception as e:
            logger.error(f"Error en scheduler: {e}", exc_info=True)

        await asyncio.sleep(300)


# ─────────────────────────────────────────────────────────────
#  COMANDOS
# ─────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚾ <b>Baseball Bot</b>\n\n"
        "<b>Comandos:</b>\n"
        "/juegos — Juegos de hoy de todas las ligas\n"
        "/resultados — Resultados del día\n"
        "/livescore — Iniciar livescore de un partido\n"
        "/lineup — Forzar envío de alineaciones al canal\n"
        "/test — Preview de imagen de resultado\n"
        "/status — Estado del bot\n"
        "/hoy — Seguimiento en vivo (solo en privado)",
        parse_mode=ParseMode.HTML,
    )


async def cmd_juegos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_str = date.today().strftime("%Y-%m-%d")
    lines     = ["🍿 ¡JUEGOS DE HOY! ⚾️", ""]
    found_any = False

    async with aiohttp.ClientSession() as session:
        for league, fetcher, header in [
            ("mlb",    df.get_mlb_games,    "🇺🇸 | <b>MLB</b>"),
            ("lvbp",   df.get_lvbp_games,   "🇻🇪 | <b>LVBP Venezuela</b>"),
            ("caribe", df.get_caribe_games, "🌊 | <b>Serie del Caribe</b>"),
        ]:
            games = await fetcher(session, today_str)
            if not games:
                continue
            found_any = True
            lines.append(header)
            lines.append("")
            for g in games:
                away = df.get_team_info(g, "away")
                home = df.get_team_info(g, "home")
                t    = df.get_game_time_str(g, _tz_for(league))
                lines.append(f"{away['full_name']} - {home['full_name']}  {t}")
            lines.append("")

        wbc = await df.get_wbc_games_auto(session, today_str)
        if wbc:
            found_any = True
            lines.append("🌍 | <b>World Baseball Classic</b>")
            lines.append("")
            for g in wbc:
                away = df.get_team_info(g, "away")
                home = df.get_team_info(g, "home")
                t    = df.get_game_time_str(g, ET_TZ)
                lines.append(f"{away['full_name']} - {home['full_name']}  {t}")
            lines.append("")

    if not found_any:
        await update.message.reply_text("No hay juegos programados para hoy.")
        return
    lines.append(SUBSCRIBE_LINE)
    await update.message.reply_animation(
        animation=GAMES_TODAY_GIF,
        caption="\n".join(lines)[:1024],
        parse_mode=ParseMode.HTML,
    )


async def cmd_hoy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Seguimiento en vivo — solo privado."""
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "Este comando solo funciona en privado para no spamear el canal."
        )
        return
    today_str = date.today().strftime("%Y-%m-%d")
    lines     = [f"🔴 <b>EN VIVO — {date.today().strftime('%d/%m/%Y')}</b>\n"]
    async with aiohttp.ClientSession() as session:
        for label, fetcher, emoji, tz in [
            ("MLB",          df.get_mlb_games,    "🇺🇸", ET_TZ),
            ("LVBP",         df.get_lvbp_games,   "🇻🇪", VZ_TZ),
            ("Serie Caribe", df.get_caribe_games, "🌊",  VZ_TZ),
        ]:
            games = await fetcher(session, today_str)
            if not games:
                continue
            lines.append(f"{emoji} | <b>{label}</b>")
            lines.append("")
            for g in games:
                away  = df.get_team_info(g, "away")
                home  = df.get_team_info(g, "home")
                t     = df.get_game_time_str(g, tz)
                state = g.get("status", {}).get("abstractGameState", "")
                if state == "Final":
                    lines.append(f"✅ {away['full_name']} <b>{away['score']}</b>-<b>{home['score']}</b> {home['full_name']}")
                elif state == "Live":
                    inn = g.get("linescore", {}).get("currentInningOrdinal", "")
                    lines.append(f"🔴 {away['full_name']} {away['score']}-{home['score']} {home['full_name']} ({inn})")
                else:
                    lines.append(f"⏳ {away['full_name']} - {home['full_name']}  {t}")
            lines.append("")
        wbc = await df.get_wbc_games_auto(session, today_str)
        if wbc:
            lines.append("🌍 | <b>World Baseball Classic</b>")
            lines.append("")
            for g in wbc:
                away  = df.get_team_info(g, "away")
                home  = df.get_team_info(g, "home")
                state = g.get("status", {}).get("abstractGameState", "")
                t     = df.get_game_time_str(g, ET_TZ)
                if state == "Final":
                    lines.append(f"✅ {away['full_name']} <b>{away['score']}</b>-<b>{home['score']}</b> {home['full_name']}")
                elif state == "Live":
                    lines.append(f"🔴 {away['full_name']} {away['score']}-{home['score']} {home['full_name']}")
                else:
                    lines.append(f"⏳ {away['full_name']} - {home['full_name']}  {t}")
            lines.append("")
    if len(lines) == 1:
        lines.append("No hay juegos hoy.")
    lines.append(SUBSCRIBE_LINE)
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_livescore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Muestra los juegos en curso/próximos con botones.
    El usuario selecciona uno y el bot inicia el livescore al canal.
    """
    today_str = date.today().strftime("%Y-%m-%d")
    candidates = []   # (game_pk, label, league)

    async with aiohttp.ClientSession() as session:
        for league, fetcher in [
            ("mlb",    df.get_mlb_games),
            ("lvbp",   df.get_lvbp_games),
            ("caribe", df.get_caribe_games),
        ]:
            games = await fetcher(session, today_str)
            for g in games:
                state = g.get("status", {}).get("abstractGameState", "")
                if state in ("Live", "Preview"):
                    away  = df.get_team_info(g, "away")
                    home  = df.get_team_info(g, "home")
                    label = f"{away['abbreviation'] or away['name']} vs {home['abbreviation'] or home['name']}"
                    if state == "Live":
                        label = "🔴 " + label
                    else:
                        label = "⏳ " + label
                    candidates.append((g.get("gamePk"), label, league))

        wbc = await df.get_wbc_games_auto(session, today_str)
        for g in wbc:
            state = g.get("status", {}).get("abstractGameState", "")
            if state in ("Live", "Preview"):
                away  = df.get_team_info(g, "away")
                home  = df.get_team_info(g, "home")
                label = f"{away['name']} vs {home['name']}"
                if state == "Live":
                    label = "🔴 " + label
                candidates.append((g.get("gamePk"), label, "wbc"))

    if not candidates:
        await update.message.reply_text("No hay juegos en curso o próximos ahora mismo.")
        return

    # Construir teclado inline
    keyboard = []
    for game_pk, label, league in candidates[:10]:
        keyboard.append([InlineKeyboardButton(
            label, callback_data=f"ls_{game_pk}_{league}"
        )])
    # Botón para cancelar todos
    if _livescore_tasks:
        keyboard.append([InlineKeyboardButton(
            "⛔ Detener todos los livescores", callback_data="ls_stop_all"
        )])

    await update.message.reply_text(
        "⚾ <b>Selecciona el juego para activar el livescore al canal:</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )


async def cb_livescore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback del teclado inline de /livescore."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "ls_stop_all":
        for task in _livescore_tasks.values():
            t = task.get("task")
            if t and not t.done():
                t.cancel()
        _livescore_tasks.clear()
        await query.edit_message_text("⛔ Todos los livescores detenidos.")
        return

    # Formato: ls_{game_pk}_{league}
    parts    = data.split("_", 2)
    game_pk  = int(parts[1])
    league   = parts[2]
    chat_id  = _channel_for(league)

    if game_pk in _livescore_tasks:
        await query.edit_message_text(f"⚠️ Ya hay un livescore activo para ese juego.")
        return

    task = asyncio.create_task(
        _livescore_loop(context.bot, game_pk, league, chat_id)
    )
    _livescore_tasks[game_pk] = {"league": league, "task": task}

    await query.edit_message_text(
        f"✅ <b>Livescore activado</b> para el juego #{game_pk}\n"
        f"Se publicarán actualizaciones en el canal por cada inning.",
        parse_mode=ParseMode.HTML,
    )
    logger.info(f"[LIVESCORE] Activado para gamePk={game_pk} → canal {chat_id}")


async def cmd_lineup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_str  = date.today().strftime("%Y-%m-%d")
    sent_count = 0
    async with aiohttp.ClientSession() as session:
        for league, fetcher in [
            ("mlb",    df.get_mlb_games),
            ("lvbp",   df.get_lvbp_games),
            ("caribe", df.get_caribe_games),
        ]:
            games = await fetcher(session, today_str)
            for game in games:
                if league == "mlb" and not df.is_postseason(game):
                    continue
                key = _game_key(game, f"lineup_{league}")
                _sent_lineups.discard(key)
                before = len(_sent_lineups)
                await publish_lineup(update.get_bot(), game, league, session)
                if len(_sent_lineups) > before:
                    sent_count += 1
        wbc = await df.get_wbc_games_auto(session, today_str)
        for game in wbc:
            key = _game_key(game, "lineup_wbc")
            _sent_lineups.discard(key)
            before = len(_sent_lineups)
            await publish_lineup(update.get_bot(), game, "wbc", session)
            if len(_sent_lineups) > before:
                sent_count += 1
    msg = f"✅ {sent_count} alineación(es) enviada(s)." if sent_count \
          else "No hay alineaciones disponibles ahora."
    await update.message.reply_text(msg)


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Busca juegos reales finalizados hoy para hacer el preview.
    Si no hay juegos reales usa datos de ejemplo.
    Uso: /test  o  /test mlb|lvbp|caribe|wbc
    """
    args   = context.args
    league = (args[0].lower() if args else "mlb")
    if league not in ("mlb", "lvbp", "caribe", "wbc"):
        await update.message.reply_text("Uso: /test mlb|lvbp|caribe|wbc")
        return

    today_str = date.today().strftime("%Y-%m-%d")
    game_data = None

    await update.message.reply_text("🔄 Buscando juego finalizado...")

    async with aiohttp.ClientSession() as session:
        # Intentar encontrar un juego real finalizado hoy
        try:
            if league == "wbc":
                games = await df.get_wbc_games_auto(session, today_str)
            elif league == "mlb":
                games = await df.get_mlb_games(session, today_str)
            elif league == "lvbp":
                games = await df.get_lvbp_games(session, today_str)
            else:
                games = await df.get_caribe_games(session, today_str)

            finals = [g for g in games if df.is_game_final(g)]
            if finals:
                game_data = await _build_game_data(finals[0], session)
        except Exception as e:
            logger.warning(f"[/test] Error buscando juego real: {e}")

    # Fallback a datos de ejemplo si no hay juego real
    if not game_data:
        fallbacks = {
            "mlb": {
                "away_name": "New York Yankees", "home_name": "Los Angeles Dodgers",
                "away_id": 147, "home_id": 119,
                "away_country": None, "home_country": None,
                "away_score": 5, "home_score": 3, "innings": 9,
                "away_hits": 10, "home_hits": 7, "away_errors": 0, "home_errors": 1,
                "winner_pitcher": "Gerrit Cole", "loser_pitcher": "Clayton Kershaw",
                "save_pitcher": "Clay Holmes",
            },
            "lvbp": {
                "away_name": "Leones del Caracas", "home_name": "Cardenales de Lara",
                "away_id": None, "home_id": None,
                "away_country": "VEN", "home_country": "VEN",
                "away_score": 4, "home_score": 2, "innings": 9,
                "away_hits": 8, "home_hits": 5, "away_errors": 1, "home_errors": 0,
                "winner_pitcher": "José Rodríguez", "loser_pitcher": "Carlos Pérez",
                "save_pitcher": "",
            },
            "caribe": {
                "away_name": "Venezuela", "home_name": "Dominican Republic",
                "away_id": None, "home_id": None,
                "away_country": "VEN", "home_country": "DOM",
                "away_score": 3, "home_score": 6, "innings": 9,
                "away_hits": 6, "home_hits": 11, "away_errors": 2, "home_errors": 0,
                "winner_pitcher": "Framber Valdez", "loser_pitcher": "Rangel Ravelo",
                "save_pitcher": "",
            },
            "wbc": {
                "away_name": "Japan", "home_name": "United States",
                "away_id": None, "home_id": None,
                "away_country": "JPN", "home_country": "USA",
                "away_score": 3, "home_score": 2, "innings": 9,
                "away_hits": 7, "home_hits": 6, "away_errors": 0, "home_errors": 1,
                "winner_pitcher": "Shohei Ohtani", "loser_pitcher": "Adam Wainwright",
                "save_pitcher": "Yoshinobu Yamamoto",
            },
        }
        game_data = fallbacks[league]
        note = " (datos de ejemplo — no hay juego finalizado hoy)"
    else:
        note = f" (juego real de hoy: {game_data['away_name']} vs {game_data['home_name']})"

    image_bytes = await ig.generate_final_result_image(game_data, league)
    await update.message.reply_photo(
        photo=io.BytesIO(image_bytes),
        caption=f"🧪 <b>Preview {league.upper()}</b>{note}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_resultados(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_str = date.today().strftime("%Y-%m-%d")
    lines     = ["📢 | <b>RESULTADOS DE HOY</b>\n"]
    found_any = False
    async with aiohttp.ClientSession() as session:
        for label, fetcher, emoji in [
            ("MLB",          df.get_mlb_games,    "🇺🇸"),
            ("LVBP",         df.get_lvbp_games,   "🇻🇪"),
            ("Serie Caribe", df.get_caribe_games, "🌊"),
        ]:
            games  = await fetcher(session, today_str)
            finals = [g for g in games if df.is_game_final(g)]
            if not finals:
                continue
            found_any = True
            lines.append(f"{emoji} | <b>{label}</b>")
            lines.append("")
            for g in finals:
                away = df.get_team_info(g, "away")
                home = df.get_team_info(g, "home")
                lines.append(
                    f"↪️ {away['full_name']} <b>{away['score']}</b>-<b>{home['score']}</b> {home['full_name']}"
                )
            lines.append("")
        wbc   = await df.get_wbc_games_auto(session, today_str)
        finals_wbc = [g for g in wbc if df.is_game_final(g)]
        if finals_wbc:
            found_any = True
            lines.append("🌍 | <b>WBC</b>")
            lines.append("")
            for g in finals_wbc:
                away = df.get_team_info(g, "away")
                home = df.get_team_info(g, "home")
                lines.append(
                    f"↪️ {away['full_name']} <b>{away['score']}</b>-<b>{home['score']}</b> {home['full_name']}"
                )
            lines.append("")
    if not found_any:
        lines.append("Aún no hay resultados finales.")
    lines.append(SUBSCRIBE_LINE)
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_str = date.today().strftime("%Y-%m-%d")
    results_sent = len([k for k in _sent_results if league_key in k for league_key in ["mlb","lvbp","caribe","wbc"]])
    ls_active = [f"#{pk} ({v['league']})" for pk, v in _livescore_tasks.items()]
    await update.message.reply_text(
        f"✅ <b>Bot activo</b>\n"
        f"📅 Fecha: {today_str}\n"
        f"📊 Resultados enviados hoy: {len(_sent_results)}\n"
        f"📋 Lineups enviados: {len(_sent_lineups)}\n"
        f"🍿 Juegos del día: {'✅ enviado' if _games_today_sent == today_str else '⏳ pendiente'}\n"
        f"🔴 Livescores activos: {ls_active or 'ninguno'}\n"
        f"📡 Canal: {CHANNEL_ID or 'no configurado'}",
        parse_mode=ParseMode.HTML,
    )


# ─────────────────────────────────────────────────────────────
#  CLASE PRINCIPAL
# ─────────────────────────────────────────────────────────────
class BaseballBot:
    def __init__(self):
        self.app = Application.builder().token(BOT_TOKEN).build()
        self.app.add_handler(CommandHandler("start",      cmd_start))
        self.app.add_handler(CommandHandler("juegos",     cmd_juegos))
        self.app.add_handler(CommandHandler("hoy",        cmd_hoy))
        self.app.add_handler(CommandHandler("livescore",  cmd_livescore))
        self.app.add_handler(CommandHandler("lineup",     cmd_lineup))
        self.app.add_handler(CommandHandler("test",       cmd_test))
        self.app.add_handler(CommandHandler("resultados", cmd_resultados))
        self.app.add_handler(CommandHandler("status",     cmd_status))
        self.app.add_handler(CallbackQueryHandler(cb_livescore, pattern=r"^ls_"))

    async def run(self):
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info("Bot iniciado ✅")
        try:
            await run_scheduler(self.app.bot)
        finally:
            # Cancelar livescores activos al apagar
            for v in _livescore_tasks.values():
                t = v.get("task")
                if t and not t.done():
                    t.cancel()
            logger.info("Apagando bot...")
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
