"""Owner panel for the portal — builders + one action handler.

This project drives everything through ONE big callback dispatcher in
bot/app.py (not per-command decorators like Makiioo), so the panel is exposed as
plain functions the dispatcher calls, keeping the logic here and only thin
delegation lines in app.py.
"""
from __future__ import annotations

import contextlib

from . import net as portal_net
from . import status as portal_status
from . import stats


_STATE_LABEL = {"off": "⚫ خاموش", "starting": "🟡 در حال شروع",
                "running": "🟢 فعال", "failed": "🔴 خطا"}


def _log_line(store) -> str:
    if not store.log_group_id:
        return "🗒 گروه لاگ: ثبت نشده"
    state = "🟢 روشن" if store.log_group_enabled else "⚪ خاموش"
    return f"🗒 گروه لاگ: {state}  (id {store.log_group_id})"


def _warm_browsers() -> str:
    try:
        from capture.pool import pool
        st = pool.status()
        return f"{st.get('warm', 0)} گرم / سقف {st.get('max_open', 1)}"
    except Exception:  # noqa: BLE001
        return "—"


def panel_text(store) -> str:
    snap = portal_status.snapshot()
    s = stats.summary()
    today, total = s["today"], s["total"]
    mode = "دامنه اختصاصی" if store.portal_mode == "domain" else "لینک سریع (Quick)"
    url = snap.get("url") or store.portal_url or "—"
    lines = [
        "🌐 پنل پورتال ورود", "-" * 31,
        f"{_STATE_LABEL.get(snap['status'], snap['status'])}  |  {mode}",
        f"📥 امروز: {today['success']}/{today['started']}  (موفقیت {today['rate']:g}٪)",
        f"⏳ منتظر کد: {today['pending']}  |  منقضی: {today['expired']}  |  ناموفق: {today['failed']}",
        f"❌ کد اشتباه: {today['wrong_code_events']}   📦 کل ورودی موفق: {total['success']}",
        f"🖥 مرورگرهای گرم: {_warm_browsers()}",
        _log_line(store),
        f"🔗 {url}",
    ]
    if snap.get("detail"):
        lines.append(f"• {snap['detail']}")
    return "\n".join(lines)


def panel_kb(Button, store):
    return [
        [Button.inline("🔴 خاموش کن" if store.portal_enabled else "🟢 روشن کن",
                       b"portal:toggle")],
        [Button.inline("🔗 لینک سریع", b"portal:mode:quick"),
         Button.inline("🌍 دامنه اختصاصی", b"portal:domain")],
        [Button.inline("📊 آمار کامل", b"portal:stats"),
         Button.inline("🔄 ری‌استارت", b"portal:restart")],
        [Button.inline("🗒 گروه لاگ", b"portal:log")],
        [Button.inline("♻️ بروزرسانی", b"portal:panel")],
        [Button.inline("⬅ Settings", b"menu:settings")],
    ]


def log_text(store) -> str:
    gid = store.log_group_id
    return "\n".join([
        "🗒 گروه لاگ مرکزی", "-" * 31,
        f"وضعیت: {'🟢 روشن' if store.log_group_enabled else '⚪ خاموش'}",
        f"آیدی گروه: {gid if gid else 'ثبت نشده'}",
        "",
        "به این گروه ارسال می‌شود:",
        "• هر پیامی که ربات به اکانت‌های اضافه‌شده می‌فرستد",
        "• هر ورود حساب از طریق پورتال",
        "",
        "روش فعال‌سازی: ربات را در گروه ادمین کن، بعد آیدی عددی گروه را",
        "اینجا ثبت کن (مثبت یا منفی؛ گروه‌ها معمولاً با -100 شروع می‌شوند).",
    ])


def log_kb(Button, store):
    return [
        [Button.inline("🔢 ثبت/تغییر آیدی گروه", b"portal:log:set")],
        [Button.inline("🔴 خاموش کن" if store.log_group_enabled else "🟢 روشن کن",
                       b"portal:log:toggle")],
        [Button.inline("🧪 تست پیام", b"portal:log:test")],
        [Button.inline("⬅ پورتال", b"portal:panel")],
    ]


def domain_text(store) -> str:
    snap = portal_status.snapshot()
    return "\n".join([
        "🌍 تنظیمات دامنه اختصاصی", "-" * 31,
        f"دامنه: {store.portal_domain or 'ثبت نشده'}",
        f"توکن Cloudflare: {'ثبت شده' if store.portal_cf_token else 'ثبت نشده'}",
        f"DNS: {snap.get('dns', '—')}  |  SSL: {snap.get('ssl', '—')}  |  Ping: {snap.get('domain_ping', '—')}",
        f"جزئیات: {snap.get('detail') or '—'}",
    ])


def domain_kb(Button):
    return [
        [Button.inline("🌐 ثبت/تغییر دامنه", b"portal:domain:set"),
         Button.inline("🔑 ثبت توکن", b"portal:token:set")],
        [Button.inline("🧪 تست دامنه", b"portal:domain:test"),
         Button.inline("🚀 فعال‌سازی دامنه", b"portal:domain:go")],
        [Button.inline("🗑 حذف تنظیمات دامنه", b"portal:domain:del")],
        [Button.inline("🔗 بازگشت به لینک سریع", b"portal:mode:quick")],
        [Button.inline("⬅ پورتال", b"portal:panel")],
    ]


def stats_text() -> str:
    data = stats.summary()
    rows = ["📊 آمار کامل پورتال", "-" * 31]
    for title, key in (("امروز", "today"), ("کل", "total")):
        it = data[key]
        rows += [
            f"{title}: شروع {it['started']} | موفق {it['success']} | {it['rate']:g}٪",
            f"   منتظر {it['pending']} | منقضی {it['expired']} | ناموفق {it['failed']} | کد اشتباه {it['wrong_code_events']}",
        ]
    return "\n".join(rows)
