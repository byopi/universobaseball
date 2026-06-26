import os, re, json, asyncio, random, signal, tempfile, time, hashlib, logging
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple, Set, Dict
from urllib.parse import unquote

import httpx, aiohttp, aiofiles, feedparser, requests
from groq import Groq
from bs4 import BeautifulSoup
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from telegram import Bot, InputMediaPhoto, InputMediaVideo, Update
from telegram.error import TelegramError, RetryAfter
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
NBA_CHANNEL_ID         = os.getenv("NBA_CHANNEL_ID", "")
MLB_CHANNEL_ID         = os.getenv("MLB_CHANNEL_ID", "")
BARCA_CHANNEL_ID       = os.getenv("BARCA_CHANNEL_ID", "")
MADRID_CHANNEL_ID      = os.getenv("MADRID_CHANNEL_ID", "")
PREMIER_CHANNEL_ID     = os.getenv("PREMIER_CHANNEL_ID", "")
FICHAJES_CHANNEL_ID    = os.getenv("FICHAJES_CHANNEL_ID", "")
GROQ_API_KEY           = os.getenv("GROQ_API_KEY", "")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))
MAX_VIDEO_SIZE_BYTES   = int(os.getenv("MAX_VIDEO_SIZE_MB", "50")) * 1024 * 1024
PORT                   = int(os.getenv("PORT", "8080"))
ADMIN_TELEGRAM_ID      = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))

NBA_SUBSCRIBE      = "📲 Suscríbete en t.me/NBA_Latinoamerica"
MLB_SUBSCRIBE      = "📲 Suscríbete en t.me/UniversoBaseball"
BARCA_SUBSCRIBE    = "📲 Suscríbete en t.me/barcanewses"
MADRID_SUBSCRIBE   = "📲 Suscríbete en t.me/rmnews_es"
PREMIER_SUBSCRIBE  = "📲 Suscríbete en t.me/PremierLeague_ES"
FICHAJES_SUBSCRIBE = "📲 Suscríbete en t.me/fichajesdefutbol"

# ── Cuentas por deporte ───────────────────────────────────────────
NBA_ACCOUNTS = {
    "UnderdogNBA": {"translate": True,  "photos_only": False},
    "NBALatam":    {"translate": False, "photos_only": True},
}
MLB_ACCOUNTS = {
    "UnderdogMLB": {"translate": True,  "photos_only": False},
    "MLBespanol":  {"translate": False, "photos_only": True},
}
BARCA_ACCOUNTS = {
    "BarcaUniversal": {"translate": True, "photos_only": False},
}
MADRID_ACCOUNTS = {
    "MadridUniversal": {"translate": True, "photos_only": False},
}
PREMIER_ACCOUNTS = {
    "Mercado_Ingles": {"translate": False, "photos_only": False},
}
FICHAJES_ACCOUNTS = {
    "mercatosphera": {"translate": True, "photos_only": False},
}

# ── Grupos registrados para los comandos /scan y /accounts ────────
# (nombre legible, dict de cuentas, channel_id, sport, subscribe_msg, use_filter)
GROUPS = [
    ("NBA",       NBA_ACCOUNTS,       "NBA_CHANNEL_ID",      "nba",      NBA_SUBSCRIBE,      False),
    ("MLB",       MLB_ACCOUNTS,       "MLB_CHANNEL_ID",      "mlb",      MLB_SUBSCRIBE,       False),
    ("Barça",     BARCA_ACCOUNTS,     "BARCA_CHANNEL_ID",    "futbol",   BARCA_SUBSCRIBE,     True),
    ("Madrid",    MADRID_ACCOUNTS,    "MADRID_CHANNEL_ID",   "futbol",   MADRID_SUBSCRIBE,    True),
    ("Premier",   PREMIER_ACCOUNTS,   "PREMIER_CHANNEL_ID",  "premier",  PREMIER_SUBSCRIBE,   True),
    ("Fichajes",  FICHAJES_ACCOUNTS,  "FICHAJES_CHANNEL_ID", "futbol",   FICHAJES_SUBSCRIBE,  True),
]

NITTER_INSTANCES = [
    "https://nitter.net",
    "https://xcancel.com",
    "https://nitter.cz",
]

TEMP_DIR = Path(tempfile.gettempdir()) / "sports_bot"
TEMP_DIR.mkdir(exist_ok=True)
DATA_DIR      = Path(os.getenv("DATA_DIR", "data"))
STATE_FILE    = DATA_DIR / "processed_tweets.json"
LAST_IDS_FILE = DATA_DIR / "last_tweet_ids.json"

bot         = Bot(token=TELEGRAM_BOT_TOKEN)
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# ══════════════════════════════════════════════════════════════════
#  ESTADO EN MEMORIA
# ══════════════════════════════════════════════════════════════════
_processed_ids: Set[str]  = set()
_last_ids: Dict[str, str] = {}
_disk_ok = True

def _load_state():
    global _disk_ok
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text())
            _processed_ids.update(data.get("ids", []))
            _sent_messages.update({k: v for k, v in data.get("sent_msgs", {}).items()})
        if LAST_IDS_FILE.exists():
            _last_ids.update(json.loads(LAST_IDS_FILE.read_text()))
    except Exception as e:
        log.warning(f"Disco no disponible: {e}"); _disk_ok = False

def _save():
    if not _disk_ok: return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({
            "ids": list(_processed_ids),
            "sent_msgs": _sent_messages,
        }))
        LAST_IDS_FILE.write_text(json.dumps(_last_ids))
    except Exception: pass

def is_processed(tid: str) -> bool: return str(tid) in _processed_ids
def mark_processed(tid: str):
    _processed_ids.add(str(tid))
    if len(_processed_ids) > 5000:
        for old in sorted(_processed_ids)[:-4000]: _processed_ids.discard(old)
    _save()

def set_last_id(u: str, tid: str): _last_ids[u] = tid; _save()
def get_last_id(u: str) -> Optional[str]: return _last_ids.get(u)

# ══════════════════════════════════════════════════════════════════
#  SCRAPER — Twitter Syndication API (sin auth, igual que bothomeruns1)
# ══════════════════════════════════════════════════════════════════
SYNDICATION_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

@dataclass
class TweetMedia:
    type: str          # "photo" | "video" | "gif"
    url: str           # URL directa del medio
    thumb_url: str = ""

@dataclass
class Tweet:
    id: str
    text: str
    author_username: str
    created_at: str
    media: List[TweetMedia] = field(default_factory=list)
    tweet_url: str = ""
    quoted_tweet_id: str = ""   # ID del tweet citado (si lo hay)

def tweet_id_to_timestamp(tweet_id: str) -> Optional[float]:
    """Decodifica el timestamp (epoch segundos) embebido en un ID de tweet (Snowflake)."""
    try:
        tid = int(tweet_id)
        ts_ms = (tid >> 22) + 1288834974657
        return ts_ms / 1000
    except (ValueError, TypeError):
        return None

def _parse_syndication(html: str, username: str) -> List[Tweet]:
    """Extrae tweets del HTML de la Syndication API de Twitter."""
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.DOTALL)
    if not match:
        return []

    import time as _time
    cutoff_ms = int((_time.time() - 30 * 86400) * 1000)
    min_id = (cutoff_ms - 1288834974657) << 22
    if min_id < 0: min_id = 0

    try:
        data = json.loads(match.group(1))
        entries = (data.get("props", {})
                      .get("pageProps", {})
                      .get("timeline", {})
                      .get("entries", []))[:10]
        tweets = []
        for entry in entries:
            tweet = entry.get("content", {}).get("tweet", {})
            if not tweet: continue

            tid = tweet.get("id_str", "")
            if not tid: continue
            if tweet.get("retweeted_status"): continue

            try:
                if int(tid) < min_id:
                    log.info(f"[Synd] Saltando tweet antiguo {tid} de @{username}")
                    continue
            except ValueError: pass

            text = tweet.get("full_text", tweet.get("text", ""))
            text = re.sub(r"https?://t\.co/\S+", "", text).strip()
            # Las menciones (@usuario) se dejan tal cual están en el tweet original,
            # pegadas sin espacio extra — no se eliminan ni se separan.

            # Detectar si es un tweet citado
            quoted_tweet_id = ""
            quoted = tweet.get("quoted_status")
            if quoted:
                quoted_tweet_id = quoted.get("id_str", "")
                # El texto del tweet original no incluye el cuerpo del citado — solo el comentario
                # El texto del tweet citado lo ignoramos, solo lo usamos para saber a qué responder

            media_list = []
            extended = tweet.get("extended_entities", {}).get("media", [])
            entities  = tweet.get("entities", {}).get("media", [])
            all_media = extended or entities

            for m in all_media:
                mtype = m.get("type", "")
                if mtype == "photo":
                    url = m.get("media_url_https", "") or m.get("media_url", "")
                    if url:
                        media_list.append(TweetMedia(type="photo", url=url))  # sin :orig para evitar 403
                elif mtype in ("video", "animated_gif"):
                    variants = m.get("video_info", {}).get("variants", [])
                    best = sorted(
                        [v for v in variants if v.get("content_type") == "video/mp4"],
                        key=lambda v: v.get("bitrate", 0), reverse=True
                    )
                    thumb = m.get("media_url_https", "")
                    if best:
                        media_list.append(TweetMedia(type="video", url=best[0]["url"], thumb_url=thumb))
                    elif thumb:
                        media_list.append(TweetMedia(type="photo", url=thumb))

            tweets.append(Tweet(
                id=tid, text=text, author_username=username,
                created_at=tweet.get("created_at", ""),
                media=media_list,
                tweet_url=f"https://x.com/{username}/status/{tid}",
                quoted_tweet_id=quoted_tweet_id,
            ))
        return tweets
    except Exception as e:
        log.error(f"Error parseando syndication @{username}: {e}")
        return []

async def fetch_tweets_syndication(username: str, since_id: Optional[str] = None) -> List[Tweet]:
    """Método principal: Twitter Syndication API (igual que bothomeruns1)."""
    url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}"
    headers = {**SYNDICATION_HEADERS, "Referer": f"https://twitter.com/{username}"}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                tweets = _parse_syndication(resp.text, username)
                if tweets:
                    filtered = []
                    for t in tweets:
                        if since_id:
                            try:
                                if int(t.id) <= int(since_id): continue
                            except ValueError:
                                if t.id <= since_id: continue
                        filtered.append(t)
                    if filtered:
                        log.info(f"[Synd] ✅ @{username} ({len(filtered)} nuevos)")
                    return filtered
    except Exception as e:
        log.warning(f"[Synd] Error @{username}: {e}")

    # Fallback: Nitter RSS
    return await _fetch_tweets_nitter(username, since_id)

async def _fetch_tweets_nitter(username: str, since_id: Optional[str] = None) -> List[Tweet]:
    """Fallback con Nitter RSS si la Syndication API falla."""
    instances = NITTER_INSTANCES.copy()
    random.shuffle(instances)
    for base in instances:
        try:
            rss_url = f"{base}/{username}/rss"
            # IMPORTANTE: pasarle la URL (string) a feedparser, NO los bytes ya
            # descargados con requests. Cuando feedparser conoce la URL base,
            # resuelve internamente los src="/pic/pbs.twimg.com%2F..." relativos
            # de Nitter a URLs absolutas y descargables. Si se le pasan bytes
            # crudos (sin URL base) esos src quedan rotos y nunca se publican
            # las fotos/videos — este era el bug.
            feed = await asyncio.get_event_loop().run_in_executor(
                None, lambda u=rss_url: feedparser.parse(u, agent="Mozilla/5.0", request_headers={"User-Agent": "Mozilla/5.0"})
            )
            if feed.bozo and not feed.entries:
                continue
            if not feed.entries: continue

            tweets = []
            for entry in feed.entries[:10]:
                link = entry.get("link", "")
                m = re.search(r'/status/(\d+)', link)
                tid = m.group(1) if m else ""
                if not tid: continue
                if since_id:
                    try:
                        if int(tid) <= int(since_id): continue
                    except ValueError: pass

                soup = BeautifulSoup(entry.get("description", ""), "html.parser")
                for br in soup.find_all("br"): br.replace_with("\n")
                text = soup.get_text(separator="\n")
                lines = [l.strip() for l in text.splitlines()]
                text = "\n".join(l for l in lines if l)
                text = re.sub(r"https?://t\.co/\S+", "", text).strip()
                # Las menciones (@usuario) se dejan tal cual, pegadas sin espacio extra.
                if not text or text.startswith("RT "): continue

                xurl = re.sub(r'https://(nitter\.net|xcancel\.com|nitter\.cz)/', 'https://x.com/', link)
                media = []
                for img in soup.find_all("img"):
                    src = img.get("src", "")
                    if not src:
                        continue
                    # Tras la resolución de feedparser, src ya debería ser absoluto:
                    #   https://nitter.net/pic/pbs.twimg.com%2Fmedia%2F...   (proxy, válido para descargar)
                    #   https://pbs.twimg.com/media/...                      (directo, también válido)
                    if not src.startswith("http"):
                        # Por si alguna instancia no resolvió la URL, intentamos a mano
                        src = base.rstrip("/") + "/" + src.lstrip("/")
                    if "pbs.twimg.com" not in src and "/pic/" not in src:
                        continue

                    # Si la <img> está dentro de un <a href=".../video/N">, es video
                    parent_a = img.find_parent("a")
                    if parent_a and "/video/" in parent_a.get("href", ""):
                        media.append(TweetMedia(type="video", url=src))
                    else:
                        media.append(TweetMedia(type="photo", url=src))

                # Si no captamos ningún medio pero la descripción sugiere video/gif,
                # marcar para que get_video() intente yt-dlp con tweet_url
                has_video_hint = any(x in entry.get("description", "").lower() for x in ["/video/", "animated_gif"])
                if has_video_hint and not any(m.type == "video" for m in media):
                    media.append(TweetMedia(type="video", url=""))

                tweets.append(Tweet(
                    id=tid, text=text, author_username=username,
                    created_at=entry.get("published", ""),
                    media=media, tweet_url=xurl,
                ))

            if tweets:
                log.info(f"[Nitter] ✅ @{username} via {base} ({len(tweets)} nuevos)")
                return tweets
            elif since_id:
                return []
        except Exception as e:
            log.warning(f"[Nitter] ❌ {base}: {e}")
    return []

# ══════════════════════════════════════════════════════════════════
#  TRADUCCIÓN — Groq
# ══════════════════════════════════════════════════════════════════
_SYSTEM = """Eres un traductor experto en deportes. Traduce del inglés al español latino.
REGLAS:
1. NO traduzcas nombres de jugadores, equipos ni clubes.
2. NO traduzcas abreviaciones (pts, reb, ast, ERA, RBI, HR, SP, DH, 1B, 2B, 3B, SS, CF, RF, LF, C…).
3. NO traduzcas hashtags ni menciones.
4. PRESERVA el formato exacto: saltos de línea, listas, espacios.
5. Si ya está en español, devuélvelo SIN cambios.
6. Solo la traducción. Sin comillas ni explicaciones."""

# Frases que indican que el modelo se negó a traducir o se confundió de contexto
# (p.ej. el bug de contexto NBA/MLB/fútbol cruzado) en vez de dar la traducción real.
_REFUSAL_PATTERNS = [
    re.compile(r"no\s+hay\s+texto", re.IGNORECASE),
    re.compile(r"no\s+se\s+proporcion[oó]\s+texto", re.IGNORECASE),
    re.compile(r"parece\s+ser\s+sobre", re.IGNORECASE),
    re.compile(r"estar[ée]\s+encantad[oa]\s+de\s+ayudar", re.IGNORECASE),
    re.compile(r"no\s+puedo\s+traducir", re.IGNORECASE),
    re.compile(r"como\s+(modelo|asistente|ia)\s+de\s+lenguaje", re.IGNORECASE),
    re.compile(r"lo\s+siento,?\s+(pero\s+)?no", re.IGNORECASE),
    re.compile(r"si\s+(se\s+)?(proporciona|proporcionas|me\s+proporcionas)", re.IGNORECASE),
]

def _looks_like_refusal(original: str, result: str) -> bool:
    """Detecta si la 'traducción' es en realidad una negativa/disculpa del modelo
    en lugar del texto traducido (p.ej. por confundir el contexto deportivo)."""
    if not result.strip():
        return True
    # Una traducción real debería tener una longitud comparable al original.
    # Una negativa suele ser mucho más corta que el post real (salvo posts muy breves).
    if len(original) > 60 and len(result) < len(original) * 0.35:
        return True
    for pattern in _REFUSAL_PATTERNS:
        if pattern.search(result):
            return True
    return False

async def translate(text: str, sport: str, _attempt: int = 1, _max_attempts: int = 3) -> str:
    if not text.strip() or not groq_client: return text
    if sport == "mlb":
        ctx = "béisbol (MLB)"
    elif sport in ("football", "futbol", "fútbol", "soccer"):
        ctx = "fútbol (La Liga / Champions League)"
    elif sport == "premier":
        ctx = "fútbol (Premier League)"
    elif sport == "nba":
        ctx = "baloncesto (NBA)"
    else:
        ctx = "deportes en general"

    try:
        def _call():
            return groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user",   "content": f"Contexto: {ctx}\n\nTexto:\n{text}"},
                ],
                temperature=0.1, max_tokens=600,
            )
        resp = await asyncio.get_event_loop().run_in_executor(None, _call)
        result = resp.choices[0].message.content.strip().strip("\"'")

        if _looks_like_refusal(text, result):
            if _attempt < _max_attempts:
                log.warning(f"[Groq] ⚠️ Respuesta no parece traducción (intento {_attempt}/{_max_attempts}) — reintentando: {result[:80]!r}")
                await asyncio.sleep(1.5 * _attempt)  # backoff progresivo
                return await translate(text, sport, _attempt=_attempt + 1, _max_attempts=_max_attempts)
            log.warning(f"[Groq] ❌ Tras {_max_attempts} intentos sigue sin traducir bien — usando texto original")
            return text

        log.info(f"[Groq] ✅ Traducido" + (f" (intento {_attempt})" if _attempt > 1 else ""))
        return result
    except Exception as e:
        if _attempt < _max_attempts:
            log.warning(f"[Groq] ⚠️ Error (intento {_attempt}/{_max_attempts}): {e} — reintentando")
            await asyncio.sleep(1.5 * _attempt)
            return await translate(text, sport, _attempt=_attempt + 1, _max_attempts=_max_attempts)
        log.warning(f"[Groq] ❌ Error tras {_max_attempts} intentos: {e} — usando texto original")
        return text

# ══════════════════════════════════════════════════════════════════
#  DESCARGA DE VIDEO — yt-dlp desde x.com
# ══════════════════════════════════════════════════════════════════
def _del(path: str):
    try:
        if path and os.path.exists(path): os.remove(path)
    except Exception: pass

async def _run(cmd, timeout=180):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    return await asyncio.wait_for(proc.communicate(), timeout=timeout)

async def video_dims(path: str) -> Tuple[int, int]:
    try:
        out, _ = await _run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0", path], timeout=15)
        parts = out.decode().strip().split(",")
        if len(parts) == 2: return int(parts[0]), int(parts[1])
    except Exception: pass
    return 1280, 720

async def download_video_from_url(direct_url: str, tweet_id: str) -> Optional[Tuple[str, int, int]]:
    """Descarga un video directamente desde su URL (pbs.twimg.com o video.twimg.com)."""
    out = str(TEMP_DIR / f"v_{tweet_id}_direct.mp4")
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(direct_url)
            if resp.status_code == 200:
                Path(out).write_bytes(resp.content)
                if os.path.getsize(out) > 1000:
                    w, h = await video_dims(out)
                    return out, w, h
    except Exception as e:
        log.warning(f"[Video] Descarga directa falló: {e}")
    _del(out)
    return None

async def download_video_ytdlp(tweet_url: str, tweet_id: str) -> Optional[Tuple[str, int, int]]:
    """Descarga un video de x.com usando yt-dlp."""
    raw = str(TEMP_DIR / f"v_{tweet_id}_raw.mp4")
    enc = str(TEMP_DIR / f"v_{tweet_id}_enc.mp4")

    # Asegurar que la URL sea de x.com
    xcom = re.sub(r'https://(nitter\.net|xcancel\.com|nitter\.cz|twitter\.com)/', 'https://x.com/', tweet_url)
    log.info(f"[Video] yt-dlp → {xcom}")

    try:
        stdout, stderr = await _run([
            "yt-dlp", "-f", "best[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "-o", raw, "--no-playlist", "--quiet", xcom
        ], timeout=120)
        if stderr:
            log.debug(f"[yt-dlp stderr] {stderr.decode()[:200]}")
    except Exception as e:
        log.warning(f"[Video] yt-dlp error: {e}"); return None

    if not os.path.exists(raw) or os.path.getsize(raw) < 1000:
        log.warning("[Video] Archivo vacío"); _del(raw); return None

    # Re-encodear para garantizar compatibilidad con Telegram
    w, h = await video_dims(raw)
    w = w if w % 2 == 0 else w - 1
    h = h if h % 2 == 0 else h - 1
    try:
        await _run([
            "ffmpeg", "-y", "-i", raw,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-vf", f"scale={w}:{h}",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", "-pix_fmt", "yuv420p", enc
        ])
    except Exception as e:
        log.warning(f"[Video] ffmpeg error: {e}"); _del(raw); return None

    _del(raw)
    if not os.path.exists(enc): return None

    if os.path.getsize(enc) > MAX_VIDEO_SIZE_BYTES:
        comp = str(TEMP_DIR / f"v_{tweet_id}_comp.mp4")
        try:
            await _run(["ffmpeg", "-y", "-i", enc,
                "-c:v", "libx264", "-preset", "medium", "-crf", "28",
                "-vf", "scale=1280:-2", "-c:a", "aac", "-b:a", "96k",
                "-movflags", "+faststart", "-pix_fmt", "yuv420p", comp])
        except Exception: pass
        _del(enc)
        if os.path.exists(comp):
            w2, h2 = await video_dims(comp)
            return comp, w2, h2
        return None

    return enc, w, h

async def get_video(tweet: Tweet) -> Optional[Tuple[str, int, int]]:
    """
    Estrategia:
    1. Si tenemos URL directa del video (de la Syndication API), descargarla directo.
    2. Si no, usar yt-dlp con la URL del tweet en x.com.
    """
    videos = [m for m in tweet.media if m.type == "video"]
    if videos and videos[0].url:
        result = await download_video_from_url(videos[0].url, tweet.id)
        if result: return result

    # Fallback: yt-dlp desde x.com
    return await download_video_ytdlp(tweet.tweet_url, tweet.id)

async def download_image(url: str, fname: str) -> Optional[str]:
    path = str(TEMP_DIR / fname)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status == 200:
                    async with aiofiles.open(path, "wb") as f:
                        await f.write(await r.read())
                    return path
    except Exception as e:
        log.warning(f"[Imagen] Error: {e}")
    return None

def cleanup_old():
    now = time.time()
    for f in TEMP_DIR.glob("*"):
        if now - f.stat().st_mtime > 3600:
            try: f.unlink()
            except Exception: pass

# ══════════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════════
async def send_text(cid: str, text: str, reply_to: int = None) -> bool:
    try:
        await bot.send_message(chat_id=cid, text=text, parse_mode=ParseMode.MARKDOWN,
                               disable_web_page_preview=True,
                               reply_to_message_id=reply_to)
        return True
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after); return await send_text(cid, text, reply_to)
    except TelegramError as e:
        log.error(f"[TG] texto: {e}"); return False

async def send_photo(cid: str, url: str, caption: str, reply_to: int = None) -> bool:
    fname = f"img_{hashlib.md5(url.encode()).hexdigest()[:8]}.jpg"
    local = await download_image(url, fname)
    # Fallback: si falla con :orig intentar sin él
    if not local and ":orig" in url:
        local = await download_image(url.replace(":orig", ""), fname)
    try:
        if local:
            with open(local, "rb") as f:
                await bot.send_photo(chat_id=cid, photo=f, caption=caption,
                                     parse_mode=ParseMode.MARKDOWN,
                                     reply_to_message_id=reply_to)
        else:
            await bot.send_photo(chat_id=cid, photo=url, caption=caption,
                                 parse_mode=ParseMode.MARKDOWN,
                                 reply_to_message_id=reply_to)
        return True
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after); return await send_photo(cid, url, caption, reply_to)
    except TelegramError as e:
        log.error(f"[TG] foto: {e}"); return False
    finally:
        _del(local or "")

async def send_video_tg(cid: str, tweet: Tweet, caption: str, reply_to: int = None) -> bool:
    result = await get_video(tweet)
    if not result:
        log.warning("[TG] Sin video — enviando texto")
        return await send_text(cid, caption, reply_to)
    path, w, h = result
    try:
        with open(path, "rb") as f:
            await bot.send_video(chat_id=cid, video=f, caption=caption,
                                 parse_mode=ParseMode.MARKDOWN, width=w, height=h,
                                 supports_streaming=True, reply_to_message_id=reply_to)
        return True
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after); return await send_video_tg(cid, tweet, caption, reply_to)
    except TelegramError as e:
        log.error(f"[TG] video: {e}"); return False
    finally:
        _del(path)

async def send_album(cid: str, urls: list, caption: str, reply_to: int = None) -> bool:
    locals_, media, handles = [], [], []
    try:
        for i, url in enumerate(urls[:10]):
            fname = f"img_{hashlib.md5(url.encode()).hexdigest()[:8]}.jpg"
            p = await download_image(url, fname)
            if not p and ":orig" in url:
                p = await download_image(url.replace(":orig", ""), fname)
            if p:
                locals_.append(p)
                fh = open(p, "rb")
                handles.append(fh)
                media.append(InputMediaPhoto(
                    media=fh,
                    caption=caption if i == 0 else None,
                    parse_mode=ParseMode.MARKDOWN if i == 0 else None,
                ))
        if media:
            await bot.send_media_group(chat_id=cid, media=media,
                                       reply_to_message_id=reply_to)
            return True
    except TelegramError as e:
        log.error(f"[TG] álbum: {e}")
    finally:
        for fh in handles:
            try: fh.close()
            except Exception: pass
        for p in locals_: _del(p)
    return False

# ══════════════════════════════════════════════════════════════════
#  FILTRO DE ALINEACIONES MLB — UnderdogMLB
# ══════════════════════════════════════════════════════════════════
# Patrones que identifican posts de alineaciones MLB de UnderdogMLB.
# Estos posts tienen un formato muy reconocible: encabezado con el equipo,
# posiciones (C, 1B, 2B, 3B, SS, LF, CF, RF, DH, SP) y nombres de jugadores.
_MLB_LINEUP_PATTERNS = [
    # Encabezado típico: "Starting Lineup" o "Today's Lineup"
    re.compile(r"(starting|today.?s|tonight.?s)\s+lineup", re.IGNORECASE),
    # Tres o más posiciones de campo en el mismo tweet
    re.compile(r"\b(SP|C|1B|2B|3B|SS|LF|CF|RF|DH)\b.*\b(SP|C|1B|2B|3B|SS|LF|CF|RF|DH)\b.*\b(SP|C|1B|2B|3B|SS|LF|CF|RF|DH)\b", re.DOTALL),
    # Listas numeradas de jugadores con posición (formato típico UnderdogMLB)
    re.compile(r"^\s*[1-9]\.\s+\w.*\b(CF|LF|RF|SS|2B|3B|1B|DH|C|SP)\b", re.MULTILINE | re.IGNORECASE),
]

def is_mlb_lineup(text: str) -> bool:
    """Detecta si un tweet de UnderdogMLB es una alineación de equipo MLB."""
    for pattern in _MLB_LINEUP_PATTERNS:
        if pattern.search(text):
            log.info(f"[FiltroMLB] Alineación detectada — omitiendo: {text[:60]!r}")
            return True
    return False


# ══════════════════════════════════════════════════════════════════
#  FILTRO DE LIVESCORE EN VIVO — BarcaUniversal / MadridUniversal
# ══════════════════════════════════════════════════════════════════
# Detecta por patrones fijos los tweets de seguimiento minuto a minuto
# ANTES de gastar una llamada a Groq. Si pasa este filtro, se llama a Groq
# solo para filtrar contenido irrelevante adicional (apuestas, trivial, etc.)
_LIVESCORE_PATTERNS = [
    # Marcador en vivo con minuto: "45' | Barça 1-0 Madrid" o "FT: 2-1"
    re.compile(r"\b\d{1,3}['′']\s*[|│]", re.IGNORECASE),
    # Etiquetas explícitas de livescore
    re.compile(r"\b(LIVE|EN VIVO|MINUTO\s+A\s+MINUTO|DIRECTO|MARCADOR\s+EN\s+VIVO|LIVESCORE)\b", re.IGNORECASE),
    # Bloque de marcador tipo "Barça 2 - 1 Madrid  ⏱ 67'"
    re.compile(r"\d\s*[-–]\s*\d.*[⏱🕐🕑🕒⌚]\s*\d{1,3}['′']", re.DOTALL),
    # Formato "HT:" o "FT:" con marcador numérico seguido de contexto de live
    re.compile(r"\b(HT|FT)\s*:\s*\d\s*[-–]\s*\d", re.IGNORECASE),
    # Actualizaciones de minuto: "Min. 34" o "Minuto 78"
    re.compile(r"\bmin(uto)?\.?\s*\d{1,3}\b", re.IGNORECASE),
]

def is_livescore_tweet(text: str) -> bool:
    """Detecta si un tweet es seguimiento en vivo (livescore / minuto a minuto)."""
    for pattern in _LIVESCORE_PATTERNS:
        if pattern.search(text):
            log.info(f"[FiltroLive] Livescore detectado — omitiendo: {text[:60]!r}")
            return True
    return False


# ══════════════════════════════════════════════════════════════════
#  FILTRO DE RELEVANCIA (fútbol) — Groq decide
# ══════════════════════════════════════════════════════════════════
_FILTER_PROMPT = """Eres un editor de un canal de fútbol profesional. Tu tarea es decidir si un tweet debe publicarse.

PUBLICAR si es:
- Noticia importante: fichaje, traspaso, lesión, convocatoria, rueda de prensa, clasificación de liga/copa
- Resultado FINAL de un partido (no actualizaciones en vivo)
- Gol confirmado, tarjeta roja, estadística destacada POST-partido
- Declaraciones relevantes de un jugador, entrenador o directivo
- Resumen o highlights de partido ya terminado

NO PUBLICAR si es:
- Contenido de apuestas, casas de apuestas, pronósticos (Stake, Bet365, Livescore, odds, cuotas…)
- Seguimiento en vivo, minuto a minuto, marcador en tiempo real, actualizaciones de partido en curso
- Contenido irrelevante (comida de aficionados, estadios vacíos, efemérides sin importancia)
- Encuestas, trivial o entretenimiento sin valor noticioso
- Publicidad, patrocinio o contenido promocional

Responde ÚNICAMENTE con una sola palabra: PUBLICAR o IGNORAR"""

async def is_relevant_football(text: str) -> bool:
    """Filtra livescore por regex primero (rápido y sin coste), luego Groq para el resto."""
    # 1. Filtro rápido de livescore sin coste de API
    if is_livescore_tweet(text):
        return False

    # 2. Si no hay cliente Groq, publicar todo lo que pasó el filtro de livescore
    if not groq_client:
        return True

    # 3. Groq filtra apuestas, trivial y demás contenido no relevante
    try:
        def _call():
            return groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": _FILTER_PROMPT},
                    {"role": "user",   "content": text[:800]},
                ],
                temperature=0.0, max_tokens=5,
            )
        resp = await asyncio.get_event_loop().run_in_executor(None, _call)
        decision = resp.choices[0].message.content.strip().upper()
        log.info(f"[Filtro] {decision[:8]} — {text[:60]!r}")
        return "PUBLICAR" in decision
    except Exception as e:
        log.warning(f"[Filtro] Error Groq: {e} — publicando por defecto")
        return True

# Dict en memoria: tweet_id → telegram message_id (para responder a tweets citados)
# Se guarda en disco junto con el estado
_sent_messages: Dict[str, int] = {}

def get_sent_msg_id(tweet_id: str) -> Optional[int]:
    return _sent_messages.get(tweet_id)

def save_sent_msg_id(tweet_id: str, msg_id: int):
    _sent_messages[tweet_id] = msg_id
    # Limpiar si crece mucho (guardar solo los últimos 2000)
    if len(_sent_messages) > 2000:
        oldest_keys = list(_sent_messages.keys())[:-1500]
        for k in oldest_keys: del _sent_messages[k]
    _save()

# ══════════════════════════════════════════════════════════════════
#  FORMATO
# ══════════════════════════════════════════════════════════════════
def _truncate(text: str, max_len: int = 1024) -> str:
    return text if len(text) <= max_len else text[:max_len - 3] + "..."

# ══════════════════════════════════════════════════════════════════
#  FORMATO ESPECIAL — mercatosphera (FichajesDeFutbol)
# ══════════════════════════════════════════════════════════════════
# La cuenta mercatosphera publica casi siempre fichajes oficiales o retiros.
# En vez de traducir el tweet tal cual, se reescribe con un formato fijo:
#
#   🚨🇦🇹 | OFICIAL: <Jugador> es nuevo jugador del <Club destino>
#
#   ▫️ Llega procedente del <Club origen>, firma hasta <año> con los <gentilicio>.
#
#   📲 Suscríbete en t.me/FichajesDeFutbol
#
# Para retiros:
#   🚨🏁 | OFICIAL: <Jugador> anuncia su retiro del fútbol profesional
#
#   ▫️ Pone fin a su carrera tras su paso por el <Club/Selección>.
#
#   📲 Suscríbete en t.me/FichajesDeFutbol

_TRANSFER_EXTRACT_PROMPT = """Eres un editor de un canal de fichajes de fútbol. Analiza el tweet y \
extrae los datos en JSON puro (sin texto extra, sin markdown, sin explicación).

Si es un FICHAJE/TRASPASO OFICIAL, responde:
{
  "tipo": "fichaje",
  "jugador": "<nombre del jugador>",
  "club_destino": "<nombre del club al que se une>",
  "club_origen": "<club del que llega, o \\"\\" si no se menciona>",
  "duracion": "<ej. 'hasta 2031', 'por una temporada', '' si no se menciona>",
  "gentilicio_destino": "<gentilicio plural del país/ciudad del club destino, ej. 'austriacos', 'ingleses', 'españoles', 'merengues' — usa algo natural y reconocible>",
  "pais_emoji_destino": "<emoji de bandera del país del club destino, ej. 🇦🇹>"
}

Si es un RETIRO, responde:
{
  "tipo": "retiro",
  "jugador": "<nombre del jugador>",
  "club_o_seleccion": "<último club o selección relevante, o \\"\\" si no se menciona>"
}

Si el tweet NO es un fichaje oficial ni un retiro (rumor, especulación, otra noticia), responde:
{"tipo": "otro"}

Responde ÚNICAMENTE el JSON, nada más."""

async def _extract_transfer_data(text: str) -> Optional[dict]:
    if not groq_client:
        return None
    try:
        def _call():
            return groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": _TRANSFER_EXTRACT_PROMPT},
                    {"role": "user", "content": text[:800]},
                ],
                temperature=0.0, max_tokens=300,
            )
        resp = await asyncio.get_event_loop().run_in_executor(None, _call)
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        return data
    except Exception as e:
        log.warning(f"[Mercatosphera] No se pudo extraer datos estructurados: {e}")
        return None

def _format_transfer(data: dict) -> Optional[str]:
    jugador = (data.get("jugador") or "").strip()
    if not jugador:
        return None

    if data.get("tipo") == "fichaje":
        club_destino = (data.get("club_destino") or "").strip()
        if not club_destino:
            return None
        club_origen  = (data.get("club_origen") or "").strip()
        duracion     = (data.get("duracion") or "").strip()
        gentilicio   = (data.get("gentilicio_destino") or "").strip()
        bandera      = (data.get("pais_emoji_destino") or "").strip()

        header = f"🚨{bandera} | *OFICIAL: {jugador} es nuevo jugador del {club_destino}*"

        detalle_parts = []
        if club_origen:
            detalle_parts.append(f"Llega procedente del {club_origen}")
        else:
            detalle_parts.append("Llega como nuevo refuerzo")
        if duracion:
            detalle_parts.append(f"firma {duracion}" + (f" con los {gentilicio}" if gentilicio else ""))
        elif gentilicio:
            detalle_parts.append(f"se une a los {gentilicio}")
        detalle = ", ".join(detalle_parts) + "."
        body = f"▫️ {detalle}"

        return f"{header}\n\n{body}"

    elif data.get("tipo") == "retiro":
        club = (data.get("club_o_seleccion") or "").strip()
        header = f"🚨🏁 | *OFICIAL: {jugador} anuncia su retiro del fútbol profesional*"
        if club:
            body = f"▫️ Pone fin a su carrera tras su paso por el {club}."
        else:
            body = "▫️ Pone fin a su carrera como futbolista profesional."
        return f"{header}\n\n{body}"

    return None

async def build_mercatosphera_caption(text: str, subscribe_msg: str) -> Optional[str]:
    """Intenta reformatear un tweet de mercatosphera como fichaje/retiro.
    Devuelve None si no aplica (no es fichaje/retiro), para que el caller
    use el formato genérico (traducción normal) como respaldo."""
    data = await _extract_transfer_data(text)
    if not data or data.get("tipo") not in ("fichaje", "retiro"):
        return None
    formatted = _format_transfer(data)
    if not formatted:
        return None
    return _truncate(f"{formatted}\n\n*{subscribe_msg}*")

async def build_caption(text: str, sport: str, translate_it: bool, subscribe_msg: str,
                        username: str = "") -> str:
    # Estilo directo especial para mercatosphera: fichajes y retiros con formato fijo.
    if username == "mercatosphera":
        special = await build_mercatosphera_caption(text, subscribe_msg)
        if special:
            return special
        # Si no es fichaje/retiro reconocible, cae al formato genérico de abajo.

    body = text
    if translate_it:
        body = await translate(body, sport)
    return _truncate(f"{body}\n\n*{subscribe_msg}*")

def should_post_basic(text: str, photos_only: bool, media_types: list,
                      username: str = "") -> bool:
    """Filtro básico para NBA/MLB.
    - Bloquea alineaciones MLB de UnderdogMLB.
    - Aplica photos_only cuando corresponde.
    """
    # Filtro de alineaciones MLB — solo para UnderdogMLB
    if username == "UnderdogMLB" and is_mlb_lineup(text):
        return False

    if not photos_only:
        return True
    if "photo" not in media_types:
        return False
    tl = text.lower()
    kw = ["final", "score", "recap", "result", "tonight", "last night",
          "game", "wins", "beats", "defeats", "victory", "walk-off",
          "highlights", "starting"]
    return any(k in tl for k in kw) or bool(re.search(r'\b\d{1,3}[-–]\d{1,3}\b', text))

# ══════════════════════════════════════════════════════════════════
#  CICLO
# ══════════════════════════════════════════════════════════════════
async def process_tweet(tweet: Tweet, channel_id: str, sport: str, config: dict,
                        subscribe_msg: str, use_filter: bool = False) -> bool:
    media_types = [m.type for m in tweet.media]

    if not should_post_basic(tweet.text, config.get("photos_only", False), media_types,
                             username=tweet.author_username):
        return False

    if use_filter:
        if not await is_relevant_football(tweet.text):
            return False

    caption = await build_caption(tweet.text, sport, config.get("translate", False), subscribe_msg,
                                  username=tweet.author_username)
    photos  = [m for m in tweet.media if m.type == "photo"]
    videos  = [m for m in tweet.media if m.type == "video"]

    # Si es un tweet citado, responder al mensaje original que ya enviamos
    reply_to = None
    if tweet.quoted_tweet_id:
        reply_to = get_sent_msg_id(tweet.quoted_tweet_id)
        if reply_to:
            log.info(f"[Bot] Tweet citado → respondiendo a msg_id {reply_to}")

    # Enviar y capturar el message_id de Telegram para futuras respuestas
    msg_id = None
    try:
        if videos:
            result = await get_video(tweet)
            if not result:
                msg = await bot.send_message(chat_id=channel_id, text=caption,
                                             parse_mode=ParseMode.MARKDOWN,
                                             disable_web_page_preview=True,
                                             reply_to_message_id=reply_to)
                msg_id = msg.message_id
            else:
                path, w, h = result
                try:
                    with open(path, "rb") as f:
                        msg = await bot.send_video(chat_id=channel_id, video=f, caption=caption,
                                                   parse_mode=ParseMode.MARKDOWN, width=w, height=h,
                                                   supports_streaming=True, reply_to_message_id=reply_to)
                    msg_id = msg.message_id
                finally:
                    _del(path)

        elif not tweet.media:
            msg = await bot.send_message(chat_id=channel_id, text=caption,
                                         parse_mode=ParseMode.MARKDOWN,
                                         disable_web_page_preview=True,
                                         reply_to_message_id=reply_to)
            msg_id = msg.message_id

        elif len(photos) == 1:
            fname = f"img_{hashlib.md5(photos[0].url.encode()).hexdigest()[:8]}.jpg"
            local = await download_image(photos[0].url, fname)
            try:
                src = open(local, "rb") if local else photos[0].url
                if local:
                    with open(local, "rb") as f:
                        msg = await bot.send_photo(chat_id=channel_id, photo=f, caption=caption,
                                                   parse_mode=ParseMode.MARKDOWN,
                                                   reply_to_message_id=reply_to)
                else:
                    msg = await bot.send_photo(chat_id=channel_id, photo=photos[0].url,
                                               caption=caption, parse_mode=ParseMode.MARKDOWN,
                                               reply_to_message_id=reply_to)
                msg_id = msg.message_id
            finally:
                _del(local or "")

        else:
            # Álbum — el message_id del primero
            locals_, media, handles = [], [], []
            try:
                for i, p in enumerate(photos[:10]):
                    fname = f"img_{hashlib.md5(p.url.encode()).hexdigest()[:8]}.jpg"
                    lp = await download_image(p.url, fname)
                    if lp:
                        locals_.append(lp)
                        fh = open(lp, "rb")
                        handles.append(fh)
                        media.append(InputMediaPhoto(
                            media=fh,
                            caption=caption if i == 0 else None,
                            parse_mode=ParseMode.MARKDOWN if i == 0 else None,
                        ))
                if media:
                    msgs = await bot.send_media_group(chat_id=channel_id, media=media,
                                                      reply_to_message_id=reply_to)
                    msg_id = msgs[0].message_id if msgs else None
            finally:
                for fh in handles:
                    try: fh.close()
                    except Exception: pass
                for lp in locals_: _del(lp)

        if msg_id:
            save_sent_msg_id(tweet.id, msg_id)
        return msg_id is not None

    except RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        return await process_tweet(tweet, channel_id, sport, config, subscribe_msg, use_filter)
    except TelegramError as e:
        log.error(f"[TG] Error enviando tweet {tweet.id}: {e}")
        return False


async def _process_group(accounts: dict, channel_id: str, sport: str,
                          subscribe_msg: str, use_filter: bool = False,
                          max_age_minutes: Optional[int] = None,
                          force_reprocess: bool = False) -> list:
    """Obtiene tweets de un grupo de cuentas y devuelve tareas pendientes.

    Si max_age_minutes está definido, solo se consideran tweets publicados
    dentro de esa ventana de tiempo (usado por /scan 5m, /scan 30m, etc),
    ignorando el since_id guardado — útil para re-escanear una ventana
    reciente bajo demanda sin tocar el estado normal del bot.

    force_reprocess=True además ignora is_processed, es decir, vuelve a
    publicar tweets de esa ventana aunque ya se hayan publicado antes
    (usado por /scan Xm force). Por defecto es False para no duplicar posts.
    """
    tasks = []
    cutoff_ts = None
    if max_age_minutes is not None:
        cutoff_ts = time.time() - max_age_minutes * 60

    for username, config in accounts.items():
        if max_age_minutes is not None:
            # Escaneo manual por ventana de tiempo: ignoramos since_id,
            # traemos los tweets recientes de la cuenta y filtramos por edad.
            tweets = await fetch_tweets_syndication(username, since_id=None)
            fresh = []
            for t in tweets:
                ts = tweet_id_to_timestamp(t.id)
                if ts is not None and ts >= cutoff_ts:
                    fresh.append(t)
            for t in reversed(fresh):
                if force_reprocess or not is_processed(t.id):
                    tasks.append((t, channel_id, sport, config, username, subscribe_msg, use_filter))
            continue

        since_id = get_last_id(username)
        tweets = await fetch_tweets_syndication(username, since_id=since_id)

        # Primera vez que vemos esta cuenta (sin since_id):
        # marcar todos como vistos sin publicar nada, para no inundar el canal con posts viejos
        if since_id is None and tweets:
            newest_id = max(tweets, key=lambda t: int(t.id)).id
            set_last_id(username, newest_id)
            for t in tweets:
                mark_processed(t.id)
            log.info(f"[Bot] Primera vez @{username} — {len(tweets)} tweets marcados, sin publicar")
            continue

        for t in reversed(tweets):
            if not is_processed(t.id):
                tasks.append((t, channel_id, sport, config, username, subscribe_msg, use_filter))
    return tasks


async def run_cycle(max_age_minutes: Optional[int] = None, force_reprocess: bool = False) -> int:
    log.info("[Bot] 🔄 Ciclo iniciado" + (f" (ventana: últimos {max_age_minutes} min{', forzado' if force_reprocess else ''})" if max_age_minutes else ""))
    tasks = []

    for name, accounts, channel_env, sport, sub_msg, use_filter in GROUPS:
        channel_id = globals()[channel_env]
        if not channel_id:
            continue
        tasks += await _process_group(accounts, channel_id, sport, sub_msg,
                                      use_filter=use_filter, max_age_minutes=max_age_minutes,
                                      force_reprocess=force_reprocess)

    if not tasks:
        log.info("[Bot] Sin tweets nuevos."); return 0

    log.info(f"[Bot] Procesando {len(tasks)} tweets...")
    published = 0
    for tweet, channel, sport, config, username, sub_msg, use_filter in tasks:
        try:
            ok = await process_tweet(tweet, channel, sport, config, sub_msg, use_filter)
            mark_processed(tweet.id)
            # En escaneo por ventana de tiempo no movemos el since_id hacia atrás,
            # solo lo actualizamos si el tweet es más nuevo que el guardado.
            current_last = get_last_id(username)
            if current_last is None or int(tweet.id) > int(current_last):
                set_last_id(username, tweet.id)
            if ok:
                published += 1
            log.info(f"[Bot] {'✅' if ok else '⏭'} [{sport.upper()}] @{username}/{tweet.id}")
            await asyncio.sleep(3)
        except Exception as e:
            log.error(f"[Bot] ❌ {tweet.id}: {e}")

    cleanup_old()
    log.info(f"[Bot] ✅ Ciclo completado. Publicados: {published}/{len(tasks)}")
    return published

# ══════════════════════════════════════════════════════════════════
#  COMANDOS DE TELEGRAM
# ══════════════════════════════════════════════════════════════════
_scheduler_ref = {"scheduler": None}  # se asigna en main()

def _parse_interval(text: str) -> Optional[int]:
    """Convierte '5m', '30m', '1h', '90s' o un número plano (minutos) a minutos.
    Devuelve None si no se pudo interpretar."""
    text = text.strip().lower()
    m = re.fullmatch(r"(\d+)\s*(m|min|minuto|minutos)?", text)
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"(\d+)\s*(h|hr|hora|horas)", text)
    if m:
        return int(m.group(1)) * 60
    m = re.fullmatch(r"(\d+)\s*(s|seg|segundo|segundos)", text)
    if m:
        # apscheduler trabaja en minutos en este bot; redondeamos hacia arriba a 1 min mínimo
        secs = int(m.group(1))
        return max(1, round(secs / 60))
    return None

def _is_admin(update: Update) -> bool:
    if not ADMIN_TELEGRAM_ID:
        return True  # si no se configuró admin, cualquiera puede usar los comandos
    return bool(update.effective_user) and update.effective_user.id == ADMIN_TELEGRAM_ID

async def _safe_reply(update: Update, text: str, parse_mode=ParseMode.MARKDOWN):
    """Responde con Markdown, pero si Telegram rechaza el parseo (p.ej. por un
    guión bajo suelto en un username como Mercado_Ingles, que el Markdown
    legacy interpreta como cursiva sin cerrar), reintenta en texto plano en
    vez de fallar en silencio. Sin esto, un comando podía "no responder"
    sin dejar rastro visible para el usuario."""
    try:
        await update.message.reply_text(text, parse_mode=parse_mode)
    except TelegramError as e:
        log.warning(f"[Telegram] Falló el parseo de Markdown ({e}) — reintentando en texto plano")
        plain = re.sub(r"[*_`\[\]]", "", text)
        try:
            await update.message.reply_text(plain)
        except TelegramError as e2:
            log.error(f"[Telegram] No se pudo enviar ni en texto plano: {e2}")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 *Sports Telegram Bot*\n\n"
        "Este bot lee, traduce (cuando aplica) y publica posts de X/Twitter "
        "en los canales configurados, junto con sus fotos/videos.\n\n"
        "*Comandos disponibles:*\n\n"
        "📡 `/scan` — Escanea ahora todo lo pendiente (sin filtrar por tiempo).\n"
        "📡 `/scan 5m` — Escanea solo lo publicado en los últimos 5 minutos.\n"
        "📡 `/scan 30m` — Escanea solo lo publicado en los últimos 30 minutos.\n"
        "📡 `/scan 1h` — Escanea solo lo publicado en la última hora.\n"
        "   _(acepta minutos `m`, horas `h` o un número plano = minutos)_\n\n"
        "⏱ `/interval` — Muestra cada cuánto corre el escaneo automático.\n"
        "⏱ `/interval 5m` / `30m` / `1h` — Cambia ese intervalo automático.\n\n"
        "📋 `/accounts` — Lista todas las cuentas de X y a qué canal de "
        "Telegram publica cada una.\n\n"
        "ℹ️ `/status` — Estado actual del bot (ciclos corridos, intervalo, etc).\n\n"
        "❓ `/start` — Muestra este mensaje de ayuda."
    )
    await _safe_reply(update, text)

async def cmd_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["📋 *Cuentas configuradas:*\n"]
    for name, accounts, channel_env, sport, sub_msg, use_filter in GROUPS:
        channel_id = globals()[channel_env]
        estado = channel_id if channel_id else "⚠️ sin canal configurado"
        lines.append(f"\n*{name}* → `{estado}`")
        for username in accounts:
            # Escapamos guiones bajos (ej. Mercado_Ingles) para que el Markdown
            # legacy de Telegram no los interprete como apertura de cursiva sin
            # cerrar — eso hacía que el mensaje completo fuera rechazado por la
            # API y el comando pareciera "no responder".
            safe_username = username.replace("_", "\\_")
            lines.append(f"   • @{safe_username}")
    await _safe_reply(update, "\n".join(lines))

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = _state
    await _safe_reply(
        update,
        f"📊 *Estado del bot*\n\n"
        f"Estado: `{s['status']}`\n"
        f"Iniciado: `{s['started_at']}`\n"
        f"Ciclos completados: `{s['cycles']}`\n"
        f"Último ciclo: `{s['last_cycle']}`\n"
        f"Intervalo actual: `{CHECK_INTERVAL_MINUTES} min`"
    )

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /scan               -> escanea TODO lo nuevo desde el último tweet visto por cuenta.
    /scan 5m            -> escanea solo tweets publicados en los últimos 5 minutos
                           (que aún no se hayan publicado antes).
    /scan 30m           -> ídem con los últimos 30 minutos.
    /scan 1h            -> ídem con la última hora.
    /scan 1h force      -> ídem pero IGNORA si ya se publicó antes — vuelve a
                           publicar todo lo que haya en esa ventana de tiempo.
                           Útil para verificar/forzar reenvíos de prueba.
    No afecta el intervalo automático — para eso usa /interval.
    """
    if not _is_admin(update):
        await _safe_reply(update, "⛔ No tienes permiso para usar este comando.", parse_mode=None)
        return

    args = context.args
    max_age_minutes = None
    force_reprocess = False

    if args:
        if args[0].lower() in ("force", "forzar") and len(args) == 1:
            await _safe_reply(update, "❌ `/scan force` necesita una ventana de tiempo, ej:\n`/scan 1h force`")
            return

        max_age_minutes = _parse_interval(args[0])
        if max_age_minutes is None or max_age_minutes <= 0:
            await _safe_reply(
                update,
                "❌ Formato inválido. Usa algo como:\n`/scan 5m`, `/scan 30m`, `/scan 1h`, `/scan 1h force`\n\n"
                "O usa `/scan` sin argumentos para escanear todo lo pendiente."
            )
            return

        if len(args) > 1 and args[1].lower() in ("force", "forzar"):
            force_reprocess = True

        aviso = f"🔎 Escaneando publicaciones de los últimos *{args[0]}*"
        aviso += " (modo forzado, puede duplicar posts ya enviados)..." if force_reprocess else "..."
    else:
        aviso = "🔎 Escaneando todas las cuentas (todo lo pendiente)..."

    await _safe_reply(update, aviso)

    # Se ejecuta como tarea en segundo plano para no bloquear el loop de eventos
    # (y así el bot sigue respondiendo a otros comandos mientras escanea).
    async def _run():
        try:
            published = await bot_cycle(max_age_minutes=max_age_minutes, force_reprocess=force_reprocess)
            ventana_txt = f" en los últimos {args[0]}" if args else ""
            await _safe_reply(update, f"✅ Escaneo completado{ventana_txt}. Publicados: {published} post(s).", parse_mode=None)
        except Exception as e:
            log.error(f"[Bot] ❌ Error en /scan: {e}")
            await _safe_reply(update, f"❌ Error durante el escaneo: {e}", parse_mode=None)

    asyncio.create_task(_run())

async def cmd_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /interval 5m / 30m / 1h -> cambia cada cuánto corre el ciclo AUTOMÁTICO.
    /interval               -> muestra el intervalo actual.
    """
    global CHECK_INTERVAL_MINUTES
    if not _is_admin(update):
        await _safe_reply(update, "⛔ No tienes permiso para usar este comando.", parse_mode=None)
        return

    args = context.args
    if not args:
        await _safe_reply(
            update,
            f"⏱ Intervalo automático actual: *{CHECK_INTERVAL_MINUTES} min*\n\n"
            f"Para cambiarlo: `/interval 5m`, `/interval 30m`, `/interval 1h`"
        )
        return

    new_minutes = _parse_interval(args[0])
    if new_minutes is None or new_minutes <= 0:
        await _safe_reply(update, "❌ Formato inválido. Usa algo como:\n`/interval 5m`, `/interval 30m`, `/interval 1h`")
        return

    scheduler = _scheduler_ref["scheduler"]
    if scheduler is None:
        await _safe_reply(update, "❌ El scheduler aún no está disponible.", parse_mode=None)
        return

    scheduler.reschedule_job("cycle", trigger=IntervalTrigger(minutes=new_minutes))
    CHECK_INTERVAL_MINUTES = new_minutes
    await _safe_reply(update, f"⏱ Intervalo automático actualizado a *{new_minutes} min*.")
    log.info(f"[Bot] Intervalo automático reprogramado a {new_minutes} min vía /interval")

# ══════════════════════════════════════════════════════════════════
#  HTTP + MAIN
# ══════════════════════════════════════════════════════════════════
_state = {"started_at": None, "cycles": 0, "last_cycle": None, "status": "starting"}

async def handle_health(req): return web.Response(text="OK", content_type="text/plain")
async def handle_status(req):
    return web.Response(
        text=json.dumps({**_state, "interval_min": CHECK_INTERVAL_MINUTES,
                         "nba": NBA_CHANNEL_ID, "mlb": MLB_CHANNEL_ID,
                         "barca": BARCA_CHANNEL_ID, "madrid": MADRID_CHANNEL_ID,
                         "premier": PREMIER_CHANNEL_ID, "fichajes": FICHAJES_CHANNEL_ID}, indent=2),
        content_type="application/json")

async def bot_cycle(max_age_minutes: Optional[int] = None, force_reprocess: bool = False) -> int:
    try:
        _state["status"] = "running"
        published = await run_cycle(max_age_minutes=max_age_minutes, force_reprocess=force_reprocess)
        _state["cycles"] += 1
        _state["last_cycle"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        _state["status"] = "idle"
        return published
    except Exception as e:
        _state["status"] = "error"; log.error(f"[Bot] ❌ {e}")
        return 0

async def main():
    _load_state()
    _state["started_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    log.info("╔══════════════════════════════════════════╗")
    log.info("║  🏀⚾⚽ SPORTS TELEGRAM BOT ⚽⚾🏀      ║")
    log.info("╚══════════════════════════════════════════╝")
    log.info(f"NBA:      {NBA_CHANNEL_ID}")
    log.info(f"MLB:      {MLB_CHANNEL_ID}")
    log.info(f"Barça:    {BARCA_CHANNEL_ID}")
    log.info(f"Madrid:   {MADRID_CHANNEL_ID}")
    log.info(f"Premier:  {PREMIER_CHANNEL_ID}")
    log.info(f"Fichajes: {FICHAJES_CHANNEL_ID}")
    log.info(f"Groq:     {'✅' if groq_client else '⚠️ sin key'}")

    # ── Servidor HTTP (health/status para Render) ──────────────────
    app = web.Application()
    app.router.add_get("/", handle_status)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/status", handle_status)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"Web:      ✅ 0.0.0.0:{PORT}")

    # ── Bot de Telegram con comandos (/start, /scan, /interval, /accounts, /status) ──
    tg_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("scan", cmd_scan))
    tg_app.add_handler(CommandHandler("interval", cmd_interval))
    tg_app.add_handler(CommandHandler("accounts", cmd_accounts))
    tg_app.add_handler(CommandHandler("status", cmd_status))
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)
    log.info("Telegram: ✅ comandos /start /scan /interval /accounts /status activos")

    # ── Primer ciclo en segundo plano ───────────────────────────────
    # IMPORTANTE: no se usa "await" aquí. Si se espera (await bot_cycle())
    # el loop de eventos queda ocupado todo el tiempo que tarda el primer
    # escaneo (descargas de fotos/videos, llamadas a Groq, etc) y el polling
    # de Telegram no recibe tiempo de CPU para procesar comandos como
    # /accounts o /scan durante ese rato — por eso parecía no responder.
    asyncio.create_task(bot_cycle())

    # ── Scheduler de ciclos automáticos ─────────────────────────────
    scheduler = AsyncIOScheduler()
    scheduler.add_job(bot_cycle, IntervalTrigger(minutes=CHECK_INTERVAL_MINUTES),
                      id="cycle", replace_existing=True, max_instances=1)
    scheduler.start()
    _scheduler_ref["scheduler"] = scheduler
    log.info(f"Scheduler: cada {CHECK_INTERVAL_MINUTES} min")

    loop = asyncio.get_running_loop()
    def _stop(sig):
        scheduler.shutdown(wait=False)
        asyncio.ensure_future(tg_app.updater.stop())
        asyncio.ensure_future(tg_app.stop())
        loop.stop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop, sig.name)

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
