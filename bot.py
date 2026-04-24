import os, re, json, asyncio, random, signal, tempfile, time, hashlib
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple, Set, Dict
from urllib.parse import unquote

import feedparser, requests, aiohttp, aiofiles
from groq import Groq
from bs4 import BeautifulSoup
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from telegram import Bot, InputMediaPhoto
from telegram.error import TelegramError, RetryAfter
from telegram.constants import ParseMode
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
NBA_CHANNEL_ID         = os.getenv("NBA_CHANNEL_ID", "")
MLB_CHANNEL_ID         = os.getenv("MLB_CHANNEL_ID", "")
GROQ_API_KEY           = os.getenv("GROQ_API_KEY", "")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))
MAX_VIDEO_SIZE_BYTES   = int(os.getenv("MAX_VIDEO_SIZE_MB", "50")) * 1024 * 1024
PORT                   = int(os.getenv("PORT", "8080"))

NBA_SUBSCRIBE = "📲 Suscríbete en t.me/NBA_Latinoamerica"
MLB_SUBSCRIBE = "📲 Suscríbete en t.me/UniversoBaseball"

NBA_ACCOUNTS = {
    "UnderdogNBA": {"translate": True,  "photos_only": False},
    "NBALatam":    {"translate": False, "photos_only": True},
}
MLB_ACCOUNTS = {
    "UnderdogMLB": {"translate": True,  "photos_only": False},
    "MLB":         {"translate": False, "photos_only": True},
}

NITTER_INSTANCES = [
    "https://nitter.net",
    "https://xcancel.com",
    "https://nitter.cz",
]

TEMP_DIR      = Path(tempfile.gettempdir()) / "sports_bot"
TEMP_DIR.mkdir(exist_ok=True)
DATA_DIR      = Path(os.getenv("DATA_DIR", "data"))
STATE_FILE    = DATA_DIR / "processed_tweets.json"
LAST_IDS_FILE = DATA_DIR / "last_tweet_ids.json"

bot         = Bot(token=TELEGRAM_BOT_TOKEN)
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# ══════════════════════════════════════════════════════════════════
#  ESTADO EN MEMORIA (+ backup en disco si está disponible)
# ══════════════════════════════════════════════════════════════════
_processed_ids: Set[str]   = set()
_last_ids: Dict[str, str]  = {}   # siempre en memoria
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
        print(f"[State] Disco no disponible: {e}"); _disk_ok = False

def _save_ids():
    if not _disk_ok: return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({"ids": list(_processed_ids)}))
        LAST_IDS_FILE.write_text(json.dumps(_last_ids))
    except Exception: pass

def is_processed(tid: str) -> bool:
    return str(tid) in _processed_ids

def mark_processed(tid: str):
    _processed_ids.add(str(tid))
    if len(_processed_ids) > 5000:
        for old in sorted(_processed_ids)[:-4000]: _processed_ids.discard(old)
    _save_ids()

def set_last_id(username: str, tid: str):
    _last_ids[username] = tid
    _save_ids()

def get_last_id(username: str) -> Optional[str]:
    return _last_ids.get(username)

# ══════════════════════════════════════════════════════════════════
#  SCRAPER NITTER RSS
# ══════════════════════════════════════════════════════════════════
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SportsBot/1.0)"}

@dataclass
class TweetMedia:
    type: str   # "photo" | "video"
    url: str

@dataclass
class Tweet:
    id: str
    text: str
    author_username: str
    created_at: str
    media: List[TweetMedia] = field(default_factory=list)
    url: str = ""

def _tid(link: str) -> str:
    m = re.search(r'/status/(\d+)', link)
    return m.group(1) if m else link.split("/")[-1].split("#")[0]

def _extract_text(soup) -> str:
    """
    Extrae el texto del RSS de Nitter preservando saltos de línea.
    BeautifulSoup con separator='\n' respeta los <br> y bloques.
    """
    # Reemplazar <br> por marcador antes de get_text
    for br in soup.find_all("br"):
        br.replace_with("\n")
    text = soup.get_text(separator="\n")
    # Limpiar líneas: quitar espacios al inicio/fin de cada línea
    lines = [line.strip() for line in text.splitlines()]
    # Colapsar más de 2 líneas vacías seguidas en 1
    result = []
    blanks = 0
    for line in lines:
        if line == "":
            blanks += 1
            if blanks <= 1:
                result.append("")
        else:
            blanks = 0
            result.append(line)
    return "\n".join(result).strip()

def _normalize_img(url: str) -> Optional[Tuple[str, str]]:
    """Devuelve (tipo, url_real) o None si no es media válida."""
    if not url or not url.startswith("http"):
        return None
    if "twimg.com" in url:
        t = "video" if "video.twimg.com" in url else "photo"
        return t, url
    # URL de nitter: https://nitter.net/pic/enc_url
    for prefix in ["/pic/", "/video/"]:
        if prefix in url:
            path = unquote(url.split(prefix, 1)[1])
            if any(x in path for x in ["ext_tw_video", "amplify_video"]):
                return "video", f"https://video.twimg.com/{path}"
            if "tweet_video" in path and "thumb" not in path:
                return "video", f"https://video.twimg.com/{path}"
            return "photo", f"https://pbs.twimg.com/{path}"
    return None

def _parse_entry(entry, username: str) -> Optional[Tweet]:
    link = entry.get("link", "")
    soup = BeautifulSoup(entry.get("description", ""), "html.parser")
    text = _extract_text(soup)

    if not text or "rss reader" in text.lower() or text.startswith("RT @"):
        return None

    # Quitar URLs de t.co
    text = re.sub(r'https?://t\.co/\S+', '', text).strip()
    # @menciones → solo el nombre
    text = re.sub(r'@(\w+)', r'\1', text)

    media = []
    seen_urls = set()
    for img in soup.find_all("img"):
        r = _normalize_img(img.get("src", ""))
        if r and r[1] not in seen_urls:
            seen_urls.add(r[1])
            media.append(TweetMedia(type=r[0], url=r[1]))
    for video in soup.find_all("video"):
        src = video.get("src", "")
        if not src:
            s = video.find("source")
            src = s.get("src", "") if s else ""
        r = _normalize_img(src)
        if r and r[1] not in seen_urls:
            seen_urls.add(r[1])
            media.append(TweetMedia(type="video", url=r[1]))

    return Tweet(
        id=_tid(link), text=text, author_username=username,
        created_at=entry.get("published", ""), media=media, url=link,
    )

async def fetch_tweets(username: str, since_id: Optional[str] = None, max_results: int = 10) -> List[Tweet]:
    instances = NITTER_INSTANCES.copy()
    random.shuffle(instances)
    for base in instances:
        try:
            resp = await asyncio.get_event_loop().run_in_executor(
                None, lambda u=f"{base}/{username}/rss": requests.get(u, headers=HEADERS, timeout=10)
            )
            if resp.status_code != 200: continue
            feed = feedparser.parse(resp.content)
            if not feed.entries: continue

            tweets = []
            for entry in feed.entries[:max_results]:
                t = _parse_entry(entry, username)
                if not t: continue
                if since_id:
                    try:
                        if int(t.id) <= int(since_id): continue
                    except ValueError:
                        if t.id <= since_id: continue
                tweets.append(t)

            if tweets:
                print(f"[Nitter] ✅ @{username} via {base} ({len(tweets)} nuevos)")
                return tweets
            if since_id:
                print(f"[Nitter] ℹ️ @{username} sin tweets nuevos")
                return []
        except Exception as e:
            print(f"[Nitter] ❌ {base} → {e}")
    print(f"[Nitter] ⚠️ Sin respuesta para @{username}")
    return []

# ══════════════════════════════════════════════════════════════════
#  TRADUCCIÓN — Groq llama-3.3-70b
# ══════════════════════════════════════════════════════════════════
_SYSTEM = """Eres un traductor experto en deportes. Traduce del inglés al español latino.

REGLAS ESTRICTAS:
1. NO traduzcas nombres de jugadores ni equipos.
2. NO traduzcas abreviaciones deportivas (pts, reb, ast, ERA, RBI, AVG, HR, SB, SP, DH, 1B, 2B, 3B, SS, CF, RF, LF, C, K, BB, IP, OPS…).
3. NO traduzcas hashtags ni menciones.
4. PRESERVA EXACTAMENTE el formato: saltos de línea, listas, espacios. No juntes líneas.
5. Si el texto ya está en español o es una lista de nombres/posiciones, devuélvelo SIN cambios.
6. Responde SOLO con la traducción. Cero explicaciones, cero comillas."""

async def translate(text: str, sport: str) -> str:
    if not text.strip(): return text
    if not groq_client:
        return text
    ctx = "béisbol (MLB)" if sport == "mlb" else "baloncesto (NBA)"
    try:
        def _call():
            return groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user",   "content": f"Contexto: {ctx}\n\nTexto a traducir:\n{text}"},
                ],
                temperature=0.1, max_tokens=600,
            )
        resp = await asyncio.get_event_loop().run_in_executor(None, _call)
        result = resp.choices[0].message.content.strip().strip("\"'")
        print(f"[Groq] ✅ Traducido")
        return result
    except Exception as e:
        print(f"[Groq] Error: {e}"); return text

# ══════════════════════════════════════════════════════════════════
#  MEDIA — descarga y encodeo
# ══════════════════════════════════════════════════════════════════
def _del(path: str):
    try:
        if path and os.path.exists(path): os.remove(path)
    except Exception: pass

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
        print(f"[Media] Error imagen: {e}")
    return None

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

async def download_video(tweet_url: str, tweet_id: str) -> Optional[Tuple[str, int, int]]:
    """
    Convierte URL de nitter a x.com y descarga con yt-dlp.
    Luego re-encodea con ffmpeg para garantizar compatibilidad con Telegram.
    """
    # Convertir nitter → x.com
    xcom = re.sub(r'https://(nitter\.net|xcancel\.com|nitter\.cz|twitter\.com)/', 'https://x.com/', tweet_url)
    raw = str(TEMP_DIR / f"v_{tweet_id}_raw.mp4")
    enc = str(TEMP_DIR / f"v_{tweet_id}_enc.mp4")

    print(f"[Video] Descargando {xcom}")
    try:
        await _run([
            "yt-dlp", "-f", "best[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "-o", raw, "--no-playlist", "--quiet", xcom
        ], timeout=120)
    except Exception as e:
        print(f"[Video] yt-dlp error: {e}"); return None

    if not os.path.exists(raw) or os.path.getsize(raw) < 1000:
        print(f"[Video] Archivo vacío o no descargado"); _del(raw); return None

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
        print(f"[Video] ffmpeg error: {e}"); _del(raw); return None

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
        await bot.send_message(chat_id=cid, text=text,
                               parse_mode=ParseMode.MARKDOWN,
                               disable_web_page_preview=True)
        return True
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after); return await send_text(cid, text)
    except TelegramError as e:
        print(f"[TG] Error texto: {e}"); return False

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
        print(f"[TG] Error foto: {e}"); return False
    finally:
        _del(local or "")

async def send_video(cid: str, tweet_url: str, caption: str, tweet_id: str) -> bool:
    result = await download_video(tweet_url, tweet_id)
    if not result:
        print(f"[TG] Sin video — enviando texto"); return await send_text(cid, caption)
    path, w, h = result
    try:
        with open(path, "rb") as f:
            await bot.send_video(chat_id=cid, video=f, caption=caption,
                                 parse_mode=ParseMode.MARKDOWN,
                                 width=w, height=h, supports_streaming=True)
        return True
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after); return await send_video(cid, tweet_url, caption, tweet_id)
    except TelegramError as e:
        print(f"[TG] Error video: {e}"); return False
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
        print(f"[TG] Error álbum: {e}")
    finally:
        for p in locals_: _del(p)
    return False

# ══════════════════════════════════════════════════════════════════
#  FORMATO
# ══════════════════════════════════════════════════════════════════
def _truncate(text: str, max_len: int = 1024) -> str:
    return text if len(text) <= max_len else text[:max_len - 3] + "..."

async def build_caption(text: str, sport: str, translate_it: bool) -> str:
    body = text  # el texto ya viene limpio de _parse_entry
    if translate_it:
        body = await translate(body, sport)
    sub = MLB_SUBSCRIBE if sport == "mlb" else NBA_SUBSCRIBE
    return _truncate(f"{body}\n\n*{sub}*")

def should_post(text: str, photos_only: bool, media_types: list) -> bool:
    if not photos_only: return True
    if "photo" not in media_types: return False
    tl = text.lower()
    kw = ["final", "score", "recap", "result", "tonight", "last night",
          "game", "wins", "beats", "defeats", "victory", "walk-off", "highlights",
          "line", "lineup", "starting"]
    return any(k in tl for k in kw) or bool(re.search(r'\b\d{1,3}[-–]\d{1,3}\b', text))

# ══════════════════════════════════════════════════════════════════
#  CICLO PRINCIPAL
# ══════════════════════════════════════════════════════════════════
async def process_tweet(tweet: Tweet, channel_id: str, sport: str, config: dict) -> bool:
    media_types = [m.type for m in tweet.media]
    if not should_post(tweet.text, config["photos_only"], media_types):
        return False

    caption = await build_caption(tweet.text, sport, config["translate"])
    photos  = [m for m in tweet.media if m.type == "photo"]
    videos  = [m for m in tweet.media if m.type == "video"]

    if not tweet.media:
        return await send_text(channel_id, caption)
    elif videos:
        ok = await send_video(channel_id, tweet.url, caption, tweet.id)
        if photos: await send_album(channel_id, [p.url for p in photos], "")
        return ok
    elif len(photos) == 1:
        return await send_photo(channel_id, photos[0].url, caption)
    else:
        return await send_album(channel_id, [p.url for p in photos], caption)

async def run_cycle():
    print("[Bot] 🔄 Iniciando ciclo...")
    tasks: list = []

    for username, config in NBA_ACCOUNTS.items():
        tweets = await fetch_tweets(username, since_id=get_last_id(username))
        for t in reversed(tweets):
            if not is_processed(t.id):
                tasks.append((t, NBA_CHANNEL_ID, "nba", config, username))

    for username, config in MLB_ACCOUNTS.items():
        tweets = await fetch_tweets(username, since_id=get_last_id(username))
        for t in reversed(tweets):
            if not is_processed(t.id):
                tasks.append((t, MLB_CHANNEL_ID, "mlb", config, username))

    if not tasks:
        print("[Bot] Sin tweets nuevos."); return

    print(f"[Bot] Procesando {len(tasks)} tweets...")
    for tweet, channel, sport, config, username in tasks:
        try:
            ok = await process_tweet(tweet, channel, sport, config)
            mark_processed(tweet.id)
            set_last_id(username, tweet.id)
            print(f"[Bot] {'✅' if ok else '⏭'} [{sport.upper()}] @{username}/{tweet.id}")
            await asyncio.sleep(3)
        except Exception as e:
            print(f"[Bot] ❌ {tweet.id}: {e}")

    cleanup_old()
    print("[Bot] ✅ Ciclo completado.")

# ══════════════════════════════════════════════════════════════════
#  HTTP SERVER (Render healthcheck)
# ══════════════════════════════════════════════════════════════════
_state = {"started_at": None, "cycles": 0, "last_cycle": None, "status": "starting"}

async def handle_health(req):
    return web.Response(text="OK", content_type="text/plain")

async def handle_status(req):
    return web.Response(
        text=json.dumps({**_state, "interval_min": CHECK_INTERVAL_MINUTES,
                         "nba": NBA_CHANNEL_ID, "mlb": MLB_CHANNEL_ID}, indent=2),
        content_type="application/json")

async def bot_cycle():
    try:
        _state["status"] = "running"
        await run_cycle()
        _state["cycles"] += 1
        _state["last_cycle"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        _state["status"] = "idle"
    except Exception as e:
        _state["status"] = "error"; print(f"[Bot] ❌ {e}")

# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════
async def main():
    _load_state()
    _state["started_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print("╔══════════════════════════════════════════╗")
    print("║   🏀⚾ SPORTS TELEGRAM BOT  ⚾🏀        ║")
    print("╚══════════════════════════════════════════╝")
    print(f"[NBA]  {NBA_CHANNEL_ID}")
    print(f"[MLB]  {MLB_CHANNEL_ID}")
    print(f"[Groq] {'✅ activo' if groq_client else '⚠️ sin key'}")

    app = web.Application()
    app.router.add_get("/", handle_status)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/status", handle_status)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print(f"[Web]  ✅ 0.0.0.0:{PORT}")

    await bot_cycle()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(bot_cycle, IntervalTrigger(minutes=CHECK_INTERVAL_MINUTES),
                      id="cycle", replace_existing=True, max_instances=1)
    scheduler.start()
    _state["status"] = "idle"
    print(f"[Bot]  ✅ Ciclo cada {CHECK_INTERVAL_MINUTES} min")

    loop = asyncio.get_running_loop()
    def _stop(sig): scheduler.shutdown(wait=False); loop.stop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop, sig.name)

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
