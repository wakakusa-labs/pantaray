"""Shared raw-source lifetime and HTTP/1 send authorization boundary."""

from collections.abc import AsyncIterator, Iterator
from contextlib import (
    AbstractAsyncContextManager,
    asynccontextmanager,
    contextmanager,
    nullcontext,
)
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TypedDict

import httpcore
import httpcore2
import httpx
import httpx2

from pantaray_agents.local_runtime.context.source_gate import (
    ActiveSource,
    SourceGate,
    SourceInvalidated,
)
from pantaray_llm.errors import ProviderError


class SourceTransportError(RuntimeError):
    """The HTTP dependency did not satisfy the required send trace contract."""


@dataclass(frozen=True)
class _SourceScope:
    gate: SourceGate
    source: ActiveSource


_source_scope: ContextVar[_SourceScope | None] = ContextVar("raw_source", default=None)


@contextmanager
def source_scope(gate: SourceGate, source: ActiveSource) -> Iterator[None]:
    """Bind only calls containing raw source; derived-only Action calls omit this."""
    token = _source_scope.set(_SourceScope(gate, source))
    try:
        yield
    finally:
        _source_scope.reset(token)


class _HeaderTrace:
    def __init__(self, scope: _SourceScope, max_requests: int) -> None:
        self.scope = scope
        self.guard: AbstractAsyncContextManager[None] | None = None
        self.max_requests = max_requests
        self.started = 0
        self.failure: SourceTransportError | None = None
        self.headers_finished = False

    def invalid(self) -> SourceTransportError:
        self.failure = SourceTransportError(
            "proxy HTTP/1 send trace contract was violated"
        )
        return self.failure

    async def release(self) -> None:
        if self.guard is not None:
            guard, self.guard = self.guard, None
            await guard.__aexit__(None, None, None)

    async def __call__(self, event: str, info: dict[str, object]) -> None:
        # httpcore's trace is an unstructured dependency boundary. Only the
        # request-bearing started event has a method; CONNECT inherits our trace.
        if event.startswith("http2."):
            raise self.invalid()
        if event == "http11.send_request_headers.started":
            request = info.get("request")
            if not isinstance(request, httpcore.Request | httpcore2.Request):
                raise self.invalid()
            if request.method == b"CONNECT":
                return
            if (
                request.method != b"POST"
                or self.guard is not None
                or self.started >= self.max_requests
            ):
                raise self.invalid()
            guard = self.scope.gate.guard(self.scope.source)
            await guard.__aenter__()
            self.guard = guard
            self.started += 1
            self.headers_finished = False
        elif (
            event
            in (
                "http11.send_request_headers.complete",
                "http11.send_request_headers.failed",
            )
            and self.guard is not None
        ):
            # httpcore may recover WriteError by reading a valid HTTP error
            # response. Both terminal events prove that the send guard finished.
            self.headers_finished = True
            await self.release()


class SourceRequestOptions(TypedDict, total=False):
    extensions: dict[str, object]


@asynccontextmanager
async def source_http_request(
    user_id: str | None,
    *,
    max_requests: int = 1,
) -> AsyncIterator[SourceRequestOptions]:
    """Track through client close; serialize headers and reject stale responses."""
    scope = _source_scope.get()
    if scope is not None and user_id != scope.source.binding.user_id:
        raise SourceInvalidated("request subject differs from context source")
    trace = _HeaderTrace(scope, max_requests) if scope is not None else None
    options: SourceRequestOptions = {}
    if trace is not None:
        options["extensions"] = {"trace": trace}
    tracking = scope.gate.track(scope.source) if scope is not None else nullcontext()
    async with tracking:
        try:
            yield options
            if trace is not None:
                # Missing trace is detected after I/O, not proof of non-transmission.
                # Pinned real-network tests protect this dependency boundary.
                if not trace.headers_finished:
                    raise trace.invalid()
                async with trace.scope.gate.guard(trace.scope.source):
                    pass
        except (
            httpx.HTTPError,
            httpx2.HTTPError,
            ProviderError,
            SourceTransportError,
        ) as exc:
            if trace is not None:
                # A provider failure must not authorize local fallback for a
                # revoked source. Release the send turn before checking again.
                await trace.release()
                async with trace.scope.gate.guard(trace.scope.source):
                    pass
                # SDK adapters normalize transport exceptions into ProviderError.
                # Keep a broken trace non-retryable even after that normalization.
                if trace.failure is not None and trace.failure is not exc:
                    raise trace.failure from exc
            raise
        finally:
            if trace is not None:
                await trace.release()


@asynccontextmanager
async def source_http_client(
    user_id: str,
    *,
    timeout: httpx.Timeout,
    max_requests: int = 1,
) -> AsyncIterator[httpx.AsyncClient]:
    """Lend a provider an HTTP client tracked through every send and client close."""
    async with source_http_request(user_id, max_requests=max_requests) as options:

        async def bind_trace(request: httpx.Request) -> None:
            request.extensions.update(options.get("extensions", {}))

        async with httpx.AsyncClient(
            timeout=timeout,
            http1=True,
            http2=False,
            follow_redirects=False,
            trust_env=True,
            event_hooks={"request": [bind_trace]},
        ) as client:
            yield client
