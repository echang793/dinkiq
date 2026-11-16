"""Push notifications: analysis/ingest finished or failed (ntfy or Discord/Slack)."""

import logging
import os
from pathlib import Path

from jsonio import load_json

log = logging.getLogger("dinkiq.notify")


def notify_message(sdir: Path, ok: bool, error: str | None = None,
                   what: str = "analyze") -> str:
    """Human-readable analysis-finished message.

    Analysis runs for many minutes, so the useful notification is one you
    can act on without opening the app first: the result itself, plus a
    deep link. Separate from notify_webhook so it's testable without a
    network call.
    """
    meta = load_json(sdir / "meta.json")
    label = meta.get("label") or meta.get("filename") or sdir.name
    if not ok:
        return f"⚠️ DinkIQ: {label} failed to {what} — {error or 'unknown error'}"

    parts = [f"✅ DinkIQ: {label} finished analyzing."]
    dupr = load_json(sdir / "dupr.json")
    if dupr.get("band") is not None:
        line = f"Plays like ~{dupr['band']} DUPR"
        if dupr.get("confidence") is not None:
            line += f" ({dupr['confidence']:.0%} confidence)"
        parts.append(line)
    public = os.environ.get("DINKIQ_PUBLIC_URL", "").strip().rstrip("/")
    if public:
        parts.append(public)
    return "\n".join(parts)


def notify_webhook(sdir: Path, ok: bool, error: str | None = None,
                   what: str = "analyze") -> None:
    """Push an analysis-finished notification to DINKIQ_WEBHOOK_URL.

    No-op if unset. Never raises — a notification failure must never break
    the analysis pipeline.

    Supports the two shapes worth caring about, picked by URL so there's
    nothing extra to configure: ntfy (plain-text body — real phone push,
    no account needed) and Discord/Slack-compatible JSON, which stays the
    default so an already-configured Discord webhook keeps working.
    """
    url = os.environ.get("DINKIQ_WEBHOOK_URL", "").strip()
    if not url:
        return
    msg = notify_message(sdir, ok, error, what)[:2000]
    try:
        import requests
        if "ntfy" in url:
            requests.post(url, data=msg.encode(), timeout=10,
                          headers={"Title": "DinkIQ", "Tags": "ping" if ok else "warning"})
        else:
            requests.post(url, json={"content": msg}, timeout=10)
    except Exception as e:
        log.warning("webhook notify failed (non-fatal): %s", e)
