"""Crawl and mirror the Espresso intranet site with a headful Chromium.

Edge cannot be automated on this machine (corporate policy RemoteDebuggingAllowed=0),
so this drives Playwright's own Chromium with a persistent profile. Flow:

  1. A visible browser opens on the start URL. The user logs in by hand.
  2. The script waits until the file <out>/.go exists (create it once logged in).
  3. Breadth-first crawl of same-host pages: each page is rendered, every network
     response it triggers is saved, and a static snapshot (scripts removed, links
     rewritten to local relative paths) is written next to a plain-text copy.

Progress is checkpointed to <out>/state.json, so re-running resumes the crawl.

    .venv/Scripts/python.exe scripts/crawl_espresso.py [--max-pages N] [--delay S]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path, PurePosixPath
import base64
import collections
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, unquote

from playwright.async_api import async_playwright, Response, Page

START_URL = "https://expresso.sso.infra.ftgroup/public/home"
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "data" / "espresso"
PROFILE_DIR = ROOT / "data" / ".chromium-profile"

# Never follow these: they would end the session or change server state.
SKIP_PATTERNS = re.compile(
    r"logout|log-out|signout|sign-out|deconnexion|d%C3%A9connexion|disconnect|/oauth|/saml|"
    r"/sso/|delete|supprimer|unsubscribe",
    re.I,
)
# Linked files fetched directly instead of rendered as pages.
FILE_EXT = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".csv",
    ".txt", ".odt", ".ods", ".odp", ".msg", ".rtf", ".mp4", ".mp3",
}
ASSET_TYPES = {"stylesheet", "script", "image", "font", "media", "fetch", "xhr", "document"}
BAD_CHARS = re.compile(r'[<>:"\\|?*\x00-\x1f]')


# Query parameters that only record navigation origin; they produce duplicate pages.
DROP_PARAMS = {"origin"}
NETWORK_ERRORS = re.compile(
    r"ERR_NAME_NOT_RESOLVED|ERR_INTERNET_DISCONNECTED|ERR_NETWORK_CHANGED|ERR_CONNECTION_(REFUSED|RESET|TIMED_OUT|CLOSED)"
    r"|ERR_ADDRESS_UNREACHABLE|ERR_NETWORK_ACCESS_DENIED|ERR_PROXY_CONNECTION_FAILED|Failed to fetch"
)
FILE_PATH_MARKERS = ("/api/v1/documents/download/", "/api/v1/download-id/")


def normalize(url: str) -> str:
    """Canonical page key: drop fragments (except SPA hash routes) and tracking params."""
    p = urlsplit(url)
    frag = p.fragment if p.fragment.startswith("/") or p.fragment.startswith("!/") else ""
    path = p.path or "/"
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                       if k not in DROP_PARAMS])
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, query, frag))


def is_file(url: str) -> bool:
    path = urlsplit(url).path
    return PurePosixPath(path).suffix.lower() in FILE_EXT or any(m in path for m in FILE_PATH_MARKERS)


def disposition_filename(cd: str | None) -> str | None:
    if not cd:
        return None
    m = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", cd, re.I)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
    return m.group(1).strip() if m else None


def _short_hash(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:8]


def local_path(url: str, *, page: bool) -> PurePosixPath:
    """Map a URL to a relative file path under site/."""
    p = urlsplit(url)
    parts = [BAD_CHARS.sub("_", unquote(x)) for x in p.path.split("/") if x not in ("", ".", "..")]
    parts = [x[:120] for x in parts]
    suffix = ""
    if p.query or (page and p.fragment):
        suffix = "__" + _short_hash(p.query + "#" + p.fragment)
    host = BAD_CHARS.sub("_", p.netloc.lower())
    if page:
        return PurePosixPath(host, *parts, f"index{suffix}.html")
    if not parts or p.path.endswith("/"):
        parts.append("index")
    name = parts[-1]
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    parts[-1] = f"{stem}{suffix}.{ext}" if ext else f"{stem}{suffix}"
    return PurePosixPath(host, *parts)


def relpath(target: PurePosixPath, from_file: PurePosixPath) -> str:
    return os.path.relpath(str(target), str(from_file.parent)).replace("\\", "/")


class Crawler:
    def __init__(self, out: Path, max_pages: int, delay: float, host: str, workers: int = 4):
        self.out = out
        self.site = out / "site"
        self.host = host
        self.max_pages = max_pages
        self.delay = delay
        self.state_file = out / "state.json"
        self.manifest = out / "manifest.jsonl"
        self.assets: dict[str, str] = {}  # url -> relative path under site/
        self.seen: set[str] = set()
        self.queue: list[str] = []
        self.done: set[str] = set()
        self.pending: set[asyncio.Task] = set()
        self.prio: dict[str, int] = {}
        self.offer_ids: set[str] = set()
        self.fatal = False
        self.offline = False
        self.stop = False
        self.workers = workers
        self.inflight: dict[str, int] = {}
        self.retries: dict[str, int] = {}
        self.live: dict[int, set] = {}
        self.stragglers: collections.Counter = collections.Counter()
        self.cache_hits = 0
        self.spa_nav = True
        self.spa_hits = 0
        self.spa_misses = 0
        self.net_lock = asyncio.Lock()
        self._last_save = 0.0
        self._load()

    # ---- state -------------------------------------------------------------
    def _load(self) -> None:
        if self.state_file.exists():
            s = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.assets = s.get("assets", {})
            self.prio = s.get("prio", {})
            self.offer_ids = set(s.get("offer_ids", []))
            self.done = {normalize(u) for u in s["done"]}
            # Retry file downloads that failed or never got saved.
            retry = {u for u in self.done if is_file(u) and u not in self.assets}
            # ...and anything whose latest manifest record is an error (e.g. browser closed).
            latest: dict[str, dict] = {}
            if self.manifest.exists():
                for line in self.manifest.read_text(encoding="utf-8").splitlines():
                    try:
                        rec = json.loads(line)
                        latest[normalize(rec["url"])] = rec
                    except Exception:
                        continue
            retry |= {u for u, rec in latest.items() if "error" in rec and u in self.done}
            # Pages saved before they rendered (title is just "Expresso").
            retry |= {u for u, rec in latest.items() if rec.get("kind") == "page"
                      and isinstance(rec.get("title"), str)
                      and " - " not in rec["title"] and u in self.done
                      and "/public/" in u}
            self.done -= retry
            queue: list[str] = []
            for u in list(retry) + [normalize(u) for u in s["queue"]]:
                if u not in self.done and u not in queue:
                    queue.append(u)
            self.queue = queue
            self.seen = set(self.queue) | self.done
            print(f"Resuming: {len(self.done)} done, {len(self.queue)} queued "
                  f"({len(retry)} file retries)")

    def _save_state(self, force: bool = False) -> None:
        # The state carries every asset URL (megabytes); with several tabs, write it at
        # most every few seconds. URLs still in flight are saved as queued.
        if not force and time.time() - self._last_save < 5:
            return
        self._last_save = time.time()
        queue = list(self.inflight) + self.queue
        prio = {**{u: self.prio[u] for u in self.queue if u in self.prio}, **self.inflight}
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"queue": queue, "done": sorted(self.done), "assets": self.assets,
             "prio": prio,
             "offer_ids": sorted(self.offer_ids)}
        ), encoding="utf-8")
        tmp.replace(self.state_file)

    def _log(self, rec: dict) -> None:
        with self.manifest.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ---- priority ----------------------------------------------------------
    # 0 = offers (their pages, tabs and documents), 1 = other solution-type content
    # (value propositions, thematic folders, campaigns), 2 = everything else.
    def default_prio(self, url: str) -> int:
        m = re.search(r"/public/(solutions|offers)/([0-9a-f]{24})", url)
        if m:
            return 0 if m.group(2) in self.offer_ids else 1
        return 2

    def enqueue(self, url: str, prio: int | None = None) -> None:
        u = normalize(url)
        p = urlsplit(u)
        if p.scheme not in ("http", "https") or p.netloc != self.host:
            return
        if SKIP_PATTERNS.search(u):
            return
        pr = self.default_prio(u)
        if prio is not None and is_file(u):
            pr = min(pr, prio)  # documents inherit their parent page's priority
        if u in self.seen:
            if u in self.prio and pr < self.prio[u]:
                self.prio[u] = pr
            return
        self.seen.add(u)
        self.queue.append(u)
        self.prio[u] = pr

    def next_index(self) -> int:
        best, best_i = 99, 0
        for i, u in enumerate(self.queue):
            pr = self.prio.get(u, 2)
            if pr < best:
                best, best_i = pr, i
                if pr == 0:
                    break
        return best_i

    async def seed(self, page: Page) -> None:
        """Queue every item the API lists, so items behind paging/infinite scroll aren't missed."""
        types = [("offer", "solutions"), ("valueProposition", "solutions"),
                 ("thematicFolder", "solutions"), ("campaign", "solutions"), ("client", "clients")]
        found: dict[str, set[str]] = {t: set() for t, _ in types}
        # Item lists already captured from the value-proposition pages.
        for u, rel in self.assets.items():
            if "strategicValuePropositions=" in u and "limit=0" in u:
                try:
                    d = json.loads((self.site / rel).read_text(encoding="utf-8"))
                    for it in d if isinstance(d, list) else d.get("data", []):
                        if it.get("type") in found:
                            found[it["type"]].add(it["_id"])
                except Exception:
                    pass
        for t, _ in types:
            for api in (f"/api/v1/contents?type={t}&status=1&limit=0&mode=light",
                        f"/api/v1/contents/public/?type={t}&limit=0&mode=light"):
                try:
                    d = await page.evaluate("""async (u) => {
                        const r = await fetch(u, {credentials: 'include'});
                        return r.ok ? await r.json() : null; }""", api)
                except Exception as e:
                    self._fatal_check(e)
                    d = None
                if d is not None:
                    # Keep the list: pages re-request it, and serve_cached_list answers
                    # those repeats from this copy (the browser capture can miss a body
                    # this large).
                    full = f"https://{self.host}{api}"
                    rel = local_path(full, page=False)
                    (self.site / rel).parent.mkdir(parents=True, exist_ok=True)
                    (self.site / rel).write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
                    self.assets[full] = str(rel)
                items = d if isinstance(d, list) else (d or {}).get("data", []) if isinstance(d, dict) else []
                n0 = len(found[t])
                for it in items or []:
                    if isinstance(it, dict) and it.get("_id") and it.get("type", t) == t:
                        found[t].add(it["_id"])
                print(f"Seed {t}: {api} -> {len(items or [])} items ({len(found[t]) - n0} new)", flush=True)
        self.offer_ids |= found["offer"]
        for t, section in types:
            for i in found[t]:
                self.enqueue(f"https://{self.host}/public/{section}/{i}")
        # Re-rank everything queued now that the offer ids are known.
        for u in self.queue:
            d = self.default_prio(u)
            self.prio[u] = min(self.prio.get(u, 2), d) if is_file(u) else d
        print(f"Seeded: {len(self.offer_ids)} offers known; queue {len(self.queue)} "
              f"({sum(1 for u in self.queue if self.prio.get(u) == 0)} offer URLs first)", flush=True)
        self._save_state()

    # ---- network capture ---------------------------------------------------
    def on_response(self, resp: Response) -> None:
        t = asyncio.ensure_future(self._save_response(resp))
        self.pending.add(t)
        t.add_done_callback(self.pending.discard)

    async def _save_response(self, resp: Response) -> None:
        try:
            req = resp.request
            if req.method != "GET" or resp.status != 200:
                return
            if req.resource_type not in ASSET_TYPES or req.resource_type == "document":
                return
            url = resp.url
            if url.startswith("data:") or url in self.assets:
                return
            body = await resp.body()
            rel = local_path(url, page=False)
            dest = self.site / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(body)
            self.assets[url] = str(rel)
        except Exception:
            pass  # redirects, aborted requests, bodies evicted from cache

    async def fetch_file(self, page: Page, url: str) -> None:
        """Download through the page's own fetch(), so the browser's TLS trust store
        (which holds the corporate root CA) and session cookies are used."""
        try:
            if urlsplit(page.url).netloc != self.host:
                await page.goto(f"https://{self.host}/public/home", wait_until="load", timeout=60_000)
            # Headers only: the body is cancelled so nothing large crosses the driver.
            r = await page.evaluate("""async (u) => {
                const r = await fetch(u, {credentials: 'include'});
                const h = {status: r.status, ok: r.ok, cd: r.headers.get('content-disposition'),
                           ct: r.headers.get('content-type'), len: r.headers.get('content-length')};
                try { await r.body.cancel(); } catch (e) {}
                return h;
            }""", url)
            rel = local_path(url, page=False)
            name = disposition_filename(r["cd"])
            if name:
                rel = rel.parent / rel.name / BAD_CHARS.sub("_", name)[:150]
            if r["ok"]:
                # The browser streams the body straight to disk as a download.
                async with page.expect_download(timeout=900_000) as dl_info:
                    await page.evaluate("""(u) => {
                        const a = document.createElement('a');
                        a.href = u; a.download = ''; document.body.appendChild(a); a.click(); a.remove();
                    }""", url)
                dl = await dl_info.value
                if not name:
                    rel = rel.parent / rel.name / BAD_CHARS.sub("_", dl.suggested_filename)[:150]
                dest = self.site / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                await dl.save_as(dest)
                self.assets[url] = str(rel)
            self._log({"url": url, "kind": "file", "status": r["status"], "type": r["ct"],
                       "bytes": r["len"], "path": str(rel) if r["ok"] else None})
        except Exception as e:
            self._fatal_check(e)
            self._log({"url": url, "kind": "file", "error": str(e)[:300]})

    def _fatal_check(self, e: Exception) -> None:
        s = str(e)
        if "has been closed" in s or "Connection closed" in s:
            self.fatal = True
        elif NETWORK_ERRORS.search(s):
            self.offline = True

    async def wait_for_network(self) -> None:
        """Block until the host resolves and answers again (VPN dropped, laptop asleep...)."""
        print("Network unreachable; waiting for Espresso to come back...", flush=True)
        loop = asyncio.get_running_loop()
        while True:
            try:
                await loop.getaddrinfo(self.host, 443)
                print("Network is back, resuming.", flush=True)
                return
            except OSError:
                await asyncio.sleep(30)

    def finalize_links(self) -> None:
        """Rewrite absolute URLs left in snapshots (assets/files saved after the page)."""
        if not self.assets:
            return
        pat = re.compile(r'\b(href|src)=(["\'])(https?://[^"\']+)\2')
        n = 0
        for f in self.site.rglob("index*.html"):
            rel = PurePosixPath(f.relative_to(self.site).as_posix())
            html = f.read_text(encoding="utf-8")

            def repl(m: re.Match) -> str:
                t = m.group(3).replace("&amp;", "&")
                hit = self.assets.get(t) or self.assets.get(normalize(t))
                return f'{m.group(1)}={m.group(2)}{relpath(PurePosixPath(hit), rel)}{m.group(2)}' if hit else m.group(0)

            new = pat.sub(repl, html)
            if new != html:
                f.write_text(new, encoding="utf-8")
                n += 1
        print(f"Relinked {n} snapshots.", flush=True)

    # ---- pages -------------------------------------------------------------
    async def snapshot(self, page: Page, url: str) -> dict:
        rel = local_path(url, page=True)
        # Make every href/src absolute and strip scripts so the snapshot is static.
        # Work on a copy: the live page stays intact for the next in-app navigation.
        info = await page.evaluate("""() => {
            const live = document.documentElement;
            const doc = live.cloneNode(true);
            // Absolute URLs come from the live elements (a detached clone has no base).
            const fix = (sel, attr) => {
                const a = live.querySelectorAll(sel), b = doc.querySelectorAll(sel);
                for (let i = 0; i < a.length && i < b.length; i++) {
                    const v = a[i][attr];
                    if (v && typeof v === 'string') b[i].setAttribute(attr, v);
                }
            };
            fix('[href]', 'href');
            fix('[src]', 'src');
            doc.querySelectorAll('script, noscript, base').forEach(e => e.remove());
            doc.querySelectorAll('[srcset]').forEach(e => e.removeAttribute('srcset'));
            return { title: document.title, text: document.body ? document.body.innerText : '',
                     html: '<!DOCTYPE html>\\n' + doc.outerHTML };
        }""")
        html = info["html"]

        def repl(m: re.Match) -> str:
            attr, q, target = m.group(1), m.group(2), m.group(3)
            t = target.replace("&amp;", "&")
            if t in self.assets:
                return f'{attr}={q}{relpath(PurePosixPath(self.assets[t]), rel)}{q}'
            n = normalize(t)
            if urlsplit(n).netloc == self.host and not SKIP_PATTERNS.search(n):
                if is_file(n):
                    return f'{attr}={q}{n}{q}'  # relinked by finalize_links once downloaded
                return f'{attr}={q}{relpath(local_path(n, page=True), rel)}{q}'
            return m.group(0)

        html = re.sub(r'\b(href|src)=(["\'])(https?://[^"\']+)\2', repl, html)
        # url(...) references inside inline styles
        html = re.sub(
            r'url\((["\']?)(https?://[^)"\']+)\1\)',
            lambda m: f'url({m.group(1)}{relpath(PurePosixPath(self.assets[m.group(2)]), rel)}{m.group(1)})'
            if m.group(2) in self.assets else m.group(0),
            html,
        )
        dest = self.site / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(html, encoding="utf-8")
        dest.with_suffix(".txt").write_text(f"{info['title']}\n{url}\n\n{info['text']}", encoding="utf-8")
        return {"path": str(rel), "title": info["title"]}

    # ---- page settling -------------------------------------------------------
    # Playwright's "networkidle" waits for 500 ms with no connections at all, which a
    # page that polls (notifications) or holds a socket open may never reach, costing
    # the full timeout. Instead count this page's own short requests and stop once
    # none has been outstanding for QUIET seconds.
    QUIET = 0.4
    SETTLE_CAP = 3.0  # content is already in (title check); this only catches late assets
    IGNORE_REQ = re.compile(r"notification|/sockjs|/socket|/ws\b|/events\b|/poll|analytics|/download", re.I)

    def track(self, page: Page) -> None:
        live: set = set()
        self.live[id(page)] = live

        def counts(req) -> bool:
            return (req.resource_type not in ("websocket", "eventsource", "media", "other")
                    and not self.IGNORE_REQ.search(req.url))

        page.on("request", lambda r: live.add(r) if counts(r) else None)
        page.on("requestfinished", lambda r: live.discard(r))
        page.on("requestfailed", lambda r: live.discard(r))

    async def settle(self, page: Page) -> float:
        live = self.live.get(id(page), set())
        t0 = last_busy = time.time()
        while time.time() - t0 < self.SETTLE_CAP:
            if live:
                last_busy = time.time()
            elif time.time() - last_busy >= self.QUIET:
                break
            await asyncio.sleep(0.05)
        if live:
            self.stragglers.update(f"{r.resource_type} {r.method} {r.url[:160]}" for r in list(live)[:5])
        live.clear()  # anything still open is a straggler; don't let it bleed into the next page
        return round(time.time() - t0, 2)

    async def serve_cached_list(self, route) -> None:
        """Whole-catalogue list calls (limit=0) are made by every page of a section --
        each client page re-downloads all 4,000+ clients. They were saved on first sight,
        so answer repeats from disk: same bytes, no load on Espresso, no multi-second wait."""
        url = route.request.url
        rel = self.assets.get(url)
        if route.request.method == "GET" and "limit=0" in url and rel and (self.site / rel).is_file():
            self.cache_hits += 1
            await route.fulfill(path=str(self.site / rel), status=200,
                                headers={"content-type": "application/json; charset=utf-8"})
        else:
            await route.continue_()

    async def crawl_page(self, ctx, page: Page, url: str) -> None:
        t_start = time.time()
        phases: dict[str, float] = {}
        if is_file(url):
            await self.fetch_file(page, url)
            return
        resp = None
        if self.spa_nav and urlsplit(page.url).netloc == self.host and "/public/" in page.url:
            # In-app navigation: the app is already loaded in this tab, so let its router
            # switch pages (as a click would) instead of reloading the whole application.
            try:
                await page.evaluate("""(u) => {
                    document.title = '';
                    history.pushState(null, '', u);
                    window.dispatchEvent(new PopStateEvent('popstate', {state: null}));
                }""", url)
                await page.wait_for_function("document.title && document.title.includes(' - ')",
                                             timeout=15_000)
                phases["t_spa"] = round(time.time() - t_start, 2)
                self.spa_hits += 1
            except Exception as e:
                self._fatal_check(e)
                if self.fatal or self.offline:
                    return
                self.spa_misses += 1
                phases["t_spa_failed"] = round(time.time() - t_start, 2)
                if self.spa_misses >= 8 and self.spa_hits < self.spa_misses:
                    self.spa_nav = False
                    print("In-app navigation isn't working here; using full page loads.", flush=True)
        if "t_spa" not in phases:
          try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            phases["t_dom"] = round(time.time() - t_start, 2)
            try:  # the SPA sets "<name> - <section> - Expresso" once the content is in
                await page.wait_for_function("document.title && document.title.includes(' - ')",
                                             timeout=20_000)
                phases["t_title"] = round(time.time() - t_start, 2)
            except Exception:
                if is_file(page.url) or urlsplit(page.url).netloc != self.host:
                    pass  # handled below
                else:
                    n = self.retries.get(url, 0) + 1
                    self.retries[url] = n
                    self._log({"url": url, "kind": "page", "error": f"not rendered (try {n})"})
                    if n < 3:
                        self.queue.append(url)  # try again later, when Espresso is less busy
                        self.prio[url] = 2
                        self.seen.add(url)
                    return
          except Exception as e:
            if "Download is starting" in str(e):
                await self.fetch_file(page, url)
            else:
                self._fatal_check(e)
                self._log({"url": url, "kind": "page", "error": str(e)[:300]})
            return
        waited = await self.settle(page)
        phases["t_settled"] = round(time.time() - t_start, 2)
        await page.wait_for_timeout(150)  # one more frame for the SPA to render
        final = normalize(page.url)
        if urlsplit(final).netloc != self.host:
            self._log({"url": url, "kind": "page", "redirected_offsite": final})
            return
        links = await page.evaluate(
            "() => Array.from(document.querySelectorAll('a[href], area[href]'), a => a.href)"
        )
        parent = self.prio.get(url, self.default_prio(url))
        for l in links:
            self.enqueue(l, parent)
        if self.pending:
            await asyncio.gather(*list(self.pending), return_exceptions=True)
        phases["t_saved"] = round(time.time() - t_start, 2)
        snap = await self.snapshot(page, url)
        self._log({"url": url, "final": final, "kind": "page",
                   "status": resp.status if resp else None, **snap, "links": len(links),
                   "settle_s": waited, **phases, "total_s": round(time.time() - t_start, 2)})

    async def worker(self, page: Page, wid: int) -> None:
        """One tab. Takes the highest-priority URL; marks it done only on success."""
        while not self.stop and len(self.done) < self.max_pages:
            if not self.queue:
                if not self.inflight:
                    return  # nothing queued and nobody can add more
                await asyncio.sleep(0.5)
                continue
            qi = self.next_index()
            url = self.queue.pop(qi)
            pr = self.prio.pop(url, 2)
            self.inflight[url] = pr
            try:
                await self.crawl_page(None, page, url)
            except Exception as e:
                self._fatal_check(e)
                if not (self.fatal or self.offline):
                    self._log({"url": url, "kind": "page", "error": f"worker{wid}: {str(e)[:300]}"})
            del self.inflight[url]
            if self.offline and not (self.fatal or page.is_closed()):
                self.retries[url] = self.retries.get(url, 0) + 1
                if self.retries[url] > 3:  # network looks fine, so this URL itself fails
                    async with self.net_lock:
                        if self.offline:
                            await self.wait_for_network()
                            self.offline = False
                    self.done.add(url)
                    continue
            if self.offline or self.fatal or page.is_closed():
                # Put it back at the front; it is retried once the problem clears.
                self.queue.insert(0, url)
                self.prio[url] = pr
                if self.fatal or page.is_closed():
                    if not self.stop:
                        print("Browser window was closed; stopping. Re-run to resume.", flush=True)
                    self.stop = True
                    return
                async with self.net_lock:
                    if self.offline:
                        await self.wait_for_network()
                        self.offline = False
                continue
            if url in self.queue:
                continue  # re-queued for another try (page didn't render)
            self.done.add(url)
            self._save_state()
            if len(self.done) % 25 == 0 and self.stragglers:
                print("STRAGGLERS " + json.dumps(self.stragglers.most_common(5))
                      + f" cache_hits={self.cache_hits} spa={self.spa_hits}/{self.spa_misses}", flush=True)
            print(f"[{len(self.done)}/{len(self.done) + len(self.queue) + len(self.inflight)}] "
                  f"w{wid} p{pr} {url}", flush=True)
            await asyncio.sleep(self.delay)

    async def run(self, start_url: str, login_timeout: float) -> None:
        self.site.mkdir(parents=True, exist_ok=True)
        go = self.out / ".go"
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                str(PROFILE_DIR), headless=False, viewport={"width": 1400, "height": 900},
                accept_downloads=True,
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await self.wait_for_network()
            await page.goto(start_url, wait_until="load", timeout=120_000)
            print(f"LOGIN: log in in the Chromium window, then create {go}", flush=True)
            t0 = time.time()
            while not go.exists():
                if time.time() - t0 > login_timeout:
                    print("Timed out waiting for login.")
                    await ctx.close()
                    return
                await asyncio.sleep(2)
            print("Login confirmed, crawling.", flush=True)

            page.on("response", self.on_response)
            if not self.queue and not self.done:
                self.enqueue(start_url)
                # Also seed with wherever the post-login page landed.
                self.enqueue(page.url)
            await self.seed(page)
            await ctx.route("**/api/v1/contents**", self.serve_cached_list)
            pages = [page]
            self.track(page)
            for _ in range(self.workers - 1):
                p = await ctx.new_page()
                p.on("response", self.on_response)
                self.track(p)
                await p.goto(f"https://{self.host}/public/home", wait_until="load", timeout=120_000)
                pages.append(p)
            print(f"Crawling with {len(pages)} parallel tabs.", flush=True)
            await asyncio.gather(*(self.worker(p, i) for i, p in enumerate(pages)))
            self._save_state(force=True)
            self.finalize_links()
            print(f"Finished: {len(self.done)} pages, {len(self.assets)} assets/files, "
                  f"{len(self.queue)} still queued.", flush=True)
            try:
                await ctx.close()
            except Exception:
                pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default=START_URL)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--max-pages", type=int, default=50000)
    ap.add_argument("--delay", type=float, default=0.2, help="seconds between pages, per tab")
    ap.add_argument("--login-timeout", type=float, default=3600)
    ap.add_argument("--workers", type=int, default=8, help="parallel browser tabs")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    host = urlsplit(a.start).netloc.lower()
    asyncio.run(Crawler(a.out, a.max_pages, a.delay, host, a.workers).run(a.start, a.login_timeout))


if __name__ == "__main__":
    main()
