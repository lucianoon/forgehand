"""A suíte não pode herdar o .env do operador (regressão de 03/09/2026:
AUDIT_LOG_PATH em /srv derrubou dez testes de API fora da CI)."""

from app.infrastructure.settings import Settings, resolve_env_file


def test_suite_runs_with_env_file_disabled() -> None:
    # conftest define FORGEHAND_ENV_FILE="" antes de importar app.*
    assert Settings.model_config.get("env_file") is None


def test_resolve_env_file_defaults_to_dotenv_and_honours_override(monkeypatch) -> None:
    monkeypatch.delenv("FORGEHAND_ENV_FILE", raising=False)
    assert resolve_env_file() == ".env"

    monkeypatch.setenv("FORGEHAND_ENV_FILE", "")
    assert resolve_env_file() is None

    monkeypatch.setenv("FORGEHAND_ENV_FILE", "/etc/forgehand/.env.prod")
    assert resolve_env_file() == "/etc/forgehand/.env.prod"


def test_operator_dotenv_in_cwd_does_not_leak_into_settings(tmp_path, monkeypatch) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("APP_NAME=vazou-do-operador\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APP_NAME", raising=False)

    assert Settings().app_name == "forgehand"
    # Controle positivo: o mesmo arquivo é lido quando pedido explicitamente.
    assert Settings(_env_file=dotenv).app_name == "vazou-do-operador"
