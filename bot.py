"""
bot.py — Bot principal de Telegram para béisbol
MLB, LVBP, Serie del Caribe y WBC.
"""
import os
import io
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

BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
ADMIN_ID       = os.environ.get("ADMIN_ID", "")
CHANNEL_ID     = os.environ.get("CHANNEL_ID", "")
CHANNEL_MLB    = os.environ.get("CHANNEL_MLB",    "") or CHANNEL_ID
CHANNEL_LVBP   = os.environ.get("CHANNEL_LVBP",   "") or CHANNEL_ID
CHANNEL_CARIBE = os.environ.get("CHANNEL_CARIBE", "") or CHANNEL_ID
CHANNEL_WBC    = os.environ.get("CHANNEL_WBC",    "") or CHANNEL_ID

SUBSCRIBE_LINE = "<i>⚾️ Suscribete en t.me/UniversoBaseball</i>"
VIDEOS_LINE    = "🎦 <b>Todos los videos del juego en: @homerunsmlb / @ubvideos</b>"
GAMES_TODAY_GIF = (
    "https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEjXxAzGsrFLFoVAQz-TbKEUpko0U2QhpGWF0EiksuZ_"
    "sxciC2yS1v6ilCzz9HHvzLfOH9-MKUgASBOAMzVfuduR-Ww_UzYAlNPI9RZju1rmR4DxpURoNkmg8naEHuPABgsQd2jkk3Bq"
    "Tlb3HzbgEDtRJuioYg6R1Vy4Nwiaw-PoUmimdenyfebBnOW17N4/w665-h443/juegos-de-hoy.gif"
)

_sent_results: set = set()
_sent_lineups: set = set()
# Para MLB: acumular resultados del día hasta que todos estén listos
_mlb_pending_results: dict = {}   # gamePk -> game_data dict
_mlb_results_sent_today: bool = False
_mlb_results_date: str = ""


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


# ─────────────────────────────────────────────────────────────
#  UTILIDADES DE BANDERAS Y PAÍSES
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

_FLAG_EMOJI = {
    "DOM": "🇩🇴", "VEN": "🇻🇪", "PUR": "🇵🇷", "CUB": "🇨🇺",
    "MEX": "🇲🇽", "PAN": "🇵🇦", "USA": "🇺🇸", "JPN": "🇯🇵",
    "KOR": "🇰🇷", "AUS": "🇦🇺", "ITA": "🇮🇹", "NED": "🇳🇱",
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
#  FORMATO 1: JUEGOS DEL DÍA — GIF + texto con nombres completos
#  🍿 ¡JUEGOS DE HOY! ⚾️
#
#  🇺🇸 | MLB (bold)
#
#  New York Yankees - Boston Red Sox  19:05
#  ...
#
#  ⚾️ Suscribete en t.me/UniversoBaseball (italic)
# ─────────────────────────────────────────────────────────────
async def publish_games_today(bot: Bot, league: str, games: list,
                               date_str: str, league_logo_url: str = None):
    if not games:
        return

    display = _parse_games_for_display(games, league)

    league_headers = {
        "mlb":    "🇺🇸 | <b>MLB</b>",
        "lvbp":   "🇻🇪 | <b>LVBP Venezuela</b>",
        "caribe": "🌊 | <b>Serie del Caribe</b>",
        "wbc":    "🌍 | <b>World Baseball Classic</b>",
    }

    lines = [
        "🍿 ¡JUEGOS DE HOY! ⚾️",
        "",
        league_headers.get(league, f"⚾️ | <b>{league.upper()}</b>"),
        "",
    ]

    for g in display:
        # Siempre nombre completo, nunca abreviación
        away = g["away_name"]
        home = g["home_name"]
        lines.append(f"{away} - {home}  {g['time']}")

    lines += ["", SUBSCRIBE_LINE]

    caption = "\n".join(lines)
    channel = _channel_for(league)
    try:
        await bot.send_animation(
            chat_id=channel,
            animation=GAMES_TODAY_GIF,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        logger.error(f"Error enviando GIF juegos del día a {channel}: {e}")

    logger.info(f"[{league.upper()}] Juegos del día publicados: {len(display)}")


# ─────────────────────────────────────────────────────────────
#  FORMATO 2: RESULTADO FINAL (LVBP / WBC / Caribe — uno por juego)
#  📢 | FINAL DEL JUEGO (bold)
#
#  ↪️ Yankees 5-3 Red Sox
#
#  🎦 Todos los videos del juego en: @homerunsmlb / @ubvideos (bold)
#
#  ⚾️ Suscribete en t.me/UniversoBaseball (italic)
# ─────────────────────────────────────────────────────────────
async def publish_single_result(bot: Bot, game: dict, league: str,
                                 session: aiohttp.ClientSession):
    """Resultado individual — para LVBP, Caribe, WBC y MLB postseason."""
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
        "away_name": away["name"], "home_name": home["name"],
        "away_id":   away["id"] if not away_country else None,
        "home_id":   home["id"] if not home_country else None,
        "away_country": away_country, "home_country": home_country,
        "away_score": away["score"], "home_score": home["score"],
        "innings": innings,
        "away_hits": away_hits, "home_hits": home_hits,
        "away_errors": away_errors, "home_errors": home_errors,
        "winner_pitcher": winner_p, "loser_pitcher": loser_p, "save_pitcher": save_p,
    }

    image_bytes = await ig.generate_final_result_image(game_data, league)

    away_display = away["abbreviation"] or away["name"]
    home_display = home["abbreviation"] or home["name"]

    lines = [
        "📢 | <b>FINAL DEL JUEGO</b>",
        "",
        f"↪️ {away_display} {away['score']}-{home['score']} {home_display}",
    ]
    if winner_p or loser_p:
        lines.append("")
        if winner_p: lines.append(f"✅ W: {winner_p}")
        if loser_p:  lines.append(f"❌ L: {loser_p}")
        if save_p:   lines.append(f"💾 S: {save_p}")

    lines += [
        "",
        VIDEOS_LINE,
        "",
        SUBSCRIBE_LINE,
    ]

    await _send_photo(bot, _channel_for(league), image_bytes, "\n".join(lines))
    logger.info(f"[{league.upper()}] Resultado: {away_display} {away['score']}-{home['score']} {home_display}")


# ─────────────────────────────────────────────────────────────
#  FORMATO 2 MLB: TODOS LOS RESULTADOS EN UN SOLO MENSAJE
#  📢 | FINAL DEL JUEGO (bold)
#
#  ↪️ Yankees 5-3 Red Sox
#  ↪️ Dodgers 7-2 Giants
#  ...
#
#  🎦 Todos los videos del juego en: @homerunsmlb / @ubvideos (bold)
#
#  ⚾️ Suscribete en t.me/UniversoBaseball (italic)
# ─────────────────────────────────────────────────────────────
async def publish_mlb_results_bulk(bot: Bot, games: list,
                                    session: aiohttp.ClientSession):
    """
    Espera a que TODOS los juegos MLB del día estén finales
    y publica todos los resultados en un solo mensaje con imagen.
    """
    global _mlb_results_sent_today, _mlb_results_date

    today_str = date.today().strftime("%Y-%m-%d")

    # Reset si es un nuevo día
    if _mlb_results_date != today_str:
        _mlb_results_sent_today = False
        _mlb_results_date = today_str
        _mlb_pending_results.clear()

    if _mlb_results_sent_today:
        return

    # Filtrar solo juegos de hoy que ya terminaron
    final_games = [g for g in games if df.is_game_final(g)]
    non_final   = [g for g in games if not df.is_game_final(g)
                   and not df.is_game_postponed(g)]

    # Si aún hay juegos en curso o por empezar, esperar
    if non_final:
        logger.info(f"[MLB] Esperando {len(non_final)} juego(s) restante(s) para publicar resultados")
        return

    if not final_games:
        return

    # Marcar como enviado
    _mlb_results_sent_today = True

    # Construir texto con todos los resultados
    lines = [
        "📢 | <b>FINAL DEL JUEGO</b>",
        "",
    ]

    for game in final_games:
        away = df.get_team_info(game, "away")
        home = df.get_team_info(game, "home")
        away_d = away["abbreviation"] or away["name"]
        home_d = home["abbreviation"] or home["name"]
        lines.append(f"↪️ {away_d} {away['score']}-{home['score']} {home_d}")

    lines += [
        "",
        VIDEOS_LINE,
        "",
        SUBSCRIBE_LINE,
    ]

    # Usar el primer juego para generar imagen representativa
    first_game = final_games[0]
    away = df.get_team_info(first_game, "away")
    home = df.get_team_info(first_game, "home")

    box = await df.get_game_boxscore(session, first_game.get("gamePk"))
    linescore = await df.get_game_linescore(session, first_game.get("gamePk"))
    decisions = first_game.get("decisions", {})

    game_data = {
        "away_name": away["name"], "home_name": home["name"],
        "away_id": away["id"], "home_id": home["id"],
        "away_country": None, "home_country": None,
        "away_score": away["score"], "home_score": home["score"],
        "innings": linescore.get("currentInning", 9),
        "away_hits": box.get("teams", {}).get("away", {}).get("teamStats", {}).get("batting", {}).get("hits", "-"),
        "home_hits": box.get("teams", {}).get("home", {}).get("teamStats", {}).get("batting", {}).get("hits", "-"),
        "away_errors": box.get("teams", {}).get("away", {}).get("teamStats", {}).get("fielding", {}).get("errors", "-"),
        "home_errors": box.get("teams", {}).get("home", {}).get("teamStats", {}).get("fielding", {}).get("errors", "-"),
        "winner_pitcher": decisions.get("winner", {}).get("fullName", ""),
        "loser_pitcher":  decisions.get("loser",  {}).get("fullName", ""),
        "save_pitcher":   decisions.get("save",   {}).get("fullName", ""),
    }

    image_bytes = await ig.generate_final_result_image(game_data, "mlb")
    await _send_photo(bot, _channel_for("mlb"), image_bytes, "\n".join(lines))
    logger.info(f"[MLB] Resultados bulk publicados: {len(final_games)} juegos finales")


# ─────────────────────────────────────────────────────────────
#  FORMATO 3: ALINEACIONES
#  👥 ALINEACIONES #WBC | Korea vs. Dominican Republic (bold)
#
#  🇰🇷 Korea (bold): Name, Name, Name, ...
#
#  🇩🇴 Dominican Republic (bold): Name, Name, Name, ...
#
#  ⚾️ Suscribete en t.me/UniversoBaseball (italic)
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
        "away_name": away["name"], "home_name": home["name"],
        "away_id":   away["id"] if not away_country else None,
        "home_id":   home["id"] if not home_country else None,
        "away_country": away_country, "home_country": home_country,
    }

    image_bytes = await ig.generate_lineup_image(game_data, league, away_lineup, home_lineup)

    # Nombres separados por coma
    def _names(lineup):
        return ", ".join(p.get("fullName", "") for p in lineup if p.get("fullName"))

    away_flag  = _flag_emoji(away_country)
    home_flag  = _flag_emoji(home_country)
    league_tag = f"#{league.upper()}"

    lines = [
        f"👥 <b>ALINEACIONES {league_tag} | {away['name']} vs. {home['name']}</b>",
        "",
        f"{away_flag} <b>{away['name']}</b>: {_names(away_lineup)}",
        "",
        f"{home_flag} <b>{home['name']}</b>: {_names(home_lineup)}",
        "",
        SUBSCRIBE_LINE,
    ]

    await _send_photo(bot, _channel_for(league), image_bytes, "\n".join(lines))
    logger.info(f"[{league.upper()}] Alineación: {away['name']} vs {home['name']}")


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

                # ── 1. Juegos del día (una sola vez por día) ──────────
                if last_date_published != today_str:
                    mlb_games = await df.get_mlb_games(session, today_str)
                    if mlb_games:
                        await publish_games_today(bot, "mlb", mlb_games, date_display)
                        await asyncio.sleep(3)

                    lvbp_games = await df.get_lvbp_games(session, today_str)
                    if lvbp_games:
                        await publish_games_today(bot, "lvbp", lvbp_games, date_display)
                        await asyncio.sleep(3)

                    caribe_games = await df.get_caribe_games(session, today_str)
                    if caribe_games:
                        await publish_games_today(bot, "caribe", caribe_games, date_display)
                        await asyncio.sleep(3)

                    wbc_games = await df.get_wbc_games_auto(session, today_str)
                    if wbc_games:
                        await publish_games_today(bot, "wbc", wbc_games, date_display)
                        await asyncio.sleep(3)

                    last_date_published = today_str
                    _sent_results.clear()
                    _sent_lineups.clear()
                    logger.info(f"✅ Publicación diaria: {today_str}")

                # ── 2. MLB: esperar a todos los finales, luego bulk ───
                mlb_games = await df.get_mlb_games(session, today_str)
                await publish_mlb_results_bulk(bot, mlb_games, session)
                # Alineaciones MLB solo en postseason
                for game in mlb_games:
                    if df.is_postseason(game):
                        await publish_lineup(bot, game, "mlb", session)
                await asyncio.sleep(5)

                # ── 3. LVBP: resultado + alineación por juego ────────
                lvbp_games = await df.get_lvbp_games(session, today_str)
                for game in lvbp_games:
                    if df.is_game_final(game):
                        await publish_single_result(bot, game, "lvbp", session)
                    await publish_lineup(bot, game, "lvbp", session)
                await asyncio.sleep(5)

                # ── 4. Serie del Caribe ───────────────────────────────
                caribe_games = await df.get_caribe_games(session, today_str)
                for game in caribe_games:
                    if df.is_game_final(game):
                        await publish_single_result(bot, game, "caribe", session)
                    await publish_lineup(bot, game, "caribe", session)
                await asyncio.sleep(5)

                # ── 5. WBC ────────────────────────────────────────────
                wbc_games = await df.get_wbc_games_auto(session, today_str)
                for game in wbc_games:
                    if df.is_game_final(game):
                        await publish_single_result(bot, game, "wbc", session)
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
        "• <b>MLB</b> — Juegos del día + todos los resultados juntos al final\n"
        "• <b>LVBP</b> — Juegos + alineaciones + resultados individuales\n"
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
    lines = [f"🍿 ¡JUEGOS DE HOY! ⚾️  — {date_display}\n"]

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
                ad = away["name"]
                hd = home["name"]
                if state == "Final":
                    lines.append(f"✅ {ad} <b>{away['score']}</b>-<b>{home['score']}</b> {hd}")
                elif state == "Live":
                    lines.append(f"🔴 {ad} {away['score']}-{home['score']} {hd} (VIVO)")
                else:
                    lines.append(f"{ad} - {hd}  {t}")
            lines.append("")

        wbc = await df.get_wbc_games_auto(session, today_str)
        if wbc:
            lines.append("🌍 | <b>World Baseball Classic</b>")
            lines.append("")
            for g in wbc:
                away = df.get_team_info(g, "away")
                home = df.get_team_info(g, "home")
                t    = df.get_game_time_str(g, ET_TZ)
                lines.append(f"{away['name']} - {home['name']}  {t}")
            lines.append("")

    if len(lines) == 1:
        lines.append("No hay juegos programados para hoy.")

    lines.append(SUBSCRIBE_LINE)
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_hoy_mlb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_str    = date.today().strftime("%Y-%m-%d")
    date_display = date.today().strftime("%d/%m/%Y")
    async with aiohttp.ClientSession() as session:
        games = await df.get_mlb_games(session, today_str)
    if games:
        display = _parse_games_for_display(games, "mlb")
        lines = ["🍿 ¡JUEGOS DE HOY! ⚾️", "", "🇺🇸 | <b>MLB</b>", ""]
        for g in display:
            lines.append(f"{g['away_name']} - {g['home_name']}  {g['time']}")
        lines += ["", SUBSCRIBE_LINE]
        await update.message.reply_animation(
            animation=GAMES_TODAY_GIF,
            caption="\n".join(lines),
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text("No hay juegos MLB hoy.")


async def cmd_hoy_lvbp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_str    = date.today().strftime("%Y-%m-%d")
    date_display = date.today().strftime("%d/%m/%Y")
    async with aiohttp.ClientSession() as session:
        games = await df.get_lvbp_games(session, today_str)
    if games:
        display = _parse_games_for_display(games, "lvbp")
        lines = ["🍿 ¡JUEGOS DE HOY! ⚾️", "", "🇻🇪 | <b>LVBP Venezuela</b>", ""]
        for g in display:
            lines.append(f"{g['away_name']} - {g['home_name']}  {g['time']}")
        lines += ["", SUBSCRIBE_LINE]
        await update.message.reply_animation(
            animation=GAMES_TODAY_GIF,
            caption="\n".join(lines),
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text("No hay juegos LVBP hoy.")


async def cmd_resultados(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_str = date.today().strftime("%Y-%m-%d")
    lines = ["📢 | <b>RESULTADOS DE HOY</b>\n"]
    async with aiohttp.ClientSession() as session:
        mlb    = await df.get_mlb_games(session, today_str)
        finals = [g for g in mlb if df.is_game_final(g)]
    if finals:
        lines.append("🇺🇸 | <b>MLB</b>")
        lines.append("")
        for g in finals:
            away = df.get_team_info(g, "away")
            home = df.get_team_info(g, "home")
            ad   = away["abbreviation"] or away["name"]
            hd   = home["abbreviation"] or home["name"]
            lines.append(f"↪️ {ad} {away['score']}-{home['score']} {hd}")
    else:
        lines.append("Aún no hay resultados finales.")
    lines += ["", SUBSCRIBE_LINE]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"✅ <b>Bot activo</b>\n"
        f"📅 Fecha: {date.today()}\n"
        f"📊 Resultados enviados: {len(_sent_results)}\n"
        f"📋 Alineaciones enviadas: {len(_sent_lineups)}\n"
        f"⚾ MLB bulk enviado hoy: {'Sí' if _mlb_results_sent_today else 'No'}\n"
        f"🔄 WBC: detección automática\n"
        f"📡 Canal principal: {CHANNEL_ID or 'no configurado'}"
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
        try:
            await run_scheduler(self.app.bot)
        finally:
            logger.info("Apagando bot...")
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
