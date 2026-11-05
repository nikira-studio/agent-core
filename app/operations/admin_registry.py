"""Admin operations an agent may propose and a person may approve.

Each operation is a named, allowlisted wrapper around an existing route
handler. Approving one calls that handler in-process with the approving
person as the caller, so validation, audit and side effects are exactly those
of the person doing it in the dashboard. Agents never reach the handler
themselves; they only submit a proposal naming an operation and its params.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from pydantic import BaseModel, ValidationError

from app.security.exceptions import APIError


class OperationFailed(Exception):
    """The handler refused or failed; the whole bundle is rolled back."""

    def __init__(self, operation: str, status: int, body: Any):
        super().__init__(f"{operation} failed with {status}")
        self.operation = operation
        self.status = status
        self.body = body


@dataclass(frozen=True)
class AdminOperation:
    name: str
    summary: str
    handler: Callable[..., Any]
    # Params the person types on the approval page, never the agent (secret
    # values). A proposal that includes one is refused.
    person_fields: tuple[str, ...] = ()
    # Returns a fingerprint of the target's current state, or None when the
    # target does not exist. Checked at proposal time and again before running,
    # so a target changed in between fails the bundle instead of being
    # overwritten.
    fingerprint: Optional[Callable[[dict], Optional[str]]] = None
    path_params: tuple[str, ...] = field(default=())
    body_model: Optional[type[BaseModel]] = None
    body_arg: Optional[str] = None


_REGISTRY: dict[str, AdminOperation] = {}


def _introspect(handler: Callable[..., Any]) -> tuple[tuple[str, ...], Optional[type[BaseModel]], Optional[str]]:
    path_params: list[str] = []
    body_model = body_arg = None
    for name, param in inspect.signature(handler).parameters.items():
        annotation = _unwrap_optional(param.annotation)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            body_model, body_arg = annotation, name
        elif annotation is str and param.default is inspect.Parameter.empty:
            path_params.append(name)
    return tuple(path_params), body_model, body_arg


def _unwrap_optional(annotation: Any) -> Any:
    """`Model | None` -> Model."""
    import typing

    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    return args[0] if len(args) == 1 else annotation


def register(name: str, summary: str, handler: Callable[..., Any], *,
             person_fields: tuple[str, ...] = (),
             fingerprint: Optional[Callable[[dict], Optional[str]]] = None) -> AdminOperation:
    path_params, body_model, body_arg = _introspect(handler)
    op = AdminOperation(name, summary, handler, person_fields, fingerprint, path_params, body_model, body_arg)
    _REGISTRY[name] = op
    return op


def get(name: str) -> Optional[AdminOperation]:
    _load()
    return _REGISTRY.get(name)


def names() -> list[str]:
    _load()
    return sorted(_REGISTRY)


def validate(name: str, params: dict, *, allow_person_fields: bool = False) -> AdminOperation:
    """Check a proposed operation's name and params without running it."""
    op = get(name)
    if op is None:
        raise APIError("UNKNOWN_OPERATION", f"No approvable operation named {name!r}", 400)
    if not isinstance(params, dict):
        raise APIError("INVALID_PARAMS", f"{name}: params must be an object", 400)
    supplied = [f for f in op.person_fields if params.get(f) not in (None, "")]
    if supplied and not allow_person_fields:
        raise APIError("PERSON_FIELD", f"{name}: {', '.join(supplied)} is entered by the approving person, not proposed", 400)
    missing = [p for p in op.path_params if not isinstance(params.get(p), str) or not params.get(p)]
    if missing:
        raise APIError("INVALID_PARAMS", f"{name}: missing {', '.join(missing)}", 400)
    if op.body_model is not None:
        body = {k: v for k, v in params.items() if k not in op.path_params}
        if not allow_person_fields:
            body.update({f: _PLACEHOLDER for f in op.person_fields if f in op.body_model.model_fields})
        try:
            op.body_model.model_validate(body)
        except ValidationError as exc:
            raise APIError("INVALID_PARAMS", f"{name}: {exc.errors()[0]['msg']}", 400) from None
    return op


_PLACEHOLDER = "entered-on-approval"


def _call(op: AdminOperation, params: dict, session: dict, request: Any) -> Any:
    from app.security.context import build_user_context
    from app.security.effective_authority import EffectiveAuthority, permanent_authority
    from fastapi import Request

    kwargs: dict[str, Any] = {}
    for name, param in inspect.signature(op.handler).parameters.items():
        annotation = param.annotation
        if name in op.path_params:
            kwargs[name] = params[name]
        elif name == op.body_arg:
            kwargs[name] = op.body_model.model_validate(
                {k: v for k, v in params.items() if k not in op.path_params})
        elif annotation is Request or annotation == "Request":
            kwargs[name] = request
        elif annotation is EffectiveAuthority or annotation == "EffectiveAuthority":
            kwargs[name] = permanent_authority(build_user_context(session))
        elif param.default is not inspect.Parameter.empty and not _is_dependency(param.default):
            continue
        else:
            # Session-style dependencies (require_admin, get_current_session,
            # require_auth) all resolve to the person's session.
            kwargs[name] = session
    result = op.handler(**kwargs)
    if inspect.iscoroutine(result):
        result = asyncio.run(result)
    return result


def _is_dependency(default: Any) -> bool:
    return type(default).__name__ == "Depends" or hasattr(default, "dependency")


def run(name: str, params: dict, session: dict, request: Any) -> dict:
    """Run one operation as the person in `session`; raise OperationFailed on refusal."""
    op = validate(name, params, allow_person_fields=True)
    try:
        response = _call(op, params, session, request)
    except APIError as exc:
        raise OperationFailed(name, exc.status_code, {"code": exc.code, "message": exc.message}) from None
    status = getattr(response, "status_code", 200)
    body: Any = None
    raw = getattr(response, "body", None)
    if raw:
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
    if status >= 400:
        raise OperationFailed(name, status, (body or {}).get("error") if isinstance(body, dict) else body)
    return {"status": status, "data": (body or {}).get("data") if isinstance(body, dict) else None}


_loaded = False


def _load() -> None:
    global _loaded
    if not _loaded:
        _loaded = True
        from app.operations import admin_operations  # noqa: F401  (registers on import)
