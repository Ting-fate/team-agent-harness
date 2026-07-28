from collections.abc import Callable
from contextlib import asynccontextmanager
from ipaddress import ip_address
from pathlib import Path
import re
from typing import Any, get_args
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.api import create_api_router, create_harness_state
from app.core.browser_tools import BrowserToolProvider
from app.core.runner import AgentExecutor
from app.core.web_tools import WebToolProvider


_TRUSTED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_HOSTNAME_PATTERN = re.compile(r"^[A-Za-z0-9.-]+$")
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_MAX_VALIDATION_ERRORS = 32
_MAX_VALIDATION_LOC_DEPTH = 8
_VALIDATION_SOURCES = frozenset({"body", "query", "path", "header", "cookie"})
_VALIDATION_TYPE_PATTERN = re.compile(r"^[a-z0-9_.]{1,64}$")
_UI_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
        "form-action 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def _parse_authority(value: str) -> tuple[str, int | None] | None:
    if not value or value != value.strip() or any(char in value for char in "/\\?#@"):
        return None

    port: int | None = None
    if value.startswith("["):
        closing_bracket = value.find("]")
        if closing_bracket <= 1:
            return None
        host = value[1:closing_bracket]
        remainder = value[closing_bracket + 1 :]
        if remainder:
            if not remainder.startswith(":") or not remainder[1:].isdigit():
                return None
            port = int(remainder[1:])
        try:
            parsed_ip = ip_address(host)
        except ValueError:
            return None
        if parsed_ip.version != 6:
            return None
        host = parsed_ip.compressed
    else:
        if value.count(":") > 1:
            return None
        host, separator, port_text = value.partition(":")
        if separator:
            if not port_text.isdigit():
                return None
            port = int(port_text)
        if not host or not _HOSTNAME_PATTERN.fullmatch(host):
            return None
        try:
            host = ip_address(host).compressed
        except ValueError:
            host = host.lower()

    if port is not None and not 1 <= port <= 65535:
        return None
    return host, port


def _is_same_origin(request: Request, origin: str) -> bool:
    try:
        parsed_origin = urlsplit(origin)
    except ValueError:
        return False
    scheme = parsed_origin.scheme.lower()
    if (
        scheme not in _DEFAULT_PORTS
        or not parsed_origin.netloc
        or parsed_origin.path
        or parsed_origin.query
        or parsed_origin.fragment
    ):
        return False

    origin_authority = _parse_authority(parsed_origin.netloc)
    request_authority = _parse_authority(request.headers.get("host", ""))
    request_scheme = request.url.scheme.lower()
    if origin_authority is None or request_authority is None or request_scheme not in _DEFAULT_PORTS:
        return False

    origin_host, origin_port = origin_authority
    request_host, request_port = request_authority
    return (
        scheme == request_scheme
        and origin_host == request_host
        and (origin_port or _DEFAULT_PORTS[scheme])
        == (request_port or _DEFAULT_PORTS[request_scheme])
    )


def create_app(
    db_path: str | Path = "data/harness.sqlite3",
    artifact_root: str | Path = "data/artifacts",
    executor_factory: Callable[[], AgentExecutor] | None = None,
    config_root: str | Path | None = None,
    web_tool_provider: WebToolProvider | None = None,
    browser_tool_provider: BrowserToolProvider | None = None,
    skill_roots_override: list[str | Path] | None = None,
) -> FastAPI:
    inferred_config_root = config_root
    if inferred_config_root is None and Path(db_path).is_absolute():
        inferred_config_root = Path(db_path).parent
    state = create_harness_state(
        db_path,
        artifact_root,
        executor_factory,
        config_root=inferred_config_root,
        web_tool_provider=web_tool_provider,
        browser_tool_provider=browser_tool_provider,
        skill_roots_override=skill_roots_override,
    )
    api_router = create_api_router(state)
    validation_location_fields = _request_validation_field_names(api_router)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            state.start()
            yield
        finally:
            state.close()

    app = FastAPI(title="Team Agent Harness", version="0.1.0", lifespan=lifespan)
    app.state.harness = state

    @app.exception_handler(RequestValidationError)
    async def sanitized_request_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        details = []
        for error in errors[:_MAX_VALIDATION_ERRORS]:
            raw_type = str(error.get("type", "validation_error"))
            error_type = raw_type if _VALIDATION_TYPE_PATTERN.fullmatch(raw_type) else "validation_error"
            details.append(
                {
                    "type": error_type,
                    "loc": _safe_validation_location(
                        error.get("loc", ()),
                        allowed_fields=validation_location_fields,
                    ),
                    "msg": "Invalid request.",
                }
            )
        if len(errors) > _MAX_VALIDATION_ERRORS:
            details.append(
                {
                    "type": "too_many_errors",
                    "loc": ["request"],
                    "msg": "Additional validation errors omitted.",
                }
            )
        return JSONResponse(status_code=422, content={"detail": details})

    @app.middleware("http")
    async def enforce_local_request_boundary(request: Request, call_next):
        authority = _parse_authority(request.headers.get("host", ""))
        test_client_host = (
            authority is not None
            and authority[0] == "testserver"
            and request.client is not None
            and request.client.host == "testclient"
        )
        if authority is None or (authority[0] not in _TRUSTED_HOSTS and not test_client_host):
            return PlainTextResponse("Invalid host header", status_code=400)

        if request.headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
            return PlainTextResponse("Cross-site request rejected", status_code=403)

        origin = request.headers.get("origin")
        if request.method in _MUTATING_METHODS and origin is not None:
            if not _is_same_origin(request, origin):
                return PlainTextResponse("Cross-origin mutation rejected", status_code=403)

        return await call_next(request)

    @app.get("/health")
    def health() -> JSONResponse:
        worker = state.run_worker
        if worker is None or not worker.is_running:
            return JSONResponse(
                status_code=503,
                content={"status": "degraded", "worker": "stopped"},
            )
        return JSONResponse(content={"status": "ok", "worker": "running"})

    app.include_router(api_router)
    static_dir = Path(__file__).parent / "static"

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html", headers=_UI_SECURITY_HEADERS)

    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    return app


def _utf8_safe_text(value: object) -> str:
    return str(value).encode("utf-8", errors="replace").decode("utf-8")


def _request_validation_field_names(router: object) -> frozenset[str]:
    names: set[str] = set()
    visited_models: set[type[BaseModel]] = set()

    def collect_model(annotation: Any) -> None:
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            if annotation in visited_models:
                return
            visited_models.add(annotation)
            for field_name, field in annotation.model_fields.items():
                names.add(str(field.alias or field_name))
                collect_model(field.annotation)
            return
        for argument in get_args(annotation):
            collect_model(argument)

    for route in getattr(router, "routes", ()):  # FastAPI APIRoute objects.
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        for group_name in ("body_params", "query_params", "path_params", "header_params", "cookie_params"):
            for parameter in getattr(dependant, group_name, ()):
                alias = getattr(parameter, "alias", None)
                if isinstance(alias, str):
                    names.add(alias)
                collect_model(getattr(parameter, "type_", None))
                collect_model(getattr(getattr(parameter, "field_info", None), "annotation", None))
    return frozenset(names)


def _safe_validation_location(value: object, *, allowed_fields: frozenset[str]) -> list[str | int]:
    items = value if isinstance(value, (list, tuple)) else ()
    safe: list[str | int] = []
    for index, item in enumerate(items[:_MAX_VALIDATION_LOC_DEPTH]):
        if isinstance(item, int) and not isinstance(item, bool):
            safe.append(item)
        elif isinstance(item, str) and (
            (index == 0 and item in _VALIDATION_SOURCES) or item in allowed_fields
        ):
            safe.append(item)
        else:
            safe.append("[field]")
    return safe or ["request"]


app = create_app()
