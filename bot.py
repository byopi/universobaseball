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
from telegram import Bot, InputMediaPhoto, InputMediaVideo
from telegram.error import TelegramError, RetryAfter
from telegram.constants import ParseMode
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
GROQ_API_KEY           = os.getenv("GROQ_API_KEY", "")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))
MAX_VIDEO_SIZE_BYTES   = int(os.getenv("MAX_VIDEO_SIZE_MB", "50")) * 1024 * 1024
PORT                   = int(os.getenv("PORT", "8080"))

NBA_SUBSCRIBE     = "📲 Suscríbete en t.me/NBA_Latinoamerica"
MLB_SUBSCRIBE     = "📲 Suscríbete en t.me/UniversoBaseball"
BARCA_SUBSCRIBE   = "📲 Suscríbete en t.me/iFCBNewsES"
MADRID_SUBSCRIBE  = "📲 Suscríbete en t.me/iRMNewsES"
PREMIER_SUBSCRIBE = "📲 Suscríbete en t.me/PremierLeague_ES"

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
            _processed_ids.update(json.loads(STATE_FILE.read_text()).get("ids", []))
        if LAST_IDS_FILE.exists():
            _last_ids.update(json.loads(LAST_IDS_FILE.read_text()))
    except Exception as e:
        log.warning(f"Disco no disponible: {e}"); _disk_ok = False

def _save():
    if not _disk_ok: return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({"ids": list(_processed_ids)}))
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
    tweet_url: str = ""   # URL de x.com para descargar video con yt-dlp

def _parse_syndication(html: str, username: str) -> List[Tweet]:
    """Extrae tweets del HTML de la Syndication API de Twitter."""
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
        entries = (data.get("props", {})
                      .get("pageProps", {})
                      .get("timeline", {})
                      .get("entries", []))
        tweets = []
        for entry in entries:
            tweet = entry.get("content", {}).get("tweet", {})
            if not tweet: continue

            tid = tweet.get("id_str", "")
            if not tid: continue

            # Saltar retweets
            if tweet.get("retweeted_status"): continue

            text = tweet.get("full_text", tweet.get("text", ""))
            # Limpiar URLs de t.co
            text = re.sub(r"https?://t\.co/\S+", "", text).strip()
            # @menciones → solo nombre
            text = re.sub(r"@(\w+)", r"\1", text)

            # Extraer media de extended_entities (más completo que entities)
            media_list = []
            extended = tweet.get("extended_entities", {}).get("media", [])
            entities  = tweet.get("entities", {}).get("media", [])
            all_media = extended or entities

            has_video = False
            for m in all_media:
                mtype = m.get("type", "")
                if mtype == "photo":
                    url = m.get("media_url_https", "") or m.get("media_url", "")
                    if url:
                        media_list.append(TweetMedia(type="photo", url=url + ":orig"))
                elif mtype in ("video", "animated_gif"):
                    has_video = True
                    # Elegir variante mp4 de mayor bitrate
                    variants = m.get("video_info", {}).get("variants", [])
                    best = sorted(
                        [v for v in variants if v.get("content_type") == "video/mp4"],
                        key=lambda v: v.get("bitrate", 0), reverse=True
                    )
                    thumb = m.get("media_url_https", "")
                    if best:
                        media_list.append(TweetMedia(
                            type="video", url=best[0]["url"], thumb_url=thumb
                        ))
                    elif thumb:
                        media_list.append(TweetMedia(type="photo", url=thumb))

            tweets.append(Tweet(
                id=tid, text=text, author_username=username,
                created_at=tweet.get("created_at", ""),
                media=media_list,
                tweet_url=f"https://x.com/{username}/status/{tid}",
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
            resp = await asyncio.get_event_loop().run_in_executor(
                None, lambda u=f"{base}/{username}/rss": requests.get(u, headers={"User-Agent": "SportsBot/1.0"}, timeout=10)
            )
            if resp.status_code != 200: continue
            feed = feedparser.parse(resp.content)
            if not feed.entries: continue

            tweets = []
            for entry in feed.entries[:15]:
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
                text = re.sub(r"@(\w+)", r"\1", text)
                if not text or text.startswith("RT "): continue

                # Nitter no nos da videos directos, pero sí la URL del tweet
                xurl = re.sub(r'https://(nitter\.net|xcancel\.com|nitter\.cz)/', 'https://x.com/', link)
                media = []
                for img in soup.find_all("img"):
                    src = img.get("src", "")
                    if src and src.startswith("http") and "pbs.twimg.com" in src:
                        media.append(TweetMedia(type="photo", url=src))
                # Marcar que puede tener video (lo descargaremos por tweet_url)
                has_video_hint = any(x in entry.get("description","") for x in ["video", "gif"])
                if has_video_hint and not media:
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

async def translate(text: str, sport: str) -> str:
    if not text.strip() or not groq_client: return text
    ctx = "béisbol (MLB)" if sport == "mlb" else ("fútbol (La Liga)" if sport == "football" else "baloncesto (NBA)")
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
        log.info(f"[Groq] ✅ Traducido")
        return result
    except Exception as e:
        log.warning(f"[Groq] Error: {e}"); return text

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
async def send_text(cid: str, text: str) -> bool:
    try:
        await bot.send_message(chat_id=cid, text=text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        return True
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after); return await send_text(cid, text)
    except TelegramError as e:
        log.error(f"[TG] texto: {e}"); return False

async def send_photo(cid: str, url: str, caption: str) -> bool:
    fname = f"img_{hashlib.md5(url.encode()).hexdigest()[:8]}.jpg"
    local = await download_image(url, fname)
    try:
        if local:
            with open(local, "rb") as f:
                await bot.send_photo(chat_id=cid, photo=f, caption=caption, parse_mode=ParseMode.MARKDOWN)
        else:
            await bot.send_photo(chat_id=cid, photo=url, caption=caption, parse_mode=ParseMode.MARKDOWN)
        return True
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after); return await send_photo(cid, url, caption)
    except TelegramError as e:
        log.error(f"[TG] foto: {e}"); return False
    finally:
        _del(local or "")

async def send_video_tg(cid: str, tweet: Tweet, caption: str) -> bool:
    result = await get_video(tweet)
    if not result:
        log.warning("[TG] Sin video — enviando texto")
        return await send_text(cid, caption)
    path, w, h = result
    try:
        with open(path, "rb") as f:
            await bot.send_video(chat_id=cid, video=f, caption=caption,
                                 parse_mode=ParseMode.MARKDOWN, width=w, height=h, supports_streaming=True)
        return True
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after); return await send_video_tg(cid, tweet, caption)
    except TelegramError as e:
        log.error(f"[TG] video: {e}"); return False
    finally:
        _del(path)

async def send_album(cid: str, urls: list, caption: str) -> bool:
    locals_, media = [], []
    try:
        for i, url in enumerate(urls[:10]):
            fname = f"img_{hashlib.md5(url.encode()).hexdigest()[:8]}.jpg"
            p = await download_image(url, fname)
            if p:
                locals_.append(p)
                media.append(InputMediaPhoto(
                    media=open(p, "rb"),
                    caption=caption if i == 0 else None,
                    parse_mode=ParseMode.MARKDOWN if i == 0 else None,
                ))
        if media:
            await bot.send_media_group(chat_id=cid, media=media); return True
    except TelegramError as e:
        log.error(f"[TG] álbum: {e}")
    finally:
        for p in locals_: _del(p)
    return False

# ══════════════════════════════════════════════════════════════════
#  FILTRO DE RELEVANCIA (fútbol) — Groq decide
# ══════════════════════════════════════════════════════════════════
_FILTER_PROMPT = """Eres un editor de un canal de fútbol profesional. Tu tarea es decidir si un tweet debe publicarse.

PUBLICAR si es:
- Noticia importante: fichaje, lesión, convocatoria, rueda de prensa, resultado de partido, clasificación
- Resultado final de un partido
- Gol, tarjeta roja, estadística destacada de un partido
- Declaraciones relevantes de un jugador o entrenador

NO PUBLICAR si es:
- Contenido de apuestas, casas de apuestas, pronósticos (Stake, Bet365, Livescore, odds, cuotas…)
- Seguimiento en vivo / live score / minuto a minuto
- Contenido irrelevante o curioso (comida de aficionados, estadios vacíos, anécdotas sin importancia)
- Encuestas o trivial
- Publicidad o patrocinio

Responde ÚNICAMENTE con una sola palabra: PUBLICAR o IGNORAR"""

async def is_relevant_football(text: str) -> bool:
    """Usa Groq para decidir si un tweet de fútbol es relevante. Devuelve True si debe publicarse."""
    if not groq_client:
        return True  # sin filtro si no hay key
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

# ══════════════════════════════════════════════════════════════════
#  FORMATO
# ══════════════════════════════════════════════════════════════════
def _truncate(text: str, max_len: int = 1024) -> str:
    return text if len(text) <= max_len else text[:max_len - 3] + "..."

async def build_caption(text: str, sport: str, translate_it: bool, subscribe_msg: str) -> str:
    body = text
    if translate_it:
        body = await translate(body, sport)
    return _truncate(f"{body}\n\n*{subscribe_msg}*")

def should_post_basic(text: str, photos_only: bool, media_types: list) -> bool:
    """Filtro básico para NBA/MLB (photos_only)."""
    if not photos_only: return True
    if "photo" not in media_types: return False
    tl = text.lower()
    kw = ["final", "score", "recap", "result", "tonight", "last night",
          "game", "wins", "beats", "defeats", "victory", "walk-off",
          "highlights", "lineup", "starting"]
    return any(k in tl for k in kw) or bool(re.search(r'\b\d{1,3}[-–]\d{1,3}\b', text))

# ══════════════════════════════════════════════════════════════════
#  CICLO
# ══════════════════════════════════════════════════════════════════
async def process_tweet(tweet: Tweet, channel_id: str, sport: str, config: dict,
                        subscribe_msg: str, use_filter: bool = False) -> bool:
    media_types = [m.type for m in tweet.media]

    # Filtro básico photos_only para NBA/MLB
    if not should_post_basic(tweet.text, config.get("photos_only", False), media_types):
        return False

    # Filtro de relevancia con Groq para cuentas de fútbol
    if use_filter:
        relevant = await is_relevant_football(tweet.text)
        if not relevant:
            return False

    caption = await build_caption(tweet.text, sport, config.get("translate", False), subscribe_msg)
    photos  = [m for m in tweet.media if m.type == "photo"]
    videos  = [m for m in tweet.media if m.type == "video"]

    if videos:
        return await send_video_tg(channel_id, tweet, caption)
    elif not tweet.media:
        return await send_text(channel_id, caption)
    elif len(photos) == 1:
        return await send_photo(channel_id, photos[0].url, caption)
    else:
        return await send_album(channel_id, [p.url for p in photos], caption)


async def _process_group(accounts: dict, channel_id: str, sport: str,
                          subscribe_msg: str, use_filter: bool = False) -> list:
    """Obtiene tweets de un grupo de cuentas y devuelve tareas pendientes."""
    tasks = []
    for username, config in accounts.items():
        tweets = await fetch_tweets_syndication(username, since_id=get_last_id(username))
        for t in reversed(tweets):
            if not is_processed(t.id):
                tasks.append((t, channel_id, sport, config, username, subscribe_msg, use_filter))
    return tasks


async def run_cycle():
    log.info("[Bot] 🔄 Ciclo iniciado")
    tasks = []

    tasks += await _process_group(NBA_ACCOUNTS,    NBA_CHANNEL_ID,    "nba",     NBA_SUBSCRIBE)
    tasks += await _process_group(MLB_ACCOUNTS,    MLB_CHANNEL_ID,    "mlb",     MLB_SUBSCRIBE)
    tasks += await _process_group(BARCA_ACCOUNTS,  BARCA_CHANNEL_ID,  "futbol",  BARCA_SUBSCRIBE,  use_filter=False)
    tasks += await _process_group(MADRID_ACCOUNTS, MADRID_CHANNEL_ID, "futbol",  MADRID_SUBSCRIBE, use_filter=False)
    tasks += await _process_group(PREMIER_ACCOUNTS,PREMIER_CHANNEL_ID,"premier", PREMIER_SUBSCRIBE,use_filter=True)

    if not tasks:
        log.info("[Bot] Sin tweets nuevos."); return

    log.info(f"[Bot] Procesando {len(tasks)} tweets...")
    for tweet, channel, sport, config, username, sub_msg, use_filter in tasks:
        try:
            ok = await process_tweet(tweet, channel, sport, config, sub_msg, use_filter)
            mark_processed(tweet.id)
            set_last_id(username, tweet.id)
            log.info(f"[Bot] {'✅' if ok else '⏭'} [{sport.upper()}] @{username}/{tweet.id}")
            await asyncio.sleep(3)
        except Exception as e:
            log.error(f"[Bot] ❌ {tweet.id}: {e}")

    cleanup_old()
    log.info("[Bot] ✅ Ciclo completado.")

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
                         "premier": PREMIER_CHANNEL_ID}, indent=2),
        content_type="application/json")

async def bot_cycle():
    try:
        _state["status"] = "running"
        await run_cycle()
        _state["cycles"] += 1
        _state["last_cycle"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        _state["status"] = "idle"
    except Exception as e:
        _state["status"] = "error"; log.error(f"[Bot] ❌ {e}")

async def main():
    _load_state()
    _state["started_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    log.info("╔══════════════════════════════════════════╗")
    log.info("║  🏀⚾⚽ SPORTS TELEGRAM BOT ⚽⚾🏀      ║")
    log.info("╚══════════════════════════════════════════╝")
    log.info(f"NBA:     {NBA_CHANNEL_ID}")
    log.info(f"MLB:     {MLB_CHANNEL_ID}")
    log.info(f"Barça:   {BARCA_CHANNEL_ID}")
    log.info(f"Madrid:  {MADRID_CHANNEL_ID}")
    log.info(f"Premier: {PREMIER_CHANNEL_ID}")
    log.info(f"Groq:    {'✅' if groq_client else '⚠️ sin key'}")

    app = web.Application()
    app.router.add_get("/", handle_status)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/status", handle_status)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"Web:      ✅ 0.0.0.0:{PORT}")

    await bot_cycle()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(bot_cycle, IntervalTrigger(minutes=CHECK_INTERVAL_MINUTES),
                      id="cycle", replace_existing=True, max_instances=1)
    scheduler.start()
    log.info(f"Scheduler: cada {CHECK_INTERVAL_MINUTES} min")

    loop = asyncio.get_running_loop()
    def _stop(sig): scheduler.shutdown(wait=False); loop.stop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop, sig.name)

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
