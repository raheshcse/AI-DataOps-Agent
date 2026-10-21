"""Publish the mounted /api/v1 sub-application in the root OpenAPI document.

FastAPI does not include mounted sub-apps in the parent's ``/openapi.json``, so Swagger at ``/docs`` showed only
``/sim/*``, ``/health`` and ``/ready``. This only changes the *documentation*; routing, authentication and the
sub-application's own ``/api/v1/openapi.json`` are untouched.
"""

from __future__ import annotations

import copy
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi


def install_merged_openapi(app: FastAPI, mount_path: str, sub_app: FastAPI) -> None:
    def build() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        root = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
        sub = copy.deepcopy(sub_app.openapi())
        paths = root.setdefault("paths", {})
        for path, item in sub.get("paths", {}).items():
            paths[mount_path + path] = item
        components = root.setdefault("components", {})
        for kind, entries in sub.get("components", {}).items():
            target = components.setdefault(kind, {})
            for name, definition in entries.items():
                if name in target and target[name] != definition:
                    raise RuntimeError(f"OpenAPI component name clash: {kind}/{name}")  # fail loudly, never silently overwrite
                target[name] = definition
        seen = {t.get("name") for t in root.get("tags", [])}
        root.setdefault("tags", []).extend(t for t in sub.get("tags", []) if t.get("name") not in seen)
        app.openapi_schema = root
        return root

    app.openapi = build  # type: ignore[method-assign]
