"""경로·환경변수 공통 설정."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
WORK_DIR = DATA_DIR / "work"
REPORTS_DIR = ROOT / "reports"
DB_PATH = DATA_DIR / "stock_news.db"
CACHE_DIR = DATA_DIR / "cache"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def load_env(path: Path = ROOT / ".env") -> None:
    """.env의 KEY=VALUE를 환경변수로 (이미 설정된 값은 유지)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


SESSIONS = ("morning", "evening")      # 아침 장전 리포트 / 저녁 종가매매 리포트
WORK_SESSIONS = SESSIONS + ("preopen",)  # + 장 전 추가(07:50 + 08:00 재확인, 07:00 이후 새 공시·정책만 — 별도 리포트 없이 알림·API)


def session_dir(date: str, session: str = "morning") -> Path:
    """그날 작업 폴더: 아침 data/work/YYYY-MM-DD, 저녁 data/work/YYYY-MM-DD_evening, 장 전 추가 _preopen."""
    return WORK_DIR / (date if session == "morning" else f"{date}_{session}")


def report_path(date: str, session: str = "morning") -> Path:
    """리포트 파일: 아침 reports/YYYY-MM-DD.md, 저녁 reports/evening/YYYY-MM-DD.md."""
    return REPORTS_DIR / f"{date}.md" if session == "morning" else REPORTS_DIR / session / f"{date}.md"
