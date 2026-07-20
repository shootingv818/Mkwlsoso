"""DOM selector candidates for Eitaa Web.

Eitaa Web is based on Telegram Web K (tweb). The selectors below list several
candidates per element so the driver stays resilient if class names differ.
The first selector that matches on the live page wins.

Adjust these after running `python cli.py inspect --account <acc>` against the
real page if any element is not found.
"""

from __future__ import annotations

# Left-column search input used to find a chat/contact by name or username.
# Confirmed on live Eitaa: input.input-search-input (placeholder "جستجو").
SEARCH_INPUT = [
    "input.input-search-input",
    "input.input-field-input.input-search-input",
    "#column-left input.input-field-input",
    ".input-search input",
    "input.input-field-input",
    'input[type="text"]',
]

# Search result rows / chat list items (clicking opens the chat).
# Confirmed on live Eitaa: items use class .chatlist-chat (NOT <a>).
CHAT_RESULT = [
    ".search-super .chatlist-chat",
    "#search-container .chatlist-chat",
    ".search-group .chatlist-chat",
    ".chatlist-chat",
    "a.chatlist-chat",
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

# Hamburger / main menu toggle in the left sidebar header. We avoid the
# "sidebar-close-button" (that is a back button, not the menu opener).
MENU_BUTTON = [
    ".sidebar-header .btn-menu-toggle",
    "button.btn-menu-toggle",
    "#column-left .sidebar-header .btn-icon:not(.sidebar-close-button)",
    ".sidebar-header .btn-icon:not(.sidebar-close-button)",
    ".sidebar-tools-button",
]

# The Contacts menu item, targeted by its language-independent icon class.
# Confirmed on live Eitaa: `btn-menu-item tgico-user` with text "مخاطبین".
CONTACTS_MENU_ITEM = [
    ".btn-menu-item.tgico-user",
    "button.btn-menu-item.tgico-user",
    ".btn-menu .tgico-user",
]

# Text labels that identify the "Contacts" menu item (fa + en fallback).
CONTACTS_LABELS = [
    "مخاطبین",
    "مخاطب‌ها",
    "مخاطب ها",
    "Contacts",
]

# Scrollable container that holds the contacts list once the Contacts view is
# open. The active tab is what we scope collection to.
CONTACTS_CONTAINER = [
    "#column-left .tabs-tab.active .scrollable",
    "#column-left .tabs-tab.active .chatlist",
    ".contacts-container .scrollable",
    "#column-left .sidebar-slider .tabs-tab.active",
    "#column-left .scrollable-y",
]

# Rows inside the contacts view (tweb reuses .chatlist-chat).
CONTACT_ROW = [
    ".tabs-tab.active .chatlist-chat",
    ".contacts-container .chatlist-chat",
    ".chatlist-chat",
]

# --- Add-contact flow ---------------------------------------------------
# Floating "add contact" button, usually a round corner button in the
# contacts view, or a header add icon.
ADD_CONTACT_BUTTON = [
    ".tabs-tab.active .btn-circle.btn-corner",
    "#column-left .btn-circle.btn-corner",
    ".btn-circle.btn-corner",
    ".sidebar-header .btn-icon.tgico-add",
    "button.btn-circle",
]

# The "new contact" popup/dialog container.
NEW_CONTACT_POPUP = [
    ".popup.active",
    ".popup-new-contact",
    ".popup.popup-create-contact",
    ".popup-container.active",
]

# Text inputs inside the new-contact popup (first/last name).
NEW_CONTACT_TEXT_INPUTS = [
    ".popup.active .input-field-input",
    ".popup.active input.input-field-input",
    ".popup.active input[type='text']",
]

# The phone input inside the new-contact popup.
NEW_CONTACT_PHONE_INPUT = [
    ".popup.active input[type='tel']",
    ".popup.active .input-field-phone input",
    ".popup.active input.input-field-input",
]

# The confirm/add button inside the popup.
NEW_CONTACT_CONFIRM = [
    ".popup.active .popup-button",
    ".popup.active .btn-primary",
    ".popup.active button.btn-color-primary",
]

# Confirm-button label variants (fa + en) as a text fallback.
ADD_CONTACT_LABELS = [
    "افزودن مخاطب",
    "افزودن",
    "ذخیره",
    "Add Contact",
    "Add",
]
