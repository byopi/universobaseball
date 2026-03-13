"""
data_fetcher.py — Obtiene datos de béisbol para MLB, LVBP, Serie del Caribe y WBC
Usa MLB Stats API (gratuita, sin API key) como fuente principal.
El WBC se detecta automáticamente — no hace falta configurar nada.
"""
import aiohttp
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

MLB_API = "https://statsapi.mlb.com/api/v1"

VZ_TZ = ZoneInfo("America/Caracas")
ET_TZ = ZoneInfo("America/New_York")

# Temporadas en las que hubo WBC (para búsqueda automática)
WBC_SEASONS = [2006, 2009, 2013, 2017, 2023]


async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict = None):
    try:
        async with session.get(url, params=params,
                               timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception as e:
        logger.warning(f"Error fetching {url}: {e}")
    return None


async def get_schedule(session: aiohttp.ClientSession,
                       sport_id: int = None,
                       league_id: int = None,
                       game_date: str = None,
                       season: int = None) -> list:
    if not game_date:
        game_date = date.today().strftime("%Y-%m-%d")
    if not season:
        season = date.today().year

    params = {
        "date": game_date,
        "season": season,
        "hydrate": "team,linescore,decisions,probablePitcher,lineups,flags",
        "fields": (
            "dates,date,games,gamePk,gameDate,gameType,status,abstractGameState,"
            "statusCode,detailedState,"
            "teams,away,home,team,id,name,abbreviation,score,"
            "linescore,currentInning,currentInningOrdinal,inningHalf,"
            "decisions,winner,loser,save,person,fullName,"
            "probablePitcher,fullName,"
            "lineups,homePlayers,awayPlayers,fullName,primaryPosition,abbreviation"
        ),
    }
    if sport_id:
        params["sportId"] = sport_id
    if league_id:
        params["leagueId"] = league_id

    data = await fetch_json(session, f"{MLB_API}/schedule", params)
    if not data or not data.get("dates"):
        return []

    games = []
    for date_entry in data["dates"]:
        games.extend(date_entry.get("games", []))
    return games


async def get_mlb_games(session: aiohttp.ClientSession, game_date: str = None) -> list:
    return await get_schedule(session, sport_id=1, game_date=game_date)


async def get_lvbp_games(session: aiohttp.ClientSession, game_date: str = None) -> list:
    games = await get_schedule(session, sport_id=23, game_date=game_date)
    if not games:
        games = await get_schedule(session, league_id=236, game_date=game_date)
    return games


async def get_caribe_games(session: aiohttp.ClientSession, game_date: str = None) -> list:
    games = await get_schedule(session, sport_id=22, game_date=game_date)
    if not games:
        games = await get_schedule(session, league_id=311, game_date=game_date)
    return games


async def get_wbc_games_auto(session: aiohttp.ClientSession, game_date: str = None) -> list:
    """
    Detecta juegos WBC automáticamente sin necesitar variable de entorno.
    Prueba el año actual primero; si no hay, prueba el WBC más reciente conocido.
    """
    if not game_date:
        game_date = date.today().strftime("%Y-%m-%d")

    current_year = date.today().year

    # 1. Año actual con sportId=51 (torneos internacionales)
    games = await get_schedule(session, sport_id=51, game_date=game_date,
                               season=current_year)
    if games:
        return games

    # 2. Año actual con leagueId=160 (WBC oficial)
    games = await get_schedule(session, league_id=160, game_date=game_date,
                               season=current_year)
    if games:
        return games

    # 3. WBC pasados (por si la fecha solicitada cae en un torneo histórico)
    for season in reversed(WBC_SEASONS):
        if season == current_year:
            continue
        games = await get_schedule(session, sport_id=51, game_date=game_date,
                                   season=season)
        if games:
            return games

    return []


def is_game_final(game: dict) -> bool:
    return game.get("status", {}).get("abstractGameState") == "Final"


def is_game_live(game: dict) -> bool:
    return game.get("status", {}).get("abstractGameState") == "Live"


def is_game_preview(game: dict) -> bool:
    return game.get("status", {}).get("abstractGameState") == "Preview"


def is_postseason(game: dict) -> bool:
    return game.get("gameType", "") in ("D", "L", "W", "F")


def get_team_info(game: dict, side: str) -> dict:
    teams = game.get("teams", {})
    team_data = teams.get(side, {})
    team = team_data.get("team", {})
    return {
        "id": team.get("id"),
        "name": team.get("teamName") or team.get("name", ""),
        "full_name": team.get("name", ""),
        "abbreviation": team.get("abbreviation", ""),
        "score": team_data.get("score", 0) or 0,
    }


def get_game_time_str(game: dict, tz=ET_TZ) -> str:
    game_date = game.get("gameDate", "")
    if not game_date:
        return "TBD"
    try:
        dt = datetime.fromisoformat(game_date.replace("Z", "+00:00"))
        return dt.astimezone(tz).strftime("%I:%M %p")
    except Exception:
        return "TBD"


def get_lineup_from_game(game: dict) -> dict:
    lineups = game.get("lineups", {})
    return {
        "home": lineups.get("homePlayers", []),
        "away": lineups.get("awayPlayers", []),
    }


async def get_game_boxscore(session: aiohttp.ClientSession, game_pk: int) -> dict:
    data = await fetch_json(session, f"{MLB_API}/game/{game_pk}/boxscore")
    return data or {}


async def get_game_linescore(session: aiohttp.ClientSession, game_pk: int) -> dict:
    data = await fetch_json(session, f"{MLB_API}/game/{game_pk}/linescore")
    return data or {}
