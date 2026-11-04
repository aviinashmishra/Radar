FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt requirements-full.txt ./
# Chromium is for the daily homepage screenshots (screenshots: true in config/competitors.yaml)
RUN pip install --no-cache-dir -r requirements-full.txt \
 && python -m playwright install --with-deps chromium
COPY . .
ENV RADAR_HOST=0.0.0.0 RADAR_PORT=8080 RADAR_DB=/data/radar.db
VOLUME ["/data"]
EXPOSE 8080
CMD ["python", "-m", "radar.server"]
