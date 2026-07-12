# Overlay Mundial 2026

The overlay gets fixtures, live scores, match time, goals, cards and penalty
shootouts through `bridge.py`.

Data sources:

- ESPN public World Cup endpoints (primary, refreshed every overlay poll).
- `wcup2026.org` open community API (automatic fallback).

No API key or paid subscription is required.

## Run locally

```powershell
python bridge.py
```

Then check:

- `http://localhost:8001/health`
- `http://localhost:8001/match`

Open any `fifa-overlay*.html` file. The overlay polls the local bridge every five
seconds. Keep the PowerShell window running; stop it with `Ctrl+C`.

## Render

Deploy using `render.yaml`. No secret environment variables are needed. The HTML
files point to `https://overlay-mundial-bridge.onrender.com` for hosted use.
