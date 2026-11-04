"""Vercel entrypoint. Vercel looks for a FastAPI `app` in this file and serves it as one Python function.

The radar app is mounted whole, so its routes, password gate and cloud-mode snapshot loading all apply.
On Vercel the radar runs in cloud mode (radar/cloud.py): the dashboard reads the database snapshot that
the GitHub Actions workflow publishes; the collectors themselves never run here.

Running locally? Use `python run.py` (or start.bat): that also runs the collectors.
"""
from fastapi import FastAPI

from radar.server import app as radar

app = FastAPI(title="Pebble Competitor Radar", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=radar.router.lifespan_context)
app.mount("/", radar)
