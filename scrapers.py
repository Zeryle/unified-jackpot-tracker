"""
Scrapers for the unified tracker.

Each scraper implements a common interface so the dashboard can drive
both Zitobox and CarnivalCiti from the same code path:

    class BaseScraper:
        source: str               # 'zitobox' or 'carnivalciti'
        display_name: str
        def load_games() -> list[dict]
        async def login(force=False) -> bool
        async def run(force_login=False) -> list[dict]
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
from playwright.async_api import async_playwright, BrowserContext, Page

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from db import init_db, save_jackpot, upsert_game  # noqa: E402

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ─── Shared helpers ────────────────────────────────────────────────────────


def _parse_access_file(path: Path) -> tuple[str, str]:
    email = password = ""
    if not path.exists():
        return email, password
    current: Optional[str] = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            low = line.lstrip("#").strip().lower()
            if low.startswith("email"):
                current = "email"
            elif low.startswith("password"):
                current = "password"
            continue
        if current == "email" and not email:
            email = line
        elif current == "password" and not password:
            password = line
        elif not email:
            email = line
        elif not password:
            password = line
    return email, password


def _read_kv_links(path: Path) -> list[dict]:
    """Parse a pipe-separated links file: name | jackpot_display | url.

    The code is derived from the URL by taking the segment that looks
    like a game id (starts with the game id pattern, contains dashes or
    underscores, not the literal 'slotgame' placeholder). Falls back
    to a slugified version of the display name.
    """
    if not path.exists():
        return []
    out: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            parts += [""] * (2 - len(parts))
        name, jp_display = parts[0], parts[1]
        url = parts[2] if len(parts) >= 3 else ""
        if not url:
            continue
        # Take the LAST path segment — that's the game id (zitobox's
        # URL has /game/slotgame/{GameId}/{...} so the id is at the
        # end, not right after /game/).
        m = re.search(r"/game/([^/?#]+)", url)
        code = ""
        if m:
            # /game/slotgame/Lightning-Fruits/5862/... -> take "Lightning-Fruits"
            tail = url.rstrip("/").split("/")
            # tail[-1] would be "00" for example; find first non-numeric
            # segment that isn't "slotgame"
            for seg in reversed(tail):
                if not seg:
                    continue
                if seg.isdigit():
                    continue
                if seg.lower() == "slotgame":
                    continue
                code = seg
                break
        if not code:
            code = name.lower().replace(" ", "-")
        out.append({"code": code, "name": name, "url": url, "jackpot_display": jp_display})
    return out


def _read_simple_links(path: Path) -> list[dict]:
    """Parse a simple links file with one URL per line, inferring the code."""
    if not path.exists():
        return []
    out: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.search(r"/game/([A-Za-z0-9_-]+)", line)
        code = (m.group(1) if m else line).upper()
        out.append({"code": code, "url": line, "name": code, "jackpot_display": ""})
    return out


def _extract_biggest_dedup(payload: dict) -> Optional[tuple[float, str]]:
    """Find the biggest jackpot level in a RGS jackpot/get response.

    Dedupes groups by jackpotName (keeps the first occurrence of each
    name) so Ten Tigers-style responses with multiple duplicate groups
    don't pick the wrong value.
    """
    try:
        if "jackpots" in payload and isinstance(payload["jackpots"], dict):
            groups = payload["jackpots"].get("data", []) or []
        elif "jackpots" in payload and isinstance(payload["jackpots"], list):
            groups = payload["jackpots"]
        elif "data" in payload and isinstance(payload["data"], dict) and "jackpots" in payload["data"]:
            groups = payload["data"]["jackpots"].get("data", []) or []
        else:
            return None
        if not groups:
            return None

        seen: set[str] = set()
        deduped: list[dict] = []
        for g in groups:
            if not isinstance(g, dict):
                continue
            gname = (g.get("jackpotName") or g.get("name") or "").strip()
            if gname:
                if gname in seen:
                    continue
                seen.add(gname)
            deduped.append(g)
        if not deduped:
            return None

        best_value, best_name = 0.0, ""
        for group in deduped:
            gname = (group.get("jackpotName") or group.get("name") or "").strip()
            for level in group.get("levels", []) or []:
                try:
                    val = float(level.get("currentValue", 0) or 0) / 100.0
                except (TypeError, ValueError):
                    continue
                if val <= 0:
                    continue
                if val > best_value:
                    best_value = val
                    best_name = gname if gname and gname.lower() not in ("unknown", "") else f"Jackpot L{level.get('level', '?')}"
        if best_value > 0:
            return best_value, best_name
    except Exception as e:
        print(f"  [extract] error: {e}")
    return None


# ─── Zitobox scraper ───────────────────────────────────────────────────────


class ZitoboxScraper:
    source = "zitobox"
    display_name = "Zitobox"
    description = "Slot jackpots from zitobox.com (no login required)"

    LINKS_FILE = HERE / "links_zitobox.txt"

    # Endpoint template. The token is generated client-side by Zitobox's
    # game.min.js, so we use a tiny headless browser ONCE to harvest a
    # token per game, cache it, and re-use it for all subsequent
    # jackpot calls via plain httpx.
    JACKPOT_URL_TEMPLATE = (
        "https://gmw-us-central.prod.gmwrgs.com/rgs-integrationservice/games/ZB/{code}/jackpot/get"
    )
    GAME_URL_TEMPLATE = "https://www.zitobox.com/game/slotgame/{code}/{game_id}/9/2/00"

    def __init__(self) -> None:
        self._token_cache: dict[str, str] = {}
        self._rgs_code: dict[str, str] = {}  # slug -> G-code mapping

    def load_games(self) -> list[dict]:
        games = _read_kv_links(self.LINKS_FILE)
        for idx, g in enumerate(games):
            upsert_game(self.source, g["code"], g["name"], g["url"], g.get("jackpot_display", ""), idx + 1)
        return games

    async def run(self, force_login: bool = False) -> list[dict]:
        """Run a Zitobox scrape cycle.

        Lite mode: use httpx to fetch the jackpot endpoint directly,
        only spinning up a headless browser the first time per game
        (or when the cached token is rejected) to harvest a token.
        """
        init_db()
        games = self.load_games()
        if not games:
            return [{"error": "no_games"}]
        print(f"[{datetime.now().isoformat()}] [zitobox] Scrape starting for {len(games)} game(s)")

        results: list[dict] = []
        for g in games:
            r = await self._scrape_game(g)
            results.append(r)
            await asyncio.sleep(0.5)

        success = sum(1 for r in results if "value" in r)
        print(f"[{datetime.now().isoformat()}] [zitobox] Done: {success}/{len(results)}")
        return results

    async def _scrape_game(self, game: dict) -> dict:
        """Scrape one Zitobox game, retrying once if the cached token
        is stale.

        Root cause of the historic "2nd cycle takes 5x longer" bug:
        the cached token expires between cycles but the old code
        silently fell through to a full Playwright token harvest
        (~30-90s per game). With 5 games that's 5x the duration. The
        fix: try cached token, detect expiry via HTTP status or empty
        response, invalidate cache, re-harvest once.
        """
        code = game["code"]
        name = game["name"]
        display_name = game.get("jackpot_display") or None
        print(f"\n  [zitobox] Scraping {name} ({code})")

        # Try the cached token first; fall back to harvesting one.
        token = self._token_cache.get(code)
        rgs_code = self._rgs_code.get(code)
        if token and rgs_code:
            result = await self._try_cached_token(code, name, rgs_code, token, display_name)
            if result is not None:
                return result
            # Cached token stale — invalidate and fall through to harvest.
            print(f"  [zitobox] cached token stale for {code}, re-harvesting")
            self._token_cache.pop(code, None)
            self._rgs_code.pop(code, None)

        # Need a new token: spin up a headless browser once.
        token = await self._harvest_token(code, game["url"])
        if not token:
            print(f"  [WARN] {name}: could not harvest token")
            return {"code": code, "name": name, "error": "no_token"}

        self._token_cache[code] = token
        rgs_code = self._rgs_code.get(code, code)  # fall back to slug if not detected
        text = await self._fetch_jackpot_httpx(rgs_code, token)
        if not text or "jackpots" not in text:
            print(f"  [WARN] {name}: jackpot endpoint returned no data (rgs={rgs_code})")
            return {"code": code, "name": name, "error": "no_jackpot"}
        return self._parse_and_save(code, name, text, display_name)

    async def _try_cached_token(self, code: str, name: str, rgs_code: str, token: str, display_name) -> Optional[dict]:
        """Return a parsed result dict if cached token works, else None."""
        status, text = await self._fetch_jackpot_httpx_status(rgs_code, token)
        if status == 200 and text and "jackpots" in text:
            return self._parse_and_save(code, name, text, display_name)
        return None

    async def _fetch_jackpot_httpx(self, rgs_code: str, token: str) -> str:
        _, text = await self._fetch_jackpot_httpx_status(rgs_code, token)
        return text

    async def _fetch_jackpot_httpx_status(self, rgs_code: str, token: str) -> tuple[int, str]:
        """Return (http_status, body) so callers can detect token expiry.
        Instance method (not static) so test mocks via monkey-patching
        the instance can intercept it.
        """
        url = ZitoboxScraper.JACKPOT_URL_TEMPLATE.format(code=rgs_code) + f"?token={token}"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    url,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                )
                return (r.status_code, r.text if r.status_code == 200 else "")
        except Exception as e:
            print(f"  [zitobox] httpx fetch error: {e}")
            return (0, "")

    def _parse_and_save(self, code: str, name: str, text: str, display_name: str | None) -> dict:
        m = re.search(r"(\{.*\})", text)
        if not m:
            return {"code": code, "name": name, "error": "parse"}
        try:
            data = json.loads(m.group(1))
        except Exception:
            return {"code": code, "name": name, "error": "parse"}
        result = _extract_biggest_dedup(data)
        if not result:
            return {"code": code, "name": name, "error": "no_jackpot"}
        value, jp_name = result
        if display_name:
            jp_name = display_name
        save_jackpot(self.source, code, name, value, jp_name)
        print(f"  [OK] {name}: {jp_name} = {value:,.2f}")
        return {"code": code, "name": name, "value": value, "jackpot_name": jp_name}

    async def _harvest_token(self, code: str, game_url: str) -> str | None:
        """Open the game page in headless Chromium once, capture the
        token the game.min.js includes in its jackpot poll, return it.
        Also captures the real RGS game code (e.g. G243) from the
        jackpot URL, since our local code field is a slug.
        """
        token: str | None = None
        rgs_code: str | None = None
        print(f"  [zitobox] Harvesting token for {code} (one-time, ~30-90s)...")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(user_agent=USER_AGENT, viewport={"width": 1280, "height": 900})
                page = await context.new_page()

                async def on_resp(r):
                    nonlocal token, rgs_code
                    if token and rgs_code:
                        return
                    if "jackpot/get" in r.url and "gmw" in r.url:
                        import re as _re
                        m = _re.search(r"/games/ZB/([A-Z0-9]+)/jackpot/get", r.url)
                        if m:
                            rgs_code = m.group(1)
                        m = _re.search(r"token=([a-f0-9]+)", r.url)
                        if m:
                            token = m.group(1)
                page.on("response", on_resp)
                try:
                    await page.goto(game_url, wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    print(f"  [zitobox] goto err: {e}")
                # The jackpot poll happens ~25-90s after page load depending on
                # how many assets the game loads. Wait up to 90s for the token
                # to surface in network traffic.
                for i in range(90):
                    if token and rgs_code:
                        break
                    await asyncio.sleep(1)
                if not token:
                    print(f"  [zitobox] no token captured after 90s for {code}")
                await context.close()
            finally:
                await browser.close()
        if token and rgs_code:
            self._rgs_code[code] = rgs_code
        return token


# ─── CarnivalCiti scraper ──────────────────────────────────────────────────


class CarnivalCitiScraper:
    source = "carnivalciti"
    display_name = "CarnivalCiti"
    description = "Sweepstakes jackpots from carnivalciti.com (SC mode)"

    LINKS_FILE = HERE / "links_carnivalciti.txt"
    ACCESS_FILE = HERE / "accessos.txt"
    STORAGE_FILE = HERE / "data" / "carnival_storage.json"
    STORAGE_TTL = timedelta(hours=6)

    HOME_URL = "https://www.carnivalciti.com/home"
    GAME_URL_TEMPLATE = "https://www.carnivalciti.com/game/{code}"

    # CarnivalCiti recently moved to an Angular SPA on /auth. The auth
    # button is "START PLAYING" (was "RETURNING USER" pre-redesign).
    # Field IDs shifted by one (mat-input-0 / mat-input-1).
    AUTH_URL = "https://www.carnivalciti.com/auth"
    LOGIN_BUTTON = "button:has-text('START PLAYING'), button:has-text('RETURNING USER')"
    EMAIL_FIELD = "input[data-placeholder='email']"
    PASSWORD_FIELD = "input[data-placeholder='enter your password']"
    SUBMIT_BUTTON = "button:has-text('LOGIN'), button:has-text('START'), button.next-btn2"

    # Display order for CarnivalCiti game tabs (matches links_carnivalciti.txt)
    GAME_ORDER = ["G321", "G267", "G182", "G164", "G242", "G240", "G325"]

    FRIENDLY_NAMES = {
        "G321": "Soda Pop", "G267": "Toucan", "G182": "Lucky Jesters",
        "G164": "Ten Tigers", "G242": "Diamonds Deluxe", "G240": "Sacred Flower",
        "G325": "Banditres",
    }

    def load_games(self) -> list[dict]:
        games = _read_simple_links(self.LINKS_FILE)
        for idx, g in enumerate(games):
            friendly = self.FRIENDLY_NAMES.get(g["code"], g["code"])
            g["name"] = friendly
            g["url"] = self.GAME_URL_TEMPLATE.format(code=g["code"])
            order_index = self.GAME_ORDER.index(g["code"]) + 1 if g["code"] in self.GAME_ORDER else idx + 1
            upsert_game(self.source, g["code"], friendly, g["url"], g.get("jackpot_display", ""), order_index)
        return games

    async def run(self, force_login: bool = False) -> list[dict]:
        init_db()
        games = self.load_games()
        if not games:
            return [{"error": "no_games"}]
        print(f"[{datetime.now().isoformat()}] [carnivalciti] Scrape starting for {len(games)} game(s)")

        results: list[dict] = []
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context: BrowserContext = await browser.new_context(
                    user_agent=USER_AGENT, viewport={"width": 1280, "height": 900}
                )
                page = await context.new_page()
                if not await self._ensure_logged_in(context, page, force=force_login):
                    await context.close()
                    await browser.close()
                    return [{"error": "login_failed"}]

                for g in games:
                    r = await self._scrape_game(page, g)
                    results.append(r)
                    await asyncio.sleep(1)

                # Verify every game produced a reading. If any missed
                # (got_data=False), do ONE more pass over only the
                # failed ones — the SPA may have warmed up enough on
                # the first pass to actually serve the jackpot poll
                # the second time.
                missing = [g for g, r in zip(games, results) if not r.get("got_data")]
                if missing:
                    print(f"[{datetime.now().isoformat()}] [carnivalciti] {len(missing)} game(s) missing data — retry pass")
                    for g in missing:
                        idx = games.index(g)
                        r = await self._scrape_game(page, g)
                        results[idx] = r
                        await asyncio.sleep(1)

                await context.close()
            finally:
                await browser.close()

        success = sum(1 for r in results if "value" in r)
        missing_count = sum(1 for r in results if not r.get("got_data"))
        summary = f"{success}/{len(results)}"
        if missing_count:
            summary += f" ({missing_count} missing after retry)"
        print(f"[{datetime.now().isoformat()}] [carnivalciti] Done: {summary}")
        return results

    # ─── login ────────────────────────────────────────────────────────────

    async def _do_login(self, context: BrowserContext, page: Page, email: str, password: str) -> bool:
        print(f"  [carnivalciti/login] Opening {self.AUTH_URL}", flush=True)
        await page.goto(self.AUTH_URL, wait_until="domcontentloaded", timeout=60000)
        # CarnivalCiti is an Angular SPA — wait extra time for the app to
        # mount and render the login button.
        await page.wait_for_timeout(15000)
        try:
            await page.locator(self.LOGIN_BUTTON).first.wait_for(state="visible", timeout=30000)
        except Exception as e:
            print(f"  [login] login button missing: {e}", flush=True)
            return False
        await page.locator(self.LOGIN_BUTTON).first.click()
        print("  [login] Opened modal", flush=True)
        try:
            await page.locator(self.EMAIL_FIELD).wait_for(state="visible", timeout=15000)
        except Exception as e:
            print(f"  [login] Email field missing: {e}", flush=True)
            return False
        # Use evaluate to set values + dispatch input event. Workaround
        # for a Playwright 1.58 bug where `fill()` raises
        # "Object of type Playwright is not JSON serializable".
        async def set_input(selector: str, value: str) -> bool:
            ok = await page.evaluate(
                """([sel, v]) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    ).set;
                    setter.call(el, v);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    return el.value === v;
                }""",
                [selector, value],
            )
            return bool(ok)
        if not await set_input(self.EMAIL_FIELD, email):
            print(f"  [login] Failed to set email value", flush=True)
            return False
        if not await set_input(self.PASSWORD_FIELD, password):
            print(f"  [login] Failed to set password value", flush=True)
            return False
        await asyncio.sleep(0.3)
        # Find the submit button inside the form (the last .next-btn2)
        try:
            await page.locator("button.next-btn2").last.click(timeout=10000)
        except Exception as e:
            print(f"  [login] Submit button click err: {e}", flush=True)
            return False
        print("  [login] Submitted", flush=True)
        try:
            await page.wait_for_function(
                """() => {
                    const t = document.body.innerText.toLowerCase();
                    return t.includes('log out') || t.includes('sign out')
                        || t.includes('my account') || !!localStorage.getItem('token');
                }""",
                timeout=30000,
            )
        except Exception:
            await asyncio.sleep(5)
        token = await page.evaluate("() => localStorage.getItem('token')")
        if not token:
            print(f"  [login] FAILED — no token. URL={page.url}", flush=True)
            return False
        print(f"  [login] OK — token={token[:20]}...", flush=True)
        return True

    async def _switch_to_real(self, page: Page) -> None:
        before = await page.evaluate("() => localStorage.getItem('state')")
        print(f"  [switch] state before: {before!r}")
        await page.evaluate(
            """() => {
                try {
                    const raw = localStorage.getItem('state');
                    let obj = {};
                    if (raw) { try { obj = JSON.parse(raw); } catch(e) { obj = {}; } }
                    obj.switchBalance = 'real';
                    localStorage.setItem('state', JSON.stringify(obj));
                } catch (e) {}
            }"""
        )
        after = await page.evaluate("() => localStorage.getItem('state')")
        print(f"  [switch] state after: {after!r}")
        await page.reload(wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(4)
        verify = await page.evaluate("() => localStorage.getItem('state')")
        print(f"  [switch] state after reload: {verify!r}")

    async def _ensure_logged_in(self, context: BrowserContext, page: Page, force: bool) -> bool:
        if not force and self.STORAGE_FILE.exists():
            age = self._storage_age()
            if age is not None and age < self.STORAGE_TTL and self.STORAGE_FILE.stat().st_size > 200:
                try:
                    saved = json.loads(self.STORAGE_FILE.read_text(encoding="utf-8"))
                    await context.add_cookies(saved.get("cookies", []))
                    print(f"  [login] Reusing storage state (age={age})")
                except Exception as e:
                    print(f"  [login] cookie restore failed: {e}")

        await page.goto(self.AUTH_URL, wait_until="domcontentloaded", timeout=60000)
        # Wait for Angular to mount (CarnivalCiti is a SPA)
        await asyncio.sleep(12)

        if not force and self.STORAGE_FILE.exists():
            try:
                saved = json.loads(self.STORAGE_FILE.read_text(encoding="utf-8"))
                for o in saved.get("origins", []):
                    if "carnivalciti" in o.get("origin", ""):
                        for entry in (o.get("localStorage") or []):
                            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                                k, v = entry
                            elif isinstance(entry, dict):
                                k, v = entry.get("name"), entry.get("value")
                            else:
                                continue
                            if k == "state" and v:
                                try:
                                    obj = json.loads(v)
                                    if obj.get("switchBalance") != "real":
                                        obj["switchBalance"] = "real"
                                        v = json.dumps(obj)
                                        print("  [login] Patched cached state.switchBalance -> real")
                                except Exception:
                                    pass
                            await page.evaluate(
                                "([k, v]) => { try { localStorage.setItem(k, v); } catch(e){} }",
                                [k, v],
                            )
            except Exception as e:
                print(f"  [login] localStorage restore failed: {e}")

        token = await page.evaluate("() => localStorage.getItem('token')")
        if token and not force:
            ok = await page.evaluate(
                """async () => {
                    const t = localStorage.getItem('token');
                    const sid = localStorage.getItem('sessionId');
                    if (!t) return false;
                    try {
                        const r = await fetch(
                            'https://kong-proxy.prod.gmwrgs.com/players-be/v1/fo/se/players',
                            { headers: { 'Authorization': 'Bearer ' + t, 'sessionid': sid || '', 'Accept': 'application/json' } }
                        );
                        return r.status === 200;
                    } catch (e) { return false; }
                }"""
            )
            if ok:
                print("  [login] Saved session still valid")
                # Even with a valid session, we must force switchBalance to
                # "real" each run — CarnivalCiti resets it on page reload.
                try:
                    await self._switch_to_real(page)
                except Exception as e:
                    print(f"  [login] WARNING: switch to real failed: {e}")
                return True
            print("  [login] Saved token stale — fresh login")

        email, password = _parse_access_file(self.ACCESS_FILE)
        if not email or not password:
            print(f"  [login] Missing credentials in {self.ACCESS_FILE}")
            return False

        if not await self._do_login(context, page, email, password):
            return False

        try:
            await self._switch_to_real(page)
        except Exception as e:
            print(f"  [login] WARNING: switch to real failed: {e}")

        try:
            state = await context.storage_state(path=str(self.STORAGE_FILE))
            wrapped = {"saved_at": datetime.now().isoformat(), **state}
            self.STORAGE_FILE.write_text(json.dumps(wrapped, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"  [login] WARNING: could not save storage state: {e}")
        return True

    def _storage_age(self) -> Optional[timedelta]:
        if not self.STORAGE_FILE.exists():
            return None
        try:
            meta = json.loads(self.STORAGE_FILE.read_text(encoding="utf-8"))
            if "saved_at" not in meta:
                return None
            return datetime.now() - datetime.fromisoformat(meta["saved_at"])
        except Exception:
            return None

    # ─── per-game ────────────────────────────────────────────────────────

    async def _scrape_game(self, page: Page, game: dict) -> dict:
        """Scrape one CarnivalCiti game with bounded retry.

        Root cause of the historic "Carnival tracking silently fails"
        bug: the page took longer than 180s to mount + emit the
        jackpot/get response (especially the last 3-4 games after the
        SPA warmed up). Old code gave up after one 180s attempt. New
        code retries up to N times with a hard wall-clock cap, and
        records a `got_data` flag so the caller can verify each game
        produced a reading inside the cycle window.
        """
        code = game["code"]
        name = game["name"]
        print(f"\n  [carnivalciti] Scraping {name} ({code})")
        captured = {"done": False, "result": None}
        code_marker = f"/CC/{code}/"
        code_marker_alt = f"/CC/{code}?"

        async def on_response(resp):
            if captured["done"]:
                return
            url = resp.url
            if "jackpot/get" not in url or "CC/" not in url:
                return
            if code_marker not in url and code_marker_alt not in url:
                return
            try:
                text = await resp.text()
            except Exception:
                return
            m = re.search(r"(\{.*\})", text)
            if not m:
                return
            try:
                data = json.loads(m.group(1))
            except Exception:
                return
            result = _extract_biggest_dedup(data)
            if result:
                value, jp_name = result
                save_jackpot(self.source, code, name, value, jp_name)
                captured["done"] = True
                captured["result"] = {"value": value, "jackpot_name": jp_name}

        page.on("response", on_response)
        try:
            # Up to MAX_ATTEMPTS goto+wait passes per game, with a hard
            # TOTAL deadline so we never blow past the cycle window.
            MAX_ATTEMPTS = 3
            PER_ATTEMPT_DEADLINE = 90  # seconds
            total_deadline = datetime.now() + timedelta(seconds=PER_ATTEMPT_DEADLINE * MAX_ATTEMPTS)
            attempt = 0
            while attempt < MAX_ATTEMPTS and datetime.now() < total_deadline:
                attempt += 1
                captured["done"] = False
                captured["result"] = None
                print(f"  [carnivalciti] {name}: attempt {attempt}/{MAX_ATTEMPTS}")
                try:
                    await page.goto(
                        self.GAME_URL_TEMPLATE.format(code=code),
                        wait_until="domcontentloaded",
                        timeout=75000,
                    )
                except Exception as e:
                    print(f"  [load] {name} attempt {attempt}: {e}")
                attempt_deadline = datetime.now() + timedelta(seconds=PER_ATTEMPT_DEADLINE)
                while not captured["done"] and datetime.now() < attempt_deadline and datetime.now() < total_deadline:
                    await asyncio.sleep(2)
                if captured["done"]:
                    break  # got data, no need to retry
                print(f"  [carnivalciti] {name}: no data in {PER_ATTEMPT_DEADLINE}s, will retry")
            if captured["done"]:
                v = captured["result"]["value"]
                n = captured["result"]["jackpot_name"]
                print(f"  [OK] {name}: {n} = {v:,.2f} SC (CC)")
                return {"code": code, "name": name, **captured["result"], "attempts": attempt, "got_data": True}
            print(f"  [WARN] {name}: no CC jackpot after {attempt} attempts")
            return {"code": code, "name": name, "error": "no_cc_jackpot", "attempts": attempt, "got_data": False}
        finally:
            # Always remove the listener — even on goto exception or
            # timeout — so it doesn't bleed into the next game.
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass


# ─── Registry ──────────────────────────────────────────────────────────────

SCRAPERS = {
    "zitobox": ZitoboxScraper(),
    "carnivalciti": CarnivalCitiScraper(),
}


def get_scraper(source: str):
    return SCRAPERS.get(source)
