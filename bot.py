import os
import re
import json
import asyncio
import random
import logging
import tempfile
import time
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple, Set, Dict

import httpx
import aiohttp
import aiofiles
from groq import Groq
from bs4 import BeautifulSoup
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from telegram import Bot, InputMediaPhoto, InputMediaVideo
from telegram.error import TelegramError, RetryAfter
from telegram.constants import ParseMode
from dotenv import load_dotenv

# ══════════════════════════════════════════════════════════════════
#  CONFIGURACIÓN Y VARIABLES DE ENTORNO
# ══════════════════════════════════════════════════════════════════
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("SportsBot-Full")

TELEGRAM_BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
NBA_CHANNEL_ID         = os.getenv("NBA_CHANNEL_ID", "")
MLB_CHANNEL_ID         = os.getenv("MLB_CHANNEL_ID", "")
BARCA_CHANNEL_ID       = os.getenv("BARCA_CHANNEL_ID", "")
MADRID_CHANNEL_ID      = os.getenv("MADRID_CHANNEL_ID", "")
PREMIER_CHANNEL_ID     = os.getenv("PREMIER_CHANNEL_ID", "")
GROQ_API_KEY           = os.getenv("GROQ_API_KEY", "")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))
MAX_VIDEO_SIZE_BYTES   = int(os.getenv("MAX_VIDEO_SIZE_MB", "50")) * 1024 * 1024
PORT                   = int(os.getenv("PORT", "8080"))

# Etiquetas de suscripción personalizadas
NBA_SUBSCRIBE     = "📲 Suscríbete en t.me/NBA_Latinoamerica"
MLB_SUBSCRIBE     = "📲 Suscríbete en t.me/UniversoBaseball"
BARCA_SUBSCRIBE   = "📲 Suscríbete en t.me/iFCBNewsES"
MADRID_SUBSCRIBE  = "📲 Suscríbete en t.me/iRMNewsES"
PREMIER_SUBSCRIBE = "📲 Suscríbete en t.me/PremierLeague_ES"

# Cuentas a monitorear
NBA_ACCOUNTS = {"UnderdogNBA": {"translate": True}, "NBALatam": {"translate": False, "photos_only": True}}
MLB_ACCOUNTS = {"UnderdogMLB": {"translate": True}, "MLBespanol": {"translate": False, "photos_only": True}}
BARCA_ACCOUNTS = {"BarcaUniversal": {"translate": True}}
MADRID_ACCOUNTS = {"MadridUniversal": {"translate": True}}
PREMIER_ACCOUNTS = {"Mercado_Ingles": {"translate": False}}

# Directorios de persistencia (Importante para Render)
TEMP_DIR = Path(tempfile.gettempdir()) / "sports_bot_cache"
TEMP_DIR.mkdir(exist_ok=True)
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "processed_tweets.json"
LAST_IDS_FILE = DATA_DIR / "last_tweet_ids.json"

# Inicialización de clientes
bot = Bot(token=TELEGRAM_BOT_TOKEN)
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# ══════════════════════════════════════════════════════════════════
#  SISTEMA DE PERSISTENCIA (ESTADO)
# ══════════════════════════════════════════════════════════════════
_processed_ids: Set[str] = set()
_last_ids_map: Dict[str, str] = {}

def load_state():
    """Carga los IDs procesados para evitar duplicados."""
    global _processed_ids, _last_ids_map
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text())
            _processed_ids = set(data.get("ids", []))
        if LAST_IDS_FILE.exists():
            _last_ids_map = json.loads(LAST_IDS_FILE.read_text())
        log.info(f"Estado cargado: {len(_processed_ids)} IDs en historial.")
    except Exception as e:
        log.error(f"Error cargando estado: {e}")

def save_state():
    """Guarda el estado en disco."""
    try:
        STATE_FILE.write_text(json.dumps({"ids": list(_processed_ids)}))
        LAST_IDS_FILE.write_text(json.dumps(_last_ids_map))
    except Exception as e:
        log.error(f"Error guardando estado: {e}")

def is_processed(tweet_id: str) -> bool:
    return str(tweet_id) in _processed_ids

def mark_as_processed(username: str, tweet_id: str):
    _processed_ids.add(str(tweet_id))
    _last_ids_map[username] = str(tweet_id)
    # Mantener el historial bajo control (máximo 2000 IDs)
    if len(_processed_ids) > 2000:
        sorted_ids = sorted(list(_processed_ids))
        _processed_ids = set(sorted_ids[-1500:])
    save_state()

# ══════════════════════════════════════════════════════════════════
#  MODELOS Y SCRAPER
# ══════════════════════════════════════════════════════════════════
@dataclass
class TweetMedia:
    type: str  # "photo" o "video"
    url: str
    thumbnail: str = ""

@dataclass
class Tweet:
    id: str
    text: str
    username: str
    media: List[TweetMedia] = field(default_factory=list)
    url: str = ""

async def fetch_tweets(username: str) -> List[Tweet]:
    """Obtiene tweets usando el endpoint de syndication (más estable)."""
    url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5"
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, headers=headers)
            if r.status_code != 200:
                log.error(f"Error {r.status_code} al acceder a @{username}")
                return []
            
            # Extraer JSON de la respuesta
            soup = BeautifulSoup(r.text, "html.parser")
            script = soup.find("script", id="__NEXT_DATA__")
            if not script: return []
            
            data = json.loads(script.string)
            entries = data.get("props", {}).get("pageProps", {}).get("timeline", {}).get("entries", [])
            
            results = []
            for entry in entries:
                t_data = entry.get("content", {}).get("tweet", {})
                if not t_data or t_data.get("retweeted_status"): continue
                
                tid = t_data.get("id_str")
                # Filtro de seguridad: si es un ID viejo y no lo conocemos, lo ignoramos
                last_known = _last_ids_map.get(username)
                
                # --- LÓGICA ANTI-POSTS VIEJOS ---
                if last_known:
                    if int(tid) <= int(last_known): continue
                else:
                    # Si es la primera vez que vemos esta cuenta, marcamos el último pero no enviamos nada
                    log.info(f"Primera vez rastreando @{username}. Seteando ID base: {tid}")
                    mark_as_processed(username, tid)
                    continue

                full_text = t_data.get("full_text", t_data.get("text", ""))
                full_text = re.sub(r"https://t\.co/\S+", "", full_text).strip()
                
                media_objs = []
                entities = t_data.get("extended_entities", {}).get("media", [])
                for m in entities:
                    if m["type"] == "photo":
                        media_objs.append(TweetMedia("photo", m["media_url_https"] + ":orig"))
                    elif m["type"] in ["video", "animated_gif"]:
                        variants = m.get("video_info", {}).get("variants", [])
                        valid = [v for v in variants if v.get("content_type") == "video/mp4"]
                        if valid:
                            best = max(valid, key=lambda v: v.get("bitrate", 0))
                            media_objs.append(TweetMedia("video", best["url"], m["media_url_https"]))
                
                results.append(Tweet(
                    id=tid,
                    text=full_text,
                    username=username,
                    media=media_objs,
                    url=f"https://twitter.com/{username}/status/{tid}"
                ))
            
            # Retornar máximo 5 para no saturar, ordenados de más antiguo a más nuevo
            return sorted(results, key=lambda x: int(x.id))[:5]
            
    except Exception as e:
        log.error(f"Excepción en fetch_tweets para @{username}: {e}")
        return []

# ══════════════════════════════════════════════════════════════════
#  TRADUCCIÓN Y MULTIMEDIA
# ══════════════════════════════════════════════════════════════════
async def translate_text(text: str) -> str:
    """Traduce el texto usando Groq Llama 3."""
    if not text or not groq_client: return text
    try:
        def _sync_call():
            return groq_client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": "Eres un traductor experto en deportes (NBA, MLB, Fútbol). Traduce el siguiente texto al español de forma natural y emocionante. No agregues comentarios extra, solo la traducción."},
                    {"role": "user", "content": text}
                ],
                temperature=0.3
            )
        completion = await asyncio.get_event_loop().run_in_executor(None, _sync_call)
        return completion.choices[0].message.content.strip()
    except Exception as e:
        log.warning(f"Error traducción: {e}")
        return text

async def process_and_send(tweet: Tweet, channel_id: str, config: dict, sub_text: str):
    """Maneja el envío de cada tweet al canal correspondiente."""
    if is_processed(tweet.id): return
    
    final_text = tweet.text
    if config.get("translate"):
        final_text = await translate_text(tweet.text)
    
    caption = f"{final_text}\n\n{sub_text}"
    
    try:
        # Prioridad 1: Video
        video = next((m for m in tweet.media if m.type == "video"), None)
        if video:
            await bot.send_video(
                chat_id=channel_id,
                video=video.url,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN
            )
        
        # Prioridad 2: Foto
        elif tweet.media:
            await bot.send_photo(
                chat_id=channel_id,
                photo=tweet.media[0].url,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN
            )
        
        # Prioridad 3: Solo texto
        else:
            await bot.send_message(
                chat_id=channel_id,
                text=caption,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
        
        mark_as_processed(tweet.username, tweet.id)
        log.info(f"✅ Enviado: {tweet.username} - {tweet.id}")
        await asyncio.sleep(3) # Anti-spam
        
    except RetryAfter as e:
        log.warning(f"Telegram Rate Limit. Durmiendo {e.retry_after}s")
        await asyncio.sleep(e.retry_after)
    except Exception as e:
        log.error(f"Fallo al enviar tweet {tweet.id}: {e}")

# ══════════════════════════════════════════════════════════════════
#  FLUJO PRINCIPAL Y SERVIDOR
# ══════════════════════════════════════════════════════════════════
async def main_job():
    """Tarea que corre cada X minutos."""
    log.info("--- Iniciando escaneo de redes ---")
    
    job_configs = [
        (NBA_ACCOUNTS, NBA_CHANNEL_ID, NBA_SUBSCRIBE),
        (MLB_ACCOUNTS, MLB_CHANNEL_ID, MLB_SUBSCRIBE),
        (BARCA_ACCOUNTS, BARCA_CHANNEL_ID, BARCA_SUBSCRIBE),
        (MADRID_ACCOUNTS, MADRID_CHANNEL_ID, MADRID_SUBSCRIBE),
        (PREMIER_ACCOUNTS, PREMIER_CHANNEL_ID, PREMIER_SUBSCRIBE),
    ]
    
    for accounts, channel, subscribe_msg in job_configs:
        if not channel: continue
        for username, config in accounts.items():
            new_tweets = await fetch_tweets(username)
            for t in new_tweets:
                await process_and_send(t, channel, config, subscribe_msg)

async def health_check(request):
    return web.Response(text="Bot Online", status=200)

async def start_bot():
    load_state()
    
    # Configurar Servidor Web para Render
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    
    # Configurar Programador
    scheduler = AsyncIOScheduler()
    scheduler.add_job(main_job, IntervalTrigger(minutes=CHECK_INTERVAL_MINUTES))
    scheduler.start()
    
    log.info(f"Bot iniciado. Intervalo: {CHECK_INTERVAL_MINUTES} min. Puerto: {PORT}")
    
    # Ejecución inmediata al arrancar
    await main_job()
    
    # Mantener vivo
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        save_state()

if __name__ == "__main__":
    try:
        asyncio.run(start_bot())
    except Exception as e:
        log.critical(f"Error fatal: {e}")
