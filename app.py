"""Vercel entrypoint. Vercel looks for a top-level FastAPI `app` in this file and serves it as one Python function.

What it serves is Nazar: the hub plus every sector dashboard, exactly what `python run.py` serves locally. On Vercel
it runs in cloud mode (radar/cloud.py, nazar/engine.HOSTED): each sector reads the database snapshot that the GitHub
Actions workflow publishes on the `data` release (radar.db.gz for IoT, <sector>.db.gz for the others) and the
collectors never run here; Run buttons start the workflow instead.

RADAR_ONLY=1 in the Vercel project settings serves just the IoT radar, as before.
"""
import os

from fastapi import FastAPI

if os.environ.get("RADAR_ONLY") == "1":
    from radar.server import app as inner
    lifespan = inner.router.lifespan_context
else:
    from nazar.server import app as inner, hub   # Nazar is an ASGI dispatcher around the hub and the sector apps
    lifespan = hub.router.lifespan_context

app = FastAPI(title="Nazar", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/", inner)
