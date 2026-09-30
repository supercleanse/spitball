"""Native folder chooser for `spitball pick-folder`. Tries, in order: the
xdg-desktop-portal FileChooser (works on any modern Wayland/X11 desktop,
including sandboxed apps), then `zenity`, then `kdialog`; gives up (exit 2)
so the caller (the settings panel) can fall back to a plain text field.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
import urllib.parse

PORTAL_DEST = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"

_HANDLE_RE = re.compile(r"objectpath\s+'([^']+)'")
_RESPONSE_CODE_RE = re.compile(r"uint32\s+(\d+)")
_URI_RE = re.compile(r"'uris':\s*<\[\s*'([^']*)'")


def _which(name: str) -> str | None:
    return shutil.which(name)


def _path_from_uri(uri: str) -> str:
    parsed = urllib.parse.urlparse(uri)
    return urllib.parse.unquote(parsed.path)


def _portal_call_handle(title: str, run=subprocess.run) -> str | None:
    """Calls FileChooser.OpenFile(directory=true); returns the Request
    object path it hands back immediately (the actual choice arrives later,
    as a signal on that path -- see _portal_wait_response)."""
    options = "{'directory': <true>, 'multiple': <false>}"
    cmd = ["gdbus", "call", "--session", "--dest", PORTAL_DEST, "--object-path", PORTAL_PATH,
           "--method", "org.freedesktop.portal.FileChooser.OpenFile", "", title, options]
    try:
        r = run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    m = _HANDLE_RE.search(r.stdout)
    return m.group(1) if m else None


def _portal_wait_response(handle: str, popen=subprocess.Popen, timeout: float = 120.0) -> str | None:
    """Watches `gdbus monitor` for the Request.Response signal on `handle`.
    Returns the chosen absolute path, "" if the user canceled, or None if no
    clean response was ever seen (portal unusable -- caller falls back)."""
    try:
        proc = popen(["gdbus", "monitor", "--session", "--dest", PORTAL_DEST],
                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    except OSError:
        return None
    deadline = time.monotonic() + timeout
    try:
        for line in proc.stdout:
            if handle not in line or "Response" not in line:
                if time.monotonic() > deadline:
                    return None
                continue
            code_m = _RESPONSE_CODE_RE.search(line)
            code = int(code_m.group(1)) if code_m else 1
            if code != 0:
                return ""  # 1 = canceled, 2 = ended in some other way -- both read as "no folder chosen"
            uri_m = _URI_RE.search(line)
            if not uri_m:
                return None
            return _path_from_uri(uri_m.group(1))
        return None
    finally:
        if proc.poll() is None:
            proc.terminate()


def _try_portal(title: str, run=subprocess.run, popen=subprocess.Popen) -> tuple:
    """(ran, path): ran=False means the portal couldn't be used at all (fall
    through to the next picker); ran=True + path="" means the user canceled
    (stop -- don't show a second dialog for a decision already made)."""
    handle = _portal_call_handle(title, run)
    if not handle:
        return False, ""
    result = _portal_wait_response(handle, popen)
    if result is None:
        return False, ""
    return True, result


def _try_zenity(title: str, run=subprocess.run) -> tuple:
    try:
        r = run(["zenity", "--file-selection", "--directory", f"--title={title}"],
                capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return False, ""
    if r.returncode == 0 and r.stdout.strip():
        return True, r.stdout.strip()
    if r.returncode == 1:  # zenity's own cancel/close exit code
        return True, ""
    return False, ""


def _try_kdialog(title: str, run=subprocess.run) -> tuple:
    try:
        r = run(["kdialog", "--getexistingdirectory", "~", f"--title={title}"],
                capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return False, ""
    if r.returncode == 0 and r.stdout.strip():
        return True, r.stdout.strip()
    if r.returncode == 1:
        return True, ""
    return False, ""


def pick_folder(title: str = "Choose a folder", run=subprocess.run, popen=subprocess.Popen) -> tuple:
    """Returns (exit_code, path): (0, path) chosen, (1, "") canceled,
    (2, "") no picker available at all."""
    if _which("gdbus"):
        ran, path = _try_portal(title, run, popen)
        if ran:
            return (0, path) if path else (1, "")
    if _which("zenity"):
        ran, path = _try_zenity(title, run)
        if ran:
            return (0, path) if path else (1, "")
    if _which("kdialog"):
        ran, path = _try_kdialog(title, run)
        if ran:
            return (0, path) if path else (1, "")
    return 2, ""
