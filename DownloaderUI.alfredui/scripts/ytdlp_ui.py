#!/usr/bin/env python3
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

WINDOW_EXPANDED_HEIGHT = 504
WINDOW_COMPACT_HEIGHT = 299

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
        (custom_dir or "")
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


def source_log_text(text: str) -> str:
    source = os.environ.get("url_source_label", "").partition(": ")[2]
    if source not in {"Browser tab", "Clipboard", "Clipboard history", "Entered URL"}:
        return text
    heading = f"URL source: {source}"
    return text if text.startswith(heading) else heading + "\n" + text


def read_log_tail(max_lines: int = 30) -> str:
    if not LOG_PATH.exists():
        return source_log_text("No active log.")
    lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines:
        return source_log_text("Log file is empty.")
    return source_log_text("\n".join(lines[-max_lines:]))


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
        updates["log_area"] = {"text": source_log_text(text)}


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
    if os.environ.get("url_input_resolved") == "1":
        candidate = os.environ.get("suggested_url", "").strip()
        return candidate if is_valid_http_url(candidate) else ""
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


def build_format_picker_items(formats: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int, int]:
    picker_items: List[Dict[str, Any]] = [
        {"text": "★ Best Video + Audio (MP4)", "value": "preset:best_mp4"},
        {"text": "★ Best Audio Only (M4A)", "value": "preset:audio_m4a"},
        {"text": "★ Best Audio Only (MP3)", "value": "preset:audio_mp3"},
        {"type": "separator"},
    ]

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
    has_completion_marker = (
        postprocessing
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
                source = {"browser": "Browser tab", "clipboard": "Clipboard", "history": "Clipboard history"}.get(
                    os.environ.get("suggested_url_source", ""), "Clipboard")
                status = f"{source} URL ready • Press Return or ⌘F to fetch • Output: {out_dir}"
                updates: Dict[str, Any] = {
                    "url_field": {"text": clipboard_url},
                    "url_origin_label": {"text": f"Source: {source}"},
                    "download_progress": {"progress": progress_val},
                    "percent_label": {"text": f"{pct:.1f}%"},
                    "status_label": {"text": status},
                }
                set_log_update(updates, "URL selected. Press Return or Fetch Formats to continue.")
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


def fetch_formats(url: str, custom_dir: str = "") -> Dict[str, Any]:
    out_dir = get_download_dir(custom_dir)
    url = url.strip()

    unlink_quietly(LOG_PATH)
    unlink_quietly(PID_PATH)

    if not url:
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
        proc = subprocess.run(
            [ytdlp, "-J", "--no-playlist", url],
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


def start_download(url: str, selection: str, custom_dir: str = "") -> Dict[str, Any]:
    unlink_quietly(FETCH_PENDING_PATH)
    url = url.strip()
    selection = (selection or "preset:best_mp4").strip()
    if not url:
        return ui_response({
            "status_label": {"text": "Paste a URL before downloading."}
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

    needs_ffmpeg = (
        selection in ("preset:audio_m4a", "preset:audio_mp3")
        or selection.startswith("merge:")
    )
    if needs_ffmpeg and not find_bin("ffmpeg"):
        return ui_response({
            "status_label": {
                "text": "ffmpeg not found • Install via Homebrew (brew install ffmpeg) for audio/merge presets."
            }
        })

    out_dir = get_download_dir(custom_dir)
    out_template = str(out_dir / "%(title)s [%(id)s].%(ext)s")

    cmd: List[str] = [ytdlp, "--no-playlist", "--newline", "-o", out_template]

    if selection == "preset:best_mp4":
        cmd += ["-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b", "--merge-output-format", "mp4"]
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

    cmd.append(url)

    with open(LOG_PATH, "w", encoding="utf-8") as log_file:
        log_file.write(source_log_text(f"Command: {' '.join(cmd)}\n\n"))
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
                    "height": WINDOW_COMPACT_HEIGHT,
                }
            },
        }

    unlink_quietly(LOG_HIDDEN_PATH)
    restored_text = read_log_tail(30)
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
                "height": WINDOW_EXPANDED_HEIGHT,
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
    elif mode == "fetch":
        result = fetch_formats(arg1, arg2)
    elif mode == "download":
        result = start_download(arg1, arg2, arg3)
    elif mode == "cancel":
        result = cancel_download()
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