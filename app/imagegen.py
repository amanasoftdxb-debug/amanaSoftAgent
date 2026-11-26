"""Image generation through browser cookies, with three accounts (A, then B, then C).

Account A is Bing Image Creator when a Bing "_U" cookie is set (BING_HARDCODED_U / BING_U), otherwise Gemini.
Accounts B and C are Gemini. When an account's cookie stops working it is marked dead, the next account is
used, and an alert email (to ALERT_EMAIL_TO) explains how to put a fresh cookie in app/config.py.
"""

import asyncio
import html
import logging
import os
import re
import tempfile
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import BING_HARDCODED_U, GEMINI_HARDCODED, get_settings

os.environ.setdefault("GEMINI_COOKIE_PATH", str(get_settings().data_dir / "gemini_cookies"))

from gemini_webapi import GeminiClient  # noqa: E402
from gemini_webapi.exceptions import (  # noqa: E402
    AuthError, TemporarilyBlockedError, UsageLimitExceededError,
)
from gemini_webapi.types.image import GeneratedImage  # noqa: E402

from . import emailer  # noqa: E402
from .store import get_store  # noqa: E402

log = logging.getLogger("gemini")
SLOTS = ("A", "B", "C")


class ImageGenError(RuntimeError):
    pass


class NoImageReturned(ImageGenError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bing_cookie() -> str:
    return (BING_HARDCODED_U or get_settings().bing_u or "").strip()


def is_bing(slot: str) -> bool:
    return slot == "A" and bool(bing_cookie())


class BingBlocked(ImageGenError):
    """Bing refused this prompt (its content rules); another account may still make it."""


class BingClient:
    """Bing Image Creator (bing.com/images/create), logged in with the "_U" cookie.
    Fast "boosts" are used while there are any, then Bing's free slower queue."""

    BASE = "https://www.bing.com"
    HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/131.0 Safari/537.36",
               "Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.bing.com/images/create"}
    IMG_RE = re.compile(r'src="(https://tse\d*\.mm\.bing\.net/th/id/OIG[^"]+)"')

    def __init__(self, u: str, proxy: str | None = None):
        self.http = httpx.AsyncClient(headers=self.HEADERS, cookies={"_U": u}, proxy=proxy, timeout=60)

    async def close(self):
        await self.http.aclose()

    async def check(self):
        r = await self.http.get(f"{self.BASE}/images/create", follow_redirects=True)
        if r.status_code != 200 or '"isSignedIn":true' not in r.text:
            raise AuthError(f"Bing cookie is logged out (HTTP {r.status_code})")

    async def generate(self, prompt: str) -> bytes:
        q = urllib.parse.quote(prompt)
        rid, errors = None, []
        for rt in ("4", "3"):  # 4 = fast (uses a boost), 3 = free slower queue
            r = await self.http.post(f"{self.BASE}/images/create?q={q}&rt={rt}&FORM=GENCRE",
                                     data={"q": prompt, "qs": "ds"})
            m = re.search(r"[?&]id=([^&]+)", r.headers.get("location") or "")
            if r.status_code in (301, 302) and m:
                rid = m.group(1)
                break
            if "login.live.com" in (r.headers.get("location") or "") or r.status_code in (401, 403):
                raise AuthError(f"Bing cookie is logged out (HTTP {r.status_code})")
            err = re.search(r'class="gil_err_(?:mt|sbt)"[^>]*>([^<]+)', r.text)
            errors.append(html.unescape(err.group(1)).strip() if err else f"HTTP {r.status_code}")
        if not rid:
            if any(w in " ".join(errors).lower() for w in ("blocked", "content", "policy", "unsafe", "review")):
                raise BingBlocked("Bing blocked the prompt: " + " | ".join(errors))
            raise ImageGenError("Bing did not start the picture: " + " | ".join(errors))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 300
        while loop.time() < deadline:
            await asyncio.sleep(4)
            r = await self.http.get(f"{self.BASE}/images/create/async/results/{rid}?q={q}")
            urls = [html.unescape(u) for u in self.IMG_RE.findall(r.text)]
            if urls:
                img = await self.http.get(urls[0], follow_redirects=True)
                img.raise_for_status()
                return img.content
            if "gil_err" in r.text:
                err = re.search(r'class="gil_err_(?:mt|sbt)"[^>]*>([^<]+)', r.text)
                raise BingBlocked("Bing did not make the picture: " + (html.unescape(err.group(1)) if err else "unsafe"))
        raise ImageGenError("Bing took more than 5 minutes to make the picture")


class GeminiPool:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.clients: dict[str, GeminiClient | BingClient] = {}

    # ----- cookies and status (kept in the store so they survive restarts) -----

    async def cookies(self, slot: str) -> tuple[str, str]:
        """The cookie to use for an account.

        The app saves Gemini's refreshed copy of the cookie (so restarts keep working). A cookie you put in
        .env (or Render's Environment tab) wins whenever it is new: a different login, or the same login with a
        fresh __Secure-1PSIDTS that you pasted. Otherwise the newest refreshed copy is used."""
        if is_bing(slot):
            return bing_cookie(), ""
        s = get_settings()
        store = get_store()
        env_psid = (getattr(s, f"gemini_{slot.lower()}_1psid") or "").strip()
        env_ts = (getattr(s, f"gemini_{slot.lower()}_1psidts") or "").strip()
        if GEMINI_HARDCODED.get(slot):  # cookies written in app/config.py win over .env / Render
            env_psid, env_ts = (v.strip() for v in GEMINI_HARDCODED[slot])
        saved = await store.get_setting(f"gemini_cookie_{slot}") or {}
        if env_psid:
            same_login = saved.get("psid") == env_psid
            if same_login and "env_psidts" not in saved:
                saved = {**saved, "env_psidts": env_ts}  # older saved copy: remember which .env value it came from
                await store.set_setting(f"gemini_cookie_{slot}", saved)
            elif not same_login or saved.get("env_psidts") != env_ts:
                saved = {"psid": env_psid, "psidts": env_ts, "env_psidts": env_ts, "saved_at": _now()}
                await store.set_setting(f"gemini_cookie_{slot}", saved)
                await store.set_setting(f"gemini_status_{slot}", {"state": "unknown", "checked_at": _now()})
                log.info("New cookie for Gemini account %s taken from the environment", slot)
        if saved.get("psid"):
            return saved["psid"], saved.get("psidts", "")
        return env_psid, env_ts

    async def status(self, slot: str) -> dict:
        psid, _ = await self.cookies(slot)
        st = await get_store().get_setting(f"gemini_status_{slot}") or {}
        if not psid:
            st = {**st, "state": "missing"}
        return {"slot": slot, "state": st.get("state", "unknown"), "error": st.get("error"),
                "checked_at": st.get("checked_at"), "last_ok": st.get("last_ok")}

    async def _set_status(self, slot: str, state: str, error: str | None = None):
        store = get_store()
        prev = await store.get_setting(f"gemini_status_{slot}") or {}
        st = {**prev, "state": state, "error": error, "checked_at": _now()}
        if state == "ok":
            st["last_ok"] = _now()
        await store.set_setting(f"gemini_status_{slot}", st)
        if state == "dead" and prev.get("state") != "dead":
            others = [f"{s}: {(await self.status(s))['state']}" for s in SLOTS if s != slot]
            await emailer.alert_cookie_dead(slot, error or "", others)

    async def _persist_rotated(self, slot: str, client: GeminiClient):
        """Gemini refreshes __Secure-1PSIDTS over time; save the newest value so a restart keeps working."""
        if not isinstance(client, GeminiClient):
            return
        try:
            jar = {c.name: c.value for c in client.cookies.jar}
        except Exception:
            return
        store = get_store()
        saved = await store.get_setting(f"gemini_cookie_{slot}") or {}
        psid, psidts = await self.cookies(slot)
        new_ts = jar.get("__Secure-1PSIDTS")
        if new_ts and jar.get("__Secure-1PSID") == psid and new_ts != psidts:
            await store.set_setting(f"gemini_cookie_{slot}", {**saved, "psid": psid, "psidts": new_ts, "saved_at": _now()})

    # ----- clients -----

    async def _drop_client(self, slot: str):
        client = self.clients.pop(slot, None)
        if client:
            try:
                await client.close()
            except Exception:
                pass

    async def _client(self, slot: str) -> GeminiClient | BingClient:
        if slot in self.clients:
            return self.clients[slot]
        psid, psidts = await self.cookies(slot)
        if not psid:
            raise AuthError(f"No cookie saved for account {slot}")
        if is_bing(slot):
            bing = BingClient(psid, proxy=get_settings().gemini_proxy or None)
            try:
                await bing.check()
            except BaseException:
                await bing.close()
                raise
            self.clients[slot] = bing
            return bing
        client = GeminiClient(psid, psidts or None, proxy=get_settings().gemini_proxy or None)
        await client.init(timeout=240, auto_close=False, auto_refresh=True, refresh_interval=600)
        if not client._check_account_status():
            status = getattr(client.account_status, "name", str(client.account_status))
            await client.close()
            raise AuthError(f"Google account not usable for Gemini ({status})")
        self.clients[slot] = client
        await self._persist_rotated(slot, client)
        return client

    async def check(self, slot: str) -> dict:
        """Fresh login test for one account (no image is generated)."""
        async with self.lock:
            await self._drop_client(slot)
            try:
                await self._client(slot)
                await self._set_status(slot, "ok")
            except AuthError as e:
                await self._set_status(slot, "missing" if "No cookie" in str(e) else "dead", str(e))
            except Exception as e:
                await self._set_status(slot, "error", f"{type(e).__name__}: {e}")
        return await self.status(slot)

    # ----- generation -----

    async def _generate_with(self, slot: str, prompt: str) -> bytes:
        client = await self._client(slot)
        if isinstance(client, BingClient):
            return await client.generate(prompt)
        chat = client.start_chat(model=get_settings().gemini_model or None)
        out = await chat.send_message(prompt)
        images = [i for i in out.images if isinstance(i, GeneratedImage)]
        if not images:
            # Gemini sometimes answers with a question first; nudge it once in the same chat.
            out = await chat.send_message("Please generate the image now exactly as described, with no text in it.")
            images = [i for i in out.images if isinstance(i, GeneratedImage)]
        if not images:
            raise NoImageReturned(f"Gemini replied without an image: {(out.text or '')[:300]}")
        tmp = Path(tempfile.gettempdir()) / "post-agent"
        path = await images[0].save(path=str(tmp), filename=f"{uuid.uuid4().hex}.png", full_size=True)
        data = Path(path).read_bytes()
        Path(path).unlink(missing_ok=True)
        await self._persist_rotated(slot, client)
        return data

    async def generate(self, prompt: str, only_slot: str | None = None) -> tuple[bytes, str]:
        """Returns (image bytes, slot used). Tries account A, then B, then C (or only `only_slot`)."""
        errors = []
        async with self.lock:
            for slot in ([only_slot] if only_slot else SLOTS):
                st = await self.status(slot)
                if st["state"] == "missing":
                    continue
                try:
                    data = await self._generate_with(slot, prompt)
                    await self._set_status(slot, "ok")
                    return data, slot
                except NoImageReturned:
                    raise
                except BingBlocked as e:
                    errors.append(f"{slot}: {e}")  # the account is fine; the next account may accept the prompt
                except AuthError as e:
                    await self._drop_client(slot)
                    await self._set_status(slot, "dead", str(e))
                    errors.append(f"{slot}: cookie expired ({e})")
                except (UsageLimitExceededError, TemporarilyBlockedError) as e:
                    await self._set_status(slot, "limited", str(e))
                    errors.append(f"{slot}: usage limit ({e})")
                except Exception as e:
                    await self._drop_client(slot)
                    await self._set_status(slot, "error", f"{type(e).__name__}: {e}")
                    errors.append(f"{slot}: {type(e).__name__}: {e}")
                    log.exception("Gemini account %s failed", slot)
        if not errors:
            raise ImageGenError("No Gemini cookies set. Put them in GEMINI_A_1PSID / GEMINI_A_1PSIDTS (.env or Render Environment).")
        raise ImageGenError("All Gemini accounts failed. " + " | ".join(errors))

    async def describe(self, prompt: str, pictures: list[bytes]) -> tuple[str, str]:
        """Backup for looking at the client's own pictures: Gemini reads them and answers in text.
        Returns (answer, slot). Cookie states are left to the regular checks."""
        tmp = Path(tempfile.gettempdir()) / "post-agent"
        tmp.mkdir(parents=True, exist_ok=True)
        paths = []
        for data in pictures:
            p = tmp / f"look-{uuid.uuid4().hex}.jpg"
            p.write_bytes(data)
            paths.append(p)
        errors = []
        try:
            async with self.lock:
                for slot in SLOTS:
                    if is_bing(slot) or (await self.status(slot))["state"] in ("missing", "dead"):
                        continue  # Bing only makes pictures; it can't look at them
                    try:
                        client = await self._client(slot)
                        out = await client.generate_content(prompt, files=[str(p) for p in paths], temporary=True)
                        if (out.text or "").strip():
                            return out.text, slot
                        errors.append(f"{slot}: empty answer")
                    except Exception as e:
                        await self._drop_client(slot)
                        errors.append(f"{slot}: {type(e).__name__}: {e}")
        finally:
            for p in paths:
                p.unlink(missing_ok=True)
        raise ImageGenError("Gemini could not look at the pictures: " + (" | ".join(errors) or "no working Gemini account"))

    async def health_loop(self):
        """Test every cookie shortly after start and then every few hours. In between, save the
        cookie values Gemini refreshes in the background, so a Render restart doesn't lose them."""
        every = max(get_settings().gemini_health_check_hours, 0.5) * 3600
        next_check = asyncio.get_running_loop().time() + 30
        while True:
            await asyncio.sleep(30 if not self.clients else 600)
            for slot, client in list(self.clients.items()):
                try:
                    await self._persist_rotated(slot, client)
                except Exception:
                    log.exception("Could not save refreshed cookie for %s", slot)
            if asyncio.get_running_loop().time() < next_check:
                continue
            next_check = asyncio.get_running_loop().time() + every
            for slot in SLOTS:
                try:
                    if (await self.status(slot))["state"] != "missing":
                        await self.check(slot)
                except Exception:
                    log.exception("Cookie health check failed for %s", slot)


pool = GeminiPool()
