"""Explicit lifecycle operations; passwords are never command-line arguments."""
from __future__ import annotations

import argparse
from getpass import getpass
from pathlib import Path
import time

from .contracts import DemoApp
from .db import Database
from .security import create_user, password_hash
from .server import Config, model_digest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["migrate", "create-user", "reset-password", "cleanup"])
    parser.add_argument("--username")
    args = parser.parse_args()
    model = DemoApp.model_validate_json(Path("model.json").read_text())
    database = Database(Config.environment().database_url)
    try:
        if args.command == "migrate":
            database.migrate(model_digest(model))
        else:
            database.ready(model_digest(model))
            if args.command == "cleanup":
                with database.connection() as db:
                    db.execute("DELETE FROM sessions WHERE expires<=%s", (int(time.time()),))
                    db.execute("DELETE FROM login_attempts WHERE expires<=%s", (int(time.time()),))
            else:
                username = (args.username or input("Usuário: ")).strip().lower()
                password = getpass("Senha (12–128 caracteres): ")
                if password != getpass("Repita a senha: "):
                    raise SystemExit("Senhas não coincidem; nada alterado.")
                if args.command == "create-user":
                    create_user(database, username, password)
                else:
                    encoded = password_hash(password)
                    with database.connection() as db:
                        row = db.execute("SELECT id FROM users WHERE username=%s", (username,)).fetchone()
                        if row is None:
                            raise SystemExit("Usuário não encontrado.")
                        db.execute("UPDATE users SET password_hash=%s WHERE id=%s", (encoded, row["id"]))
                        db.execute("DELETE FROM sessions WHERE user_id=%s", (row["id"],))
        print("Operação concluída.")
    finally:
        database.close()


if __name__ == "__main__":
    main()
