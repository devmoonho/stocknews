"""기사 ↔ 종목/테마 매칭 (결정적 규칙). 최종 관련성 판단은 Claude가 한다 — 여기선 후보를 좁히고 근거 문맥을 붙인다.

종목명 매칭 규칙
  - 후보 종목(is_excluded=0)의 정식 이름 + config/name_aliases.txt 별칭
  - 우선주(…우, …우B, …1우 등)는 제외 — 보통주 이름과 겹치기 때문
  - config/name_stopwords.txt 의 이름은 제외(일반 명사와 겹침)
  - 긴 이름 우선(정규식 대안을 길이 내림차순으로) → 'SK하이닉스'가 'SK'보다 먼저
  - 앞 글자가 한글/영숫자면 무시(단어 중간)
  - 2글자 한글 이름은 뒤에 조사·구두점·공백이 올 때만 인정
  - 영문/숫자로 끝나는 이름은 뒤에 영숫자가 오면 무시
테마 매칭: 테마 이름을 키워드로 쪼개 기사에서 찾는다(너무 일반적인 단어 제외).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import CONFIG_DIR

_PREF = re.compile(r"(\d?우[A-Z]?|우\(전환\))$")
_HANGUL_ALNUM = re.compile(r"[가-힣A-Za-z0-9]")
_PARTICLE_START = set("은는이가을를의에와과도로으측만까께부랑")
GENERIC_THEME_WORDS = {
    "관련", "관련주", "수혜", "수혜주", "중소형", "대형", "기타", "테마", "지수", "국내", "해외", "신규", "대표",
    "제품", "부품", "장비", "소재", "제조", "서비스", "산업", "기업", "유통", "업체", "등", "주", "기술", "사업",
    "설비", "시스템", "원료", "개발", "생산", "판매", "인프라", "플랫폼", "솔루션", "콘텐츠", "부품/장비",
    # 시황 기사마다 나오는 시장·업권 단어 (테마 키워드로 쓰면 신호가 모두 이걸로 덮임)
    "코스닥", "코스피", "증권", "은행", "보험", "금융", "지주사", "대기업", "중소기업",
    # 기사 본문에 흔히 쓰이는 일반 단어 (테마 이름 조각이지만 단독으로는 의미 없음)
    "분석", "유지", "정보", "저장", "활용", "발전", "설치", "운영", "물리", "모바일", "인터넷", "설계", "통신",
    "진단", "화학", "전자", "건설", "운송", "물류", "소비", "수출", "투자", "관리", "가공", "수입", "전력",
    "부동산", "에너지", "환경", "안전", "자동차", "의료", "식품", "음식", "치료제", "백신", "제약", "장치",
    "온라인", "오프라인", "모듈", "지능형", "국산화",
}


def _read_lines(name: str) -> list[str]:
    path = CONFIG_DIR / name
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


@dataclass
class Hit:
    key: str          # 종목코드 또는 테마 키
    term: str         # 실제로 찾은 문자열
    start: int


@dataclass
class StockMatcher:
    name_to_code: dict[str, str]
    pattern: re.Pattern = field(init=False)

    def __post_init__(self) -> None:
        names = sorted(self.name_to_code, key=len, reverse=True)
        self.pattern = re.compile("|".join(re.escape(n) for n in names))

    @classmethod
    def from_db(cls, conn) -> "StockMatcher":
        stop = set(_read_lines("name_stopwords.txt"))
        mapping: dict[str, str] = {}
        for code, name in conn.execute("SELECT code, name FROM stocks WHERE is_excluded=0 AND is_listed=1"):
            if _PREF.search(name) or name in stop or len(name) < 2:
                continue
            mapping[name] = code
        by_name = {n: c for c, n in conn.execute("SELECT code, name FROM stocks WHERE is_excluded=0")}
        for line in _read_lines("name_aliases.txt"):
            alias, _, official = line.partition("=")
            if official.strip() in by_name:
                mapping[alias.strip()] = by_name[official.strip()]
        return cls(mapping)

    def find(self, text: str) -> list[Hit]:
        hits = []
        for m in self.pattern.finditer(text):
            term, s, e = m.group(0), m.start(), m.end()
            if s > 0 and _HANGUL_ALNUM.match(text[s - 1]):
                continue
            nxt = text[e] if e < len(text) else ""
            if term[-1].isascii() and term[-1].isalnum():
                if nxt and nxt.isascii() and nxt.isalnum():
                    continue
            elif len(term) == 2 and nxt and re.match(r"[가-힣]", nxt) and nxt not in _PARTICLE_START:
                continue
            hits.append(Hit(self.name_to_code[term], term, s))
        return hits


def theme_keywords(theme_name: str, source: str = "naver") -> set[str]:
    """'GTX(수도권 광역급행철도)' → {'GTX', '수도권 광역급행철도'}.

    키움 테마는 '대분류_세부'(예: '반도체_후공정장비', '태양광_잉곳/웨이퍼') 형식이라 대분류 단어는 수십 개 테마에
    겹친다 → 키움은 '_' 뒤 세부 부분만 키워드로 쓴다('잉곳', '웨이퍼'). 대분류 테마는 네이버 쪽이 맡는다.
    """
    name = theme_name.replace("＆", "&")
    if source == "kiwoom" and "_" in name:
        name = name.split("_", 1)[1]
    parts = re.split(r"[()/_,·&\[\]]", name)
    out = set()
    for p in parts:
        p = re.sub(r"\s*(등|관련주?)$", "", p.strip())
        if len(p) < 2 or p in GENERIC_THEME_WORDS:
            continue
        if re.fullmatch(r"[A-Za-z0-9 .\-]+", p) and len(p) < 2:
            continue
        out.add(p)
    return out


@dataclass
class ThemeMatcher:
    keyword_to_themes: dict[str, set[str]]       # 키워드 → {"naver:59", ...}
    pattern: re.Pattern = field(init=False)

    def __post_init__(self) -> None:
        kws = sorted(self.keyword_to_themes, key=len, reverse=True)
        self.pattern = re.compile("|".join(re.escape(k) for k in kws)) if kws else re.compile(r"(?!x)x")

    @classmethod
    def from_db(cls, conn) -> "ThemeMatcher":
        stock_names = {r[0] for r in conn.execute("SELECT name FROM stocks")}
        blocked = set()
        for line in _read_lines("theme_keyword_block.txt"):
            theme, _, kw = line.rpartition("|")
            blocked.add((theme.strip(), kw.strip()))
        aliases: dict[str, set[str]] = {}
        for line in _read_lines("theme_keyword_aliases.txt"):
            theme, _, kws = line.partition("|")
            aliases.setdefault(theme.strip(), set()).update(k.strip() for k in kws.split(",") if k.strip())
        mapping: dict[str, set[str]] = {}
        for source, code, name in conn.execute("SELECT source, theme_code, name FROM themes"):
            for kw in theme_keywords(name, source) | aliases.get(name, set()):
                if kw in stock_names or (name, kw) in blocked:   # 종목명 조각은 종목 매칭이 맡는다
                    continue
                mapping.setdefault(kw, set()).add(f"{source}:{code}")
        return cls(mapping)

    def find(self, text: str) -> list[Hit]:
        hits = []
        for m in self.pattern.finditer(text):
            term, s, e = m.group(0), m.start(), m.end()
            if term.isascii():      # 영문 키워드는 영숫자 경계 필요 (AI ≠ EMAIL)
                if (s > 0 and text[s - 1].isascii() and text[s - 1].isalnum()) or \
                   (e < len(text) and text[e].isascii() and text[e].isalnum()):
                    continue
            elif s > 0 and re.match(r"[가-힣]", text[s - 1]):
                continue            # 한글 키워드가 단어 중간에서 시작 ('비은행'의 '은행' 등)
            for key in self.keyword_to_themes[term]:
                hits.append(Hit(key, term, s))
        return hits


def policy_terms() -> list[str]:
    return _read_lines("policy_keywords.txt")


def snippet(text: str, pos: int, radius: int = 110) -> str:
    s, e = max(0, pos - radius), min(len(text), pos + radius)
    out = text[s:e].replace("\n", " ")
    return ("…" if s > 0 else "") + out + ("…" if e < len(text) else "")
