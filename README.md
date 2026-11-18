# `poke-alerts`

Monitor autónomo e integral **$0 forever** para **Pokémon GO** y **Pokémon TCG Pocket** en GitHub Actions + `cron-job.org`. Notifica en español directo al grano por **Telegram**, genera tarjetas PNG (`1080×600` y `1080×720`) con mazos QR, counters de Raid, tablas PC 100% IV y ligas GBL, consolida códigos semanales y alimenta automáticamente un **Dashboard web interactivo** desde un JSON histórico unificado.

---

## Arquitectura integral (v6.1)

- **7 fuentes verificadas y aisladas (`src/scrapers.py`, `src/enricher.py`):**
  - **Pokémon GO:** `LeekDuck Promo` (códigos vigentes + canje), `ScrapedDuck JSON` (`events.json` + `raids.json`), `Pokémon GO Live es-MX` y `Serebii GO`.
  - **Pokémon TCG Pocket:** `Serebii TCGP`, `PTCGPocket.gg` (eventos/guías) y `PTCGPocket Codes` (descartando tablas expiradas).
- **Motor Pokémon TCG Pocket (`src/deck_qr.py`, `src/weekly_meta.py`, `src/deck_sheet_renderer.py`):**
  - Detecta restricciones de evento (*Limited Battles*, *No ex*, *1-3♦*, tipo) o eventos abiertos.
  - En eventos abiertos inyecta listas campeonas reales de torneos **Limitless**; en eventos restringidos construye 3 barajas mono-energía sin `Rare Candy` inútil y genera **1 tarjeta `1080×720` con 1 QR por mazo** + botón `📥 Descargar QR`.
- **Motor Pokémon GO (`src/pogo_intel.py`, `src/pogo_sheet_renderer.py`):**
  - Consume `PvPoke GameMaster` + `Rankings` (`Súper 1500`, `Ultra 2500`, `Ultra Mega`, `Master`) + `ScrapedDuck raids.json` + `pogoapi.net`.
  - Calcula al entero **PC 100% IV** (Nv.15/20/25/30/35/40/50), debilidades `2.56× / 1.6×`, **Top 6 Counters Raid** (máx. 1 Mega y sin duplicar especie), veredictos **Hundo & PvP** de cadena evolutiva completa y **filtros GO copiables** con 1 toque (`<code>`).
- **Deduplicación canónica y resúmenes automáticos (`src/models.py`, `src/state.py`, `src/code_digest.py`):**
  - Deduplica entre portales incluso con variantes como `(C1)` o `Missions` sin repetir avisos existentes.
  - **Lunes `01:23 AM` COT:** Top 3 Semanal Ranked TCG Pocket (Limitless) + Top 3 Meta Semanal GO (Súper, Ultra+Master, Raids 5*/Mega).
  - **Lunes `02:23 AM` COT (`>= 02:00`):** Consolidado semanal de códigos activos con vigencia exacta y botones `🎁 Canjear`.
- **JSON Histórico Unificado y Dashboard (`src/catalog_store.py`, `data/events_history.json`, `docs/index.html`):**
  - GitHub Actions actualiza automáticamente `data/events_history.json` y `docs/events_data.js` en cada novedad o cambio de vigencia.
  - Dashboard interactivo con KPIs, filtros por juego/estado/categoría/seguimiento, checklist persistente en `localStorage`, copia en 1 clic y exportación a CSV (Excel) y JSON.

---

## Pruebas locales (sin tocar Telegram ni `state.json`)

```powershell
cd "G:\Mi unidad\1.TGC Poket\poke-alerts"
python -B -m unittest discover -s tests -v
python -B -m src.main --dry-run --preview-sample --weekly-meta
python -B -m src.main --dry-run --weekly-codes --limit 0
python -B -m src.main --sync-catalog-only
```

---

## Operación y despliegue (`GitHub Actions` + `cron-job.org`)

- **Workflow:** [.github/workflows/poke_alerts.yml](.github/workflows/poke_alerts.yml) (`cron: "23 * * * *"`, con ejecución manual `workflow_dispatch`: `preview_mode`, `weekly_meta`, `weekly_codes`).
- **Disparador horario puntual:** `cron-job.org` llama `workflow_dispatch` cada hora en el minuto `:23` COT con Token Clásico permanente (`scope: repo`, `$0`).
- **Sync seguro desde Google Drive (mantiene `.git` fuera de Drive):**
  ```powershell
  .\scripts\sync_and_push_github.ps1 -RepoUrl "https://github.com/Andresillo10/poke-alerts.git"
  ```
- **Publicar Dashboard web en GitHub Pages (2 clics, opcional):** en GitHub → **Settings → Pages → Source: Deploy from a branch → `main` / `/docs` → Save**. También funciona abriendo `docs/index.html` directo en cualquier navegador.
