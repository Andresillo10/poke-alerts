# `poke-alerts`

Monitor automatizado en GitHub Actions que revisa cada hora fuentes curadas de **Pokémon GO** y **Pokémon TCG Pocket**, traduce novedades al español protegiendo códigos alfanuméricos con glosario oficial, genera una tarjeta PNG ligera (`1080x600`) con Pillow y la envía a **Telegram** con el código copiable en 1 toque y botón de canje directo.

---

## Arquitectura del proyecto

- **Fuentes activas verificadas (`src/scrapers.py`):**
  - **Pokémon GO:** `LeekDuck Promo` (códigos vigentes + enlace de canje prellenado), `ScrapedDuck JSON` (eventos curados sin HTML frágil), `Pokémon GO Live es-MX` (noticias oficiales ya en español) y `Serebii GO` (respaldo sin Cloudflare).
  - **Pokémon TCG Pocket:** `Serebii TCGP` (tabla oficial de eventos sin Cloudflare) y `PTCGPocket.gg` (eventos recientes, con tolerancia a bloqueos Cloudflare). Canje de códigos fijado a `https://gift.pokemontcgpocket.com/`.
- **Deduplicación y anti-spam (`src/state.py`):**
  - Deduplica por `uid` y por tema normalizado entre portales (`build_topic_key`) para no enviar la misma noticia/evento desde múltiples sitios.
  - En la primera ejecución (`initialized: false`), notifica los códigos promocionales vigentes + máximo 1 evento reciente por juego y siembra el histórico antiguo silenciosamente.
  - Mantiene un *heartbeat* en `state.json` cada 14 días si no hay novedades para evitar que GitHub pause el `cron` tras 60 días sin commits.
- **Traducción protegida (`src/translator.py`, `src/glossary.py`):**
  - Protege códigos exactos, URLs y términos oficiales (p. ej. *Wonder Pick* → *Elección Mágica*, *Drop Event* → *Evento de Botín*) antes de traducir al español.
- **Tarjeta PNG determinista (`src/card_renderer.py`):**
  - Genera tarjetas PNG de `1080x600` con fuentes multiplataforma (Windows + Ubuntu Linux).

---

## Prueba local rápida (sin Telegram)

```bash
cd "G:\Mi unidad\1.TGC Poket\poke-alerts"
python -m unittest discover -s tests -v
python -m src.main --dry-run
```

Las tarjetas generadas aparecerán en `out/` para inspección visual y no se modificará `state.json`.

---

## Puesta en marcha (Telegram + GitHub personal)

### Paso 1 — Crear el Bot y el Canal informativo en Telegram
> **Recomendación clave:** Usa un **Canal** de Telegram (no un Grupo abierto) si quieres que sea solo informativo para que las personas lean, copien el código en 1 toque y no envíen mensajes que tapen las alertas.

1. En Telegram busca a **`@BotFather`**, envía `/newbot`, elige un nombre y un usuario terminado en `bot` (ej. `poke_alerts_cr_bot`). Guarda el **Token** (`TELEGRAM_BOT_TOKEN`).
2. Crea un **Canal** nuevo en Telegram (ej. *"Alertas Pokémon GO & TCG Pocket"*).
3. Entra a *Administradores del canal* → **Añadir administrador** → busca tu bot y dale permiso de **Publicar mensajes**.
4. Obtén el **`TELEGRAM_CHAT_ID`**:
   - Si el canal es **público**, su ID es `@tu_canal`.
   - Si el canal es **privado**, publica un mensaje de prueba, reenvíalo a `@userinfobot` o consulta `https://api.telegram.org/bot<TU_TOKEN>/getUpdates` tras escribir en el canal para copiar el ID numérico (`-100...`).

### Paso 2 — Crear el repositorio personal en GitHub y subir el código
1. Inicia sesión en GitHub con tu cuenta personal (`cristhian.camelo@gmail.com`) y crea un repositorio vacío llamado `poke-alerts` (**recomendado: Privado**, siempre que mantengas < 2,000 min/mes; 720 corridas de ~40s consumen ~480 min/mes).
2. En el repositorio en GitHub ve a **Settings → Secrets and variables → Actions → New repository secret** y crea:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. Desde PowerShell en este equipo, sube el código usando el script aislado (que mantiene `.git` **fuera de Google Drive** y no altera la identidad laboral `analistabi-tech`):
   ```powershell
   cd "G:\Mi unidad\1.TGC Poket\poke-alerts"
   .\scripts\sync_and_push_github.ps1 -RepoUrl "https://github.com/TU_USUARIO/poke-alerts.git"
   ```
4. En GitHub ve a **Actions → Poke Alerts Hourly Monitor → Run workflow** para disparar la primera ejecución real.
