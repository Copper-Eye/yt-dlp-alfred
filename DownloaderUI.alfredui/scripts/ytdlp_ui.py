#!/usr/bin/env python3
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

EXTRA_PATHS = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
os.environ["PATH"] = ":".join(EXTRA_PATHS) + ":" + os.environ.get("PATH", "")
LOG_PATH = Path("/tmp/ytdlp_ui.log")
PID_PATH = Path("/tmp/ytdlp_ui.pid")
FETCHED_URL_PATH = Path("/tmp/ytdlp_ui.fetched_url")
FETCH_PENDING_PATH = Path("/tmp/ytdlp_ui.fetch_pending")
PCT_RE = re.compile(r"[download]\s+(\d+(?:\.\d+)?)%")
DEST_RE = re.compile(r"[download]\s+Destination:")

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

def cleanup_temp_files() -> None:
    for path in (LOG_PATH, PID_PATH, FETCHED_URL_PATH, FETCH_PENDING_PATH):
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

def read_log_tail(max_lines: int = 30) -> str:
    if not LOG_PATH.exists():
        return "No active log."
    lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines:
        return "Log file is empty."
    return "\n".join(lines[-max_lines:])

def compute_overall_progress(log_text: str, is_merging: bool, is_done: bool) -> tuple[float, str]:
    if is_done:
        return 100.0, "Completed"
    if is_merging:
        return 97.0, "Merging / finalizing"

    matches = PCT_RE.findall(log_text)
    if not matches:
        return 0.0, "Preparing stream"

    current_pct = max(0.0, min(100.0, float(matches[-1])))
    dest_count = len(DEST_RE.findall(log_text))
    expects_merge = (
        dest_count >= 2
        or "[Merger]" in log_text
        or "Merging formats" in log_text
        or ("format(s):" in log_text and "+" in log_text)
    )

    if expects_merge:
        if dest_count >= 2:
            overall = 48.0 + (current_pct / 100.0) * 48.0
            return round(overall, 1), "Downloading second stream"
        overall = (current_pct / 100.0) * 48.0
        return round(overall, 1), "Downloading first stream"

    return round(current_pct, 1), "Downloading"

def ui_response(
    updates: dict,
    notification_title: str = "",
    notification_text: str = "",
    pulse_interval: float | None = None,
    pulse_now: bool = False,
) -> dict:
    payload: dict = {
        "update": {
            "relayout": True,
            "ids": updates,
        }
    }
    if pulse_interval is not None or pulse_now:
        pulse_cfg: dict = {}
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


def get_initial_url(extra_args: list[str] | None = None) -> str:
    env_url = os.environ.get("initial_url", "").strip()
    if env_url:
        return env_url
    for item in extra_args or []:
        candidate = (item or "").strip()
        if candidate.startswith(("http://", "https://")):
            return candidate
    return ""

def poll_status(custom_dir: str = "", extra_args: list[str] | None = None) -> dict:
    out_dir = get_download_dir(custom_dir)

    # Keep "Fetching formats..." visible while an async fetch is in flight
    if FETCH_PENDING_PATH.exists():
        pending_url = FETCH_PENDING_PATH.read_text(encoding="utf-8", errors="replace").strip()
        already_fetched = (
            FETCHED_URL_PATH.read_text(encoding="utf-8", errors="replace").strip()
            if FETCHED_URL_PATH.exists()
            else ""
        )
        if pending_url and pending_url != already_fetched and not LOG_PATH.exists() and not PID_PATH.exists():
            # Trigger the actual fetch on this tick so it starts promptly
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

    if initial_url and initial_url != already_fetched and not LOG_PATH.exists() and not PID_PATH.exists():
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

    pid = read_pid()
    running = is_pid_running(pid)
    has_error = "ERROR:" in full_log
    is_merging = ("[Merger]" in full_log or "[ExtractAudio]" in full_log) and running
    is_done = (not running) and (not has_error) and (
        "[Merger]" in full_log
        or "[ExtractAudio]" in full_log
        or "has already been downloaded" in full_log
        or "100%" in full_log
    )

    pct, stage = compute_overall_progress(full_log, is_merging=is_merging, is_done=is_done)

    if running:
        status = f"Active • {stage} ({pct:.1f}%)"
        next_interval = 1.0
    elif has_error:
        status = "Error • Check log below"
        next_interval = 0.0
    elif is_done:
        pct = 100.0
        status = f"Completed • Saved to {out_dir}"
        next_interval = 0.0
    else:
        status = f"Idle • Output: {out_dir}"
        next_interval = 0.0

    return ui_response(
        {
            "download_progress": {"progress": pct / 100.0},
            "percent_label": {"text": f"{pct:.1f}%"},
            "status_label": {"text": status},
            "log_area": {"text": log_tail},
        },
        pulse_interval=next_interval,
    )

def fetch_formats(url: str, custom_dir: str = "") -> dict:
    out_dir = get_download_dir(custom_dir)
    url = url.strip()

    for path in (LOG_PATH, PID_PATH):
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass

    if not url:
        for path in (FETCH_PENDING_PATH, FETCHED_URL_PATH):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
        return ui_response(
            {
                "status_label": {"text": f"Please paste a valid URL first • Output: {out_dir}"},
                "download_progress": {"progress": 0.0},
                "percent_label": {"text": "0.0%"},
                "log_area": {"text": "No URL provided."},
            },
            pulse_interval=0.0,
        )

    FETCH_PENDING_PATH.write_text(url, encoding="utf-8")
    FETCHED_URL_PATH.write_text(url, encoding="utf-8")

    ytdlp = find_bin("yt-dlp")
    if not ytdlp:
        try:
            FETCH_PENDING_PATH.unlink()
        except OSError:
            pass
        return ui_response(
            {
                "status_label": {"text": "yt-dlp not found. Install via Homebrew (brew install yt-dlp)."},
                "log_area": {"text": "yt-dlp binary not found in PATH."},
            },
            pulse_interval=0.0,
        )

    try:
        proc = subprocess.run(
            [ytdlp, "-J", "--no-playlist", url],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:
        try:
            FETCH_PENDING_PATH.unlink()
        except OSError:
            pass
        return ui_response(
            {
                "status_label": {"text": f"Error running yt-dlp: {exc}"},
                "log_area": {"text": str(exc)},
            },
            pulse_interval=0.0,
        )

    try:
        FETCH_PENDING_PATH.unlink()
    except OSError:
        pass

    if proc.returncode != 0:
        err_lines = [line.strip() for line in (proc.stderr or "").splitlines() if line.strip()]
        err_line = err_lines[-1] if err_lines else "Failed to fetch formats"
        return ui_response(
            {
                "status_label": {"text": f"yt-dlp error: {err_line[:110]}"},
                "log_area": "\n".join(err_lines[-20:]) or err_line,
            },
            pulse_interval=0.0,
        )

    info = json.loads(proc.stdout)
    title = info.get("title") or "Untitled Media"
    uploader = info.get("uploader") or ""
    formats = info.get("formats") or []

    picker_items = [
        {"text": "★ Best Video + Audio (MP4)", "value": "preset:best_mp4"},
        {"text": "★ Best Audio Only (M4A)", "value": "preset:audio_m4a"},
        {"text": "★ Best Audio Only (MP3)", "value": "preset:audio_mp3"},
        {"type": "separator"},
    ]

    video_items = []
    audio_items = []

    for f in formats:
        fmt_id = str(f.get("format_id", "")).strip()
        ext = f.get("ext", "")
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
            height = f.get("height")
            fps = f.get("fps")
            res = f"{height}p" if height else (f.get("resolution") or "video")
            fps_str = f" {int(fps)}fps" if fps and fps > 30 else ""
            has_audio = acodec != "none"
            tag = "Video+Audio" if has_audio else "Video (+Best Audio)"
            val = f"raw:{fmt_id}" if has_audio else f"merge:{fmt_id}"
            label = f"[Video] {res}{fps_str} ({ext}) — {tag}{size_str}"
            video_items.append((height or 0, {"text": label, "value": val}))
        elif acodec != "none":
            abr = f.get("abr")
            bitrate = f" {int(abr)}kbps" if abr else ""
            label = f"[Audio] {ext.upper()}{bitrate}{size_str}"
            audio_items.append((abr or 0, {"text": label, "value": f"raw:{fmt_id}"}))

    for _, item in sorted(video_items, key=lambda x: x[0], reverse=True):
        picker_items.append(item)

    if video_items and audio_items:
        picker_items.append({"type": "separator"})

    for _, item in sorted(audio_items, key=lambda x: x[0], reverse=True):
        picker_items.append(item)

    header = f"{title} — {uploader}" if uploader else title
    return ui_response(
        {
            "url_field": {"text": url},
            "media_title_label": {"text": header[:120]},
            "format_picker": {
                "items": picker_items,
                "selected": "preset:best_mp4",
            },
            "download_progress": {"progress": 0.0},
            "percent_label": {"text": "0.0%"},
            "status_label": {
                "text": f"Found {len(video_items)} video & {len(audio_items)} audio options • Output: {out_dir}"
            },
            "log_area": {"text": "Formats loaded. Select a format and click Download Selected."},
        },
        pulse_interval=0.0,
    )

def start_download(url: str, selection: str, custom_dir: str = "") -> dict:
    url = url.strip()
    selection = (selection or "preset:best_mp4").strip()
    if not url:
        return ui_response({
            "status_label": {"text": "Paste a URL before downloading."}
        })

    ytdlp = find_bin("yt-dlp")
    if not ytdlp:
        return ui_response({
            "status_label": {"text": "yt-dlp not found. Install via Homebrew (brew install yt-dlp)."}
        })

    out_dir = get_download_dir(custom_dir)
    out_template = str(out_dir / "%(title)s [%(id)s].%(ext)s")

    cmd = [ytdlp, "--no-playlist", "--newline", "-o", out_template]

    if selection == "preset:best_mp4":
        cmd += ["-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b", "--merge-output-format", "mp4"]
    elif selection == "preset:audio_m4a":
        cmd += ["-x", "--audio-format", "m4a", "--audio-quality", "0"]
    elif selection == "preset:audio_mp3":
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    elif selection.startswith("merge:"):
        fmt_id = selection.split(":", 1)[1]
        cmd += ["-f", f"{fmt_id}+bestaudio/best", "--merge-output-format", "mp4"]
    elif selection.startswith("raw:"):
        fmt_id = selection.split(":", 1)[1]
        cmd += ["-f", fmt_id]
    else:
        cmd += ["-f", "bv*+ba/b"]

    cmd.append(url)

    log_file = open(LOG_PATH, "w", encoding="utf-8")
    log_file.write(f"Command: {' '.join(cmd)}\n\n")
    log_file.flush()

    proc = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log_file.close()
    PID_PATH.write_text(str(proc.pid), encoding="utf-8")

    return ui_response(
        {
            "download_progress": {"progress": 0.0},
            "percent_label": {"text": "0.0%"},
            "status_label": {"text": f"Active • Starting ({selection}) -> {out_dir}"},
            "log_area": {"text": read_log_tail(20)},
        },
        notification_title="yt-dlp Download Started",
        notification_text=f"Saving to {out_dir}",
        pulse_interval=1.0,
        pulse_now=True,
    )

def cancel_download() -> dict:
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

    try:
        if PID_PATH.exists():
            PID_PATH.unlink()
    except OSError:
        pass

    msg = "\n[Alfred UI] Download cancelled by user."
    if LOG_PATH.exists():
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")

    return ui_response(
        {
            "download_progress": {"progress": 0.0},
            "percent_label": {"text": "0.0%"},
            "status_label": {"text": "Cancelled • Download stopped" if stopped else "Idle • No active download"},
            "log_area": {"text": read_log_tail(30)},
        },
        pulse_interval=0.0,
    )

def open_downloads(custom_dir: str = "") -> dict:
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
        result = poll_status(arg1, sys.argv[2:])
    elif mode == "fetch":
        result = fetch_formats(arg1, arg2)
    elif mode == "download":
        result = start_download(arg1, arg2, arg3)
    elif mode == "cancel":
        result = cancel_download()
    elif mode == "open_folder":
        result = open_downloads(arg1)
    elif mode == "cleanup":
        cleanup_temp_files()
        result = {}
    else:
        result = ui_response({"status_label": {"text": "Unknown action"}})

    if result:
        print(json.dumps(result))


if __name__ == "__main__":
    main()