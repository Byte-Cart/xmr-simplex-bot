#!/usr/bin/env python3
"""
XMR price monitor bot for SimpleX Chat (multi-currency).

Connects to a local SimpleX Chat CLI over its WebSocket API (per
https://github.com/simplex-chat/simplex-chat/blob/stable/bots/README.md)
and polls the CoinGecko keyless public API for the Monero price in one or
more fiat currencies (one request per poll, whatever the number of currencies).

Commands (currency is optional and defaults to the first in VS_CURRENCIES):
  /price [cur]               XMR price in all currencies, or just one
  /above <price> [cur]       one-shot alert when price rises to/above <price>
  /below <price> [cur]       one-shot alert when price falls to/below <price>
  /move <percent> [cur]      alert every time price moves <percent>% from last alert
  /alerts                    list your active alerts
  /clear                     remove all your alerts
  /help                      show this list

Environment variables (all optional):
  SIMPLEX_WS     WebSocket URL of the CLI          (default ws://127.0.0.1:5225)
  VS_CURRENCIES  comma-separated currencies, first is the default
                 (default: VS_CURRENCY if set, else usd), e.g. usd,eur,gbp
  POLL_SECONDS   price poll interval in seconds     (default 120, minimum 60)
  CG_DEMO_KEY    CoinGecko Demo API key (sent as x-cg-demo-api-key)
  PRICE_PROXY    proxy for price requests only, e.g. socks5://127.0.0.1:9050
  STATE_FILE     where alerts are persisted          (default xmr_bot_state.json)
"""

import asyncio
import itertools
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
import websockets

WS_URL = os.getenv("SIMPLEX_WS", "ws://127.0.0.1:5225")
CURRENCIES = [
    c.strip().lower()
    for c in os.getenv("VS_CURRENCIES", os.getenv("VS_CURRENCY", "usd")).split(",")
    if c.strip()
] or ["usd"]
DEFAULT_CUR = CURRENCIES[0]
POLL_SECONDS = max(60, int(os.getenv("POLL_SECONDS", "120")))
CG_KEY = os.getenv("CG_DEMO_KEY")
PROXY = os.getenv("PRICE_PROXY")
STATE_FILE = Path(os.getenv("STATE_FILE", "xmr_bot_state.json"))

CG_URL = "https://api.coingecko.com/api/v3/simple/price"
MAX_ALERTS_PER_CONTACT = 20
MAX_BACKOFF = 1800

CUR_LIST = ", ".join(c.upper() for c in CURRENCIES)
HELP = (
    "*XMR Price Bot*\n"
    "/price - XMR in all currencies\n"
    "/'price <cur>' - XMR in one currency\n"
    "/'above <price> [cur]' - alert when price >= value\n"
    "/'below <price> [cur]' - alert when price <= value\n"
    "/'move <percent> [cur]' - alert on % move from last alert\n"
    "/alerts - list your alerts\n"
    "/clear - remove all your alerts\n"
    f"Currencies: {CUR_LIST} (default {DEFAULT_CUR.upper()}). "
    f"Prices from CoinGecko, polled every {POLL_SECONDS}s."
)

log = logging.getLogger("xmr_bot")


# ---------------------------------------------------------------- state ----

def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        return {"alerts": {}, "moves": {}}
    except (json.JSONDecodeError, OSError) as e:
        log.error("Could not read %s (%s); starting with empty state", STATE_FILE, e)
        return {"alerts": {}, "moves": {}}
    data.setdefault("alerts", {})
    data.setdefault("moves", {})
    # Alerts saved by the single-currency version have no "cur": they were
    # set in whatever VS_CURRENCY was then; assume the current default.
    for alerts in data["alerts"].values():
        for a in alerts:
            a.setdefault("cur", DEFAULT_CUR)
    for mv in data["moves"].values():
        mv.setdefault("cur", DEFAULT_CUR)
    return data


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- price ----

async def fetch_prices(client: httpx.AsyncClient):
    """One request for all currencies. Returns ({cur: price}, {cur: 24h%}, ts)."""
    params = {
        "ids": "monero",
        "vs_currencies": ",".join(CURRENCIES),
        "include_24hr_change": "true",
        "include_last_updated_at": "true",
    }
    headers = {"accept": "application/json"}
    if CG_KEY:
        headers["x-cg-demo-api-key"] = CG_KEY
    r = await client.get(CG_URL, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    d = r.json()["monero"]
    prices = {c: float(d[c]) for c in CURRENCIES if c in d}
    changes = {c: d.get(f"{c}_24h_change") for c in CURRENCIES}
    missing = [c for c in CURRENCIES if c not in prices]
    if missing:
        log.warning("CoinGecko returned no price for: %s (unsupported currency code?)",
                    ", ".join(missing))
    return prices, changes, d.get("last_updated_at")


def fmt_price(p: float, cur: str) -> str:
    return f"{p:,.2f} {cur.upper()}"


def unwrap(resp: dict) -> dict:
    """Tolerate an Either-style {"Right": {...}} / {"Left": {...}} wrapper."""
    if isinstance(resp, dict) and "type" not in resp and len(resp) == 1:
        (key, inner), = resp.items()
        if key in ("Right", "Left") and isinstance(inner, dict):
            return inner
    return resp


# ------------------------------------------------------------------ bot ----

class XmrBot:
    def __init__(self):
        self.ws = None
        self.corr = itertools.count(1)
        self.state = load_state()
        self.last = None  # (prices, changes, cg_timestamp, fetched_at)

    # --- SimpleX API ---

    async def send_cmd(self, cmd: str) -> None:
        if self.ws is None:
            raise ConnectionError("not connected to SimpleX CLI")
        await self.ws.send(json.dumps({"corrId": str(next(self.corr)), "cmd": cmd}))

    async def send_text(self, contact_id: int, text: str) -> None:
        # APISendMessages: /_send <sendRef> json <composedMessages>
        msgs = [{"msgContent": {"type": "text", "text": text}}]
        await self.send_cmd(f"/_send @{contact_id} json {json.dumps(msgs)}")

    async def handle(self, msg: dict) -> None:
        resp = unwrap(msg.get("resp") or {})
        rtype = resp.get("type")

        if msg.get("corrId"):  # response to one of our commands
            if rtype in ("chatCmdError", "chatError"):
                log.error("Command error: %s", json.dumps(resp)[:500])
            return

        if rtype == "contactConnected":
            cid = resp.get("contact", {}).get("contactId")
            if cid is not None:
                log.info("New contact %s", cid)
                await self.send_text(cid, HELP)

        elif rtype == "newChatItems":
            for item in resp.get("chatItems", []):
                chat_info = item.get("chatInfo", {})
                if chat_info.get("type") != "direct":
                    continue
                content = item.get("chatItem", {}).get("content", {})
                if content.get("type") != "rcvMsgContent":
                    continue
                mc = content.get("msgContent", {})
                if mc.get("type") != "text":
                    continue
                cid = chat_info.get("contact", {}).get("contactId")
                if cid is not None:
                    await self.on_text(cid, (mc.get("text") or "").strip())
        # all other events are ignored, as the API docs require

    # --- commands ---

    @staticmethod
    def parse_cur(token):
        """Returns a currency code, or None if the token isn't a configured one."""
        c = token.lower()
        return c if c in CURRENCIES else None

    def parse_value_and_cur(self, args):
        """'<number> [cur]' -> (value, cur), or (None, error_message_or_None)."""
        if not 1 <= len(args) <= 2:
            return None, None
        try:
            v = float(args[0].replace(",", ""))
        except ValueError:
            return None, None
        if v <= 0:
            return None, None
        if len(args) == 1:
            return v, DEFAULT_CUR
        cur = self.parse_cur(args[1])
        if cur is None:
            return None, f"Unknown currency '{args[1]}'. Available: {CUR_LIST}"
        return v, cur

    def current(self, cur):
        if not self.last:
            return None
        return self.last[0].get(cur)

    async def on_text(self, cid: int, text: str) -> None:
        parts = text.split()
        if not parts:
            return
        cmd, args = parts[0].lower(), parts[1:]
        key = str(cid)

        if cmd in ("/help", "/start"):
            await self.send_text(cid, HELP)

        elif cmd == "/price":
            if args:
                cur = self.parse_cur(args[0])
                if cur is None:
                    await self.send_text(cid, f"Unknown currency '{args[0]}'. Available: {CUR_LIST}")
                    return
                await self.send_text(cid, self.price_text([cur]))
            else:
                await self.send_text(cid, self.price_text(CURRENCIES))

        elif cmd in ("/above", "/below"):
            value, cur = self.parse_value_and_cur(args)
            if value is None:
                await self.send_text(
                    cid, cur or f"Usage: {cmd} <price> [currency], e.g. {cmd} 600 eur")
                return
            alerts = self.state["alerts"].setdefault(key, [])
            if len(alerts) >= MAX_ALERTS_PER_CONTACT:
                await self.send_text(cid, "Alert limit reached. Use /clear first.")
                return
            alerts.append({"dir": cmd[1:], "price": value, "cur": cur})
            save_state(self.state)
            await self.send_text(
                cid, f"OK, I'll alert you when XMR is {cmd[1:]} {fmt_price(value, cur)}.")

        elif cmd == "/move":
            pct, cur = self.parse_value_and_cur(args)
            if pct is None or pct > 100:
                msg = cur if (pct is None and cur) else \
                    "Usage: /move <percent> [currency], e.g. /move 5 eur"
                await self.send_text(cid, msg)
                return
            ref = self.current(cur)
            if ref is None:
                await self.send_text(cid, "No price yet, try again in a minute.")
                return
            self.state["moves"][key] = {"pct": pct, "ref": ref, "cur": cur}
            save_state(self.state)
            await self.send_text(
                cid, f"OK, I'll alert you on every {pct:g}% move from {fmt_price(ref, cur)}.")

        elif cmd == "/alerts":
            await self.send_text(cid, self.alerts_text(key))

        elif cmd == "/clear":
            self.state["alerts"].pop(key, None)
            self.state["moves"].pop(key, None)
            save_state(self.state)
            await self.send_text(cid, "All your alerts were removed.")

        else:
            await self.send_text(cid, "Unknown command. Send /help")

    def price_text(self, curs) -> str:
        if not self.last:
            return "No price yet, try again in a minute."
        prices, changes, ts, _ = self.last
        lines = ["*XMR price*"]
        for c in curs:
            if c not in prices:
                lines.append(f"{c.upper()}: unavailable")
                continue
            line = fmt_price(prices[c], c)
            if changes.get(c) is not None:
                line += f" ({changes[c]:+.2f}% 24h)"
            lines.append(line)
        if ts:
            t = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            lines.append(f"CoinGecko updated: {t}")
        return "\n".join(lines)

    def alerts_text(self, key: str) -> str:
        lines = [f"{a['dir']} {fmt_price(a['price'], a['cur'])}"
                 for a in self.state["alerts"].get(key, [])]
        mv = self.state["moves"].get(key)
        if mv:
            lines.append(f"move {mv['pct']:g}% (ref {fmt_price(mv['ref'], mv['cur'])})")
        return "Your alerts:\n" + "\n".join(lines) if lines else "You have no alerts."

    # --- monitoring ---

    async def check_alerts(self, prices: dict) -> None:
        if self.ws is None:
            return  # keep alerts until we can actually deliver them
        changed = False

        for key, alerts in list(self.state["alerts"].items()):
            remaining = []
            for a in alerts:
                price = prices.get(a["cur"])
                hit = price is not None and (
                    (a["dir"] == "above" and price >= a["price"]) or
                    (a["dir"] == "below" and price <= a["price"]))
                if hit:
                    await self.send_text(
                        int(key),
                        f"🔔 XMR is {a['dir']} {fmt_price(a['price'], a['cur'])}: "
                        f"now {fmt_price(price, a['cur'])}",
                    )
                    changed = True
                else:
                    remaining.append(a)
            if remaining:
                self.state["alerts"][key] = remaining
            else:
                self.state["alerts"].pop(key)

        for key, mv in self.state["moves"].items():
            price = prices.get(mv["cur"])
            if price is None:
                continue
            delta = (price - mv["ref"]) / mv["ref"] * 100
            if abs(delta) >= mv["pct"]:
                arrow = "📈" if delta > 0 else "📉"
                await self.send_text(
                    int(key),
                    f"{arrow} XMR moved {delta:+.2f}% from {fmt_price(mv['ref'], mv['cur'])}: "
                    f"now {fmt_price(price, mv['cur'])}",
                )
                mv["ref"] = price
                changed = True

        if changed:
            save_state(self.state)

    async def monitor(self, client: httpx.AsyncClient) -> None:
        delay = POLL_SECONDS
        while True:
            try:
                prices, changes, ts = await fetch_prices(client)
                if prices:
                    self.last = (prices, changes, ts, time.time())
                    log.info("XMR %s", " | ".join(fmt_price(p, c) for c, p in prices.items()))
                    await self.check_alerts(prices)
                delay = POLL_SECONDS
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    delay = min(delay * 2, MAX_BACKOFF)
                    log.warning("CoinGecko rate limit hit; backing off to %ss", delay)
                else:
                    log.error("CoinGecko HTTP %s", e.response.status_code)
            except Exception as e:  # network errors, bad JSON, send failures
                log.error("Monitor error: %r", e)
            await asyncio.sleep(delay)

    # --- main loop ---

    async def run(self) -> None:
        host = urlparse(WS_URL).hostname
        if host not in ("127.0.0.1", "localhost", "::1"):
            log.warning("SIMPLEX_WS is not localhost. The CLI WebSocket API has no auth "
                        "or encryption; put it behind a TLS proxy with auth.")
        log.info("Currencies: %s (default %s)", CUR_LIST, DEFAULT_CUR.upper())

        async with httpx.AsyncClient(proxy=PROXY) as client:
            monitor_task = asyncio.create_task(self.monitor(client))
            try:
                while True:
                    try:
                        async with websockets.connect(WS_URL, max_size=None) as ws:
                            self.ws = ws
                            log.info("Connected to SimpleX CLI at %s", WS_URL)
                            async for raw in ws:
                                try:
                                    await self.handle(json.loads(raw))
                                except Exception:
                                    log.exception("Failed to handle message")
                    except (OSError, websockets.exceptions.WebSocketException) as e:
                        log.warning("SimpleX connection lost: %r", e)
                    self.ws = None
                    await asyncio.sleep(5)
            finally:
                monitor_task.cancel()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(XmrBot().run())
    except KeyboardInterrupt:
        pass
