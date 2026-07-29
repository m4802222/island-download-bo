"""Small standard-library HTTP transport with explicit errors."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    headers: Any

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


def fetch(
    url: str,
    *,
    form: Mapping[str, Any] | None = None,
    json_body: Any = None,
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: int = 40,
    opener: Any = None,
) -> Response:
    supplied = sum(value is not None for value in (form, json_body, body))
    if supplied > 1:
        raise ValueError("form、json_body、body 只能使用一种")
    request_headers = dict(headers or {})
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
    elif json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False).encode()
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, body, request_headers)
    try:
        response = (
            opener.open(request, timeout=timeout)
            if opener is not None
            else urllib.request.urlopen(request, timeout=timeout)
        )
        return Response(response.status, response.read(), response.headers)
    except urllib.error.HTTPError as exc:
        return Response(exc.code, exc.read(), exc.headers)
    except Exception as exc:
        return Response(599, str(exc).encode(), {})


def require_ok(response: Response, action: str) -> Response:
    if response.status >= 300:
        detail = response.text.strip()[:180]
        raise RuntimeError(f"{action}失败（HTTP {response.status}）{detail}")
    return response
