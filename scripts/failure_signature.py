"""failure_signature.py — one failure, one workflow-independent key.

W3.1 of the reliability program (`docs/design/reliability-program.md`). Six portal
workflows died on 2026-08-26 with byte-identical `CheckViolation` text and produced
ten unrelated-looking emails, because nothing ever compared the *reason*. This
module turns a failure into a key derived **only from the error text** — never from
`workflow_path` — so those six reds collapse into one incident.

Pure stdlib (`re`), no DB, no network: importable from the scraper chokepoint, the
Actions poller (which installs base deps only) and tests alike.

Signature shapes, in the priority order `signature_from_log` tries them:

    checkviolation|new row for relation listings violates check constraint listings_area_basis_check
    check:property_maintenance|fail          # verify_pipeline's own per-check line
    error|openai call failed http 429        # scripts that catch and print their own errors
    step:build|exit:1@.github/workflows/x.yml  # unreadable red — scoped, never merged

The fallback is the only shape carrying `workflow_path`: an unreadable red says
nothing about *why*, so merging two of them would manufacture a mega-incident.
"""

from __future__ import annotations

import re

MAX_SIGNATURE_LEN = 200
MAX_MESSAGE_LEN = 160

# Actions job logs prefix EVERY line with an ISO timestamp; strip it before any
# line-anchored grammar runs, or nothing is ever at column 0.
_LOG_TS_PREFIX_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ", re.MULTILINE
)

# A Python exception line: optional dotted module path, then a CapWord class, then
# ": ". Deliberately NOT an `*Error` suffix allowlist — the corpus's most important
# classes (CheckViolation, QueryCanceled, AdminShutdown, AmbiguousFunction,
# InsufficientPrivilege) all fail that test. The `[a-z]` requirement rejects
# all-caps log labels like `ERROR: ...`.
_EXC_LINE_RE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*\.)*"
    r"(?P<cls>[A-Z][A-Za-z0-9_]*[a-z][A-Za-z0-9_]*)"
    r": (?P<msg>\S.*)$",
    re.MULTILINE,
)

# verify_pipeline's own line (`LOG.info("CHECK %s status=%s value=%s")`). Reading it
# gives 37% of the failure corpus one stable signature PER CHECK — keying on the
# set-of-failing-checks instead fragments one outage into a dozen incidents.
# Unanchored: the line still carries logging's `%(asctime)s %(levelname)s %(name)s`
# prefix once the Actions timestamp is stripped.
_CHECK_LINE_RE = re.compile(r"\bCHECK (?P<key>[a-z0-9_]+) status=(?P<status>\w+)")

# Scripts that catch their own errors exit 1 with no traceback (the bazos enrichment
# lane is 14% of the corpus). Without this tier the single biggest LLM-outage
# signature is invisible to both producers.
_ABORTING_RE = re.compile(r"\baborting:\s*(?P<msg>\S.*)$", re.MULTILINE | re.IGNORECASE)
_ERROR_KV_RE = re.compile(r"\berror=(?P<msg>\S.*)$", re.MULTILINE)

_ANNOTATION_RE = re.compile(r"^##\[error\](?P<msg>\S.*)$", re.MULTILINE)

_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_TS_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://\S+")
_PATH_RE = re.compile(r"(?:/[\w.\-]+){2,}/?")
_LONG_HEX_RE = re.compile(r"\b[0-9a-fA-F]{12,}\b")
_DIGITS_RE = re.compile(r"\d+")
_QUOTED_RE = re.compile(r"\"([^\"\n]{1,120})\"|'([^'\n]{1,120})'")
_HTTP_CODE_RE = re.compile(r"(?<![\w.])[1-5]\d\d(?![\w.])")
_HTTP_CONTEXT = ("http", "status", "code", "error", "returned")

# Placeholders must contain NO digits: the digit-stripper runs after them and would
# otherwise eat the very identifier the key exists to preserve.
_PH_OPEN = "\x00"
_PH_CLOSE = "\x01"


def _letters(index: int) -> str:
    out = ""
    n = index
    while True:
        out = chr(ord("a") + (n % 26)) + out
        n //= 26
        if n == 0:
            return out


class _Vault:
    """Digit-safe stash for spans that must survive normalization verbatim."""

    def __init__(self) -> None:
        self._items: list[str] = []

    def stash(self, value: str) -> str:
        self._items.append(value)
        return f"{_PH_OPEN}{_letters(len(self._items) - 1)}{_PH_CLOSE}"

    def restore(self, text: str) -> str:
        def _sub(m: re.Match[str]) -> str:
            idx = 0
            for ch in m.group(1):
                idx = idx * 26 + (ord(ch) - ord("a"))
            return self._items[idx] if idx < len(self._items) else " "

        return re.sub(f"{_PH_OPEN}([a-z]+){_PH_CLOSE}", _sub, text)


def first_line(text: str) -> str:
    """Line 1 only. A psycopg message's line 2 is `DETAIL: Failing row contains (…)`
    — a whole listing row: high-cardinality and PII-adjacent, and it would make every
    occurrence of one bug a different signature."""
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _protect_http_codes(text: str, vault: _Vault) -> str:
    """Keep 3-digit HTTP statuses. A blanket digit strip collapses `403 from …` and
    `500 from …` into one useless `httperror|from`; they are different incidents."""

    def _sub(m: re.Match[str]) -> str:
        before = text[max(0, m.start() - 14):m.start()].lower()
        if m.start() == 0 or any(w in before for w in _HTTP_CONTEXT):
            return vault.stash(m.group(0))
        return m.group(0)

    return _HTTP_CODE_RE.sub(_sub, text)


def normalize_message(text: str, *, max_len: int = MAX_MESSAGE_LEN) -> str:
    """Collapse one error message to its stable core.

    Drops URLs, filesystem paths, UUIDs, timestamps, long hex and every other digit
    run; keeps quoted identifiers (constraint names — the entire point of the key)
    and HTTP status codes verbatim; lowercases, squeezes punctuation to spaces and
    truncates."""
    vault = _Vault()
    out = first_line(text)
    out = _QUOTED_RE.sub(lambda m: vault.stash(m.group(1) or m.group(2) or ""), out)
    out = _URL_RE.sub(" ", out)
    out = _UUID_RE.sub(" ", out)
    out = _TS_RE.sub(" ", out)
    out = _PATH_RE.sub(" ", out)
    out = _LONG_HEX_RE.sub(" ", out)
    out = _protect_http_codes(out, vault)
    out = _DIGITS_RE.sub(" ", out)
    out = vault.restore(out)
    out = re.sub(r"[^0-9A-Za-z_ ]+", " ", out)
    out = " ".join(out.split()).lower()
    return out[:max_len].rstrip()


def _sig(prefix: str, message: str) -> str:
    return f"{prefix}|{message}"[:MAX_SIGNATURE_LEN]


def signature_from_exception(exc: BaseException) -> str:
    """The chokepoint producer's path: the exception object is already in hand, so
    the class name needs no parsing at all."""
    return _sig(type(exc).__name__.lower(), normalize_message(str(exc)))


def signature_from_text(text: str) -> str | None:
    """Signature for a single exception line (`module.Class: message`), or None."""
    m = _EXC_LINE_RE.search(_LOG_TS_PREFIX_RE.sub("", text or ""))
    if m is None:
        return None
    return _sig(m.group("cls").lower(), normalize_message(m.group("msg")))


def _last(pattern: re.Pattern[str], text: str) -> re.Match[str] | None:
    found = None
    for m in pattern.finditer(text):
        found = m
    return found


def signature_from_log(text: str) -> str | None:
    """Best signature a job log can yield, or None (caller falls back).

    Priority mirrors the measured corpus: a real exception beats verify_pipeline's
    own check line, which beats a self-caught `error=` / `aborting:`, which beats the
    bare `##[error]` annotation. NEVER a tail — the last ~25 lines of every Actions
    log are runner cleanup (`Post job cleanup`, git credential teardown)."""
    body = _LOG_TS_PREFIX_RE.sub("", text or "")
    if not body.strip():
        return None

    m = _last(_EXC_LINE_RE, body)
    if m is not None:
        return _sig(m.group("cls").lower(), normalize_message(m.group("msg")))

    failing = [
        c for c in _CHECK_LINE_RE.finditer(body) if c.group("status") != "ok"
    ]
    if failing:
        # One signature per failing check, worst status first, so a run that reds two
        # checks opens two incidents that each resolve on their own.
        worst = sorted(failing, key=lambda c: 0 if c.group("status") == "fail" else 1)[0]
        return _sig(f"check:{worst.group('key')}", worst.group("status"))

    m = _last(_ABORTING_RE, body)
    if m is not None:
        return _sig("aborting", normalize_message(m.group("msg")))

    m = _last(_ERROR_KV_RE, body)
    if m is not None:
        return _sig("error", normalize_message(m.group("msg")))

    m = _last(_ANNOTATION_RE, body)
    if m is not None:
        return _sig("annotation", normalize_message(m.group("msg")))
    return None


def fallback_signature(
    *,
    workflow_path: str | None,
    step_name: str | None = None,
    exit_code: int | str | None = None,
) -> str:
    """The key for a red nothing could read. SCOPED BY `workflow_path` on purpose:
    unscoped, `step:|exit:1` merged 13 runs across 10 unrelated workflows into one
    meaningless mega-incident."""
    step = normalize_message(step_name or "", max_len=60) or "unknown"
    code = str(exit_code) if exit_code not in (None, "") else "unknown"
    scope = workflow_path or "unknown"
    return f"step:{step}|exit:{code}@{scope}"[:MAX_SIGNATURE_LEN]


def excerpt_from_log(text: str, *, max_bytes: int = 4000, lead_lines: int = 24) -> str:
    """The human-readable proof to hang on the incident: the block ENDING at the
    error anchor, not the tail of the file."""
    body = _LOG_TS_PREFIX_RE.sub("", text or "")
    lines = [ln.rstrip() for ln in body.splitlines() if ln.strip()]
    if not lines:
        return ""
    anchor = len(lines) - 1
    for i in range(len(lines) - 1, -1, -1):
        ln = lines[i]
        if ln.startswith("##[error]") or _EXC_LINE_RE.match(ln) or "Traceback" in ln:
            anchor = i
            break
    block = "\n".join(lines[max(0, anchor - lead_lines):anchor + 1])
    return block.encode("utf-8")[-max_bytes:].decode("utf-8", "ignore")
