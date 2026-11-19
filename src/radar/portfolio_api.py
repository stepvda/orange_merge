"""`/api/portfolio` — the data behind the Portfolio browser (/portfolio).

Its own router, like explore_api, and mounted by `radar.api`, so it inherits the
application-level session dependency: customer references name customers, and
many of them have no agreement to be named outside Orange.
"""

from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, HTTPException

from .portfolio import PortfolioStore, PortfolioUnavailable


def build_router(store: PortfolioStore | None = None) -> APIRouter:
    router = APIRouter()
    store = store or PortfolioStore()

    def warm() -> None:
        try:
            store.get()
        except Exception:  # noqa: BLE001 — the request path reports the reason
            pass

    # Built in the background at startup: reading the crawl takes seconds, and
    # the first person to open /portfolio should not be the one who waits.
    threading.Thread(target=warm, name="portfolio-warm", daemon=True).start()

    @router.get("/api/portfolio")
    def portfolio() -> dict[str, Any]:
        """SVPs, value propositions, offers and customer references, normalised.

        Returned whole (about 1.5 MB) and joined in the browser: the four lists
        cascade into each other, and one fetch keeps every selection instant.
        """
        try:
            return store.get()
        except PortfolioUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc

    return router
