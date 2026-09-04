import hashlib
import io
import json
import os
import subprocess
import sys
import time
from urllib.parse import quote
from uuid import uuid4
import zipfile

import httpx
import pytest

from app.agents.product_package import fullstack_archive
from app.models.product import DemoApp
from app.product_runtime.db import Database
from app.product_runtime.security import create_user, token_hash
from app.product_runtime.server import Config, create_app, model_digest

MODEL = DemoApp.model_validate({"name":"Agenda persistente", "description":"Agenda da recepção", "theme":"ocean",
    "entities":[{"id":"appointments", "name":"Agendamentos", "fields":[
        {"id":"client", "label":"Cliente", "kind":"text", "required":True},
        {"id":"date", "label":"Data", "kind":"date", "required":True},
        {"id":"time", "label":"Horário", "kind":"time", "required":True},
        {"id":"price", "label":"Preço", "kind":"number", "required":False},
        {"id":"status", "label":"Status", "kind":"select", "required":True, "options":["Marcado","Concluído"]}],
        "records":[]}]})
VALUES = {"client":"Cliente de teste", "date":"2026-09-04", "time":"14:30", "price":"25.50", "status":"Marcado"}
PASSWORD = "only-for-disposable-tests-123"
ORIGIN = "http://test"


@pytest.fixture(params=["sqlite", "postgres"])
def configured(request, tmp_path):
    admin = None
    schema = None
    if request.param == "postgres":
        url = os.environ.get("PRODUCT_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("Set PRODUCT_TEST_POSTGRES_URL to an isolated PostgreSQL test database")
        import psycopg
        from psycopg import sql
        admin = psycopg.connect(url, autocommit=True)
        schema = "product_test_" + uuid4().hex
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        url += ("&" if "?" in url else "?") + "options=" + quote("-csearch_path=" + schema)
    else:
        url = "sqlite:///" + str(tmp_path / "runtime.sqlite3")
    config = Config(url, ORIGIN, False)
    db = Database(url)
    db.migrate(model_digest(MODEL))
    create_user(db, "alice", PASSWORD)
    create_user(db, "bob", PASSWORD)
    yield config, db
    db.close()
    if admin:
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.close()


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN,
                             headers={"Origin": ORIGIN, "Content-Type":"application/json"})


async def login(client, username="alice"):
    response = await client.post("/api/login", json={"username":username, "password":PASSWORD})
    assert response.status_code == 200, response.text
    return response


@pytest.mark.asyncio
async def test_persistent_private_crud_conflicts_and_restart(configured):
    config, db = configured
    app, second = create_app(config, MODEL), create_app(config, MODEL)
    try:
        async with client_for(app) as alice, client_for(second) as bob:
            assert (await alice.get("/api/model")).status_code == 401
            cookie = (await login(alice)).headers["set-cookie"]
            assert "HttpOnly" in cookie and "SameSite=strict" in cookie
            await login(bob, "bob")
            created = await alice.post("/api/records/appointments", json={"values":VALUES})
            assert created.status_code == 201
            record = created.json()
            path = "/api/records/appointments/" + record["id"]
            assert (await bob.get("/api/records/appointments")).json()["items"] == []
            assert (await bob.get("/api/export")).json()["appointments"] == []
            assert (await bob.put(path,json={"values":VALUES,"version":1})).status_code == 404
            assert (await bob.delete(path+"?version=1")).status_code == 404
            saved = await alice.put(path,json={"values":{**VALUES,"client":"Editado"},"version":1})
            assert saved.json()["version"] == 2
            assert (await alice.put(path,json={"values":VALUES,"version":1})).status_code == 409
            assert (await alice.delete(path+"?version=1")).status_code == 409
            assert len((await alice.get("/api/records/appointments?q=editado")).json()["items"]) == 1
            assert (await alice.get("/api/records/appointments?q=%25")).json()["items"] == []
            # A new process/pool can use the existing opaque session and persisted data.
            async with client_for(second) as restored:
                restored.cookies.update(alice.cookies)
                result = await restored.get("/api/export")
                assert result.json()["appointments"][0]["client"] == "Editado"
                assert (await restored.delete(path+"?version=2")).status_code == 204
                assert (await restored.get("/api/records/appointments")).json()["items"] == []
    finally:
        app.state.database.close()
        second.state.database.close()


@pytest.mark.asyncio
async def test_session_expiry_logout_origin_validation_and_limits(configured):
    config, db = configured
    app = create_app(config, MODEL)
    try:
        async with client_for(app) as client:
            assert (await client.post("/api/login",json={"username":"alice","password":PASSWORD},headers={"Origin":"https://attacker.invalid"})).status_code == 403
            await login(client)
            raw_token = client.cookies.get("product_session")
            with db.connection() as conn:
                row = conn.execute("SELECT token_hash FROM sessions").fetchone()
                assert row["token_hash"] == token_hash(raw_token) and row["token_hash"] != raw_token
            for update in ({"date":"2026-02-30"},{"time":"25:00"},{"price":"NaN"},{"status":"Unknown"},{"client":" "},{"surprise":"x"}):
                assert (await client.post("/api/records/appointments",json={"values":{**VALUES,**update}})).status_code == 422
            assert (await client.post("/api/records/appointments",json={"values":VALUES},headers={"Origin":"https://attacker.invalid"})).status_code == 403
            assert (await client.post("/api/records/appointments",content=b"x"*17000)).status_code == 413
            assert (await client.get("/api/records/appointments?limit=101")).status_code == 422
            assert (await client.get("/api/records/missing")).status_code == 404
            assert (await client.post("/api/logout",json={})).status_code == 204
            client.cookies.set("product_session",raw_token)
            assert (await client.get("/api/me")).status_code == 401
            await login(client)
            with db.connection() as conn:
                conn.execute("UPDATE sessions SET expires=0")
            assert (await client.get("/api/me")).status_code == 401
            response = await client.post("/api/login", json={"username":"alice","password":"short-secret"[:8]})
            assert response.status_code == 422 and "short" not in response.text
            assert (await client.get("/readyz")).status_code == 200
    finally:
        app.state.database.close()


@pytest.mark.asyncio
async def test_shared_throttle_and_atomic_edit(configured):
    import asyncio
    config, db = configured
    first, second = create_app(config, MODEL), create_app(config, MODEL)
    try:
        async with client_for(first) as a, client_for(second) as b:
            await login(a)
            b.cookies.update(a.cookies)
            record = (await a.post("/api/records/appointments",json={"values":VALUES})).json()
            path = "/api/records/appointments/" + record["id"]
            results = await asyncio.gather(a.put(path,json={"values":VALUES,"version":1}),b.put(path,json={"values":VALUES,"version":1}))
            assert sorted(r.status_code for r in results) == [200,409]
            # Seed the shared counter just below its limit to avoid 10 expensive KDFs.
            bucket=token_hash("user:alice")+":"+str(int(time.time())//900)
            with db.connection() as conn:
                conn.execute("UPDATE login_attempts SET count=10 WHERE bucket=%s", (bucket,))
            assert (await b.post("/api/login",json={"username":"alice","password":PASSWORD})).status_code == 429
    finally:
        first.state.database.close()
        second.state.database.close()


def test_schema_model_drift_and_production_config(configured):
    config, db = configured
    db.migrate(model_digest(MODEL))
    db.ready(model_digest(MODEL))
    changed = MODEL.model_copy(update={"name":"Unexpected change"})
    with pytest.raises(ValueError, match="mismatch"):
        db.migrate(model_digest(changed))
    with pytest.raises(ValueError):
        Config(config.database_url, "http://test", True)
    with pytest.raises(ValueError):
        Config("sqlite:///local.db", "https://test", True)


def test_export_is_independent_versioned_and_without_runtime_data(tmp_path):
    model = MODEL.model_dump()
    model["entities"][0]["records"] = [{"values":list(VALUES.values())}]
    package = fullstack_archive({"app":model,"brief":{"name":"Approved"}, "secret":"must-not-export"})
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        assert "runtime/server.py" in archive.namelist()
        manifest=json.loads(archive.read("manifest.json"))
        for path, digest in manifest["files_sha256"].items():
            assert hashlib.sha256(archive.read(path)).hexdigest() == digest
        assert json.loads(archive.read("model.json"))["entities"][0]["records"] == []
        assert all("must-not-export" not in archive.read(name).decode() for name in archive.namelist())
        archive.extractall(tmp_path)
    env = {**os.environ,"APP_ENV":"development","APP_ORIGIN":ORIGIN,"DATABASE_URL":"sqlite:///"+str(tmp_path/"db.sqlite3"),"PYTHONPATH":str(tmp_path)}
    result=subprocess.run([sys.executable,"-m","runtime.manage","migrate"],cwd=tmp_path,env=env,capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
    result=subprocess.run([sys.executable,"-c","from runtime.server import from_environment; a=from_environment(); print(a.title); a.state.database.close()"],cwd=tmp_path,env=env,capture_output=True,text=True)
    assert result.returncode == 0 and MODEL.name in result.stdout, result.stderr
