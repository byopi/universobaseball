import os, re, json, asyncio, random, signal, tempfile, time, hashlib
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple, Set, Dict

import feedparser
import requests
import httpx
import aiohttp
import aiofiles
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
#  CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════════

TELEGRAM_BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
NBA_CHANNEL_ID         = os.getenv("NBA_CHANNEL_ID", "")
MLB_CHANNEL_ID         = os.getenv("MLB_CHANNEL_ID", "")
GEMINI_API_KEY         = os.getenv("GEMINI_API_KEY", "")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))
MAX_VIDEO_SIZE_BYTES   = int(os.getenv("MAX_VIDEO_SIZE_MB", "50")) * 1024 * 1024
PORT                   = int(os.getenv("PORT", "8080"))

NBA_SUBSCRIBE_MSG = "📲 Suscríbete en t.me/NBA_Latinoamerica"
MLB_SUBSCRIBE_MSG = "📲 Suscríbete en t.me/UniversoBaseball"

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

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-1.5-flash-latest:generateContent"
)

TEMP_DIR = Path(tempfile.gettempdir()) / "sports_bot"
TEMP_DIR.mkdir(exist_ok=True)

DATA_DIR   = Path(os.getenv("DATA_DIR", "data"))
STATE_FILE = DATA_DIR / "processed_tweets.json"
LAST_IDS_FILE = DATA_DIR / "last_tweet_ids.json"

bot = Bot(token=TELEGRAM_BOT_TOKEN)

# ══════════════════════════════════════════════════════════════════
#  ESTADO (tweets procesados — memoria + disco)
# ══════════════════════════════════════════════════════════════════

_processed_ids: Set[str] = set()
_disk_ok = True


def _load_state():
    global _disk_ok
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text())
            _processed_ids.update(data.get("ids", []))
    except Exception as e:
        print(f"[State] Disco no disponible: {e}")
        _disk_ok = False


def _save_state():
    if not _disk_ok:
        return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({"ids": list(_processed_ids)}))
    except Exception:
        pass


def is_processed(tweet_id: str) -> bool:
    return str(tweet_id) in _processed_ids


def mark_processed(tweet_id: str):
    _processed_ids.add(str(tweet_id))
    if len(_processed_ids) > 5000:
        oldest = sorted(_processed_ids)[:-4000]
        for old in oldest:
            _processed_ids.discard(old)
    _save_state()


def load_last_ids() -> Dict[str, str]:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if LAST_IDS_FILE.exists():
            return json.loads(LAST_IDS_FILE.read_text())
    except Exception:
        pass
    return {}


def save_last_id(username: str, tweet_id: str):
    ids = load_last_ids()
    ids[username] = tweet_id
    try:
        LAST_IDS_FILE.write_text(json.dumps(ids))
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════
#  SCRAPER NITTER RSS
# ══════════════════════════════════════════════════════════════════

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SportsBot/1.0)"}


@dataclass
class TweetMedia:
    type: str
    url: str


@dataclass
class Tweet:
    id: str
    text: str
    author_username: str
    created_at: str
    media: List[TweetMedia] = field(default_factory=list)
    url: str = ""


def _tweet_id_from_url(link: str) -> str:
    m = re.search(r'/status/(\d+)', link)
    return m.group(1) if m else link.split("/")[-1].split("#")[0]


def _parse_entry(entry, username: str) -> Optional[Tweet]:
    link = entry.get("link", "")
    tweet_id = _tweet_id_from_url(link)
    soup = BeautifulSoup(entry.get("description", ""), "html.parser")
    text = soup.get_text(separator=" ", strip=True)

    if not text or "rss reader" in text.lower() or text.startswith("RT @"):
        return None

    media_list = []
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src and src.startswith("http"):
            media_list.append(TweetMedia(type="photo", url=src))

    return Tweet(
        id=tweet_id,
        text=text,
        author_username=username,
        created_at=entry.get("published", ""),
        media=media_list,
        url=link,
    )


async def fetch_tweets(username: str, since_id: Optional[str] = None, max_results: int = 10) -> List[Tweet]:
    instances = NITTER_INSTANCES.copy()
    random.shuffle(instances)

    for base in instances:
        rss_url = f"{base}/{username}/rss"
        try:
            resp = await asyncio.get_event_loop().run_in_executor(
                None, lambda u=rss_url: requests.get(u, headers=HEADERS, timeout=10)
            )
            if resp.status_code != 200:
                continue
            feed = feedparser.parse(resp.content)
            if not feed.entries:
                continue

            tweets = []
            for entry in feed.entries[:max_results]:
                t = _parse_entry(entry, username)
                if t and (not since_id or t.id > since_id):
                    tweets.append(t)

            if tweets:
                print(f"[Nitter] ✅ @{username} via {base} ({len(tweets)} tweets)")
                return tweets

        except Exception as e:
            print(f"[Nitter] ❌ {base} → {e}")

    print(f"[Nitter] ⚠️ Sin respuesta para @{username}")
    return []


# ══════════════════════════════════════════════════════════════════
#  TRADUCCIÓN — Gemini Flash (gratis)
# ══════════════════════════════════════════════════════════════════

_TRANSLATE_PROMPT = """Eres un traductor experto en deportes. Traduce del inglés al español latino.

REGLAS — nunca las rompas:
1. NO traduzcas nombres de jugadores (LeBron James, Shohei Ohtani…).
2. NO traduzcas nombres de equipos (Lakers, Yankees, Warriors…).
3. NO traduzcas estadísticas (pts, reb, ast, ERA, RBI, AVG, HR…).
4. NO traduzcas hashtags ni menciones.
5. Usa términos deportivos en español: canasta, jonrón, ponche, carrera…
6. Responde SOLO con la traducción. Sin comillas ni explicaciones."""


async def translate(text: str, sport: str) -> str:
    if not text.strip():
        return text
    es = ["el ", "la ", "los ", "las ", "de ", "en ", "con ", "que "]
    if sum(1 for m in es if m in text.lower()) >= 3:
        return text
    if not GEMINI_API_KEY:
        return text

    ctx = "béisbol (MLB)" if sport == "baseball" else "baloncesto (NBA)"
    prompt = f"{_TRANSLATE_PROMPT}\n\nContexto: {ctx}\n\nTweet:\n{text}"
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.post(
                GEMINI_URL,
                params={"key": GEMINI_API_KEY},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": 512, "temperature": 0.2},
                },
            )
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip().strip("\"'")
    except Exception as e:
        print(f"[Gemini] Error: {e}")
        return text


def is_game_end(text: str) -> bool:
    tl = text.lower()
    keywords = [
        "final score", "final:", "game over", "postgame", "recap",
        "wins ", "loses ", "defeats ", "beat the", "beats the",
        "victory", "walk-off", "walkoff", "sweep", "advances", "eliminates",
    ]
    return any(k in tl for k in keywords) or bool(re.search(r'\b\d{1,3}[-–]\d{1,3}\b', text))


# ══════════════════════════════════════════════════════════════════
#  MEDIA — descarga y re-encodeo de videos
# ══════════════════════════════════════════════════════════════════

async def download_image(url: str, fname: str) -> Optional[str]:
    path = TEMP_DIR / fname
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status == 200:
                    async with aiofiles.open(path, "wb") as f:
                        await f.write(await r.read())
                    return str(path)
    except Exception as e:
        print(f"[Media] Error imagen: {e}")
    return None


async def _run(cmd, timeout=180):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    return await asyncio.wait_for(proc.communicate(), timeout=timeout)


async def video_dimensions(path: str) -> Tuple[int, int]:
    try:
        out, _ = await _run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0", path
        ], timeout=15)
        parts = out.decode().strip().split(",")
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return 1280, 720


async def download_video(url: str, tweet_id: str) -> Optional[Tuple[str, int, int]]:
    raw = str(TEMP_DIR / f"v_{tweet_id}.mp4")
    out = str(TEMP_DIR / f"v_{tweet_id}_enc.mp4")
    try:
        await _run([
            "yt-dlp", "--no-playlist",
            "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "--output", raw, "--quiet", "--no-warnings", url
        ], timeout=120)

        if not os.path.exists(raw):
            return None

        w, h = await video_dimensions(raw)
        w = w if w % 2 == 0 else w - 1
        h = h if h % 2 == 0 else h - 1

        await _run([
            "ffmpeg", "-y", "-i", raw,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-vf", f"scale={w}:{h}",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", "-pix_fmt", "yuv420p", out
        ])

        if os.path.exists(out):
            if os.path.getsize(out) > MAX_VIDEO_SIZE_BYTES:
                comp = str(TEMP_DIR / f"v_{tweet_id}_comp.mp4")
                await _run([
                    "ffmpeg", "-y", "-i", out,
                    "-c:v", "libx264", "-preset", "medium", "-crf", "28",
                    "-vf", "scale=1280:-2",
                    "-c:a", "aac", "-b:a", "96k",
                    "-movflags", "+faststart", "-pix_fmt", "yuv420p", comp
                ])
                _del(raw); _del(out)
                if os.path.exists(comp):
                    w2, h2 = await video_dimensions(comp)
                    return comp, w2, h2
            _del(raw)
            return out, w, h
    except Exception as e:
        print(f"[Media] Error video: {e}")
    _del(raw)
    return None


def _del(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def cleanup_old_files():
    now = time.time()
    for f in TEMP_DIR.glob("*"):
        if now - f.stat().st_mtime > 3600:
            try:
                f.unlink()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════
#  TELEGRAM — envío de mensajes
# ══════════════════════════════════════════════════════════════════

async def send_text(channel_id: str, text: str) -> bool:
    try:
        await bot.send_message(chat_id=channel_id, text=text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        return True
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        return await send_text(channel_id, text)
    except TelegramError as e:
        print(f"[TG] Error texto: {e}")
        return False


async def send_photo(channel_id: str, url: str, caption: str) -> bool:
    local = await download_image(url, f"img_{hashlib.md5(url.encode()).hexdigest()[:8]}.jpg")
    try:
        if local:
            with open(local, "rb") as f:
                await bot.send_photo(chat_id=channel_id, photo=f, caption=caption, parse_mode=ParseMode.MARKDOWN)
        else:
            await bot.send_photo(chat_id=channel_id, photo=url, caption=caption, parse_mode=ParseMode.MARKDOWN)
        return True
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        return await send_photo(channel_id, url, caption)
    except TelegramError as e:
        print(f"[TG] Error foto: {e}")
        return False
    finally:
        _del(local or "")


async def send_video(channel_id: str, url: str, caption: str, tweet_id: str) -> bool:
    result = await download_video(url, tweet_id)
    if not result:
        return False
    path, w, h = result
    try:
        with open(path, "rb") as f:
            await bot.send_video(chat_id=channel_id, video=f, caption=caption,
                                 parse_mode=ParseMode.MARKDOWN, width=w, height=h, supports_streaming=True)
        return True
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        return await send_video(channel_id, url, caption, tweet_id)
    except TelegramError as e:
        print(f"[TG] Error video: {e}")
        return False
    finally:
        _del(path)


async def send_album(channel_id: str, urls: list, caption: str) -> bool:
    locals_, media = [], []
    try:
        for i, url in enumerate(urls[:10]):
            p = await download_image(url, f"img_{hashlib.md5(url.encode()).hexdigest()[:8]}.jpg")
            if p:
                locals_.append(p)
                media.append(InputMediaPhoto(
                    media=open(p, "rb"),
                    caption=caption if i == 0 else None,
                    parse_mode=ParseMode.MARKDOWN if i == 0 else None,
                ))
        if media:
            await bot.send_media_group(chat_id=channel_id, media=media)
            return True
    except TelegramError as e:
        print(f"[TG] Error álbum: {e}")
    finally:
        for p in locals_:
            _del(p)
    return False


# ══════════════════════════════════════════════════════════════════
#  FORMATO DE POSTS
# ══════════════════════════════════════════════════════════════════

def _clean(text: str) -> str:
    text = re.sub(r'https://t\.co/\S+', '', text)
    return re.sub(r' +', ' ', text).strip()


def _truncate(text: str, sub_msg: str, max_len: int = 1024) -> str:
    if len(text) <= max_len:
        return text
    footer = f"\n\n*{sub_msg}*"
    body = text.replace(footer, "")
    return body[:max_len - len(footer) - 3] + "..." + footer


async def build_caption(text: str, sport: str, translate_it: bool, game_end: bool) -> str:
    clean = _clean(text)
    if translate_it:
        clean = await translate(clean, sport)
    if game_end or is_game_end(clean):
        emoji = "⚾" if sport == "baseball" else "🏀"
        clean = f"{emoji} *FINAL DEL ENCUENTRO* {emoji}\n\n{clean}"
    sub = MLB_SUBSCRIBE_MSG if sport == "baseball" else NBA_SUBSCRIBE_MSG
    return _truncate(f"{clean}\n\n*{sub}*", sub)


def should_post(text: str, photos_only: bool, media_types: list) -> bool:
    if not photos_only:
        return True
    if "photo" not in media_types:
        return False
    tl = text.lower()
    keywords = ["final", "score", "recap", "result", "tonight", "last night",
                "game", "wins", "beats", "defeats", "victory", "walk-off", "highlights"]
    return any(k in tl for k in keywords) or bool(re.search(r'\b\d{1,3}[-–]\d{1,3}\b', text))


# ══════════════════════════════════════════════════════════════════
#  CICLO PRINCIPAL
# ══════════════════════════════════════════════════════════════════

async def process_tweet(tweet: Tweet, channel_id: str, sport: str, config: dict) -> bool:
    media_types = [m.type for m in tweet.media]
    if not should_post(tweet.text, config["photos_only"], media_types):
        return False

    caption = await build_caption(tweet.text, sport, config["translate"], is_game_end(tweet.text))
    photos  = [m for m in tweet.media if m.type == "photo"]
    videos  = [m for m in tweet.media if m.type == "video"]

    if not tweet.media:
        return await send_text(channel_id, caption)
    elif videos:
        ok = await send_video(channel_id, videos[0].url, caption, tweet.id)
        if photos:
            await send_album(channel_id, [p.url for p in photos], "")
        return ok
    elif len(photos) == 1:
        return await send_photo(channel_id, photos[0].url, caption)
    else:
        return await send_album(channel_id, [p.url for p in photos], caption)


async def run_cycle():
    print("[Bot] 🔄 Iniciando ciclo...")
    last_ids = load_last_ids()
    tasks = []

    for username, config in {**NBA_ACCOUNTS, **MLB_ACCOUNTS}.items():
        sport = "nba" if username in NBA_ACCOUNTS else "mlb"
        channel = NBA_CHANNEL_ID if sport == "nba" else MLB_CHANNEL_ID
        tweets = await fetch_tweets(username, since_id=last_ids.get(username))
        tweets.reverse()
        for t in tweets:
            if not is_processed(t.id):
                tasks.append((t, channel, sport, config, username))

    if not tasks:
        print("[Bot] Sin tweets nuevos.")
        return

    print(f"[Bot] Procesando {len(tasks)} tweets...")
    for tweet, channel, sport, config, username in tasks:
        try:
            ok = await process_tweet(tweet, channel, sport, config)
            mark_processed(tweet.id)
            save_last_id(username, tweet.id)
            print(f"[Bot] {'✅' if ok else '⏭'} @{username}/{tweet.id}")
            await asyncio.sleep(3)
        except Exception as e:
            print(f"[Bot] ❌ Error en {tweet.id}: {e}")

    cleanup_old_files()
    print("[Bot] ✅ Ciclo completado.")


# ══════════════════════════════════════════════════════════════════
#  SERVIDOR HTTP (healthcheck de Render)
# ══════════════════════════════════════════════════════════════════

_state = {"started_at": None, "cycles": 0, "last_cycle": None, "status": "starting"}


async def handle_health(req):
    return web.Response(text="OK", content_type="text/plain")


async def handle_status(req):
    return web.Response(
        text=json.dumps({**_state, "interval_min": CHECK_INTERVAL_MINUTES,
                         "nba": NBA_CHANNEL_ID, "mlb": MLB_CHANNEL_ID}, indent=2),
        content_type="application/json",
    )


async def bot_cycle():
    try:
        _state["status"] = "running"
        await run_cycle()
        _state["cycles"] += 1
        _state["last_cycle"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        _state["status"] = "idle"
    except Exception as e:
        _state["status"] = "error"
        print(f"[Bot] ❌ {e}")


# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════

async def main():
    _load_state()
    _state["started_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print("╔══════════════════════════════════════════╗")
    print("║   🏀⚾ SPORTS TELEGRAM BOT  ⚾🏀        ║")
    print("║   NBA Latinoamérica + Universo Baseball  ║")
    print("╚══════════════════════════════════════════╝")
    print(f"[Web] Puerto: {PORT}  |  [Bot] Intervalo: {CHECK_INTERVAL_MINUTES} min")

    # Servidor HTTP — Render lo necesita para validar el deploy
    app = web.Application()
    app.router.add_get("/", handle_status)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/status", handle_status)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print(f"[Web] ✅ Escuchando en 0.0.0.0:{PORT}")

    # Primer ciclo inmediato
    await bot_cycle()

    # Scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(bot_cycle, IntervalTrigger(minutes=CHECK_INTERVAL_MINUTES),
                      id="cycle", replace_existing=True, max_instances=1)
    scheduler.start()
    _state["status"] = "idle"
    print(f"[Bot] ✅ Activo — próximo ciclo en {CHECK_INTERVAL_MINUTES} min")

    loop = asyncio.get_running_loop()
    def _stop(sig):
        print(f"\n[Bot] {sig} — cerrando...")
        scheduler.shutdown(wait=False)
        loop.stop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop, sig.name)

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
