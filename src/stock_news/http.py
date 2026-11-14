"""간단한 HTTP GET/POST — 요청 간격 유지 + 재시도(+선택: 쿠키). 표준 라이브러리만 사용."""
from __future__ import annotations

import http.cookiejar
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import USER_AGENT


class Fetcher:
    def __init__(self, min_interval: float = 0.5, timeout: float = 20.0, retries: int = 2,
                 cookies: bool = False) -> None:
        self.min_interval = min_interval
        self.timeout = timeout
        self.retries = retries
        self._last = 0.0
        # cookies=True: 쿠키를 받아야 페이지를 주는 사이트(예: 국토부 307 → 쿠키 → 200)
        self._open = (urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())).open
                      if cookies else urllib.request.urlopen)

    def get(self, url: str, params: dict | None = None, referer: str | None = None,
            data: dict | None = None, headers: dict | None = None) -> bytes:
        """data가 있으면 POST(form). 나머지는 GET."""
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9", **(headers or {})}
        if referer:
            headers["Referer"] = referer
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        err: Exception | None = None
        for attempt in range(self.retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                req = urllib.request.Request(url, data=body, headers=headers)
                with self._open(req, timeout=self.timeout) as resp:
                    return resp.read()
            except (urllib.error.URLError, TimeoutError) as e:
                err = e
                if isinstance(e, urllib.error.HTTPError) and 400 <= e.code < 500 and e.code != 429:
                    break
                time.sleep(1.5 * (attempt + 1))
            finally:
                self._last = time.monotonic()
        raise RuntimeError(f"GET 실패: {url} ({err})")

    def get_json(self, url: str, params: dict | None = None, referer: str | None = None, **kw):
        return json.loads(self.get(url, params, referer, **kw).decode("utf-8"))

    def get_text(self, url: str, params: dict | None = None, referer: str | None = None, **kw) -> str:
        return self.get(url, params, referer, **kw).decode("utf-8", "replace")
