"""
data_fetcher.py — Obtiene datos de béisbol para MLB, LVBP, Serie del Caribe y WBC
Usa MLB Stats API (gratuita, sin API key).

NOTAS IMPORTANTES sobre la API:
- El campo `fields` lo quitamos del schedule para no perder datos inesperados
- El WBC usa múltiples sportIds según el año; probamos todos
- La API puede demorar en reflejar resultados finales ~5 min
"""
import aiohttp
import logging
from datetime import datetime, date

from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

MLB_API = "https://statsapi.mlb.com/api/v1"

VZ_TZ = ZoneInfo("America/Caracas")
ET_TZ = ZoneInfo("America/New_York")

# sportId conocidos en la MLB Stats API
SPORT_MLB    = 1    # MLB / MiLB mayor
SPORT_LVBP   = 23   # Liga Venezolana
SPORT_CARIBE = 22   # Serie del Caribe  (a veces 30 según el año)
SPORT_WBC    = 51   # Torneos internacionales / WBC

# leagueIds alternativos
LEAGUE_LVBP   = 236
LEAGUE_CARIBE = 311
LEAGUE_WBC    = 160


async def fetch_json(session: aiohttp.ClientSession, url: str,
                     params: dict = None) -> dict | None:
    try:
        async with session.get(url, params=params,
                               timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 200:
                return await resp.json(content_type=None)
            else:
                logger.debug(f"HTTP {resp.status} para {url} params={params}")
    except Exception as e:
        logger.warning(f"Error fetching {url}: {e}")
    return None


async def get_schedule(session: aiohttp.ClientSession,
                       sport_id: int = None,
                       league_id: int = None,
                       game_date: str = None,
                       season: int = None,
                       game_type: str = None) -> list:
    """
    Llama al endpoint /schedule de la MLB Stats API.
    NO usa el campo `fields` para asegurarse de recibir todos los datos.
    """
    if not game_date:
        game_date = date.today().strftime("%Y-%m-%d")
    if not season:
        season = date.today().year

    params = {
        "date":    game_date,
        "season":  season,
        "hydrate": "team,linescore,decisions,probablePitcher,lineups",
    }
    if sport_id:
        params["sportId"] = sport_id
    if league_id:
        params["leagueId"] = league_id
    if game_type:
        params["gameType"] = game_type

    data = await fetch_json(session, f"{MLB_API}/schedule", params)
    if not data:
        logger.debug(f"Schedule vacío: sportId={sport_id} leagueId={league_id} date={game_date}")
        return []

    dates = data.get("dates", [])
    if not dates:
        logger.debug(f"Cero fechas: sportId={sport_id} leagueId={league_id} date={game_date}")
        return []

    games = []
    for entry in dates:
        games.extend(entry.get("games", []))
    logger.debug(f"Schedule: {len(games)} juego(s) | sportId={sport_id} leagueId={league_id} date={game_date}")
    return games


# ─── Getters por liga ──────────────────────────────────────────────────────────

async def get_mlb_games(session: aiohttp.ClientSession,
                        game_date: str = None) -> list:
    return await get_schedule(session, sport_id=SPORT_MLB, game_date=game_date)


async def get_lvbp_games(session: aiohttp.ClientSession,
                         game_date: str = None) -> list:
    games = await get_schedule(session, sport_id=SPORT_LVBP, game_date=game_date)
    if not games:
        games = await get_schedule(session, league_id=LEAGUE_LVBP, game_date=game_date)
    return games


async def get_caribe_games(session: aiohttp.ClientSession,
                           game_date: str = None) -> list:
    games = await get_schedule(session, sport_id=SPORT_CARIBE, game_date=game_date)
    if not games:
        games = await get_schedule(session, league_id=LEAGUE_CARIBE, game_date=game_date)
    return games


async def get_wbc_games_auto(session: aiohttp.ClientSession,
                             game_date: str = None) -> list:
    """
    Busca juegos WBC probando varios sportIds y leagueIds.
    La API de MLB Stats indexa el WBC con distintos IDs según el año.
    Probamos todos los que conocemos, del más probable al menos probable.
    """
    if not game_date:
        game_date = date.today().strftime("%Y-%m-%d")
    year = int(game_date[:4])

    # Orden de búsqueda: de más probable a menos
    attempts = [
        {"sport_id": 51,  "season": year},    # International tournaments
        {"sport_id": 51,  "season": year, "game_type": "C"},  # Classic
        {"league_id": 160, "season": year},   # WBC league id histórico
        {"sport_id": 22,  "season": year},    # Caribbean / international
        {"sport_id": 51,  "season": 2023},    # WBC 2023 (fallback histórico)
        {"league_id": 160, "season": 2023},
    ]

    for attempt in attempts:
        games = await get_schedule(session, game_date=game_date, **attempt)
        if games:
            logger.info(f"WBC encontrado: {len(games)} juego(s) con params={attempt}")
            return games

    logger.debug(f"WBC: ningún juego encontrado para {game_date}")
    return []


# ─── Helpers de estado de juego ────────────────────────────────────────────────

def is_game_final(game: dict) -> bool:
    status = game.get("status", {})
    # Verificar tanto el estado abstracto como el detailedState
    abstract = status.get("abstractGameState", "")
    detailed = status.get("detailedState", "")
    return abstract == "Final" or detailed in ("Final", "Game Over", "Completed Early")


def is_game_live(game: dict) -> bool:
    return game.get("status", {}).get("abstractGameState") == "Live"


def is_game_preview(game: dict) -> bool:
    return game.get("status", {}).get("abstractGameState") in ("Preview", "Scheduled")


def is_game_postponed(game: dict) -> bool:
    detail = game.get("status", {}).get("detailedState", "")
    return "Postponed" in detail or "Suspended" in detail


def is_postseason(game: dict) -> bool:
    return game.get("gameType", "") in ("D", "L", "W", "F")


# ─── Extracción de datos ───────────────────────────────────────────────────────

def get_team_info(game: dict, side: str) -> dict:
    """side = 'away' | 'home'"""
    teams     = game.get("teams", {})
    team_data = teams.get(side, {})
    team      = team_data.get("team", {})
    # Intentar obtener el nombre de varias formas
    name = (team.get("teamName")
            or team.get("clubName")
            or team.get("name", ""))
    full_name = team.get("name", name)
    return {
        "id":           team.get("id"),
        "name":         name,
        "full_name":    full_name,
        "abbreviation": team.get("abbreviation", ""),
        "score":        team_data.get("score", 0) or 0,
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


# ─── Boxscore y linescore ──────────────────────────────────────────────────────

async def get_game_boxscore(session: aiohttp.ClientSession, game_pk: int) -> dict:
    data = await fetch_json(session, f"{MLB_API}/game/{game_pk}/boxscore")
    return data or {}


async def get_game_linescore(session: aiohttp.ClientSession, game_pk: int) -> dict:
    data = await fetch_json(session, f"{MLB_API}/game/{game_pk}/linescore")
    return data or {}


async def get_game_feed(session: aiohttp.ClientSession, game_pk: int) -> dict:
    """Feed completo del juego — más datos pero más lento."""
    data = await fetch_json(session, f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live")
    return data or {}
