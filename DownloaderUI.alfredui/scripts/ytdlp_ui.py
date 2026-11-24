#!/usr/bin/env python3
import configparser
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

EXTRA_PATHS = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
os.environ["PATH"] = ":".join(EXTRA_PATHS) + ":" + os.environ.get("PATH", "")

WF_TAG = hashlib.sha256(str(Path(__file__).resolve().parent).encode("utf-8")).hexdigest()[:8]
LOG_PATH = Path(f"/tmp/ytdlp_ui_{WF_TAG}.log")
PID_PATH = Path(f"/tmp/ytdlp_ui_{WF_TAG}.pid")
FETCHED_URL_PATH = Path(f"/tmp/ytdlp_ui_{WF_TAG}.fetched_url")
FETCH_PENDING_PATH = Path(f"/tmp/ytdlp_ui_{WF_TAG}.fetch_pending")
CLIPBOARD_CHECKED_PATH = Path(f"/tmp/ytdlp_ui_{WF_TAG}.clipboard_checked")
LOG_HIDDEN_PATH = Path(f"/tmp/ytdlp_ui_{WF_TAG}.log_hidden")

WINDOW_EXPANDED_HEIGHT = 520
WINDOW_COMPACT_HEIGHT = 315

PCT_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
MEDIA_DEST_RE = re.compile(
    r"\[download\]\s+Destination:.*\.(?:mp4|m4a|mp3|webm|mkv|opus|ogg|aac|wav|flac)(?:\s|$)",
    re.IGNORECASE,
)
MERGED_RE = re.compile(r'\[Merger\]\s+Merging formats into "(.+?)"')
EXTRACTED_RE = re.compile(r"\[ExtractAudio\]\s+Destination:\s+(.+)")
DOWNLOAD_DEST_RE = re.compile(r"\[download\]\s+Destination:\s+(.+)")
ERROR_LINE_RE = re.compile(r"^ERROR:\s*(.+)$", re.MULTILINE)


def find_bin(name: str) -> str:
    return shutil.which(name) or ""


def get_download_dir(custom_dir: str = "") -> Path:
    chosen = (
        os.environ.get("destination_override", "")
        or (custom_dir or "")
        or os.environ.get("download_dir", "")
        or os.environ.get("DOWNLOAD_DIR", "")
    ).strip()
    target = Path(chosen).expanduser() if chosen else (Path.home() / "Downloads")
    target.mkdir(parents=True, exist_ok=True)
    return target


def unlink_quietly(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_pid() -> int:
    if not PID_PATH.exists():
        return 0
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except ValueError:
        return 0


def cleanup_temp_files() -> None:
    active_download = is_pid_running(read_pid())
    targets: List[Path] = [
        FETCHED_URL_PATH,
        FETCH_PENDING_PATH,
        CLIPBOARD_CHECKED_PATH,
        LOG_HIDDEN_PATH,
    ]
    if not active_download:
        targets.extend([LOG_PATH, PID_PATH])
    for path in targets:
        unlink_quietly(path)


def read_log_tail(max_lines: int = 30) -> str:
    if not LOG_PATH.exists():
        return "No active log."
    lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines:
        return "Log file is empty."
    return "\n".join(lines[-max_lines:])


def extract_saved_filename(log_text: str) -> str:
    for pattern in (MERGED_RE, EXTRACTED_RE, DOWNLOAD_DEST_RE):
        matches = pattern.findall(log_text)
        if matches:
            return Path(matches[-1].strip().strip('"')).name
    return ""


def extract_error_summary(log_text: str) -> str:
    matches = ERROR_LINE_RE.findall(log_text)
    if not matches:
        return "Error • Check log below"
    summary = matches[-1].strip()
    if len(summary) > 90:
        summary = summary[:87].rstrip() + "..."
    return f"Error • {summary}"


def is_valid_http_url(candidate: str) -> bool:
    if not candidate or len(candidate.splitlines()) != 1:
        return False
    parsed = urlsplit(candidate.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def compute_overall_progress(log_text: str, is_merging: bool, is_done: bool) -> Tuple[float, str]:
    if is_done:
        return 100.0, "Completed"
    if is_merging:
        return 97.0, "Merging / finalizing"

    matches = PCT_RE.findall(log_text)
    if not matches:
        return 0.0, "Preparing stream"

    current_pct = max(0.0, min(100.0, float(matches[-1])))
    media_dest_count = len(MEDIA_DEST_RE.findall(log_text))
    expects_merge = (
        media_dest_count >= 2
        or "[Merger]" in log_text
        or "Merging formats" in log_text
        or ("format(s):" in log_text and "+" in log_text)
    )

    if expects_merge:
        if media_dest_count >= 2:
            overall = 48.0 + (current_pct / 100.0) * 48.0
            return round(overall, 1), "Downloading second stream"
        overall = (current_pct / 100.0) * 48.0
        return round(overall, 1), "Downloading first stream"

    return round(current_pct, 1), "Downloading"


def ui_response(
    updates: Dict[str, Any],
    notification_title: str = "",
    notification_text: str = "",
    pulse_interval: Optional[float] = None,
    pulse_now: bool = False,
    relayout: bool = False,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "update": {
            "relayout": relayout,
            "ids": updates,
        }
    }
    if pulse_interval is not None or pulse_now:
        pulse_cfg: Dict[str, Any] = {}
        if pulse_interval is not None:
            pulse_cfg["interval"] = pulse_interval
        if pulse_now:
            pulse_cfg["now"] = True
        payload["pulse"] = pulse_cfg
    if notification_title:
        payload["notification"] = {
            "title": notification_title,
            "text": notification_text,
        }
    return payload


def log_textarea_spec(text: str = "Waiting for activity...") -> Dict[str, Any]:
    return {
        "type": "textarea",
        "id": "log_area",
        "text": text,
        "editable": False,
        "selectable": True,
        "bordertype": "solid",
        "font": {
            "name": "Menlo",
            "size": 11,
        },
        "behaviour": {
            "mode": "replace",
            "scroll": "end",
        },
        "constraints": {
            "position": "left",
            "stretch": "horizontal",
            "size": {
                "width": 660,
                "height": 150,
            },
        },
    }


def set_log_update(updates: Dict[str, Any], text: str) -> None:
    if not LOG_HIDDEN_PATH.exists():
        updates["log_area"] = {"text": text}


def get_initial_url(extra_args: Optional[List[str]] = None) -> str:
    env_url = os.environ.get("initial_url", "").strip()
    if is_valid_http_url(env_url):
        return env_url
    for item in extra_args or []:
        candidate = (item or "").strip()
        if is_valid_http_url(candidate):
            return candidate
    return ""


def get_clipboard_url() -> str:
    try:
        proc = subprocess.run(
            ["pbpaste"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if proc.returncode == 0:
            content = (proc.stdout or "").strip()
            if is_valid_http_url(content):
                return content
    except Exception:
        pass
    return ""


def preset_items() -> List[Dict[str, Any]]:
    return [
        {"text": "★ Best Video + Audio (MP4)", "value": "preset:best_mp4"},
        *[
            {"text": f"★ Video + Audio up to {height}p (MP4)", "value": f"preset:mp4_{height}"}
            for height in (2160, 1080, 720, 480)
        ],
        {"text": "★ Best Audio Only (M4A)", "value": "preset:audio_m4a"},
        {"text": "★ Best Audio Only (MP3)", "value": "preset:audio_mp3"},
    ]


COOKIE_BROWSERS = ("brave", "chrome", "chromium", "edge", "firefox", "opera", "safari", "vivaldi", "whale")


# Standard macOS application names and data locations used by yt-dlp.
BROWSER_LOCATIONS = {
    "brave": ("Brave Browser", "BraveSoftware/Brave-Browser"),
    "chrome": ("Google Chrome", "Google/Chrome"),
    "chromium": ("Chromium", "Chromium"),
    "edge": ("Microsoft Edge", "Microsoft Edge"),
    "firefox": ("Firefox", "Firefox"),
    "opera": ("Opera", "com.operasoftware.Opera"),
    "safari": ("Safari", ""),
    "vivaldi": ("Vivaldi", "Vivaldi"),
    "whale": ("Whale", "Naver/Whale"),
}


def installed_cookie_browsers(home: Optional[Path] = None,
                              app_dirs: Optional[List[Path]] = None) -> List[Dict[str, str]]:
    home = home or Path.home()
    app_dirs = app_dirs if app_dirs is not None else [
        Path("/Applications"), home / "Applications", Path("/System/Applications")]
    items = [{"text": "Cookies: None", "value": "none"}]
    for browser, (name, _) in BROWSER_LOCATIONS.items():
        if any((directory / (name + ".app")).is_dir() for directory in app_dirs):
            items.append({"text": "Cookies: " + name, "value": browser})
    return items


def browser_profiles(browser: str, home: Optional[Path] = None) -> List[Dict[str, str]]:
    """Read profile names/paths only. Never open cookie stores or decrypt data."""
    items = [{"text": "Profile: Automatic", "value": ""}]
    if browser not in BROWSER_LOCATIONS or browser == "safari":
        return items
    home = home or Path.home()
    root = home / "Library/Application Support" / BROWSER_LOCATIONS[browser][1]
    profiles: Dict[str, str] = {}
    try:
        if browser == "firefox":
            config = configparser.ConfigParser(interpolation=None)
            config.read(root / "profiles.ini", encoding="utf-8")
            for section in config.sections():
                if not section.startswith("Profile") or not config.has_option(section, "Path"):
                    continue
                path = Path(config.get(section, "Path")).expanduser()
                if config.get(section, "IsRelative", fallback="1") == "1":
                    path = root / path
                if path.is_dir():
                    profiles[str(path)] = config.get(section, "Name", fallback=path.name)
            # Also cover profiles whose registration metadata is missing.
            profile_root = root / "Profiles"
            if profile_root.is_dir():
                for path in sorted(profile_root.iterdir()):
                    if path.is_dir():
                        profiles.setdefault(str(path), path.name)
        else:
            state_path = root / "Local State"
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                cache = state.get("profile", {}).get("info_cache", {})
                if isinstance(cache, dict):
                    for directory, metadata in cache.items():
                        # Names in Local State must be immediate profile directories.
                        if Path(directory).name != directory or directory in (".", ".."):
                            continue
                        path = root / directory
                        if path.is_dir():
                            name = metadata.get("name", directory) if isinstance(metadata, dict) else directory
                            profiles[str(path)] = str(name)
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            if root.is_dir():
                for path in sorted(root.iterdir()):
                    if path.is_dir() and (path.name == "Default" or path.name.startswith("Profile ")):
                        profiles.setdefault(str(path), path.name)
                if (root / "Preferences").is_file():
                    profiles.setdefault(str(root), "Default")
    except (OSError, ValueError, configparser.Error):
        pass
    for path, name in sorted(profiles.items(), key=lambda pair: (pair[1].casefold(), pair[0])):
        items.append({"text": name + " (" + Path(path).name + ")", "value": path})
    return items


def discover_cookie_options(browser: str = "", profile: str = "", startup: bool = False) -> Dict[str, Any]:
    browsers = installed_cookie_browsers()
    browser = browser or "none"
    if browser not in {item["value"] for item in browsers}:
        browser = "none"
    profiles = browser_profiles(browser)
    # Keep a previously configured custom path/container until the browser changes.
    if startup and browser != "none" and profile and profile not in {item["value"] for item in profiles}:
        profiles.append({"text": "Saved profile: " + Path(profile).name, "value": profile})
    selected = profile if profile in {item["value"] for item in profiles} else ""
    updates = {"cookie_profile_field": {"items": profiles, "selected": selected}}
    if startup:
        updates["cookie_browser_picker"] = {"items": browsers, "selected": browser}
    result = ui_response(updates, relayout=True)
    result["variables"] = {"cookie_browser": browser, "cookie_profile": selected}
    return result


def cookie_args(browser: Optional[str] = None, profile: Optional[str] = None) -> List[str]:
    browser = (os.environ.get("cookie_browser", "none") if browser is None else browser).strip()
    profile = (os.environ.get("cookie_profile", "") if profile is None else profile).strip()
    if not browser or browser == "none":
        return []
    if browser not in COOKIE_BROWSERS:
        raise ValueError("Choose a supported cookie browser.")
    return ["--cookies-from-browser", browser + (":" + profile if profile else "")]


def parse_timestamp(value: str) -> float:
    parts = value.split(":")
    if len(value) > 32 or not 1 <= len(parts) <= 3:
        raise ValueError("Use seconds, MM:SS, or HH:MM:SS for clip times.")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,3})?", parts[-1]) or any(
        not re.fullmatch(r"[0-9]+", part) for part in parts[:-1]
    ):
        raise ValueError("Use seconds, MM:SS, or HH:MM:SS for clip times.")
    if len(parts) > 1 and (float(parts[-1]) >= 60 or (len(parts) == 3 and int(parts[1]) >= 60)):
        raise ValueError("Minutes/seconds within a timestamp must be below 60.")
    total = 0.0
    for part in parts:
        total = total * 60 + float(part)
    return total


def download_section(options: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    start = str(options.get("start_time", "")).strip()
    end = str(options.get("end_time", "")).strip()
    if not start and not end:
        return None
    begin = parse_timestamp(start) if start else 0.0
    finish = parse_timestamp(end) if end else None
    if finish is not None and finish <= begin:
        raise ValueError("Clip end must be later than its start.")
    def normalized(value: float) -> str:
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return normalized(begin), normalized(finish) if finish is not None else "inf"


def download_option_args(options: Dict[str, Any]) -> List[str]:
    args: List[str] = []
    if options.get("subtitles"):
        languages = str(options.get("languages", "")).strip() or "en.*"
        args += ["--write-subs", "--sub-langs", languages]
        if options.get("auto_subtitles"):
            args += ["--write-auto-subs"]
        if options.get("embed_subtitles"):
            args += ["--embed-subs"]
    if options.get("metadata"):
        args += ["--embed-metadata"]
    if options.get("thumbnail"):
        args += ["--embed-thumbnail"]
    rate = str(options.get("rate_limit", "")).strip()
    retries = str(options.get("retries", "")).strip()
    if rate:
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?[KMGkmg]?", rate) or float(rate.rstrip("KMGkmg")) <= 0:
            raise ValueError("Speed limit must be positive, e.g. 500K or 5M.")
        args += ["--limit-rate", rate]
    if retries:
        if not retries.isascii() or not retries.isdigit() or len(retries) > 4:
            raise ValueError("Retries must be a whole number from 0 to 9999.")
        args += ["--retries", retries, "--fragment-retries", retries]
    section = download_section(options)
    if section:
        args += ["--download-sections", "*" + section[0] + "-" + section[1]]
    return args


def build_format_picker_items(formats: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int, int]:
    picker_items = preset_items() + [{"type": "separator"}]

    video_candidates: List[Tuple[Tuple[int, int], Dict[str, Any]]] = []
    audio_candidates: List[Tuple[Tuple[int, int], Dict[str, Any]]] = []
    seen_labels: set = set()

    for f in formats:
        fmt_id = str(f.get("format_id", "")).strip()
        ext = str(f.get("ext", "")).strip()
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        if not fmt_id or ext == "mhtml":
            continue

        size_bytes = f.get("filesize") or f.get("filesize_approx")
        size_str = ""
        if size_bytes:
            val = float(size_bytes)
            for unit in ("B", "KB", "MB", "GB"):
                if val < 1024.0:
                    size_str = f" • {val:.1f} {unit}"
                    break
                val /= 1024.0

        if vcodec != "none":
            height = int(f.get("height") or 0)
            fps = int(f.get("fps") or 0)
            res = f"{height}p" if height else (f.get("resolution") or "video")
            fps_str = f" {fps}fps" if fps > 30 else ""
            has_audio = acodec != "none"
            tag = "Video+Audio" if has_audio else "Video (+Best Audio)"
            val_id = f"raw:{fmt_id}" if has_audio else f"merge:{fmt_id}"
            label = f"[Video] {res}{fps_str} ({ext}) — {tag}{size_str}"
            if label not in seen_labels:
                seen_labels.add(label)
                video_candidates.append(((height, fps), {"text": label, "value": val_id}))
        elif acodec != "none":
            abr = int(f.get("abr") or 0)
            ext_rank = 1 if ext.lower() in ("m4a", "opus", "mp3") else 0
            bitrate = f" {abr}kbps" if abr else ""
            label = f"[Audio] {ext.upper()}{bitrate}{size_str}"
            if label not in seen_labels:
                seen_labels.add(label)
                audio_candidates.append(((abr, ext_rank), {"text": label, "value": f"raw:{fmt_id}"}))

    for _, item in sorted(video_candidates, key=lambda x: x[0], reverse=True):
        picker_items.append(item)

    if video_candidates and audio_candidates:
        picker_items.append({"type": "separator"})

    for _, item in sorted(audio_candidates, key=lambda x: x[0], reverse=True):
        picker_items.append(item)

    return picker_items, len(video_candidates), len(audio_candidates)


def is_postprocessing_log(log_text: str) -> bool:
    markers = (
        "[Merger]",
        "Merging formats",
        "[ExtractAudio]",
        "[Fixup",
        "[Metadata",
        "[MoveFiles",
    )
    return any(marker in log_text for marker in markers)


def poll_status(custom_dir: str = "", extra_args: Optional[List[str]] = None) -> Dict[str, Any]:
    out_dir = get_download_dir(custom_dir)
    pid = read_pid()
    running = is_pid_running(pid)

    if not running and PID_PATH.exists():
        unlink_quietly(PID_PATH)

    if FETCH_PENDING_PATH.exists() and not running and not LOG_PATH.exists():
        pending_url = FETCH_PENDING_PATH.read_text(encoding="utf-8", errors="replace").strip()
        already_fetched = (
            FETCHED_URL_PATH.read_text(encoding="utf-8", errors="replace").strip()
            if FETCHED_URL_PATH.exists()
            else ""
        )
        if pending_url and pending_url != already_fetched:
            return fetch_formats(pending_url, str(out_dir))

        return ui_response(
            {
                "status_label": {"text": "Fetching formats..."},
                "download_progress": {"progress": 0.0},
                "percent_label": {"text": "0.0%"},
            },
            pulse_interval=0.5,
        )

    initial_url = get_initial_url(extra_args)
    already_fetched = (
        FETCHED_URL_PATH.read_text(encoding="utf-8", errors="replace").strip()
        if FETCHED_URL_PATH.exists()
        else ""
    )

    if initial_url and initial_url != already_fetched and not LOG_PATH.exists() and not running:
        FETCH_PENDING_PATH.write_text(initial_url, encoding="utf-8")
        return ui_response(
            {
                "url_field": {"text": initial_url},
                "status_label": {"text": "Fetching formats..."},
                "download_progress": {"progress": 0.0},
                "percent_label": {"text": "0.0%"},
            },
            pulse_interval=0.2,
            pulse_now=True,
        )

    log_tail = read_log_tail(30)
    full_log = LOG_PATH.read_text(encoding="utf-8", errors="replace") if LOG_PATH.exists() else ""

    has_error = "ERROR:" in full_log
    postprocessing = is_postprocessing_log(full_log)
    archived = "has already been recorded in the archive" in full_log
    has_completion_marker = (
        archived
        or postprocessing
        or "has already been downloaded" in full_log
        or "[download] 100%" in full_log
        or "[download] 100.0%" in full_log
    )
    is_merging = postprocessing and running
    is_done = (not running) and (not has_error) and has_completion_marker

    pct, stage = compute_overall_progress(full_log, is_merging=is_merging, is_done=is_done)
    progress_val = max(0.0, min(1.0, pct / 100.0))

    if running:
        status = f"Active • {stage} ({pct:.1f}%)"
        next_interval = 1.0
    elif has_error:
        status = extract_error_summary(full_log)
        next_interval = 0.0
    elif archived:
        status = "Skipped • Already recorded in this folder’s download history"
        next_interval = 0.0
    elif is_done:
        pct = 100.0
        progress_val = 1.0
        saved_name = extract_saved_filename(full_log)
        if saved_name:
            status = f"Completed • Saved {saved_name} to {out_dir}"
        else:
            status = f"Completed • Saved to {out_dir}"
        next_interval = 0.0
    else:
        if (
            not initial_url
            and not already_fetched
            and not LOG_PATH.exists()
            and not CLIPBOARD_CHECKED_PATH.exists()
        ):
            CLIPBOARD_CHECKED_PATH.write_text("1", encoding="utf-8")
            clipboard_url = get_clipboard_url()
            if clipboard_url:
                status = f"Clipboard URL ready • Press Return or ⌘F to fetch • Output: {out_dir}"
                updates: Dict[str, Any] = {
                    "url_field": {"text": clipboard_url},
                    "download_progress": {"progress": progress_val},
                    "percent_label": {"text": f"{pct:.1f}%"},
                    "status_label": {"text": status},
                }
                set_log_update(updates, f"Detected URL in clipboard: {clipboard_url}")
                return ui_response(updates, pulse_interval=0.0, relayout=False)
        status = f"Idle • Output: {out_dir}"
        next_interval = 0.0

    updates = {
        "download_progress": {"progress": progress_val},
        "percent_label": {"text": f"{pct:.1f}%"},
        "status_label": {"text": status},
    }
    set_log_update(updates, log_tail)
    return ui_response(updates, pulse_interval=next_interval, relayout=False)


def fetch_formats(url: str, custom_dir: str = "", browser: Optional[str] = None,
                  profile: Optional[str] = None) -> Dict[str, Any]:
    out_dir = get_download_dir(custom_dir)
    url = url.strip()

    unlink_quietly(LOG_PATH)
    unlink_quietly(PID_PATH)

    if not is_valid_http_url(url):
        unlink_quietly(FETCH_PENDING_PATH)
        unlink_quietly(FETCHED_URL_PATH)
        updates: Dict[str, Any] = {
            "status_label": {"text": f"Please paste a valid URL first • Output: {out_dir}"},
            "download_progress": {"progress": 0.0},
            "percent_label": {"text": "0.0%"},
        }
        set_log_update(updates, "No URL provided.")
        return ui_response(updates, pulse_interval=0.0)

    FETCH_PENDING_PATH.write_text(url, encoding="utf-8")
    FETCHED_URL_PATH.write_text(url, encoding="utf-8")

    ytdlp = find_bin("yt-dlp")
    if not ytdlp:
        unlink_quietly(FETCH_PENDING_PATH)
        updates = {
            "status_label": {"text": "yt-dlp not found. Install via Homebrew (brew install yt-dlp)."},
        }
        set_log_update(updates, "yt-dlp binary not found in PATH.")
        return ui_response(updates, pulse_interval=0.0)

    try:
        auth_args = cookie_args(browser, profile)
    except ValueError as exc:
        unlink_quietly(FETCH_PENDING_PATH)
        return ui_response({"status_label": {"text": str(exc)}}, pulse_interval=0.0)

    try:
        proc = subprocess.run(
            [ytdlp, "-J", "--no-playlist", *auth_args, "--", url],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        unlink_quietly(FETCH_PENDING_PATH)
        updates = {
            "status_label": {"text": "Timed out fetching formats — try again or check URL."},
        }
        set_log_update(updates, "yt-dlp metadata request timed out after 60s.")
        return ui_response(updates, pulse_interval=0.0)
    except Exception as exc:
        unlink_quietly(FETCH_PENDING_PATH)
        updates = {
            "status_label": {"text": f"Error running yt-dlp: {exc}"},
        }
        set_log_update(updates, str(exc))
        return ui_response(updates, pulse_interval=0.0)

    unlink_quietly(FETCH_PENDING_PATH)

    if proc.returncode != 0:
        err_lines = [line.strip() for line in (proc.stderr or "").splitlines() if line.strip()]
        err_line = err_lines[-1] if err_lines else "Failed to fetch formats"
        updates = {
            "status_label": {"text": f"yt-dlp error: {err_line[:110]}"},
        }
        set_log_update(updates, "\n".join(err_lines[-20:]) or err_line)
        return ui_response(updates, pulse_interval=0.0)

    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError:
        updates = {
            "status_label": {"text": "yt-dlp returned invalid metadata."},
        }
        set_log_update(updates, (proc.stdout or proc.stderr or "Empty output")[-1500:])
        return ui_response(updates, pulse_interval=0.0)

    title = info.get("title") or "Untitled Media"
    uploader = info.get("uploader") or ""
    formats = info.get("formats") or []
    picker_items, video_count, audio_count = build_format_picker_items(formats)

    header = f"{title} — {uploader}" if uploader else title
    updates = {
        "url_field": {"text": url},
        "media_title_label": {"text": header[:120]},
        "format_picker": {
            "items": picker_items,
            "selected": "preset:best_mp4",
        },
        "download_progress": {"progress": 0.0},
        "percent_label": {"text": "0.0%"},
        "status_label": {
            "text": f"Found {video_count} video & {audio_count} audio options • Output: {out_dir}"
        },
    }
    set_log_update(updates, "Formats loaded. Select a format and click Download Selected.")
    return ui_response(updates, pulse_interval=0.0, relayout=True)


def start_download(url: str, selection: str, custom_dir: str = "",
                   options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    unlink_quietly(FETCH_PENDING_PATH)
    url = url.strip()
    selection = (selection or "preset:best_mp4").strip()
    if not is_valid_http_url(url):
        return ui_response({
            "status_label": {"text": "Paste a valid HTTP or HTTPS URL before downloading."}
        })

    existing_pid = read_pid()
    if is_pid_running(existing_pid):
        return ui_response({
            "status_label": {"text": "Download already in progress • Click Cancel (⌘.) first."}
        })

    ytdlp = find_bin("yt-dlp")
    if not ytdlp:
        return ui_response({
            "status_label": {"text": "yt-dlp not found. Install via Homebrew (brew install yt-dlp)."}
        })

    options = options or {}
    try:
        option_args = download_option_args(options)
        auth_args = cookie_args(options.get("browser"), options.get("profile"))
    except ValueError as exc:
        return ui_response({"status_label": {"text": str(exc)}})
    needs_ffmpeg = (
        selection == "preset:best_mp4"
        or selection.startswith("preset:mp4_")
        or any(flag in option_args for flag in ("--embed-subs", "--embed-metadata", "--embed-thumbnail", "--download-sections"))
        or selection in ("preset:audio_m4a", "preset:audio_mp3")
        or selection.startswith("merge:")
    )
    if needs_ffmpeg and not find_bin("ffmpeg"):
        return ui_response({
            "status_label": {
                "text": "ffmpeg not found • Install via Homebrew (brew install ffmpeg) for merging, embedding, conversion, or clips."
            }
        })

    try:
        out_dir = get_download_dir(custom_dir)
        if not out_dir.is_dir() or not os.access(out_dir, os.W_OK):
            raise OSError("Folder is not writable")
    except OSError:
        return ui_response({"status_label": {"text": "Cannot write to the download folder. Choose another folder in Options."}})
    section = download_section(options)
    suffix = f" [clip {section[0]}-{section[1]}]" if section else ""
    out_template = str(out_dir / ("%(title)s [%(id)s]" + suffix + ".%(ext)s"))

    cmd: List[str] = [ytdlp, "--no-playlist", "--newline", "-o", out_template]

    if selection == "preset:best_mp4":
        cmd += ["-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b", "--merge-output-format", "mp4"]
    elif selection in {f"preset:mp4_{h}" for h in (2160, 1080, 720, 480)}:
        height = selection.rsplit("_", 1)[1]
        video = (
            f"bv*[ext=mp4][height<={height}]+ba[ext=m4a]/"
            f"b[ext=mp4][height<={height}]/bv*[height<={height}]+ba/b[height<={height}]"
        )
        cmd += ["-f", video, "--merge-output-format", "mp4"]
    elif selection == "preset:audio_m4a":
        cmd += ["-x", "--audio-format", "m4a", "--audio-quality", "0", "--embed-metadata"]
    elif selection == "preset:audio_mp3":
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0", "--embed-metadata"]
    elif selection.startswith("merge:"):
        fmt_id = selection.split(":", 1)[1]
        cmd += ["-f", f"{fmt_id}+bestaudio/best", "--merge-output-format", "mp4"]
    elif selection.startswith("raw:"):
        fmt_id = selection.split(":", 1)[1]
        cmd += ["-f", fmt_id]
    else:
        cmd += ["-f", "bv*+ba/b"]

    if options.get("skip_downloaded") and not section:
        cmd += ["--download-archive", str(out_dir / ".yt-dlp-archive.txt")]
    cmd += option_args + auth_args
    cmd += ["--", url]

    with open(LOG_PATH, "w", encoding="utf-8") as log_file:
        log_file.write(f"Command: {' '.join(cmd)}\n\n")
        log_file.flush()
        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    PID_PATH.write_text(str(proc.pid), encoding="utf-8")

    updates: Dict[str, Any] = {
        "download_progress": {"progress": 0.0},
        "percent_label": {"text": "0.0%"},
        "status_label": {"text": f"Active • Starting ({selection}) -> {out_dir}"},
    }
    set_log_update(updates, read_log_tail(20))
    return ui_response(
        updates,
        notification_title="yt-dlp Download Started",
        notification_text=f"Saving to {out_dir}",
        pulse_interval=1.0,
        pulse_now=True,
    )


def cancel_download() -> Dict[str, Any]:
    unlink_quietly(FETCH_PENDING_PATH)
    pid = read_pid()
    stopped = False
    if pid > 0 and is_pid_running(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            stopped = True
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
                stopped = True
            except OSError:
                pass

    unlink_quietly(PID_PATH)

    msg = "\n[Alfred UI] Download cancelled by user."
    if LOG_PATH.exists():
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")

    updates: Dict[str, Any] = {
        "download_progress": {"progress": 0.0},
        "percent_label": {"text": "0.0%"},
        "status_label": {"text": "Cancelled • Download stopped" if stopped else "Idle • No active download"},
    }
    set_log_update(updates, read_log_tail(30))
    return ui_response(updates, pulse_interval=0.0)


def initialize_destination(configured: str) -> Dict[str, Any]:
    return {"variables": {
        "configured_download_dir": configured or "~/Downloads",
        "download_dir": str(get_download_dir(configured)),
    }}


def set_destination(chosen: str) -> Dict[str, Any]:
    if is_pid_running(read_pid()):
        return ui_response({"status_label": {"text": "Wait for the download to finish before changing folders."}})
    reset = chosen == "__default__"
    candidate = os.environ.get("configured_download_dir", "~/Downloads") if reset else chosen
    path = Path(candidate).expanduser()
    if not candidate or not path.is_absolute() or not path.is_dir() or not os.access(path, os.W_OK):
        return ui_response({"status_label": {"text": "Choose an existing writable download folder."}})
    result = ui_response({"destination_field": {"text": str(path)},
                          "status_label": {"text": f"Download folder • {path}"}})
    result["variables"] = {"destination_override": "" if reset else str(path), "download_dir": str(path)}
    return result


def window_height(options_expanded: bool, log_hidden: bool) -> int:
    base = WINDOW_COMPACT_HEIGHT if log_hidden else WINDOW_EXPANDED_HEIGHT
    return base + (228 if options_expanded else 0)


def toggle_options() -> Dict[str, Any]:
    expanded = os.environ.get("options_expanded", "0") == "1"
    result: Dict[str, Any] = {
        "variables": {"options_expanded": "0" if expanded else "1"},
        "update": {"relayout": True, "ids": {
            "toggle_options_btn": {"text": "▸ Options" if expanded else "▾ Options"},
        }},
        "window": {"resize": {"height": window_height(not expanded, LOG_HIDDEN_PATH.exists())}},
    }
    if expanded:
        result["remove"] = {"ids": ["options_panel"]}
        return result

    spec_path = Path(__file__).resolve().parent.parent / "ui.json"
    panel = json.loads(spec_path.read_text(encoding="utf-8"))["templates"]["options_panel"]
    fields = {
        "destination_field": ("download_dir", "text", str(get_download_dir())),
        "start_time_field": ("option_start", "text", ""),
        "end_time_field": ("option_end", "text", ""),
        "skip_downloaded_check": ("option_skip", "checked", "false"),
        "rate_limit_field": ("option_rate", "text", ""),
        "retries_field": ("option_retries", "text", ""),
        "subtitle_languages": ("option_languages", "text", "en.*"),
        "subtitles_check": ("option_subtitles", "checked", "false"),
        "auto_subtitles_check": ("option_auto_subtitles", "checked", "false"),
        "embed_subtitles_check": ("option_embed_subtitles", "checked", "false"),
        "metadata_check": ("option_metadata", "checked", "false"),
        "thumbnail_check": ("option_thumbnail", "checked", "false"),
    }
    for row in panel["components"]:
        for component in row["components"]:
            field = fields.get(component.get("id"))
            if field:
                variable, prop, default = field
                value = default if variable == "download_dir" else os.environ.get(variable, default)
                component[prop] = value.lower() in ("true", "1", "yes") if prop == "checked" else value
    discovered = discover_cookie_options(os.environ.get("cookie_browser", "none"),
                                         os.environ.get("cookie_profile", ""), startup=True)
    # Populate the panel before insertion; hidden controls need no live updates.
    for row in panel["components"]:
        for component in row["components"]:
            component.update(discovered["update"]["ids"].get(component.get("id"), {}))
    result["variables"].update(discovered["variables"])
    result["insert"] = {"ids": {"options_container": [panel]}}
    return result


def toggle_log() -> Dict[str, Any]:
    currently_hidden = LOG_HIDDEN_PATH.exists()
    next_hidden = not currently_hidden
    if next_hidden:
        LOG_HIDDEN_PATH.write_text("1", encoding="utf-8")
        return {
            "remove": {
                "ids": ["log_area"],
            },
            "update": {
                "relayout": True,
                "ids": {
                    "toggle_log_btn": {"text": "Show Log"},
                },
            },
            "window": {
                "resize": {
                    "height": window_height(os.environ.get("options_expanded", "0") == "1", True),
                }
            },
        }

    unlink_quietly(LOG_HIDDEN_PATH)
    restored_text = read_log_tail(30) if LOG_PATH.exists() else "Waiting for activity..."
    return {
        "insert": {
            "ids": {
                "log_container": [log_textarea_spec(restored_text)],
            },
        },
        "update": {
            "relayout": True,
            "ids": {
                "toggle_log_btn": {"text": "Hide Log"},
            },
        },
        "window": {
            "resize": {
                "height": window_height(os.environ.get("options_expanded", "0") == "1", False),
            }
        },
    }


def open_downloads(custom_dir: str = "") -> Dict[str, Any]:
    out_dir = get_download_dir(custom_dir)
    subprocess.run(["open", str(out_dir)], check=False)
    return ui_response({
        "status_label": {"text": f"Opened {out_dir}"}
    })


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    arg1 = sys.argv[2] if len(sys.argv) > 2 else ""
    arg2 = sys.argv[3] if len(sys.argv) > 3 else ""
    arg3 = sys.argv[4] if len(sys.argv) > 4 else ""

    if mode == "poll":
        result: Optional[Dict[str, Any]] = poll_status(arg1, sys.argv[2:])
    elif mode == "initialize_destination":
        result = initialize_destination(arg1)
    elif mode == "set_destination":
        result = set_destination(arg1)
    elif mode == "discover_cookies":
        result = discover_cookie_options(arg1, arg2, startup=True)
    elif mode == "cookie_profiles":
        current_profile = os.environ.get("cookie_profile", "") if arg1 == os.environ.get("cookie_browser", "none") else ""
        result = discover_cookie_options(arg1, current_profile, startup=False)
    elif mode == "fetch":
        result = fetch_formats(arg1, arg2, sys.argv[4] if len(sys.argv) > 4 else None,
                               sys.argv[5] if len(sys.argv) > 5 else None)
    elif mode == "download":
        option_values = sys.argv[5:]
        def checked(index: int) -> bool:
            return len(option_values) > index and option_values[index].lower() in ("true", "1", "yes")
        options = {
            "subtitles": checked(0),
            "languages": option_values[1] if len(option_values) > 1 else "en.*",
            "auto_subtitles": checked(2),
            "embed_subtitles": checked(3),
            "metadata": checked(4),
            "thumbnail": checked(5),
            "browser": option_values[6] if len(option_values) > 6 else None,
            "profile": option_values[7] if len(option_values) > 7 else None,
            "rate_limit": option_values[8] if len(option_values) > 8 else "",
            "retries": option_values[9] if len(option_values) > 9 else "",
            "skip_downloaded": checked(10),
            "start_time": option_values[11] if len(option_values) > 11 else "",
            "end_time": option_values[12] if len(option_values) > 12 else "",
        }
        result = start_download(arg1, arg2, arg3, options)
    elif mode == "cancel":
        result = cancel_download()
    elif mode == "toggle_options":
        result = toggle_options()
    elif mode == "toggle_log":
        result = toggle_log()
    elif mode == "open_folder":
        result = open_downloads(arg1)
    elif mode == "cleanup":
        cleanup_temp_files()
        result = None
    else:
        result = ui_response({"status_label": {"text": "Unknown action"}})

    if result is not None:
        print(json.dumps(result))


if __name__ == "__main__":
    main()