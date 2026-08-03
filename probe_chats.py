#!/usr/bin/env python3
"""
CHAT CENSUS PROBE -- count and classify EVERY chat, and cross-check the number.

Why: the previous probe reported 24 PV chats and the owner is certain there are
more. The pagination in that probe stopped on `dialogs.length < 100`, which is
wrong if Eitaa returns fewer dialogs per call than requested -- one short page
and it would quit having seen almost nothing. It also passed folder_id 0 only,
so anything archived was invisible. This probe fixes both and then verifies the
answer against a completely independent source.

What it measures:

  1  dialogs, folder 0   -- paged until an EMPTY page, logging every page size
                            and the exact reason it stopped
  2  dialogs, folder 1   -- the archive, which folder 0 hides
  3  classification      -- peerUser split into contact / non-contact / bot /
                            self / deleted, plus basic groups and channels
                            (broadcast vs megagroup)
  4  contacts.getContacts -- the authoritative contact id set, to label PVs
                            as contact or non-contact without guessing
  5  DOM cross-check     -- the project's own driver.collect_all_chats(), which
                            scrolls the real chat list. If this disagrees with
                            the API count, the API paging is still wrong.
  6  photo census        -- per PV, one cheap search to learn how many photos it
                            holds and how many were sent by us (concurrent,
                            capped)
  7  reconciliation      -- every number side by side, with the gaps called out

Read-only: getDialogs, getContacts, search, and a UI scroll. Nothing is sent,
nothing is written to Eitaa, no bot state is touched.

Usage:
    cd ~/Mkwlsoso
    .venv/bin/python probe_chats.py 989124089268

    # skip the slow DOM scroll:
    .venv/bin/python probe_chats.py 989124089268 --no-dom

Ctrl+C is safe: whatever has been measured is printed and saved.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PYTHONUNBUFFERED", "1")

from config import config  # noqa: E402
from capture.pool import pool as session_pool  # noqa: E402
from eitaa.driver import EitaaDriver  # noqa: E402

config.ensure_dirs()

OUT = Path("/tmp/photo_probe_chats.json")
RESULT: dict = {"steps": {}}

MAX_PAGES = 40          # 40 x up to 100 dialogs is far more than any account
PHOTO_CENSUS_CAP = 200  # how many PVs to ask about photos
PHOTO_CONC = 8
STEP_TIMEOUT = 120


def save() -> None:
    try:
        OUT.write_text(json.dumps(RESULT, ensure_ascii=False, indent=2, default=str),
                       encoding="utf-8")
    except OSError:
        pass


def show(name: str, data) -> None:
    RESULT["steps"][name] = data
    save()
    print(f"  [{name}]")
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, list):
                print(f"      {k}: ({len(v)} items)")
                for item in v[:12]:
                    print(f"        {json.dumps(item, ensure_ascii=False, default=str)[:180]}")
                if len(v) > 12:
                    print(f"        ... +{len(v) - 12} more")
            else:
                print(f"      {k}: {json.dumps(v, ensure_ascii=False, default=str)[:180]}")
    else:
        print(f"      {json.dumps(data, ensure_ascii=False, default=str)[:400]}")
    print()


# ---------------------------------------------------------------------------
# STEP 1/2: dialog paging done correctly
# ---------------------------------------------------------------------------
JS_PAGE = r"""
async (args) => {
  const AM = window.apiManager;
  if (!AM || !AM.invokeApi) return { ok: false, error: 'no apiManager' };
  const S = v => String(v);
  const folder = args.folder, maxPages = args.maxPages;
  const t0 = performance.now();

  let offset_date = 0, offset_id = 0, offset_peer = { _: 'inputPeerEmpty' };
  const pageSizes = [];
  const users = new Map(), chats = new Map();
  const dialogRows = [];
  let stop_reason = 'hit max pages';

  try {
    for (let i = 0; i < maxPages; i++) {
      const d = await AM.invokeApi('messages.getDialogs', {
        folder_id: folder, offset_date, offset_id, offset_peer,
        limit: 100, hash: 0 });
      const dl = (d && d.dialogs) || [];
      const us = (d && d.users) || [];
      const ch = (d && d.chats) || [];
      const ms = (d && d.messages) || [];
      pageSizes.push(dl.length);
      for (const u of us) if (u && u.id != null) users.set(S(u.id), u);
      for (const c of ch) if (c && c.id != null) chats.set(S(c.id), c);

      for (const dlg of dl) {
        const p = dlg.peer || {};
        dialogRows.push({
          kind: p._,
          id: S(p.user_id != null ? p.user_id
               : p.channel_id != null ? p.channel_id : p.chat_id),
          top_message: dlg.top_message || 0,
          unread: dlg.unread_count || 0,
        });
      }

      // The ONLY safe stop condition: an empty page. A short page does not mean
      // the end -- the server may simply return fewer than requested.
      if (dl.length === 0) { stop_reason = 'empty page'; break; }

      const last = dl[dl.length - 1];
      const topId = last.top_message || 0;
      const lm = ms.find(m => m.id === topId);
      const prev_id = offset_id, prev_date = offset_date;
      offset_id = topId;
      offset_date = lm ? lm.date : offset_date;

      const lp = last.peer || {};
      if (lp._ === 'peerUser') {
        const u = users.get(S(lp.user_id));
        offset_peer = u && u.access_hash != null
          ? { _: 'inputPeerUser', user_id: +u.id, access_hash: u.access_hash }
          : { _: 'inputPeerEmpty' };
      } else if (lp._ === 'peerChannel') {
        const c = chats.get(S(lp.channel_id));
        offset_peer = c && c.access_hash != null
          ? { _: 'inputPeerChannel', channel_id: +c.id, access_hash: c.access_hash }
          : { _: 'inputPeerEmpty' };
      } else if (lp._ === 'peerChat') {
        offset_peer = { _: 'inputPeerChat', chat_id: lp.chat_id };
      } else {
        stop_reason = 'unknown peer type on the last dialog';
        break;
      }
      if (offset_peer._ === 'inputPeerEmpty') {
        stop_reason = 'could not build offset_peer (missing access_hash)';
        break;
      }
      if (offset_id === prev_id && offset_date === prev_date) {
        stop_reason = 'offset did not advance (server returned the same page)';
        break;
      }
    }
  } catch (e) {
    stop_reason = 'error: ' + String(e && (e.type || e.message) || e);
  }

  // Stash for later steps.
  if (folder === 0) {
    window.__MKWL_users = users;
    window.__MKWL_chats = chats;
    window.__MKWL_rows = dialogRows;
  }

  // Classification
  let pv_contact = 0, pv_noncontact = 0, bots = 0, selfChat = 0, deleted = 0;
  let basic_groups = 0, broadcast = 0, megagroup = 0, unknown = 0;
  const pv_ids = [];
  for (const r of dialogRows) {
    if (r.kind === 'peerUser') {
      const u = users.get(r.id);
      const f = (u && u.pFlags) || {};
      if (f.self) selfChat++;
      else if (f.bot) bots++;
      else if (f.deleted) deleted++;
      else {
        pv_ids.push(r.id);
        if (f.contact || f.mutual_contact) pv_contact++; else pv_noncontact++;
      }
    } else if (r.kind === 'peerChat') basic_groups++;
    else if (r.kind === 'peerChannel') {
      const c = chats.get(r.id);
      const f = (c && c.pFlags) || {};
      if (f.broadcast) broadcast++;
      else if (f.megagroup) megagroup++;
      else unknown++;
    } else unknown++;
  }
  if (folder === 0) window.__MKWL_pv_ids = pv_ids;

  return {
    ok: true, folder,
    pages_fetched: pageSizes.length,
    page_sizes: pageSizes,
    stop_reason,
    total_dialogs: dialogRows.length,
    users_seen: users.size, chats_seen: chats.size,
    breakdown: {
      pv_real_people: pv_contact + pv_noncontact,
      pv_contact, pv_noncontact,
      bots, self_chat: selfChat, deleted_accounts: deleted,
      basic_groups, channels_broadcast: broadcast,
      supergroups: megagroup, unclassified: unknown,
    },
    ms: Math.round(performance.now() - t0),
  };
}
"""

# ---------------------------------------------------------------------------
# STEP 4: authoritative contact id set
# ---------------------------------------------------------------------------
JS_CONTACTS = r"""
async () => {
  const AM = window.apiManager;
  try {
    const t = performance.now();
    const c = await AM.invokeApi('contacts.getContacts', { hash: 0 });
    const users = (c && c.users) || [];
    const ids = new Set(users.map(u => String(u.id)));
    window.__MKWL_contact_ids = ids;
    const pv = window.__MKWL_pv_ids || [];
    const inBoth = pv.filter(id => ids.has(id)).length;
    return { ok: true, ms: Math.round(performance.now() - t),
             contacts_total: users.length,
             pv_chats_checked: pv.length,
             pv_that_are_contacts: inBoth,
             pv_that_are_NOT_contacts: pv.length - inBoth,
             contacts_with_no_chat: ids.size - inBoth };
  } catch (e) {
    return { ok: false, error: String(e && (e.type || e.message) || e) };
  }
}
"""

# ---------------------------------------------------------------------------
# STEP 6: photo census across PVs
# ---------------------------------------------------------------------------
JS_PHOTOS = r"""
async (args) => {
  const AM = window.apiManager;
  const users = window.__MKWL_users;
  const pv = (window.__MKWL_pv_ids || []).slice(0, args.cap);
  if (!pv.length) return { ok: false, error: 'no pv ids' };
  const conc = args.conc;
  const isFlood = s => /FLOOD|TOO_MANY|LIMIT/i.test(String(s || ''));

  async function census(id) {
    const u = users.get(id);
    if (!u || u.access_hash == null) return { id, error: 'no access_hash' };
    const peer = { _: 'inputPeerUser', user_id: +u.id, access_hash: u.access_hash };
    try {
      const r = await AM.invokeApi('messages.search', {
        peer, q: '', filter: { _: 'inputMessagesFilterPhotos' },
        min_date: 0, max_date: 0, offset_id: 0, add_offset: 0,
        limit: 100, max_id: 0, min_id: 0, hash: 0 });
      const msgs = (r && r.messages) || [];
      const photos = msgs.filter(m => m.media && m.media.photo);
      return {
        id,
        name: ((u.first_name || '') + ' ' + (u.last_name || '')).trim()
              || (u.phone ? String(u.phone) : String(u.id)),
        reply_type: r ? r._ : null,
        count_field: (r && r.count != null) ? r.count : null,
        returned: msgs.length,
        photos: photos.length,
        sent_by_me: photos.filter(m => m.pFlags && m.pFlags.out).length,
        needs_paging: !!(r && r._ === 'messages.messagesSlice'
                         && r.count > msgs.length),
      };
    } catch (e) {
      return { id, error: String(e && (e.type || e.message) || e).slice(0, 40) };
    }
  }

  const t0 = performance.now();
  const rows = [];
  let floods = 0;
  for (let i = 0; i < pv.length; i += conc) {
    const batch = pv.slice(i, i + conc);
    const rs = await Promise.all(batch.map(census));
    for (const r of rs) {
      rows.push(r);
      if (r.error && isFlood(r.error)) floods++;
    }
    if (floods) break;
  }
  const ms = Math.round(performance.now() - t0);

  const withPhotos = rows.filter(r => (r.photos || 0) > 0);
  const empty = rows.filter(r => !r.error && (r.photos || 0) === 0);
  const totalPhotos = rows.reduce((a, r) => a + (r.photos || 0), 0);
  const totalSent = rows.reduce((a, r) => a + (r.sent_by_me || 0), 0);
  const declared = rows.reduce(
    (a, r) => a + (r.count_field != null ? r.count_field : (r.photos || 0)), 0);

  withPhotos.sort((a, b) => (b.photos || 0) - (a.photos || 0));
  return {
    ok: true, chats_examined: rows.length, ms,
    ms_per_chat: rows.length ? Math.round(ms / rows.length) : null,
    chats_with_photos: withPhotos.length,
    chats_with_zero_photos: empty.length,
    photos_in_first_page: totalPhotos,
    photos_declared_total: declared,
    sent_by_me: totalSent, received: totalPhotos - totalSent,
    chats_needing_paging: rows.filter(r => r.needs_paging).length,
    floods,
    errors: rows.filter(r => r.error).length,
    top_chats: withPhotos.slice(0, 10).map(r => ({
      name: (r.name || '').slice(0, 24), photos: r.photos,
      declared: r.count_field, sent: r.sent_by_me, paging: r.needs_paging })),
    zero_sample: empty.slice(0, 5).map(r => ({
      name: (r.name || '').slice(0, 24), reply_type: r.reply_type,
      returned: r.returned })),
  };
}
"""


async def step(driver, label, js, arg=None, timeout=STEP_TIMEOUT):
    print(f"  ... {label}")
    t = time.time()
    try:
        coro = driver.page.evaluate(js, arg) if arg is not None \
            else driver.page.evaluate(js)
        res = await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        res = {"ok": False, "error": f"timed out after {timeout}s"}
    except Exception as exc:
        res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if isinstance(res, dict):
        res["_wall_s"] = round(time.time() - t, 1)
    show(label, res)
    return res


async def main(account: str, do_dom: bool) -> int:
    print("=" * 70)
    print(f"  CHAT CENSUS PROBE -- {account}")
    print("=" * 70)
    print()

    t0 = time.time()
    async with session_pool.lease(account, headed=config.HEADED_JOBS) as session:
        driver = EitaaDriver(session)
        await driver.open()
        print(f"  session ready in {time.time() - t0:.1f}s")
        if not await driver.is_logged_in():
            print("  FATAL: not logged in")
            return 1
        print()

        f0 = await step(driver, "1_dialogs_folder0", JS_PAGE,
                        {"folder": 0, "maxPages": MAX_PAGES})
        await step(driver, "2_dialogs_archive", JS_PAGE,
                   {"folder": 1, "maxPages": MAX_PAGES})
        await step(driver, "3_contacts", JS_CONTACTS)

        if do_dom:
            print("  ... 4_dom_scroll (scrolls the real chat list; slower)")
            t = time.time()
            try:
                chats = await asyncio.wait_for(
                    driver.collect_all_chats(), timeout=180)
                users = sum(1 for c in chats if c.get("kind") == "user")
                show("4_dom_scroll", {
                    "ok": True, "rows_rendered": len(chats),
                    "user_rows": users,
                    "group_or_channel_rows": len(chats) - users,
                    "_wall_s": round(time.time() - t, 1),
                })
            except Exception as exc:
                show("4_dom_scroll", {"ok": False,
                                      "error": f"{type(exc).__name__}: {exc}"})
        else:
            print("  ... 4_dom_scroll SKIPPED (--no-dom)\n")

        await step(driver, "5_photo_census", JS_PHOTOS,
                   {"cap": PHOTO_CENSUS_CAP, "conc": PHOTO_CONC})

    # ------------------------- reconciliation -------------------------
    st = RESULT["steps"]
    d0 = st.get("1_dialogs_folder0", {})
    d1 = st.get("2_dialogs_archive", {})
    ct = st.get("3_contacts", {})
    dom = st.get("4_dom_scroll", {})
    ph = st.get("5_photo_census", {})
    b0 = d0.get("breakdown") or {}
    b1 = d1.get("breakdown") or {}

    print("-" * 70)
    print("  RECONCILIATION")
    print()
    print(f"    folder 0: {d0.get('total_dialogs')} dialogs in "
          f"{d0.get('pages_fetched')} page(s)")
    print(f"              page sizes {d0.get('page_sizes')}")
    print(f"              stopped because: {d0.get('stop_reason')}")
    print(f"    archive : {d1.get('total_dialogs')} dialogs "
          f"(stopped: {d1.get('stop_reason')})")
    print()
    total_dialogs = (d0.get("total_dialogs") or 0) + (d1.get("total_dialogs") or 0)
    print(f"    TOTAL dialogs (both folders): {total_dialogs}")
    print()
    print("    what they are (folder 0 + archive):")
    for key in ("pv_real_people", "pv_contact", "pv_noncontact", "bots",
                "self_chat", "deleted_accounts", "basic_groups",
                "channels_broadcast", "supergroups", "unclassified"):
        v = (b0.get(key) or 0) + (b1.get(key) or 0)
        print(f"      {key:22} {v}")
    print()
    if ct.get("ok"):
        print(f"    contacts on the account   : {ct.get('contacts_total')}")
        print(f"    PV chats that ARE contacts: {ct.get('pv_that_are_contacts')}")
        print(f"    PV chats that are NOT     : {ct.get('pv_that_are_NOT_contacts')}")
        print(f"    contacts with no chat yet : {ct.get('contacts_with_no_chat')}")
        print()
    if dom.get("ok"):
        api_pv = b0.get("pv_real_people") or 0
        print(f"    DOM chat rows rendered    : {dom.get('rows_rendered')} "
              f"({dom.get('user_rows')} user rows)")
        print(f"    API folder-0 dialogs      : {d0.get('total_dialogs')}")
        gap = (d0.get("total_dialogs") or 0) - (dom.get("rows_rendered") or 0)
        print(f"    gap (API - DOM)           : {gap}")
        if abs(gap) > 3:
            print("      NOTE: a real gap means one of the two sources is")
            print("      incomplete -- the DOM scroll is capped, and the API")
            print("      hides archived chats in folder 0.")
        print()
    if ph.get("ok"):
        print(f"    photo census over {ph.get('chats_examined')} PV chats "
              f"({ph.get('ms_per_chat')} ms/chat at conc {PHOTO_CONC})")
        print(f"      chats WITH photos       : {ph.get('chats_with_photos')}")
        print(f"      chats with zero photos  : {ph.get('chats_with_zero_photos')}")
        print(f"      photos (first page)     : {ph.get('photos_in_first_page')}")
        print(f"      photos declared total   : {ph.get('photos_declared_total')}")
        print(f"      sent by me / received   : {ph.get('sent_by_me')} / "
              f"{ph.get('received')}")
        print(f"      chats needing paging    : {ph.get('chats_needing_paging')}")
        print(f"      floods hit              : {ph.get('floods')}")
        print()
        declared = ph.get("photos_declared_total") or 0
        if declared:
            print(f"    projected export time (measured rates):")
            scan = (ph.get("ms_per_chat") or 400) * \
                   (ph.get("chats_examined") or 1) / 1000
            dl = declared * 0.040
            print(f"      scan  {scan:.0f}s + download {dl:.0f}s "
                  f"+ pdf ~10s = ~{scan + dl + 10:.0f}s")
    print()
    print(f"  full JSON: {OUT}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(f"usage: {sys.argv[0]} <account> [--no-dom]")
        sys.exit(1)
    try:
        sys.exit(asyncio.run(main(args[0], "--no-dom" not in sys.argv)))
    except KeyboardInterrupt:
        print("\n  interrupted -- partial results kept:")
        for k in RESULT["steps"]:
            print(f"     {k}")
        save()
        print(f"  saved: {OUT}")
        sys.exit(130)
