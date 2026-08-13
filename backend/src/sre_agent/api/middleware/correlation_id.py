import re
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from uuid import uuid4

_CORRELATION_HEADER = b"x-correlation-id"
_SAFE_CORRELATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")

AsgiMessage = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[AsgiMessage]]
Send = Callable[[AsgiMessage], Awaitable[None]]
AsgiApp = Callable[[MutableMapping[str, Any], Receive, Send], Awaitable[None]]


class CorrelationIdMiddleware:
    def __init__(self, app: AsgiApp) -> None:
        self._app = app

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        correlation_id = _correlation_id_from(scope.get("headers", []))
        state = scope.setdefault("state", {})
        state["correlation_id"] = correlation_id

        async def send_with_correlation_id(message: AsgiMessage) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != _CORRELATION_HEADER
                ]
                headers.append((_CORRELATION_HEADER, correlation_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        await self._app(scope, receive, send_with_correlation_id)


def _correlation_id_from(headers: list[tuple[bytes, bytes]]) -> str:
    for name, value in headers:
        if name.lower() != _CORRELATION_HEADER:
            continue
        try:
            candidate = value.decode("ascii")
        except UnicodeDecodeError:
            break
        if _SAFE_CORRELATION_ID.fullmatch(candidate):
            return candidate
        break
    return str(uuid4())
