# 🏀⚾ Sports Telegram Bot — 100% Gratuito

Publica contenido de Twitter/X automáticamente en tus canales de Telegram de NBA y MLB.
Lee Twitter via **Nitter RSS** — exactamente igual que el proyecto [ufnews](https://github.com/byopi/ufnews).

**Costo total: $0** — sin tarjeta, sin APIs de pago.

---

## 💸 Stack gratuito

| Componente | Servicio | Coste |
|-----------|---------|-------|
| Hosting | Render.com (Free) | $0 — 750h/mes |
| Twitter | Nitter RSS (nitter.net / xcancel.com / nitter.cz) | $0 — sin cuenta |
| Traducciones | Google Gemini Flash | $0 — 1.500 req/día |
| Telegram | Bot API | $0 — siempre gratis |

---

## ⚙️ Variables de entorno — solo 4

### `TELEGRAM_BOT_TOKEN`
1. Abre Telegram → busca **@BotFather** → `/newbot`
2. Copia el token: `7123456789:AAFabc...`
3. **⚠️ Añade el bot como administrador** en ambos canales (permiso de publicar)

---

### `NBA_CHANNEL_ID` y `MLB_CHANNEL_ID`
Los usernames de tus canales:
- `@NBA_Latinoamerica`
- `@UniversoBaseball`

---

### `GEMINI_API_KEY`
1. Ve a [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)
2. Inicia sesión con Google → **Create API Key**
3. Copia la clave: `AIzaSy...`

> Sin tarjeta. Límite gratis: 1.500 traducciones/día. Más que suficiente.

> Si no pones esta key, el bot publica igual pero sin traducir (en inglés).

---

## 🚀 GitHub → Render en 3 pasos

### 1. Subir a GitHub
```bash
git init
git add .
git commit -m "feat: sports telegram bot"
git remote add origin https://github.com/TU_USUARIO/TU_REPO.git
git push -u origin main
```

### 2. Crear el servicio en Render
1. [render.com](https://render.com) → **New +** → **Web Service**
2. Conecta tu GitHub → selecciona el repo
3. Render detecta `render.yaml` solo (plan Free, Docker)
4. **Health Check Path:** `/health`

### 3. Añadir las 4 variables en Render
Panel → tu servicio → **Environment**:

| Variable | Valor |
|----------|-------|
| `TELEGRAM_BOT_TOKEN` | Token de @BotFather |
| `NBA_CHANNEL_ID` | `@NBA_Latinoamerica` |
| `MLB_CHANNEL_ID` | `@UniversoBaseball` |
| `GEMINI_API_KEY` | API Key de Google AI Studio |

**Save Changes** → Render despliega automáticamente.

Para futuros cambios: `git push` y Render redespliega solo.

**Estado del bot:** `https://TU-APP.onrender.com/status`

---

## 📋 ¿Qué publica?

| Canal | Cuenta | Comportamiento |
|-------|--------|----------------|
| `@NBA_Latinoamerica` | `@UnderdogNBA` | Todo — traducido al español |
| `@NBA_Latinoamerica` | `@NBALatam` | Solo fotos de final de partido |
| `@UniversoBaseball` | `@UnderdogMLB` | Todo — traducido al español |
| `@UniversoBaseball` | `@MLB` | Solo fotos de final de partido |

Cada post termina con:
- NBA → `📲 Suscríbete en t.me/NBA_Latinoamerica` **(negrita)**
- MLB → `📲 Suscríbete en t.me/UniversoBaseball` **(negrita)**

---

## 💻 Prueba local
```bash
pip install -r requirements.txt
cp .env.example .env    # rellena las 4 variables
python main.py          # arranca en http://localhost:8080
```

---

## ⚠️ Nota sobre Render Free
750h/mes = exactamente 31 días × 24h. Alcanza justo para todo el mes con un solo servicio.
Si el bot se duerme por inactividad, añade un monitor gratuito en [uptimerobot.com](https://uptimerobot.com) apuntando a `/health`.
