"""Does this text carry a credential? Decided by shape, never by keywords.

Memory must not hold passwords, keys or tokens. What a sentence *means*
(「这是我的密码」) is the model's call; what a string *looks like* is a fact
code can check:

- known structural formats: PEM private keys, JSON Web Tokens, and URLs that
  carry ``user:password@``;
- machine-made random strings: a run of 20+ letters and digits that does not
  break into words, numbers or short acronyms the way human-chosen names do
  (``ThinkPadX1Carbon2024`` splits into words and numbers; ``q8ZrT2mXv0LkP4sY9wBn``
  does not), or 32+ mixed hex digits.

A URL's scheme, host and path are an address (video ids and article slugs
look random but are not secrets); only its query values are checked.
"""
from __future__ import annotations

import re

MIN_RUN = 20       # shorter random strings are too often ids people type
MIN_HEX = 32       # 128 bits of hex: digests and keys, not codes or colours
# Share of a run that is neither a word, a number nor an acronym. Longer runs
# need less: a person rarely types 32+ characters without real words in them.
NOISE, NOISE_LONG, LONG = 0.55, 0.4, 32

PEM = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
JWT = re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}")
URL_USERINFO = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s/:@]+:[^\s/@]+@")
URL = re.compile(r"\bhttps?://[^\s<>\"'）)】]+")
RUN = re.compile(r"[A-Za-z0-9+/=_.-]+")
SEGMENT = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
VOWELS = set("aeiouy")


def _wordlike(segment):
    if segment.isdigit():
        return True                                    # a number reads as one unit
    if segment.isupper() and len(segment) <= 4:
        return True                                    # an acronym: GB, SN, HDR
    lower = segment.lower()
    # Pronounceable: has a vowel and no four consonants in a row.
    return len(segment) >= 3 and any(c in VOWELS for c in lower) and not re.search(r"[^aeiouy]{4}", lower)


def _noise(core):
    segments = SEGMENT.findall(core)
    noisy = sum(len(s) for s in segments if len(s) <= 2 or not _wordlike(s))
    return noisy / max(1, sum(len(s) for s in segments))


def _random_run(text):
    for run in RUN.findall(text):
        core = re.sub(r"[^A-Za-z0-9]", "", run)
        if re.fullmatch(r"[0-9a-fA-F]+", core):
            if len(core) >= MIN_HEX and re.search(r"\d", core) and re.search(r"[a-fA-F]", core):
                return True
            continue
        if len(core) >= MIN_RUN and re.search(r"[A-Za-z]", core) and re.search(r"\d", core) and _noise(core) >= (NOISE if len(core) < LONG else NOISE_LONG):
            return True
    return False


def _without_addresses(text):
    """Replace each URL with only its query values, the part that can carry a token."""
    def query_values(match):
        query = match.group(0).partition("?")[2].partition("#")[0]
        return " " + " ".join(v for _, _, v in (pair.partition("=") for pair in query.split("&")) if v) + " "
    return URL.sub(query_values, text)


def credential_shape(text):
    """Name the kind of credential this text appears to contain, or None."""
    if not isinstance(text, str) or not text:
        return None
    if PEM.search(text):
        return "私钥"
    if JWT.search(text):
        return "登录令牌"
    if URL_USERINFO.search(text):
        return "带密码的链接"
    if _random_run(_without_addresses(text)):
        return "像密钥或令牌的随机串"
    return None


def refuse_credentials(*texts):
    """Raise when any text looks like a credential. The message never echoes it."""
    for text in texts:
        found = credential_shape(text)
        if found:
            raise ValueError(f"这段内容里有{found}，记忆不保存密码、密钥和令牌（记忆会被读给模型，也会出现在页面上）。请放进密码管理器；需要时告诉我去哪里取，而不是取值本身。")
