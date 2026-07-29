"""Tests for the non-blocking live card.

Run: python -m bot.tests.test_live_card

Why this matters: the card used to be edited INSIDE the send loop, so every
progress update put a Telegram round trip (and any edit rate limiting Telethon
sleeps through) between two messages. A run measured 6.2s per message while Eitaa
itself answered in 1-2s.

bot.app imports Telethon and needs credentials, so the class under test is loaded
from source into a tiny module with a fake `bot` client. That keeps the test
honest (it runs the real code) without pulling in the whole panel.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
import types
from pathlib import Path

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, extra: object = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name} {extra}")


class FakeMessage:
    def __init__(self, holder):
        self.holder = holder

    async def edit(self, text):
        # Telegram is slow and can rate-limit an edited message; that latency is
        # exactly what must NOT reach the send loop.
        await asyncio.sleep(self.holder.latency)
        self.holder.edits.append(text)


class FakeBot:
    def __init__(self, latency=0.05):
        self.latency = latency
        self.edits = []
        self.sends = []

    async def send_message(self, chat_id, text):
        await asyncio.sleep(self.latency)
        self.sends.append(text)
        return FakeMessage(self)


def load_live_card(fake_bot):
    """Load the real LiveCard class with a stubbed module namespace."""
    src = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    m = re.search(r"^class LiveCard:.*?(?=^class |^def |^# ---- keyboards)",
                  src, re.S | re.M)
    if not m:
        raise AssertionError("LiveCard class not found in bot/app.py")
    mod = types.ModuleType("live_card_under_test")
    mod.__dict__.update({
        "asyncio": asyncio, "time": time, "bot": fake_bot,
        "MessageNotModifiedError": type("MessageNotModifiedError", (Exception,), {}),
    })
    exec(compile("from __future__ import annotations\n" + m.group(0),
                 "<live_card>", "exec"), mod.__dict__)
    return mod.LiveCard


def test_set_does_not_block():
    print("set() never waits for Telegram")
    fake = FakeBot(latency=0.25)
    LiveCard = load_live_card(fake)

    async def go():
        card = LiveCard(1, min_interval=0.05)
        t0 = time.perf_counter()
        for i in range(20):
            await card.set(f"tick {i}")
        elapsed = time.perf_counter() - t0
        await asyncio.sleep(0.9)      # let the painter catch up
        await card.flush()
        card.close()
        return elapsed, card

    elapsed, card = asyncio.run(go())
    check(f"20 updates cost the loop almost nothing ({elapsed*1000:.0f}ms)",
          elapsed < 0.15, elapsed)
    check("something was actually painted", (fake.sends or fake.edits), fake.sends)
    check("intermediate updates were dropped, not queued", card.dropped > 0,
          card.dropped)
    check("far fewer paints than updates", card.paints < 20, card.paints)


def test_final_state_is_painted():
    print("the last state always reaches Telegram")
    fake = FakeBot(latency=0.01)
    LiveCard = load_live_card(fake)

    async def go():
        card = LiveCard(1, min_interval=5)   # deliberately long interval
        await card.set("first")
        await asyncio.sleep(0.1)
        await card.set("FINAL")
        await card.flush()
        card.close()

    asyncio.run(go())
    painted = (fake.sends + fake.edits)
    check("the final text was delivered", "FINAL" in painted, painted)


def test_painter_survives_telegram_errors():
    print("a Telegram error never breaks the job")

    class Boom(FakeBot):
        async def send_message(self, chat_id, text):
            raise RuntimeError("telegram is down")

    fake = Boom(latency=0)
    LiveCard = load_live_card(fake)

    async def go():
        card = LiveCard(1, min_interval=0.01)
        for i in range(5):
            await card.set(f"x{i}")
        await asyncio.sleep(0.2)
        await card.flush()
        card.close()
        return True

    check("no exception reached the caller", asyncio.run(go()) is True)


def main() -> int:
    for fn in (test_set_does_not_block, test_final_state_is_painted,
               test_painter_survives_telegram_errors):
        fn()
    print()
    print(f"{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    code = main()
    print("ALL LIVE CARD TESTS PASSED" if code == 0 else "LIVE CARD TESTS FAILED")
    sys.exit(code)
