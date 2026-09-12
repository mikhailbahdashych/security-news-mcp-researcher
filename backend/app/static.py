"""Serving of the built single-page app.

The SPA is mounted LAST, after every API router, and its catch-all route explicitly
refuses `/api/*` so an unknown API path always produces a JSON 404 instead of silently
returning `index.html` (which would turn a typo into an unreadable client-side error).
"""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

NOT_FOUND = "Not Found"


def _resolve_static_file(root: Path, relative_path: str) -> Path | None:
    """Return an existing file inside ``root``, or None (unknown path / traversal attempt)."""
    if not relative_path:
        return None
    candidate = (root / relative_path).resolve()
    if not candidate.is_relative_to(root.resolve()):
        return None
    return candidate if candidate.is_file() else None


def mount_spa(app: FastAPI, static_dir: Path) -> None:
    """Serve the built SPA from ``static_dir`` if it exists.

    In local dev the frontend is served by Vite and ``static_dir`` does not exist; the
    app then boots normally and only serves `/api/*`.
    """
    root = Path(static_dir)
    if not root.is_dir():
        return

    index_file = root / "index.html"
    assets_dir = root / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{spa_path:path}", include_in_schema=False)
    async def serve_spa(spa_path: str) -> FileResponse:
        # Unmatched API paths must never fall through to the SPA shell.
        if spa_path == "api" or spa_path.startswith("api/"):
            raise HTTPException(status_code=404, detail=NOT_FOUND)

        static_file = _resolve_static_file(root, spa_path)
        if static_file is not None:
            return FileResponse(static_file)

        if not index_file.is_file():
            raise HTTPException(status_code=404, detail=NOT_FOUND)
        # Any other path is a client-side route: hand back the SPA shell.
        return FileResponse(index_file)
