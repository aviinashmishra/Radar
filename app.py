"""Vercel entrypoint. Vercel looks for an ASGI `app` in this file and serves it as one Python function.

By default that is Nazar: the hub plus every sector dashboard, exactly what `python run.py` serves locally. On Vercel
it runs in cloud mode (radar/cloud.py, nazar/engine.HOSTED): each sector reads the database snapshot that the GitHub
Actions workflow publishes on the `data` release (radar.db.gz for IoT, <sector>.db.gz for the others) and the
collectors never run here; Run buttons start the workflow instead.

RADAR_ONLY=1 in the Vercel project settings serves just the IoT radar, as before.
"""
import os

if os.environ.get("RADAR_ONLY") == "1":
    from fastapi import FastAPI
    from radar.server import app as radar

    app = FastAPI(title="Pebble Competitor Radar", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=radar.router.lifespan_context)
    app.mount("/", radar)
else:
    from nazar.server import app  # noqa: F401  (ASGI: hub routes + the selected sector's radar app)
