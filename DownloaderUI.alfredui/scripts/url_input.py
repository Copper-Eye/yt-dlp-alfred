#!/usr/bin/python3
"""Resolve URL input without changing the clipboard or starting a download."""
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Iterable, Mapping, Tuple
from urllib.parse import urlsplit

HISTORY_LIMIT = 20
HISTORY_PREFIX = "url_history_"
BROWSER_TASK = Path.home() / (
    "Library/Application Support/Alfred/Automation/Tasks/"
    "com.alfredapp.automation.core/safari/.common/tabs-current"
)


def first_link(text: str) -> str:
    for match in re.finditer(r'https?://[^\s<>"\x00-\x1f]+', text or "", re.IGNORECASE):
        url = match.group(0).rstrip(".,;:'\u2019\u201d")
        for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
            while url.endswith(closing) and url.count(closing) > url.count(opening):
                url = url[:-1]
        try:
            parsed = urlsplit(url)
            if parsed.scheme.lower() in ("http", "https") and parsed.hostname:
                return url
        except ValueError:
            continue
    return ""


def browser_url() -> str:
    if not BROWSER_TASK.is_file():
        return ""
    try:
        result = subprocess.run(
            [str(BROWSER_TASK), "frontmost_browser", "1", "url"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            value = json.loads(result.stdout).get("alfredworkflow", {}).get("arg", "")
            if isinstance(value, list):
                value = value[0] if value else ""
            return first_link(value) if isinstance(value, str) else ""
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
        pass
    return ""


def current_clipboard() -> str:
    try:
        result = subprocess.run(["/usr/bin/pbpaste"], capture_output=True, text=True, timeout=2)
        return first_link(result.stdout) if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def select_url(browser: str, current: str, history: Iterable[str]) -> Tuple[str, str]:
    for text, source in [(browser, "browser"), (current, "clipboard")]:
        url = first_link(text)
        if url:
            return url, source
    for text in history:
        url = first_link(text)
        if url:
            return url, "history"
    return "", ""


def resolve_input(environment: Mapping[str, str]) -> dict:
    explicit = environment.get("initial_url", "").strip()
    url, source = "", ""
    if not explicit:
        url = browser_url()
        if url:
            source = "browser"
        else:
            history = [environment.get(f"{HISTORY_PREFIX}{i:02}", "") for i in range(HISTORY_LIMIT)]
            url, source = select_url("", current_clipboard(), history)
    variables = {
        "initial_url": explicit,
        "suggested_url": url,
        "suggested_url_source": source,
        "url_input_resolved": "1",
        "url_source_label": "Source: " + ("Entered URL" if explicit else {
            "browser": "Browser tab", "clipboard": "Clipboard", "history": "Clipboard history",
        }.get(source, "—")),
    }
    # Do not pass unrelated clipboard history on to the UI or download process.
    variables.update({f"{HISTORY_PREFIX}{i:02}": "" for i in range(HISTORY_LIMIT)})
    return {"alfredworkflow": {"arg": "", "variables": variables}}


if __name__ == "__main__":
    print(json.dumps(resolve_input(os.environ)))
