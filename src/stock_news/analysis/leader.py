"""테마 결집도(theme_cluster)와 테마 안 대장 후보(leader) — 객관 점수.

결집도: 그날 한 테마로 재료가 얼마나 모였나
  점수 재료 = 정부 정책뉴스 ×2 + 제목에 테마가 나온 기사 수 + 언론사 수 + 그날 자기 재료(공시·제목 기사)가 있는 구성 종목 수
  8↑ 5 · 5~7 4 · 3~4 3 · 2 2 · 그 외 1

대장 후보: 재료 본문과 구성 종목 '편입 사유'(네이버 테마)의 겹치는 단어를 IDF로 가중해 합산한 연관도 순위.
  모든 편입 사유에 흔한 단어(AI·반도체 등)는 가중치가 낮고, 'IP·파운드리'처럼 드문 단어가 순위를 가른다.
  동점은 테마 순수도(속한 테마 수가 적을수록) → 시가총액(작을수록). 1위 5 · 2위 4 · 나머지 3(중립).
"""
from __future__ import annotations

import math
import re
from collections import Counter

from ..config import CONFIG_DIR

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]+|[가-힣]{2,}")
_JOSA = ("에서는", "으로는", "에서", "으로", "에게", "까지", "부터", "보다", "처럼", "하는", "하고", "했다", "한다", "된다",
         "들의", "들이", "들을", "을", "를", "이", "가", "은", "는", "의", "에", "로", "와", "과", "도", "만")


def _stopwords() -> set[str]:
    path = CONFIG_DIR / "leader_stopwords.txt"
    if not path.exists():
        return set()
    return {w.strip() for line in path.read_text(encoding="utf-8").splitlines()
            for w in line.split("#", 1)[0].split(",") if w.strip()}


def tokens(text: str | None, stop: set[str] | None = None) -> list[str]:
    out = []
    for t in _TOKEN.findall(text or ""):
        if not t.isascii():
            for j in _JOSA:                       # 조사·어미를 한 번만 떼어 낸다('반도체를' → '반도체')
                if t.endswith(j) and len(t) - len(j) >= 2:
                    t = t[: -len(j)]
                    break
        else:
            t = t.upper()
        if len(t) >= 2 and (not stop or t not in stop):
            out.append(t)
    return out


def terms(text: str | None, stop: set[str] | None = None) -> set[str]:
    return set(tokens(text, stop))


def salient(head: str, body: str, stop: set[str]) -> set[str]:
    """재료의 핵심 단어: 제목·요약에 나온 단어 + 본문에 2번 이상 나온 단어 (본문에 한 번 스친 말은 제외)."""
    cnt = Counter(tokens(body, stop))
    return terms(head, stop) | {t for t, n in cnt.items() if n >= 2}


class Relevance:
    """편입 사유 전체로 IDF를 만든 연관도 계산기."""

    def __init__(self, reasons: list[str]) -> None:
        self.stop = _stopwords()
        docs = [terms(r, self.stop) for r in reasons if r]
        self.n = max(1, len(docs))
        self.df = Counter(t for d in docs for t in d)

    @classmethod
    def from_df(cls, n: int, df: dict[str, int]) -> "Relevance":
        """미리 센 문서 빈도로 만든다(테스트 고정값용)."""
        obj = cls([])
        obj.n, obj.df = n, Counter(df)
        return obj

    def idf(self, t: str) -> float:
        return math.log((self.n + 1) / (self.df.get(t, 0) + 1)) + 0.1

    def score(self, material: set[str], reason: str | None) -> tuple[float, list[str]]:
        common = terms(reason, self.stop) & material
        ranked = sorted(common, key=self.idf, reverse=True)
        return round(sum(self.idf(t) for t in common), 2), ranked[:6]


def rank_members(rel: Relevance, head: str, body: str, members: list[dict]) -> list[dict]:
    """members: [{code, name, reason, n_themes, market_cap}] → 연관도 순위(rank, relevance, matched) 붙여 정렬.
    연관도 0(편입 사유가 없거나 겹치는 핵심 단어 없음)은 순위 없음(rank None, 대장 점수 중립)."""
    mat = salient(head, body, rel.stop)
    out = []
    for m in members:
        s, matched = rel.score(mat, m.get("reason"))
        out.append({**m, "relevance": s, "matched": matched})
    out.sort(key=lambda m: (-m["relevance"], m.get("n_themes") or 99, m.get("market_cap") or 9e18))
    for i, m in enumerate(out, 1):
        m["rank"] = i if m["relevance"] > 0 else None
    return out


def leader_score(rank: int | None) -> int | None:
    if rank is None:
        return None
    return 5 if rank == 1 else 4 if rank == 2 else 3


def cluster_points(gov: int, title_mentions: int, offices: int, material_members: int) -> int:
    return 2 * gov + title_mentions + offices + material_members


def cluster_score(points: int) -> int:
    return 5 if points >= 8 else 4 if points >= 5 else 3 if points >= 3 else 2 if points >= 2 else 1
