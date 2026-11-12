"""간단한 HTTP GET — 요청 간격 유지 + 재시도. 표준 라이브러리만 사용."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import USER_AGENT


class Fetcher:
    def __init__(self, min_interval: float = 0.5, timeout: float = 20.0, retries: int = 2) -> None:
        self.min_interval = min_interval
        self.timeout = timeout
        self.retries = retries
        self._last = 0.0

    def get(self, url: str, params: dict | None = None, referer: str | None = None) -> bytes:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"}
        if referer:
            headers["Referer"] = referer
        err: Exception | None = None
        for attempt in range(self.retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read()
            except (urllib.error.URLError, TimeoutError) as e:
                err = e
                if isinstance(e, urllib.error.HTTPError) and 400 <= e.code < 500 and e.code != 429:
                    break
                time.sleep(1.5 * (attempt + 1))
            finally:
                self._last = time.monotonic()
        raise RuntimeError(f"GET 실패: {url} ({err})")

    def get_json(self, url: str, params: dict | None = None, referer: str | None = None):
        return json.loads(self.get(url, params, referer).decode("utf-8"))

    def get_text(self, url: str, params: dict | None = None, referer: str | None = None) -> str:
        return self.get(url, params, referer).decode("utf-8", "replace")
