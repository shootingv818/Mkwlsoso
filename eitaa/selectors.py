"""DOM selector candidates for Eitaa Web.

Eitaa Web is based on Telegram Web K (tweb). The selectors below list several
candidates per element so the driver stays resilient if class names differ.
The first selector that matches on the live page wins.

Adjust these after running `python cli.py inspect --account <acc>` against the
real page if any element is not found.
"""

from __future__ import annotations

# Left-column search input used to find a chat/contact by name or username.
SEARCH_INPUT = [
    "#column-left input.input-field-input",
    ".input-search input",
    "input.input-field-input",
    'input[type="text"]',
]

# Search result rows / chat list items (clicking opens the chat).
CHAT_RESULT = [
    "#column-left ul.chatlist a.chatlist-chat",
    ".chatlist a.chatlist-chat",
    "a.chatlist-chat",
    ".chatlist-chat",
    "ul.chatlist > a",
]

# The message composer (contenteditable div in tweb).
MESSAGE_INPUT = [
    'div.input-message-input[contenteditable="true"]',
    ".input-message-container div.input-message-input",
    'div[contenteditable="true"].input-message-input',
    '.chat-input [contenteditable="true"]',
    '[contenteditable="true"]',
]

# The send button (paper-plane). In tweb it toggles between mic and send.
SEND_BUTTON = [
    "button.btn-send",
    ".chat-input .btn-send",
    "button.send",
    ".btn-icon.send",
]

# Container that holds the visible message bubbles (used to verify a send).
MESSAGES_CONTAINER = [
    ".bubbles",
    ".scrollable.messages-container",
    ".chat .bubbles",
]

# An individual outgoing message bubble's text (used to confirm the marker).
MESSAGE_TEXT = [
    ".bubble.is-out .message",
    ".bubble.is-out .translatable-message",
    ".message",
]
