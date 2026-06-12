# 🌍 Overlay Mundial 2026 — OBS

Scoreboard en tiempo real para OBS. El HTML corre en **GitHub Pages** y el bridge CORS en **Render** (gratis).

---

## Estructura

```
overlay-mundial/
├── fifa-overlay.html   ← el overlay (GitHub Pages)
├── bridge.py           ← proxy CORS (Render)
├── render.yaml         ← config de Render
├── requirements.txt
└── README.md
```

---

## Deploy en 2 pasos

### Paso 1 — Bridge en Render

1. Crea cuenta en [render.com](https://render.com) (gratis)
2. **New → Web Service → Connect a repository**
3. Conecta este repo de GitHub
4. Render detecta `render.yaml` automáticamente — solo click en **Deploy**
5. Cuando termine, copia la URL que te da Render, algo como:
   ```
   https://overlay-mundial-bridge.onrender.com
   ```

### Paso 2 — Actualizar el HTML con tu URL

Abre `fifa-overlay.html` y en la línea que dice:
```js
const BRIDGE_URL = 'https://TU-BRIDGE.onrender.com';
```
Reemplaza `TU-BRIDGE` por el nombre real de tu servicio en Render. Luego hace commit y push.

### Paso 3 — GitHub Pages

1. Ve a **Settings → Pages** en tu repo
2. Source: **Deploy from branch → main → / (root)**
3. Tu overlay queda en:
   ```
   https://TU-USUARIO.github.io/overlay-mundial/fifa-overlay.html
   ```

---

## Usar en OBS

- Agrega una **Browser Source**
- URL: `https://TU-USUARIO.github.io/overlay-mundial/fifa-overlay.html`
- Width: `600` / Height: `150`
- ✅ Shutdown source when not visible

---

## Correr local (desarrollo)

```bash
python bridge.py          # puerto 8001
# abrir fifa-overlay.html con Live Server en VS Code (puerto 5500)
```

---

## Nota sobre Render gratis

El plan gratuito de Render "duerme" el servicio tras 15 min sin requests.
El primer fetch después de inactividad tarda ~30 seg en despertar.
Para evitarlo podés usar [UptimeRobot](https://uptimerobot.com) (gratis)
apuntando a `https://TU-BRIDGE.onrender.com/health` cada 5 minutos.
