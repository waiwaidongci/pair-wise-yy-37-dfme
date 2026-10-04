from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import parse_qs, urlparse

from .domain import (ConflictError, DomainError, NotFoundError, PermissionDenied,
                     ValidationError)
from .service import Service


def make_handler(service: Service, static_dir: str):
    root = Path(static_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ModularHell/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, path: Path) -> None:
            if not path.exists():
                self._json(404, {"error": "not_found"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _identity(self) -> Tuple[str, str]:
            return self.headers.get("X-Actor", ""), self.headers.get("X-Role", "")

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            if length > 2_000_000:
                raise ValidationError("请求体过大")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体不是有效JSON") from exc
            if not isinstance(value, dict):
                raise ValidationError("请求体必须是JSON对象")
            return value

        def _send_error(self, exc: Exception) -> None:
            if isinstance(exc, ValidationError):
                status = 422
            elif isinstance(exc, NotFoundError):
                status = 404
            elif isinstance(exc, PermissionDenied):
                status = 403
            elif isinstance(exc, ConflictError):
                status = 409
            elif isinstance(exc, ValueError):
                status = 422
            elif isinstance(exc, DomainError):
                status = 400
            else:
                status = 500
            payload: Dict[str, Any] = {"error": exc.__class__.__name__,
                                       "message": str(exc)}
            context = getattr(exc, "context", None)
            if context:
                payload["context"] = context
            self._json(status, payload)

        @staticmethod
        def _int_id(value: str) -> int:
            try:
                result = int(value)
            except (TypeError, ValueError) as exc:
                raise NotFoundError("资源不存在") from exc
            if result < 1:
                raise NotFoundError("资源不存在")
            return result

        def do_GET(self) -> None:
            try:
                path = urlparse(self.path).path
                query = parse_qs(urlparse(self.path).query)
                if path == "/health":
                    self._json(200, {"status": "ok"})
                elif path == "/":
                    self._html(root / "index.html")
                elif path == "/api/items":
                    actor, role = self._identity()
                    del actor
                    status = query.get("status", [None])[0]
                    self._json(200, {"items": service.list_items(role, status)})
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = self._int_id(path.split("/")[3])
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"records": service.list_records(item_id, role)})
                elif path.startswith("/api/items/") and path.endswith("/quota"):
                    item_id = self._int_id(path.split("/")[3])
                    actor, role = self._identity()
                    del actor
                    self._json(200, service.quota(item_id, role))
                elif path.startswith("/api/items/") and "/records/" in path:
                    # GET 仅用于占位，记录通过集合接口查看
                    self._json(404, {"error": "not_found"})
                elif path.startswith("/api/items/"):
                    item_id = self._int_id(path.rsplit("/", 1)[-1])
                    actor, role = self._identity()
                    del actor
                    self._json(200, service.get_item(item_id, role))
                elif path == "/api/transfers":
                    actor, role = self._identity()
                    del actor
                    item_param = query.get("item_id", [None])[0]
                    item_id = self._int_id(item_param) if item_param else None
                    self._json(200, {"transfers": service.list_transfers(role, item_id)})
                elif path.startswith("/api/transfers/"):
                    transfer_id = self._int_id(path.rsplit("/", 1)[-1])
                    actor, role = self._identity()
                    del actor
                    self._json(200, service.get_transfer(transfer_id, role))
                elif path == "/api/quota-ledger":
                    actor, role = self._identity()
                    del actor
                    item_param = query.get("item_id", [None])[0]
                    item_id = self._int_id(item_param) if item_param else None
                    self._json(200, {"entries": service.list_ledger(role, item_id)})
                elif path == "/api/audit":
                    actor, role = self._identity()
                    del actor
                    entity_param = query.get("entity_id", [None])[0]
                    entity_id = self._int_id(entity_param) if entity_param else None
                    self._json(200, {"events": service.audit(role, entity_id)})
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                actor, role = self._identity()
                body = self._body()
                if path == "/api/items":
                    self._json(201, service.create_item(body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = self._int_id(path.split("/")[3])
                    self._json(201, service.add_record(item_id, body, actor, role))
                elif path.startswith("/api/items/") and "/records/" in path \
                        and path.endswith("/close"):
                    parts = path.strip("/").split("/")
                    item_id = self._int_id(parts[2])
                    record_id = self._int_id(parts[4])
                    self._json(200, service.close_record(
                        item_id, record_id, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/transition"):
                    item_id = self._int_id(path.split("/")[3])
                    target = body.get("target")
                    expected = body.get("expected_version")
                    self._json(200, service.transition(
                        item_id, target, expected, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/quantity"):
                    item_id = self._int_id(path.split("/")[3])
                    self._json(200, service.update_quantity(
                        item_id, body, actor, role))
                elif path == "/api/transfers":
                    self._json(201, service.accept_transfer(body, actor, role))
                elif path.startswith("/api/transfers/") and path.endswith("/confirm"):
                    transfer_id = self._int_id(path.split("/")[3])
                    self._json(200, service.confirm_transfer(
                        transfer_id, actor, role))
                elif path.startswith("/api/transfers/") and path.endswith("/release"):
                    transfer_id = self._int_id(path.split("/")[3])
                    self._json(200, service.release_transfer(
                        transfer_id, actor, role, body))
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

    return Handler
