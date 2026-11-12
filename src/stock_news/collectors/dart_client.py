"""DART(전자공시) Open API 단독 클라이언트 — 공시 목록 조회 + 공시 본문(내용) 확인 + 재무(영업이익률).

표준 라이브러리만 사용(파이썬 3.9+). 리눅스/윈도우 공통.
API 키: https://opendart.fss.or.kr 에서 발급 → 환경변수 DART_API_KEY 또는 .env 파일.

출처(정리·통합): AutoInvest backend/app/analyzer/dart_client.py(신호 분류·본문 정리),
MocTrading data/dart_client.py(corp_code 매핑·목록), swingbot001 data/dart_client.py(재무).

사용 예(CLI):
    python dart_client.py list --stock 005930 --days 30
    python dart_client.py list --date 20261112 --type I          # 그날 전체 거래소 공시
    python dart_client.py doc 20261112800123 --max-chars 3000      # 공시 본문 텍스트
    python dart_client.py margin 005930 --year 2025                # 영업이익률(사업보고서)

사용 예(코드):
    from dart_client import DartClient
    dart = DartClient()                      # DART_API_KEY 환경변수/.env
    rows = dart.list_disclosures(stock_code="005930", bgn_de="20261001", end_de="20261112")
    text = dart.document_text(rows[0].rcept_no)
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

BASE_URL = "https://opendart.fss.or.kr/api"

# 공시유형(pblntf_ty): A 정기 · B 주요사항 · C 발행 · D 지분 · E 기타 · F 외부감사 · G 펀드 · H 자산유동화 · I 거래소 · J 공정위
PBLNTF_TYPES = {"A": "정기공시", "B": "주요사항보고", "C": "발행공시", "D": "지분공시", "E": "기타공시",
                "F": "외부감사관련", "G": "펀드공시", "H": "자산유동화", "I": "거래소공시", "J": "공정위공시"}

# 상태 코드 — 013(조회된 데이터 없음)은 정상 빈 결과
STATUS_NO_DATA = "013"

# ---------------------------------------------------------------------------
# 공시 제목 → 신호 분류(AutoInvest 룰셋). 매도 키워드를 먼저 본다.
# ---------------------------------------------------------------------------
BUY_KEYWORDS = (
    "자기주식 취득결정", "자기주식취득결정", "자기주식 취득 결정", "자기주식취득 결정",
    "자기주식취득 신탁계약 체결", "무상증자", "배당", "영업양수 결정", "유형자산 양수 결정",
    "타법인 주식 및 출자증권 양수", "관리절차 중단", "해외 증권시장 주권등 상장결정",
    "해외 증권시장 상장결정", "해외증권시장 상장",
)
SELL_KEYWORDS = (
    "부도발생", "부도 발생", "영업정지", "회생절차", "해산사유", "감자 결정", "감자결정",
    "관리절차 개시", "상장폐지 결정", "자기주식 처분결정", "자기주식처분결정", "자기주식 처분 결정",
    "자기주식취득 신탁계약 해지", "영업양도 결정", "유형자산 양도 결정", "타법인 주식 및 출자증권 양도",
    "소송 등의 제기",
)
# 재료가 "확정"됐다고 볼 수 있는 제목 키워드(MocTrading) — 공백 제거한 제목과 비교
CONFIRMED_KEYWORDS = (
    "공급계약", "수주", "단일판매", "계약체결", "취득결정", "처분결정", "유상증자결정", "무상증자결정",
    "자기주식취득", "합병", "분할", "잠정실적", "영업실적", "결산실적", "현금·현물배당결정",
    "전환사채발행결정", "신주인수권부사채발행결정", "타법인주식및출자증권취득결정",
)


def classify_signal(report_nm: str) -> str:
    """보고서명 → 'buy' | 'sell' | 'neutral'."""
    if any(kw in report_nm for kw in SELL_KEYWORDS):
        return "sell"
    if any(kw in report_nm for kw in BUY_KEYWORDS):
        return "buy"
    return "neutral"


def is_confirmed_material(report_nm: str) -> bool:
    t = report_nm.replace(" ", "")
    return any(kw in t for kw in CONFIRMED_KEYWORDS)


class DartApiError(Exception):
    def __init__(self, status: str, message: str):
        super().__init__(f"DART {status}: {message}")
        self.status, self.message = status, message


@dataclass
class Disclosure:
    rcept_no: str        # 접수번호(14자리) — 본문 조회 키
    rcept_dt: str        # 접수일 YYYYMMDD
    corp_name: str
    corp_code: str       # DART 고유번호(8자리)
    stock_code: str      # 종목코드(6자리, 비상장은 빈 값)
    corp_cls: str        # Y 유가 · K 코스닥 · N 코넥스 · E 기타
    report_nm: str       # 보고서명(제목)
    flr_nm: str          # 제출인
    rm: str              # 비고(유: 유가증권시장본부 소관, 코: 코스닥, 정: 정정 등)

    @property
    def url(self) -> str:
        return f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={self.rcept_no}"

    @property
    def signal(self) -> str:
        return classify_signal(self.report_nm)

    @property
    def confirmed(self) -> bool:
        return is_confirmed_material(self.report_nm)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(url=self.url, signal=self.signal, confirmed=self.confirmed)
        return d


def _load_env_key() -> str:
    key = os.environ.get("DART_API_KEY", "").strip()
    if key:
        return key
    for p in (Path.cwd() / ".env", Path(__file__).with_name(".env")):
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("DART_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


class DartClient:
    """동기 클라이언트(urllib). asyncio 코드에선 `await asyncio.to_thread(dart.list_disclosures, ...)`."""

    def __init__(self, api_key: str | None = None, cache_dir: str | Path | None = None,
                 timeout: float = 30.0, min_interval: float = 0.1) -> None:
        self.api_key = api_key if api_key is not None else _load_env_key()
        if not self.api_key:
            raise ValueError("DART_API_KEY가 없습니다 — 환경변수나 .env에 넣으세요")
        self.cache_dir = Path(cache_dir) if cache_dir else Path(__file__).with_name("cache")
        self.timeout = timeout
        self.min_interval = min_interval     # DART 분당 호출 제한 대비 최소 간격(초)
        self._last_call = 0.0
        self._corp_map: dict[str, dict] | None = None

    # ------------------------------------------------------------------ HTTP
    def _get(self, endpoint: str, params: dict) -> bytes:
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        q = urllib.parse.urlencode({"crtfc_key": self.api_key, **{k: v for k, v in params.items() if v not in (None, "")}})
        req = urllib.request.Request(f"{BASE_URL}/{endpoint}?{q}", headers={"User-Agent": "dart-client/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        finally:
            self._last_call = time.monotonic()

    def _get_json(self, endpoint: str, params: dict) -> dict:
        data = json.loads(self._get(endpoint, params).decode("utf-8"))
        status = str(data.get("status", ""))
        if status not in ("000", STATUS_NO_DATA):
            raise DartApiError(status, str(data.get("message", "")))
        return data

    @staticmethod
    def _raise_if_xml_error(raw: bytes) -> None:
        """zip을 기대한 응답이 오류 XML이면 예외(예: <result><status>014</status>...)."""
        if raw[:2] == b"PK":
            return
        try:
            root = ET.fromstring(raw)
            raise DartApiError(root.findtext("status") or "?", root.findtext("message") or raw[:200].decode("utf-8", "replace"))
        except ET.ParseError:
            raise DartApiError("?", raw[:200].decode("utf-8", "replace")) from None

    # ------------------------------------------------------------ corp_code
    def corp_map(self, refresh: bool = False) -> dict[str, dict]:
        """종목코드(6자리) → {"corp_code", "corp_name"}. 전체 상장사 매핑을 cache/에 하루 단위로 캐싱."""
        if self._corp_map is not None and not refresh:
            return self._corp_map
        path = self.cache_dir / "CORPCODE.xml"
        fresh = path.exists() and dt.date.fromtimestamp(path.stat().st_mtime) == dt.date.today()
        if refresh or not fresh:
            raw = self._get("corpCode.xml", {})
            self._raise_if_xml_error(raw)
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                xml_bytes = zf.read(zf.namelist()[0])
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(xml_bytes)
        else:
            xml_bytes = path.read_bytes()
        out: dict[str, dict] = {}
        for item in ET.fromstring(xml_bytes).iter("list"):
            stock = (item.findtext("stock_code") or "").strip()
            if stock:   # 비상장사는 빈 값
                out[stock] = {"corp_code": (item.findtext("corp_code") or "").strip(),
                              "corp_name": (item.findtext("corp_name") or "").strip()}
        self._corp_map = out
        return out

    def corp_code(self, stock_code: str) -> str | None:
        hit = self.corp_map().get(stock_code)
        return hit["corp_code"] if hit else None

    # ---------------------------------------------------------- 공시 목록
    def list_disclosures(self, *, stock_code: str | None = None, corp_code: str | None = None,
                         bgn_de: str | None = None, end_de: str | None = None,
                         pblntf_ty: str | None = None, corp_cls: str | None = None,
                         last_reprt_at: bool = True, max_pages: int = 50) -> list[Disclosure]:
        """공시 목록(최신순). 회사 지정 없으면 시장 전체 — 이때 기간은 최대 3개월(DART 제한).
        날짜는 YYYYMMDD, 기본은 오늘 하루. last_reprt_at=True면 정정 전 원본은 빼고 최종본만."""
        if stock_code and not corp_code:
            corp_code = self.corp_code(stock_code)
            if corp_code is None:
                raise ValueError(f"corp_code를 찾을 수 없습니다: {stock_code}")
        today = dt.date.today().strftime("%Y%m%d")
        params = {"corp_code": corp_code, "bgn_de": bgn_de or today, "end_de": end_de or today,
                  "pblntf_ty": pblntf_ty, "corp_cls": corp_cls,
                  "last_reprt_at": "Y" if last_reprt_at else "N", "page_count": 100}
        out: list[Disclosure] = []
        page = 1
        while page <= max_pages:
            data = self._get_json("list.json", {**params, "page_no": page})
            for r in data.get("list", []) or []:
                out.append(Disclosure(**{k: str(r.get(k, "") or "").strip() for k in (
                    "rcept_no", "rcept_dt", "corp_name", "corp_code", "stock_code", "corp_cls",
                    "report_nm", "flr_nm", "rm")}))
            if page >= int(data.get("total_page", 1) or 1):
                break
            page += 1
        out.sort(key=lambda d: d.rcept_no, reverse=True)   # 접수번호 = 날짜+일련번호 → 최신순
        return out

    def new_disclosures(self, seen: set[str], **kw) -> list[Disclosure]:
        """폴링용 — `seen`(접수번호 집합)에 없는 것만 돌려주고 seen에 추가. 예: 1~2분마다 오늘 거래소 공시."""
        fresh = [d for d in self.list_disclosures(**kw) if d.rcept_no not in seen]
        seen.update(d.rcept_no for d in fresh)
        return fresh

    # ---------------------------------------------------------- 공시 본문
    def document_raw(self, rcept_no: str) -> dict[str, str]:
        """공시 원문(document.xml, zip) → {파일명: 문자열}. 첨부(감사보고서 등)가 여러 파일로 올 수 있다."""
        raw = self._get("document.xml", {"rcept_no": rcept_no})
        self._raise_if_xml_error(raw)
        files: dict[str, str] = {}
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for name in zf.namelist():
                b = zf.read(name)
                for enc in ("utf-8", "euc-kr", "cp949"):
                    try:
                        files[name] = b.decode(enc)
                        break
                    except UnicodeDecodeError:
                        continue
                else:
                    files[name] = b.decode("utf-8", "replace")
        return files

    def document_text(self, rcept_no: str, max_chars: int | None = None, main_only: bool = True) -> str:
        """공시 본문을 읽을 수 있는 텍스트로(태그·스타일 제거, 표는 칸을 ' | '로). main_only면 첫(본문) 파일만."""
        files = self.document_raw(rcept_no)
        names = sorted(files, key=lambda n: (not n.startswith(rcept_no), n))   # 접수번호.xml이 본문
        if main_only:
            names = names[:1]
        text = "\n\n".join(xml_to_text(files[n]) for n in names)
        return text[:max_chars] if max_chars else text

    # ------------------------------------------------------------- 재무
    def single_account(self, stock_code: str, year: int, reprt_code: str = "11011", fs_div: str = "CFS") -> list[dict]:
        """단일회사 주요계정(fnlttSinglAcnt). reprt_code: 11011 사업 · 11012 반기 · 11013 1분기 · 11014 3분기.
        fs_div: CFS 연결 · OFS 별도(해당 행만 골라 돌려줌, 연결이 없으면 별도)."""
        cc = self.corp_code(stock_code)
        if cc is None:
            raise ValueError(f"corp_code를 찾을 수 없습니다: {stock_code}")
        rows = self._get_json("fnlttSinglAcnt.json", {"corp_code": cc, "bsns_year": str(year),
                                                      "reprt_code": reprt_code}).get("list", []) or []
        picked = [r for r in rows if r.get("fs_div") == fs_div]
        return picked or rows

    def operating_margin_pct(self, stock_code: str, year: int, reprt_code: str = "11011") -> float | None:
        """영업이익률(%) = 영업이익 / 매출액 × 100 (당기). 계정을 못 찾으면 None."""
        rows = self.single_account(stock_code, year, reprt_code)
        rev = _find_amount(rows, ("매출액", "수익(매출액)", "영업수익"))
        op = _find_amount(rows, ("영업이익", "영업이익(손실)"))
        if rev is None or op is None or rev == 0:
            return None
        return round(op / rev * 100, 2)


def _find_amount(rows: list[dict], names: tuple[str, ...]) -> float | None:
    for r in rows:
        if str(r.get("account_nm", "")).strip() in names:
            s = str(r.get("thstrm_amount", "")).replace(",", "").strip()
            try:
                return float(s)
            except ValueError:
                return None
    return None


_BLOCK_TAGS = r"(?:P|BR|TR|TABLE|TITLE|SECTION-\d|LIBRARY|COVER-TITLE|DIV|H\d|LI)"


def xml_to_text(src: str) -> str:
    """DART 공시 XML/HTML → 평문. 표는 행마다 한 줄, 칸은 ' | '."""
    t = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", src, flags=re.S | re.I)
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.S)
    t = re.sub(r"</(TD|TH|TE|TU)\s*>", " | ", t, flags=re.I)
    t = re.sub(rf"</?{_BLOCK_TAGS}\b[^>]*>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t).replace("\xa0", " ")
    lines = []
    for ln in t.splitlines():
        ln = re.sub(r"[ \t]+", " ", ln).strip().strip("|").strip()
        if ln:
            lines.append(ln)
    return "\n".join(lines)


# ---------------------------------------------------------------------- CLI
def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DART 공시 조회/본문 확인")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="공시 목록")
    p.add_argument("--stock", help="종목코드 6자리(없으면 시장 전체)")
    p.add_argument("--date", help="하루 YYYYMMDD(기본 오늘)")
    p.add_argument("--bgn"), p.add_argument("--end")
    p.add_argument("--days", type=int, help="오늘부터 N일 전까지")
    p.add_argument("--type", help="공시유형 A~J (I=거래소공시, B=주요사항)")
    p.add_argument("--cls", help="Y 유가 · K 코스닥 · N 코넥스")
    p.add_argument("--signal", choices=["buy", "sell", "neutral"], help="신호로 거르기")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("doc", help="공시 본문 텍스트")
    p.add_argument("rcept_no")
    p.add_argument("--max-chars", type=int, default=5000)
    p.add_argument("--all", action="store_true", help="첨부 파일까지")

    p = sub.add_parser("margin", help="영업이익률")
    p.add_argument("stock"), p.add_argument("--year", type=int, default=dt.date.today().year - 1)

    a = ap.parse_args(argv)
    dart = DartClient()
    if a.cmd == "list":
        end = a.end or a.date
        bgn = a.bgn or a.date
        if a.days:
            end = dt.date.today().strftime("%Y%m%d")
            bgn = (dt.date.today() - dt.timedelta(days=a.days)).strftime("%Y%m%d")
        rows = dart.list_disclosures(stock_code=a.stock, bgn_de=bgn, end_de=end, pblntf_ty=a.type, corp_cls=a.cls)
        if a.signal:
            rows = [r for r in rows if r.signal == a.signal]
        if a.json:
            print(json.dumps([r.to_dict() for r in rows], ensure_ascii=False, indent=1))
        else:
            for r in rows:
                flag = {"buy": "▲", "sell": "▼"}.get(r.signal, " ") + ("*" if r.confirmed else " ")
                print(f"{r.rcept_dt} {r.rcept_no} {flag} {r.corp_name}({r.stock_code or '-'}) {r.report_nm} [{r.flr_nm}]")
            print(f"— {len(rows)}건", file=sys.stderr)
    elif a.cmd == "doc":
        print(dart.document_text(a.rcept_no, max_chars=a.max_chars, main_only=not a.all))
    elif a.cmd == "margin":
        print(dart.operating_margin_pct(a.stock, a.year))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(_main())
