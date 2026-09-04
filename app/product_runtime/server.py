"""Standalone private-records application, not an unrestricted software generator."""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, time as daytime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
from typing import Any, AsyncIterator
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .contracts import DemoApp
from .db import Database
from .security import password_hash, token_hash, verify_password


@dataclass(frozen=True)
class Config:
    database_url: str
    origin: str
    production: bool = True

    def __post_init__(self) -> None:
        parsed = urlsplit(self.origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path
                or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise ValueError("APP_ORIGIN must be an exact origin without path or credentials")
        if self.production and (parsed.scheme != "https" or self.database_url.startswith("sqlite:")):
            raise ValueError("Production requires HTTPS and PostgreSQL")

    @classmethod
    def environment(cls) -> Config:
        return cls(os.environ.get("DATABASE_URL", "sqlite:///data/product.sqlite3"),
                   os.environ.get("APP_ORIGIN", "http://127.0.0.1:8000"),
                   os.environ.get("APP_ENV", "production") != "development")


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=12, max_length=128)


class RecordInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, str] = Field(max_length=8)
    version: int | None = Field(default=None, ge=1, le=2147483646)


def model_digest(model: DemoApp) -> str:
    return hashlib.sha256(json.dumps(model.model_dump(), sort_keys=True).encode()).hexdigest()


def create_app(config: Config, model: DemoApp, assets: Path | None = None) -> FastAPI:
    database = Database(config.database_url)
    digest = model_digest(model)
    assets = assets or Path(__file__).parent / "web"
    # Same KDF work for existing and unknown users; this is not an account.
    dummy_hash = password_hash(secrets.token_urlsafe(24))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            database.ready(digest)
            yield
        finally:
            database.close()

    app = FastAPI(title=model.name, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.database = database

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Default validation responses echo input, potentially including passwords.
        return JSONResponse({"detail": "Revise os campos informados."}, status_code=422)

    @app.middleware("http")
    async def boundary(request: Request, call_next: Any) -> Response:
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin") != config.origin:
                return JSONResponse({"detail": "Origem não autorizada."}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "Envie JSON."}, status_code=415)
            # Enforce streamed size too, not just an untrusted Content-Length.
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 16384:
                    return JSONResponse({"detail": "Corpo excede 16 KiB."}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response: Response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})
        if config.production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    def current_user(request: Request) -> dict[str, Any]:
        token = request.cookies.get("product_session", "")
        if not token or len(token) > 128:
            raise HTTPException(401, "Entre para continuar.")
        with database.connection() as db:
            row = db.execute("SELECT users.id, users.username FROM users JOIN sessions ON users.id=sessions.user_id WHERE token_hash=%s AND expires>%s",
                             (token_hash(token), int(time.time()))).fetchone()
        if row is None:
            raise HTTPException(401, "Sessão encerrada. Entre novamente.")
        return dict(row)

    def entity_fields(entity: str) -> Any:
        for item in model.entities:
            if item.id == entity:
                return item.fields
        raise HTTPException(404, "Cadastro não encontrado.")

    def validated(entity: str, body: RecordInput) -> dict[str, str]:
        fields = entity_fields(entity)
        if set(body.values) - {field.id for field in fields}:
            raise HTTPException(422, "Campo desconhecido.")
        values: dict[str, str] = {}
        for field in fields:
            value = body.values.get(field.id, "").strip()
            if len(value) > 300 or (field.required and not value):
                raise HTTPException(422, f"Revise o campo {field.label}.")
            if value:
                try:
                    if field.kind == "select" and value not in field.options:
                        raise ValueError()
                    if field.kind == "number" and not Decimal(value).is_finite():
                        raise ValueError()
                    if field.kind == "date" and (len(value) != 10 or date.fromisoformat(value).isoformat() != value):
                        raise ValueError()
                    if field.kind == "time" and (len(value) != 5 or daytime.fromisoformat(value).strftime("%H:%M") != value):
                        raise ValueError()
                except (ValueError, InvalidOperation):
                    raise HTTPException(422, f"Valor inválido em {field.label}.") from None
            values[field.id] = value
        return values

    def public_record(row: Any) -> dict[str, Any]:
        return {"id": row["id"], "version": row["version"], "values": json.loads(row["payload"])}

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/readyz")
    def ready() -> dict[str, str]:
        try:
            database.ready(digest)
        except Exception:
            raise HTTPException(503, "Banco ou modelo incompatível.") from None
        return {"status": "ready"}

    @app.post("/api/login")
    def login(body: Login, request: Request, response: Response) -> dict[str, str]:
        now = int(time.time())
        username = body.username.strip().lower()
        peer = request.client.host if request.client else "unknown"
        limited = False
        # Shared atomic counters work across API workers. Never trust forwarded IPs here.
        with database.connection() as db:
            for subject, limit in (("ip:" + peer, 30), ("user:" + username, 10)):
                bucket = token_hash(subject) + ":" + str(now // 900)
                count = db.execute("INSERT INTO login_attempts VALUES (%s,1,%s) ON CONFLICT(bucket) DO UPDATE SET count=login_attempts.count+1 RETURNING count",
                                   (bucket, (now // 900 + 1) * 900)).fetchone()["count"]
                limited = limited or count > limit
        if limited:
            raise HTTPException(429, "Muitas tentativas. Aguarde até 15 minutos.", headers={"Retry-After": "900"})
        with database.connection() as db:
            row = db.execute("SELECT * FROM users WHERE username=%s", (username,)).fetchone()
        valid = verify_password(body.password, row["password_hash"] if row else dummy_hash)
        if not valid or row is None:
            raise HTTPException(401, "Usuário ou senha inválidos.")
        token = secrets.token_urlsafe(32)
        with database.connection() as db:
            old = request.cookies.get("product_session", "")
            db.execute("DELETE FROM sessions WHERE token_hash=%s", (token_hash(old),))
            db.execute("INSERT INTO sessions VALUES (%s,%s,%s)", (token_hash(token), row["id"], now + 28800))
        response.set_cookie("product_session", token, httponly=True, secure=config.production,
                            samesite="strict", max_age=28800, path="/")
        return {"username": username}

    @app.post("/api/logout", status_code=204)
    def logout(request: Request, response: Response) -> None:
        with database.connection() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=%s", (token_hash(request.cookies.get("product_session", "")),))
        response.delete_cookie("product_session", path="/", httponly=True, secure=config.production, samesite="strict")

    @app.get("/api/me")
    def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        return {"username": user["username"]}

    @app.get("/api/model")
    def get_model(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        result = model.model_dump()
        for item in result["entities"]:
            item.pop("records", None)
        return result

    @app.get("/api/records/{entity}")
    def list_records(entity: str, user: dict[str, Any] = Depends(current_user),
                     q: str = Query(default="", max_length=100), limit: int = Query(default=50, ge=1, le=100),
                     offset: int = Query(default=0, ge=0, le=1000000)) -> dict[str, Any]:
        entity_fields(entity)
        needle = q.casefold().replace("!", "!!").replace("%", "!%").replace("_", "!_")
        with database.connection() as db:
            rows = db.execute("SELECT * FROM records WHERE user_id=%s AND entity=%s AND search_text LIKE %s ESCAPE '!' ORDER BY created,id LIMIT %s OFFSET %s",
                              (user["id"], entity, "%" + needle + "%", limit + 1, offset)).fetchall()
        return {"items": [public_record(row) for row in rows[:limit]], "has_more": len(rows) > limit}

    @app.post("/api/records/{entity}", status_code=201)
    def add_record(entity: str, body: RecordInput, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        values = validated(entity, body)
        record_id = str(uuid4())
        with database.connection() as db:
            db.execute("INSERT INTO records VALUES (%s,%s,%s,%s,%s,1,%s)",
                       (record_id, user["id"], entity, json.dumps(values), " ".join(values.values()).casefold(), time.time_ns() // 1000))
        return {"id": record_id, "version": 1, "values": values}

    def ensure_change(db: Any, count: int, entity: str, record_id: str, user_id: str) -> None:
        if count:
            return
        row = db.execute("SELECT id FROM records WHERE id=%s AND user_id=%s AND entity=%s", (record_id, user_id, entity)).fetchone()
        raise HTTPException(409 if row else 404, "Registro alterado em outra sessão. Atualize a lista." if row else "Registro não encontrado.")

    @app.put("/api/records/{entity}/{record_id}")
    def update_record(entity: str, record_id: UUID, body: RecordInput,
                      user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        values = validated(entity, body)
        if body.version is None:
            raise HTTPException(422, "Informe a versão do registro.")
        with database.connection() as db:
            changed = db.execute("UPDATE records SET payload=%s, search_text=%s, version=version+1 WHERE id=%s AND user_id=%s AND entity=%s AND version=%s",
                                 (json.dumps(values), " ".join(values.values()).casefold(), str(record_id), user["id"], entity, body.version))
            ensure_change(db, changed.rowcount, entity, str(record_id), user["id"])
        return {"id": str(record_id), "version": body.version + 1, "values": values}

    @app.delete("/api/records/{entity}/{record_id}", status_code=204)
    def delete_record(entity: str, record_id: UUID, version: int = Query(ge=1, le=2147483647),
                      user: dict[str, Any] = Depends(current_user)) -> None:
        entity_fields(entity)
        with database.connection() as db:
            changed = db.execute("DELETE FROM records WHERE id=%s AND user_id=%s AND entity=%s AND version=%s",
                                 (str(record_id), user["id"], entity, version))
            ensure_change(db, changed.rowcount, entity, str(record_id), user["id"])

    @app.get("/api/export")
    def export(user: dict[str, Any] = Depends(current_user)) -> Response:
        with database.connection() as db:
            rows = db.execute("SELECT * FROM records WHERE user_id=%s ORDER BY created,id LIMIT 1001", (user["id"],)).fetchall()
        if len(rows) > 1000:
            raise HTTPException(413, "Exportação limitada a 1000 registros. Use a API paginada.")
        result = {item.id: [json.loads(row["payload"]) for row in rows if row["entity"] == item.id] for item in model.entities}
        return JSONResponse(result, headers={"Content-Disposition": 'attachment; filename="dados.json"'})

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(assets / "index.html")

    @app.get("/assets/{name}")
    def asset(name: str) -> FileResponse:
        if name not in {"app.js", "app.css"}:
            raise HTTPException(404)
        return FileResponse(assets / name)

    return app


def from_environment() -> FastAPI:
    model = DemoApp.model_validate_json(Path("model.json").read_text())
    return create_app(Config.environment(), model)
