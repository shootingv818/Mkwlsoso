"""File-bridge investigation (Saved Messages only).

Tests two ways to send a file through Eitaa's own engine while uploading it
ONLY ONCE (the whole point -- no per-recipient re-upload, which is what crashed
the small server before):

  A) upload once via appMessagesManager.sendFile, then messages.forwardMessages
     with drop_author -> the user's "upload once, then forward" idea.
  B) reuse the uploaded document with messages.sendMedia (inputMediaDocument)
     -> sends the same file with NO "forwarded from" header at all.

Everything targets the owner's own Saved Messages (self), so it is safe to run.
The report tells us which path works so we can pick the campaign architecture.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import time
from typing import Any

# Runs in the page. Uploads the file to self once, finds it via getHistory,
# then tries forward(drop_author) and sendMedia(reuse). Returns a step log.
FILE_TEST_JS = r"""
async (arg) => {
  const b64 = arg.b64, filename = arg.filename, mime = arg.mime, caption = arg.caption;
  const out = { steps: [], self_id: null, msg_id: null, doc_id: null,
                forward: null, forward_plain: null, sendmedia: null };
  const log = (name, ok, info) => out.steps.push(
    { name: name, ok: ok, info: (typeof info === 'string' ? info.slice(0, 300) : info) });
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  function randId() {
    try { const a = new Uint32Array(2); crypto.getRandomValues(a);
      return a[0].toString() + a[1].toString().padStart(10, '0'); }
    catch (e) { return String(Date.now()) + '007'; }
  }
  function errCode(e) {
    try { return String((e && (e.type || e.error_message || e.code || e.message)) || e); }
    catch (x) { return 'ERR'; }
  }
  function getSelfId() {
    const c = [];
    try { c.push(window.appPeersManager && window.appPeersManager.peerId); } catch (e) {}
    try { c.push(window.appImManager && window.appImManager.myId); } catch (e) {}
    try { if (window.appUsersManager && window.appUsersManager.getSelf) {
      const s = window.appUsersManager.getSelf(); c.push(s && (s.id != null ? s.id : s)); } } catch (e) {}
    for (const x of c) { if (x != null && ['number', 'string', 'bigint'].includes(typeof x)) return String(x); }
    return null;
  }

  out.self_id = getSelfId();
  const selfNum = (out.self_id != null && !isNaN(+out.self_id)) ? +out.self_id : out.self_id;
  const peerSelf = { _: 'inputPeerSelf' };

  const AM = window.apiManager;
  if (!AM || !AM.invokeApi) { log('apiManager', false, 'no invokeApi on window'); return out; }

  // 1) Build a real File object from the bytes we were handed.
  let file;
  try {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    file = new File([bytes], filename, { type: mime || 'application/octet-stream' });
    log('build_file', true, filename + ' ' + file.size + 'B ' + (mime || ''));
  } catch (e) { log('build_file', false, errCode(e)); return out; }

  // 2) Upload + send ONCE to self via appMessagesManager.sendFile.
  const AMM = window.appMessagesManager;
  log('has_sendFile', !!(AMM && AMM.sendFile), (AMM && AMM.sendFile) ? 'present' : 'absent');
  let sent = false;
  if (AMM && AMM.sendFile) {
    try { await AMM.sendFile({ peerId: selfNum, file: file, caption: caption }); sent = true; log('sendFile(object)', true, ''); }
    catch (e) { log('sendFile(object)', false, errCode(e)); }
    if (!sent) {
      try { await AMM.sendFile(selfNum, file, { caption: caption }); sent = true; log('sendFile(positional)', true, ''); }
      catch (e) { log('sendFile(positional)', false, errCode(e)); }
    }
  }
  if (!sent) { log('upload', false, 'sendFile did not accept either signature'); return out; }

  // 3) Find the uploaded message via getHistory (matched by our caption marker).
  let doc = null, mid = null;
  for (let attempt = 0; attempt < 45 && mid == null; attempt++) {
    await sleep(1000);
    try {
      const h = await AM.invokeApi('messages.getHistory', {
        peer: peerSelf, offset_id: 0, offset_date: 0, add_offset: 0,
        limit: 8, max_id: 0, min_id: 0, hash: 0 });
      const msgs = (h && h.messages) || [];
      for (const m of msgs) {
        const cap = (m && m.message) || '';
        const hasDoc = m && m.media && m.media.document;
        if (hasDoc && cap.indexOf(caption) !== -1) { mid = m.id; doc = m.media.document; break; }
      }
    } catch (e) { log('getHistory', false, errCode(e)); break; }
  }
  if (mid == null) { log('locate_uploaded', false, 'file message not found within timeout'); return out; }
  out.msg_id = mid;
  out.doc_id = doc && String(doc.id);
  log('locate_uploaded', true, 'msg_id=' + mid + ' doc_id=' + (doc && doc.id));

  // TEST A: forward self->self with drop_author (clean, no "forwarded from").
  try {
    await AM.invokeApi('messages.forwardMessages', {
      from_peer: peerSelf, to_peer: peerSelf, id: [mid],
      random_id: [randId()], drop_author: true });
    out.forward = { ok: true };
    log('forward(drop_author)', true, 'ok');
  } catch (e) {
    const c = errCode(e);
    out.forward = { ok: false, code: c };
    log('forward(drop_author)', false, c);
    // Retry without the flag to learn whether drop_author was the problem.
    try {
      await AM.invokeApi('messages.forwardMessages', {
        from_peer: peerSelf, to_peer: peerSelf, id: [mid], random_id: [randId()] });
      out.forward_plain = { ok: true };
      log('forward(no_flag)', true, 'ok (but drop_author unsupported -> will show "forwarded from")');
    } catch (e2) { out.forward_plain = { ok: false, code: errCode(e2) }; log('forward(no_flag)', false, errCode(e2)); }
  }

  // TEST B: reuse the uploaded document via sendMedia (no forward header).
  if (doc) {
    try {
      const media = { _: 'inputMediaDocument', id: {
        _: 'inputDocument', id: doc.id, access_hash: doc.access_hash, file_reference: doc.file_reference } };
      await AM.invokeApi('messages.sendMedia', {
        peer: peerSelf, media: media, message: caption + ' (reuse)', random_id: randId() });
      out.sendmedia = { ok: true };
      log('sendMedia(reuse)', true, 'ok');
    } catch (e) { const c = errCode(e); out.sendmedia = { ok: false, code: c }; log('sendMedia(reuse)', false, c); }
  }

  return out;
}
"""


async def run_file_test(driver: Any, file_path: str) -> dict:
    """Upload `file_path` once to Saved Messages and test forward + reuse."""
    if not os.path.isfile(file_path):
        return {"error": f"file not found: {file_path}"}
    size = os.path.getsize(file_path)
    if size > 8 * 1024 * 1024:
        return {"error": f"test file is {size} bytes; please use a small file (<8 MB) for the probe"}

    with open(file_path, "rb") as f:
        data = f.read()
    b64 = base64.b64encode(data).decode("ascii")
    filename = os.path.basename(file_path)
    mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    caption = f"MKWLFILE{int(time.time())}"

    # Open Saved Messages so the results are visible to the owner (not required
    # for the API calls, but reassuring).
    try:
        await driver.open_saved_messages()
    except Exception:  # noqa: BLE001
        pass

    try:
        res = await driver.page.evaluate(
            FILE_TEST_JS,
            {"b64": b64, "filename": filename, "mime": mime, "caption": caption},
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"bridge file evaluate failed: {exc}"}

    if not isinstance(res, dict):
        return {"error": "bad result from file bridge"}
    res.update({"caption": caption, "filename": filename, "size": size, "mime": mime})
    return res


def print_file_test_summary(res: dict) -> None:
    """Print the file-bridge outcome and a recommended architecture."""
    LINE = "-" * 31
    print("")
    print("[bridge-file] ===== FILE BRIDGE INVESTIGATION =====")
    print(f"[bridge-file] {LINE}")
    if res.get("error"):
        print(f"[bridge-file] ERROR: {res['error']}")
        print("[bridge-file] =====================================")
        return

    print(f"[bridge-file] file      : {res.get('filename')}  ({res.get('size')} B, {res.get('mime')})")
    print(f"[bridge-file] caption   : {res.get('caption')}")
    print(f"[bridge-file] self id   : {res.get('self_id')}")
    print(f"[bridge-file] msg id    : {res.get('msg_id')}   doc id: {res.get('doc_id')}")
    print(f"[bridge-file] {LINE}")
    print("[bridge-file] steps:")
    for s in res.get("steps", []):
        mark = "✅" if s.get("ok") else "❌"
        info = s.get("info")
        info = f"  -> {info}" if info else ""
        print(f"[bridge-file]   {mark} {s.get('name')}{info}")

    fwd = res.get("forward") or {}
    fwd_plain = res.get("forward_plain") or {}
    media = res.get("sendmedia") or {}
    print(f"[bridge-file] {LINE}")
    forward_clean = bool(fwd.get("ok"))
    forward_any = forward_clean or bool(fwd_plain.get("ok"))
    reuse_ok = bool(media.get("ok"))

    print(f"[bridge-file] upload-once + forward (drop_author) : {'✅ works' if forward_clean else '❌'}")
    if not forward_clean and fwd_plain:
        print(f"[bridge-file] upload-once + forward (with header) : {'✅ works' if fwd_plain.get('ok') else '❌'}")
    print(f"[bridge-file] upload-once + sendMedia reuse        : {'✅ works' if reuse_ok else '❌'}")

    print(f"[bridge-file] {LINE}")
    if reuse_ok:
        print("[bridge-file] RECOMMENDATION: upload ONCE, then sendMedia-reuse per recipient.")
        print("[bridge-file]   Cleanest: the file arrives as a normal message (no 'forwarded from'),")
        print("[bridge-file]   and it is NEVER re-uploaded -> fast and no server strain.")
    elif forward_clean:
        print("[bridge-file] RECOMMENDATION: your idea works -> upload ONCE, then forward with")
        print("[bridge-file]   drop_author per recipient (clean, no 'forwarded from', no re-upload).")
    elif forward_any:
        print("[bridge-file] RECOMMENDATION: upload once + forward works, but drop_author was")
        print("[bridge-file]   rejected, so messages will show 'forwarded from'. Usable if acceptable.")
    else:
        print("[bridge-file] VERDICT: neither reuse nor forward succeeded. Check the step errors")
        print("[bridge-file]   above (paste them back) so we adjust the exact call/params.")
    print("[bridge-file] (Also open your Saved Messages to see the uploaded + resent copies.)")
    print("[bridge-file] =====================================")
