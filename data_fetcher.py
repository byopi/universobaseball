"""
data_fetcher.py — Fuentes de datos de béisbol

MLB, LVBP, Serie del Caribe → MLB Stats API (statsapi.mlb.com)
WBC → MLB.com scoreboard API (única fuente que lo tiene en 2026)
"""
import aiohttp
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

MLB_API      = "https://statsapi.mlb.com/api/v1"
# API que usa MLB.com internamente para el scoreboard — funciona para WBC
MLB_SCORE_API = "https://bdfed.stitch.mlbinfra.com/bdfed/transform-mlb-scoreboard"

VZ_TZ = ZoneInfo("America/Caracas")
ET_TZ = ZoneInfo("America/New_York")


# ─── HTTP helper ──────────────────────────────────────────────────────────────

async def fetch_json(session: aiohttp.ClientSession, url: str,
                     params: dict = None) -> dict | None:
    try:
        headers = {"User-Agent": "Mozilla/5.0 BaseballBot/1.0"}
        async with session.get(url, params=params, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 200:
                return await resp.json(content_type=None)
            logger.debug(f"HTTP {resp.status} → {url} params={params}")
    except Exception as e:
        logger.warning(f"fetch_json error {url}: {e}")
    return None


# ─── MLB Stats API schedule (para MLB, LVBP, Caribe) ─────────────────────────

async def _get_schedule_mlbapi(session: aiohttp.ClientSession,
                                sport_id: int = None, league_id: int = None,
                                game_date: str = None, season: int = None) -> list:
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

    data = await fetch_json(session, f"{MLB_API}/schedule", params)
    if not data:
        return []
    games = []
    for entry in data.get("dates", []):
        games.extend(entry.get("games", []))
    if games:
        logger.debug(f"MLB-API: {len(games)} juego(s) sportId={sport_id} leagueId={league_id} {game_date}")
    return games


async def get_mlb_games(session: aiohttp.ClientSession,
                        game_date: str = None) -> list:
    return await _get_schedule_mlbapi(session, sport_id=1, game_date=game_date)


async def get_lvbp_games(session: aiohttp.ClientSession,
                         game_date: str = None) -> list:
    games = await _get_schedule_mlbapi(session, sport_id=23, game_date=game_date)
    if not games:
        games = await _get_schedule_mlbapi(session, league_id=236, game_date=game_date)
    return games


async def get_caribe_games(session: aiohttp.ClientSession,
                           game_date: str = None) -> list:
    games = await _get_schedule_mlbapi(session, sport_id=22, game_date=game_date)
    if not games:
        games = await _get_schedule_mlbapi(session, league_id=311, game_date=game_date)
    return games


# ─── WBC: scrape MLB.com scoreboard (única fuente fiable para WBC 2026) ───────

def _build_wbc_game(raw: dict) -> dict:
    """
    Convierte un juego del scoreboard de MLB.com al formato
    que usa el resto del bot (mismo esquema que MLB Stats API).
    """
    status_code = raw.get("gameStateCode", "")          # "F" = Final, "L" = Live, "S" = Scheduled
    detailed    = raw.get("statusDisplay", "Scheduled")

    abstract = "Preview"
    if status_code == "F":
        abstract = "Final"
    elif status_code in ("L", "I"):     # Live / In progress
        abstract = "Live"

    away = raw.get("away", {})
    home = raw.get("home", {})

    away_name = away.get("teamName", away.get("name", ""))
    home_name = home.get("teamName", home.get("name", ""))
    away_full = away.get("teamFullName", away_name)
    home_full = home.get("teamFullName", home_name)

    linescore = {}
    linescore_raw = raw.get("linescore", {})
    if linescore_raw:
        linescore = {
            "currentInning":        linescore_raw.get("currentInning", 0),
            "currentInningOrdinal": linescore_raw.get("currentInningOrdinal", ""),
            "inningHalf":           linescore_raw.get("inningHalf", ""),
        }

    # Decisiones de pitching
    decisions = {}
    if raw.get("winningPitcher"):
        decisions["winner"] = {"fullName": raw["winningPitcher"].get("fullName", "")}
    if raw.get("losingPitcher"):
        decisions["loser"]  = {"fullName": raw["losingPitcher"].get("fullName", "")}
    if raw.get("savePitcher"):
        decisions["save"]   = {"fullName": raw["savePitcher"].get("fullName", "")}

    return {
        "gamePk":   raw.get("gamePk") or raw.get("id"),
        "gameDate": raw.get("gameDate", ""),
        "gameType": raw.get("gameType", "C"),     # C = Classic
        "status": {
            "abstractGameState": abstract,
            "detailedState":     detailed,
        },
        "teams": {
            "away": {
                "score": away.get("runs", 0) or 0,
                "team": {
                    "id":           away.get("id"),
                    "name":         away_full,
                    "teamName":     away_name,
                    "abbreviation": away.get("abbreviation", ""),
                },
            },
            "home": {
                "score": home.get("runs", 0) or 0,
                "team": {
                    "id":           home.get("id"),
                    "name":         home_full,
                    "teamName":     home_name,
                    "abbreviation": home.get("abbreviation", ""),
                },
            },
        },
        "linescore": linescore,
        "decisions": decisions,
        "_source": "wbc_scoreboard",
    }


async def get_wbc_games_auto(session: aiohttp.ClientSession,
                             game_date: str = None) -> list:
    """
    Obtiene los juegos del WBC.
    Busca en la fecha VZ actual Y en el día anterior (por desfase UTC).
    """
    from datetime import timedelta
    if not game_date:
        # Usar fecha en Venezuela (UTC-4) como referencia
        game_date = datetime.now(VZ_TZ).strftime("%Y-%m-%d")

    # También buscar el día anterior en caso de desfase UTC
    prev_date = (datetime.strptime(game_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    dates_to_try = [game_date, prev_date]

    # Intentar cada fecha (hoy y ayer en VZ)
    for try_date in dates_to_try:
        year = try_date[:4]

        # ── Método 1: MLB Stats API con sportId=51 ──
        for sport_id in (51, 22):
            params = {
                "sportId":   sport_id,
                "startDate": try_date,
                "endDate":   try_date,
                "season":    year,
                "hydrate":   "team,linescore,decisions",
            }
            data = await fetch_json(session, f"{MLB_API}/schedule", params)
            if data:
                games = []
                for entry in data.get("dates", []):
                    games.extend(entry.get("games", []))
                if games:
                    logger.info(f"WBC {try_date} sportId={sport_id}: {len(games)} juego(s)")
                    return games

        # ── Método 2: leagueId=160 ──
        for league_id in (160,):
            params = {
                "leagueId":  league_id,
                "startDate": try_date,
                "endDate":   try_date,
                "season":    year,
                "hydrate":   "team,linescore,decisions",
            }
            data = await fetch_json(session, f"{MLB_API}/schedule", params)
            if data:
                games = []
                for entry in data.get("dates", []):
                    games.extend(entry.get("games", []))
                if games:
                    logger.info(f"WBC {try_date} leagueId={league_id}: {len(games)} juego(s)")
                    return games

    logger.debug(f"WBC: sin juegos para {dates_to_try}")
    return []


# ─── Helpers de estado ────────────────────────────────────────────────────────

def is_game_final(game: dict) -> bool:
    status   = game.get("status", {})
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


# ─── Extracción de datos del juego ────────────────────────────────────────────

def get_team_info(game: dict, side: str) -> dict:
    teams     = game.get("teams", {})
    team_data = teams.get(side, {})
    team      = team_data.get("team", {})
    name      = (team.get("teamName") or team.get("clubName") or team.get("name", ""))
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


# ─── Boxscore y linescore ─────────────────────────────────────────────────────

async def get_game_boxscore(session: aiohttp.ClientSession, game_pk: int) -> dict:
    data = await fetch_json(session, f"{MLB_API}/game/{game_pk}/boxscore")
    return data or {}


async def get_game_linescore(session: aiohttp.ClientSession, game_pk: int) -> dict:
    data = await fetch_json(session, f"{MLB_API}/game/{game_pk}/linescore")
    return data or {}
