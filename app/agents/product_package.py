"""Export trusted runtime files and declarative data, never model-authored paths."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any
import zipfile

from app.models.product import DemoApp

RUNTIME_VERSION = "1.0.0"
RUNTIME_FILES = ("__init__.py", "db.py", "security.py", "server.py", "manage.py",
                 "web/index.html", "web/app.js", "web/app.css")
PACKAGE_FILES = ("README.md", "requirements.txt", "Dockerfile", "compose.yaml", ".env.example", ".dockerignore")


def fullstack_archive(product: dict[str, Any]) -> bytes:
    model = DemoApp.model_validate(product["app"]).model_dump()
    # No fake/customer records, credentials or studio metadata become runtime data.
    for entity in model["entities"]:
        entity["records"] = []
    base = Path(__file__).resolve().parents[1]
    model_bytes = json.dumps(model, ensure_ascii=False, indent=2).encode()
    files = {"model.json": model_bytes,
             "brief.json": json.dumps(product["brief"], ensure_ascii=False, indent=2).encode(),
             "runtime/contracts.py": (base / "models/product.py").read_bytes()}
    for path in RUNTIME_FILES:
        files["runtime/" + path] = (base / "product_runtime" / path).read_bytes()
    for path in PACKAGE_FILES:
        files[path] = (base / "product_runtime/package" / path).read_bytes()
    files["manifest.json"] = json.dumps({"runtime_version": RUNTIME_VERSION,
        "status": "foundation_requires_operator_validation",
        "files_sha256": {path: hashlib.sha256(content).hexdigest() for path, content in files.items()}}, indent=2).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return buffer.getvalue()
