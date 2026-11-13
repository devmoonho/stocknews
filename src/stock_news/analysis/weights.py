"""채점 가중치 — config/weights.json (버전 관리). 주간 리뷰(scripts/weekly_review.py)만 바꾼다. Claude는 바꾸지 않는다."""
from __future__ import annotations

import json

from ..config import CONFIG_DIR

PATH = CONFIG_DIR / "weights.json"
FACTORS = ("scale", "exclusivity", "novelty", "policy", "diffusion", "quality", "preprice", "theme_cluster", "leader")
DEFAULT = {"version": 1, "updated": "2026-11-12", "target": "oc_excess",
           "weights": {"scale": 0.30, "exclusivity": 0.15, "novelty": 0.25, "policy": 0.10, "diffusion": 0.20},
           "note": "초기값(채점표 최초 작성)"}


def load() -> dict:
    if PATH.exists():
        return json.loads(PATH.read_text(encoding="utf-8"))
    return dict(DEFAULT)


def save(cfg: dict) -> None:
    PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def current() -> tuple[dict[str, float], int]:
    cfg = load()
    return {k: float(cfg["weights"][k]) for k in FACTORS if k in cfg["weights"]}, int(cfg["version"])
