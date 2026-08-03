"""Isolated "export this account's photos to PDF" feature.

Additive and self-contained, in the same spirit as `direct/` and
`session_check/`: it defines no new transport, no new session logic and no new
send path. It composes pieces that already exist and are already proven here:

  * capture.pool.lease              - the same warm browser every job leases
  * eitaa.driver.EitaaDriver        - open / is_logged_in / page.evaluate
  * bot.cards                       - the card shell and DIVIDER
  * bot.runner.JobManager           - one job per account, _busy bookkeeping
  * playwright chromium (headless)  - already a dependency; used only to turn
                                      HTML into a PDF

Every number in the engine came from measurement on the live account, not from
guesswork (see the probe scripts in the branch history):

  * dialog paging MUST stop on an EMPTY page. Eitaa returns 25 on the first page
    and 100 afterwards, so "fewer than the limit" does not mean the end -- that
    mistake once reported 24 chats where there were 608.
  * messages.search never populates `count` on this build and returns the
    "complete" type even when it fills the limit, so photo paging also has to
    walk until a SHORT page instead of trusting the reply.
  * `returned == 0` is the reliable "this chat has no photos" signal. 557 of 601
    chats answered that way, so skipping them is most of the win.
  * pFlags.out separates sent from received.
  * upload.getFile is fastest with NO dc options, and concurrency 16 sustained 48
    downloads at 30 ms each with zero FLOOD_WAIT.
  * chunked download works: three 16 KB chunks reassembled to exactly the
    declared byte count.
  * page.pdf() only works in headless Chromium, so a separate browser is
    launched for rendering; jobs themselves run headed.
  * every image must be decoded before pdf() is called, or a partially painted
    page gets captured.

Wiring outside this folder is deliberately tiny: a button and a callback branch
in `bot/app.py`, a `run_photo_export` job in `bot/runner.py` that imports this
package inside a try/except, one setting in `bot/store.py` and one flag in
`config.py`. Deleting this directory degrades that one button and nothing else.
"""

__all__ = ["engine", "scanner", "fetcher", "renderer", "cards"]
