"""
image_generator.py — Genera imágenes para:
 - Resultado final  (estilo oscuro: logos grandes, marcador central en caja)
 - Alineaciones
"""
import os
import io
import logging
import aiohttp
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
from pathlib import Path

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).parent / "assets"
FONT_PATH  = os.environ.get("FONT_PATH", str(ASSETS_DIR / "font.ttf"))

# ── Variaciones de blanco/crema por liga ──────────────────────
BG_COLORS = {
    "mlb":    (248, 248, 252),   # blanco azulado suave
    "lvbp":   (255, 252, 245),   # crema cálido
    "caribe": (245, 252, 248),   # blanco verdoso suave
    "wbc":    (248, 245, 255),   # blanco lavanda
}
BG_CARD_COLORS = {
    "mlb":    (235, 238, 248),
    "lvbp":   (245, 238, 228),
    "caribe": (228, 245, 235),
    "wbc":    (235, 230, 248),
}
BG_SCORE_COLORS = {
    "mlb":    (220, 225, 242),
    "lvbp":   (238, 224, 208),
    "caribe": (212, 238, 220),
    "wbc":    (220, 212, 242),
}
BORDER_COLORS = {
    "mlb":    (190, 200, 225),
    "lvbp":   (210, 185, 165),
    "caribe": (175, 215, 190),
    "wbc":    (190, 178, 225),
}

# Color de acento por liga (para textos del nombre de liga y detalles)
LEAGUE_COLORS = {
    "mlb":    (0,   45,  114),   # azul MLB
    "lvbp":   (180, 10,  40),    # rojo venezolano
    "caribe": (0,   100, 65),    # verde caribe
    "wbc":    (0,   70,  160),   # azul WBC
}

LEAGUE_LABELS = {
    "mlb":    "MLB",
    "lvbp":   "LVBP VENEZUELA",
    "caribe": "SERIE DEL CARIBE",
    "wbc":    "WORLD BASEBALL CLASSIC",
}

WATERMARK_URL = (
    "https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEje4BJeo5_8IDzAs4ckNqqQHpGiZAw1Y2Nm2-VERYO-"
    "n0KHg2w2WMVgJQyw9lc9zHmBhvZMZLEdsgh7aj1uw35958gkXs4gZH5GnO_ZJHyddbMaDyrghz8WcQeR_l9fEU9EOfhNcUTOhy"
    "EeqCWcDjIqRRj2gTkYiAeeXGR12dXpkWAvPpuet5iPs0eO2uc/s1024/universobaseball.jpg"
)

FLAG_URLS = {
    "DOM": "https://flagcdn.com/w160/do.png",
    "VEN": "https://flagcdn.com/w160/ve.png",
    "PUR": "https://flagcdn.com/w160/pr.png",
    "CUB": "https://flagcdn.com/w160/cu.png",
    "MEX": "https://flagcdn.com/w160/mx.png",
    "PAN": "https://flagcdn.com/w160/pa.png",
    "USA": "https://flagcdn.com/w160/us.png",
    "JPN": "https://flagcdn.com/w160/jp.png",
    "KOR": "https://flagcdn.com/w160/kr.png",
    "AUS": "https://flagcdn.com/w160/au.png",
    "ITA": "https://flagcdn.com/w160/it.png",
    "NED": "https://flagcdn.com/w160/nl.png",
}

_img_cache: dict = {}


# ─────────────────────────────────────────────────────────────
#  UTILIDADES BASE
# ─────────────────────────────────────────────────────────────
def _get_font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except Exception:
        try:
            return ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size
            )
        except Exception:
            return ImageFont.load_default()


async def _download_image(url: str, size: tuple = None) -> "Image.Image | None":
    if url in _img_cache:
        img = _img_cache[url].copy()
        if size:
            img = img.resize(size, Image.LANCZOS)
        return img
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    img = Image.open(io.BytesIO(data)).convert("RGBA")
                    _img_cache[url] = img.copy()
                    if size:
                        img = img.resize(size, Image.LANCZOS)
                    return img
    except Exception as e:
        logger.warning(f"No se pudo descargar imagen {url}: {e}")
    return None


async def _get_team_logo(team_id: int, size: tuple) -> "Image.Image | None":
    if not team_id:
        return None
    url = f"https://www.mlbstatic.com/team-logos/{team_id}.svg"
    img = await _download_image(url, size)
    if img is None:
        img = await _download_image(
            f"https://www.mlbstatic.com/team-logos/team-cap-on-light/{team_id}.svg", size
        )
    return img


async def _get_flag(country_code: str, size: tuple) -> "Image.Image | None":
    url = FLAG_URLS.get(country_code.upper(), "")
    if not url:
        return None
    return await _download_image(url, size)


async def _get_logo(game_data: dict, side: str, size: tuple) -> "Image.Image | None":
    """Obtiene logo de equipo o bandera según si es torneo internacional."""
    country = game_data.get(f"{side}_country")
    team_id = game_data.get(f"{side}_id")
    if country:
        return await _get_flag(country, size)
    if team_id:
        return await _get_team_logo(team_id, size)
    return None


async def _apply_watermark(img: Image.Image) -> Image.Image:
    """Watermark centrado, pequeño y sutil."""
    wm = await _download_image(WATERMARK_URL)
    if wm is None:
        return img
    W, H = img.size
    max_w = int(W * 0.18)
    wm_w, wm_h = wm.size
    scale = max_w / wm_w
    new_w, new_h = int(wm_w * scale), int(wm_h * scale)
    wm = wm.resize((new_w, new_h), Image.LANCZOS).convert("RGBA")
    r, g, b, a = wm.split()
    a = a.point(lambda x: int(x * 45 / 255))
    wm.putalpha(a)
    x = (W - new_w) // 2
    y = (H - new_h) // 2
    base = img.convert("RGBA")
    base.paste(wm, (x, y), wm)
    return base


def _paste_logo(img: Image.Image, logo: Image.Image, cx: int, cy: int,
               circle_color: tuple = (230, 233, 245)):
    """Pega logo centrado en (cx, cy) con círculo de fondo suave."""
    if logo is None:
        return
    circle_r = max(logo.width, logo.height) // 2 + 14
    overlay  = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw     = ImageDraw.Draw(overlay)
    draw.ellipse(
        [cx - circle_r, cy - circle_r, cx + circle_r, cy + circle_r],
        fill=(*circle_color, 210)
    )
    img.paste(overlay, (0, 0), overlay)
    lx = cx - logo.width  // 2
    ly = cy - logo.height // 2
    try:
        img.paste(logo, (lx, ly), logo if logo.mode == "RGBA" else None)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
#  IMAGEN: RESULTADO FINAL
#  Fondo blanco/crema — logos grandes, marcador en caja central
# ─────────────────────────────────────────────────────────────
async def generate_final_result_image(game_data: dict, league: str) -> bytes:
    W, H = 900, 520

    bg        = BG_COLORS.get(league,       (248, 248, 252))
    card_col  = BG_CARD_COLORS.get(league,  (235, 238, 248))
    score_col = BG_SCORE_COLORS.get(league, (220, 225, 242))
    border    = BORDER_COLORS.get(league,   (190, 200, 225))
    accent    = LEAGUE_COLORS.get(league,   (0,  45, 114))
    label     = LEAGUE_LABELS.get(league,   league.upper())

    img  = Image.new("RGBA", (W, H), bg)
    draw = ImageDraw.Draw(img)

    font_league  = _get_font(22)
    font_name    = _get_font(26)
    font_score   = _get_font(88)
    font_final   = _get_font(18)
    font_dash    = _get_font(60)
    font_pitcher = _get_font(17)
    font_stat    = _get_font(16)

    # ── Borde exterior ──
    draw.rounded_rectangle([2, 2, W - 3, H - 3], radius=18,
                           outline=border, width=2)

    # ── Liga centrada arriba ──
    draw.line([(W // 2 - 140, 30), (W // 2 + 140, 30)], fill=border, width=1)
    draw.text((W // 2, 52), label, font=font_league, fill=accent, anchor="mm")
    draw.line([(W // 2 - 140, 72), (W // 2 + 140, 72)], fill=border, width=1)

    # ── Logos ──
    logo_size    = (160, 160)
    logo_cy      = 230
    away_logo_cx = 185
    home_logo_cx = W - 185

    away_logo = await _get_logo(game_data, "away", logo_size)
    home_logo = await _get_logo(game_data, "home", logo_size)
    _paste_logo(img, away_logo, away_logo_cx, logo_cy, circle_color=card_col)
    _paste_logo(img, home_logo, home_logo_cx, logo_cy, circle_color=card_col)

    # ── Nombres bajo los logos ──
    away_name = game_data.get("away_name", "Visitante").upper()
    home_name = game_data.get("home_name", "Local").upper()
    draw.text((away_logo_cx, logo_cy + 100), away_name[:14],
              font=font_name, fill=(30, 30, 40), anchor="mm")
    draw.text((home_logo_cx, logo_cy + 100), home_name[:14],
              font=font_name, fill=(30, 30, 40), anchor="mm")

    # ── Caja del marcador ──
    sbw, sbh = 280, 140
    sbx = (W - sbw) // 2
    sby = logo_cy - sbh // 2 - 10
    draw.rounded_rectangle([sbx, sby, sbx + sbw, sby + sbh],
                           radius=16, fill=score_col, outline=border, width=2)

    away_score = game_data.get("away_score", 0)
    home_score = game_data.get("home_score", 0)
    cx       = W // 2
    score_cy = sby + sbh // 2 - 12

    # Ganador en color de acento, perdedor en gris
    away_col = accent if away_score >= home_score else (160, 160, 170)
    home_col = accent if home_score >= away_score else (160, 160, 170)

    draw.text((cx - 75, score_cy), str(away_score),
              font=font_score, fill=away_col, anchor="mm")
    draw.text((cx,       score_cy), "—",
              font=font_dash,  fill=(180, 185, 200), anchor="mm")
    draw.text((cx + 75,  score_cy), str(home_score),
              font=font_score, fill=home_col, anchor="mm")

    draw.text((cx, sby + sbh - 18), "FINAL",
              font=font_final, fill=accent, anchor="mm")

    # ── Separador ──
    sep_y = logo_cy + 120
    draw.line([(60, sep_y), (W - 60, sep_y)], fill=border, width=1)

    # ── Pitchers ──
    pitch_y = sep_y + 22
    pitcher_parts = []
    if game_data.get("winner_pitcher"): pitcher_parts.append(f"W: {game_data['winner_pitcher']}")
    if game_data.get("loser_pitcher"):  pitcher_parts.append(f"L: {game_data['loser_pitcher']}")
    if game_data.get("save_pitcher"):   pitcher_parts.append(f"S: {game_data['save_pitcher']}")
    if pitcher_parts:
        draw.text((cx, pitch_y), "   |   ".join(pitcher_parts),
                  font=font_pitcher, fill=(80, 85, 100), anchor="mm")
        pitch_y += 28

    # ── H / R / E ──
    hits_parts = []
    ah, hh = game_data.get("away_hits", "-"), game_data.get("home_hits", "-")
    ae, he = game_data.get("away_errors", "-"), game_data.get("home_errors", "-")
    if ah != "-" or hh != "-": hits_parts.append(f"H: {ah}-{hh}")
    if ae != "-" or he != "-": hits_parts.append(f"E: {ae}-{he}")
    innings = game_data.get("innings", 9)
    if innings and innings != 9: hits_parts.append(f"Innings: {innings}")
    if hits_parts:
        draw.text((cx, pitch_y), "   |   ".join(hits_parts),
                  font=font_stat, fill=(120, 125, 140), anchor="mm")

    img = await _apply_watermark(img)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=93)
    buf.seek(0)
    return buf.read()


# ─────────────────────────────────────────────────────────────
#  IMAGEN: ALINEACIONES  (fondo blanco/crema)
# ─────────────────────────────────────────────────────────────
async def generate_lineup_image(game_data: dict, league: str,
                                 away_lineup: list, home_lineup: list) -> bytes:
    rows = max(len(away_lineup), len(home_lineup), 9)
    W    = 900
    H    = 130 + rows * 38 + 60

    bg       = BG_COLORS.get(league,       (248, 248, 252))
    card_col = BG_CARD_COLORS.get(league,  (235, 238, 248))
    score_col= BG_SCORE_COLORS.get(league, (220, 225, 242))
    border   = BORDER_COLORS.get(league,   (190, 200, 225))
    accent   = LEAGUE_COLORS.get(league,   (0,  45, 114))
    label    = LEAGUE_LABELS.get(league,   league.upper())

    img  = Image.new("RGBA", (W, H), bg)
    draw = ImageDraw.Draw(img)

    font_header = _get_font(22)
    font_sub    = _get_font(17)
    font_player = _get_font(16)
    font_pos    = _get_font(15)

    # Borde
    draw.rounded_rectangle([2, 2, W - 3, H - 3], radius=14,
                           outline=border, width=2)

    # Liga
    draw.line([(W // 2 - 140, 18), (W // 2 + 140, 18)], fill=border, width=1)
    draw.text((W // 2, 38), label, font=font_header, fill=accent, anchor="mm")
    draw.line([(W // 2 - 140, 58), (W // 2 + 140, 58)], fill=border, width=1)

    # Cabeceras de columna
    away_name = game_data.get("away_name", "Visitante").upper()
    home_name = game_data.get("home_name", "Local").upper()
    half = W // 2

    draw.rectangle([20, 68, half - 5, 100], fill=score_col)
    draw.rectangle([half + 5, 68, W - 20, 100], fill=score_col)
    draw.text((half // 2 + 10, 84), away_name[:18],
              font=font_sub, fill=accent, anchor="mm")
    draw.text((half + (W - half) // 2 - 10, 84), home_name[:18],
              font=font_sub, fill=accent, anchor="mm")

    # Jugadores
    y = 108
    for i in range(rows):
        row_fill = (255, 255, 255) if i % 2 == 0 else card_col
        draw.rectangle([20, y, W - 20, y + 34], fill=row_fill)

        if i < len(away_lineup):
            p   = away_lineup[i]
            pos = p.get("primaryPosition", {}).get("abbreviation", "")
            nm  = p.get("fullName", "")
            draw.text((32,  y + 17), f"{i+1}.", font=font_pos,    fill=accent,      anchor="lm")
            draw.text((58,  y + 17), pos,        font=font_pos,    fill=accent,      anchor="lm")
            draw.text((90,  y + 17), nm[:22],    font=font_player, fill=(30, 30, 40), anchor="lm")

        draw.line([(half, y + 2), (half, y + 32)], fill=border, width=1)

        if i < len(home_lineup):
            p   = home_lineup[i]
            pos = p.get("primaryPosition", {}).get("abbreviation", "")
            nm  = p.get("fullName", "")
            hx  = half + 10
            draw.text((hx,      y + 17), f"{i+1}.", font=font_pos,    fill=accent,      anchor="lm")
            draw.text((hx + 26, y + 17), pos,        font=font_pos,    fill=accent,      anchor="lm")
            draw.text((hx + 58, y + 17), nm[:22],    font=font_player, fill=(30, 30, 40), anchor="lm")

        y += 36

    img = await _apply_watermark(img)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=93)
    buf.seek(0)
    return buf.read()
