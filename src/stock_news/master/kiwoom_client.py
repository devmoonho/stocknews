"""키움 REST API 최소 클라이언트 — 토큰 발급/캐시, 공통 POST, 연속조회(cont-yn/next-key).

문서: https://openapi.kiwoom.com/guide/apiguide
- 운영: https://api.kiwoom.com · 모의: https://mockapi.kiwoom.com
- 토큰: POST /oauth2/token {grant_type, appkey, secretkey} → {token, expires_dt(YYYYMMDDHHMMSS)}
- TR 호출: POST {path}, 헤더 api-id(TR 코드)·authorization(Bearer)·cont-yn·next-key
- 응답 본문 return_code 0 = 정상. 연속 데이터는 응답 헤더 cont-yn=Y, next-key 로 이어서 요청.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.error
import urllib.request

from ..config import CACHE_DIR

REAL_URL = "https://api.kiwoom.com"
MOCK_URL = "https://mockapi.kiwoom.com"
RATE_LIMIT_CODES = {1700}       # 허용된 요청 개수 초과


class KiwoomError(Exception):
    def __init__(self, code, msg: str, api_id: str = ""):
        super().__init__(f"키움 {api_id} 오류 {code}: {msg}")
        self.code, self.msg = code, msg


class KiwoomClient:
    def __init__(self, app_key: str | None = None, secret_key: str | None = None,
                 mock: bool | None = None, min_interval: float = 0.25, timeout: float = 20.0) -> None:
        self.app_key = app_key or os.environ.get("KIWOOM_APP_KEY", "").strip()
        self.secret_key = secret_key or os.environ.get("KIWOOM_SECRET_KEY", "").strip()
        if not self.app_key or not self.secret_key:
            raise ValueError("KIWOOM_APP_KEY / KIWOOM_SECRET_KEY 가 없습니다 — .env에 넣으세요")
        if mock is None:
            mock = os.environ.get("KIWOOM_MOCK", "0").strip() in ("1", "true", "True", "Y")
        self.base = MOCK_URL if mock else REAL_URL
        self.min_interval = min_interval
        self.timeout = timeout
        self._last = 0.0
        self._token: str | None = None
        self._token_path = CACHE_DIR / f"kiwoom_token_{'mock' if mock else 'real'}.json"

    # ------------------------------------------------------------- 토큰
    def token(self) -> str:
        if self._token:
            return self._token
        if self._token_path.exists():
            try:
                c = json.loads(self._token_path.read_text(encoding="utf-8"))
                exp = dt.datetime.strptime(c["expires_dt"], "%Y%m%d%H%M%S")
                if c.get("app_key_tail") == self.app_key[-6:] and exp - dt.datetime.now() > dt.timedelta(minutes=10):
                    self._token = c["token"]
                    return self._token
            except (KeyError, ValueError, json.JSONDecodeError):
                pass
        body, _ = self._post_raw("/oauth2/token", {"grant_type": "client_credentials",
                                                   "appkey": self.app_key, "secretkey": self.secret_key}, headers={})
        if str(body.get("return_code", "0")) != "0" or not body.get("token"):
            raise KiwoomError(body.get("return_code"), body.get("return_msg", ""), "token")
        self._token = body["token"]
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(json.dumps({"token": self._token, "expires_dt": body.get("expires_dt", ""),
                                                "app_key_tail": self.app_key[-6:]}), encoding="utf-8")
        os.chmod(self._token_path, 0o600)
        return self._token

    # ------------------------------------------------------------- HTTP
    def _post_raw(self, path: str, payload: dict, headers: dict) -> tuple[dict, dict]:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json;charset=UTF-8", **headers})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8")), {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return json.loads(raw), {k.lower(): v for k, v in e.headers.items()}
            except json.JSONDecodeError:
                raise KiwoomError(e.code, raw[:200], path) from None
        finally:
            self._last = time.monotonic()

    def call(self, api_id: str, path: str, payload: dict, cont_yn: str = "N", next_key: str = "",
             retries: int = 3) -> tuple[dict, str, str]:
        """TR 1회 호출 → (본문, cont-yn, next-key)."""
        for attempt in range(retries + 1):
            headers = {"api-id": api_id, "authorization": f"Bearer {self.token()}",
                       "cont-yn": cont_yn, "next-key": next_key}
            body, h = self._post_raw(path, payload, headers)
            code = body.get("return_code", 0)
            try:
                code = int(code)
            except (TypeError, ValueError):
                pass
            if code == 0:
                return body, h.get("cont-yn", "N"), h.get("next-key", "")
            msg = str(body.get("return_msg", ""))
            if "8005" in msg and attempt < retries:          # 토큰 만료/무효 → 재발급
                self._token = None
                self._token_path.unlink(missing_ok=True)
                continue
            if (code in RATE_LIMIT_CODES or "1700" in msg) and attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise KiwoomError(code, msg, api_id)
        raise KiwoomError("?", "재시도 초과", api_id)

    def call_all(self, api_id: str, path: str, payload: dict, list_key: str, max_pages: int = 100) -> list[dict]:
        """연속조회로 list_key 배열을 끝까지 모은다."""
        out: list[dict] = []
        cont, nkey = "N", ""
        for _ in range(max_pages):
            body, cont, nkey = self.call(api_id, path, payload, cont, nkey)
            if list_key not in body:
                raise KiwoomError("FORMAT", f"응답에 '{list_key}' 없음. keys={sorted(body)[:15]}", api_id)
            out.extend(body.get(list_key) or [])
            if cont != "Y" or not nkey:
                break
        return out
