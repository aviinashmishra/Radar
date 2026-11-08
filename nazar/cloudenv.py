"""`python -m nazar.cloudenv <sector>` prints the environment a worker needs to run one sector's radar:

    RADAR_CONFIG=config/sectors/auto/competitors.yaml
    RADAR_DB=data/sectors/auto/radar.db
    RADAR_SNAPSHOT=auto.db.gz
    RADAR_SECTOR=auto
    RADAR_SECTOR_LABEL=Automotive & EV

GitHub Actions appends it to $GITHUB_ENV (see .github/workflows/radar.yml); a shell can `eval` it.
"""
from __future__ import annotations
import sys
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent.parent


def env_for(key: str) -> dict[str, str]:
    reg = yaml.safe_load((ROOT / "config" / "nazar.yaml").read_text(encoding="utf-8")) or {}
    for s in reg.get("sectors") or []:
        if s["key"] == key:
            Path(ROOT / s["db"]).parent.mkdir(parents=True, exist_ok=True)
            return {"RADAR_CONFIG": s["config"], "RADAR_DB": s["db"], "RADAR_SNAPSHOT": s.get("snapshot") or f"{key}.db.gz",
                    "RADAR_SECTOR": key, "RADAR_SECTOR_LABEL": s["label"]}
    raise SystemExit(f"unknown sector {key!r}; known: {', '.join(x['key'] for x in reg.get('sectors') or [])}")


if __name__ == "__main__":
    for k, v in env_for(sys.argv[1] if len(sys.argv) > 1 else "iot").items():
        print(f"{k}={v}")
