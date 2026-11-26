"""The dashboard's About > Updates: is a newer Project Maya release out, and updating this folder to it.

The check asks GitHub for the latest release of the repository this folder was cloned from (git's origin; else
mw00/project-maya) - only when the dashboard asks, at most every six hours (MAYA_UPDATE_CHECK=0 turns it off).
Updating fast-forwards the git checkout to that release's tag, then the server ends with UPDATE_EXIT: maya.sh /
START-MAYA.bat (maya.py) start Maya again, which compiles what changed in the engine and loads the model.  A folder
that can't do that (no git, local changes, a server started some other way) gets the reason and the steps by hand.

A fork of Maya releases its own builds of one upstream version, tagged with a suffix (v1.1.81-arm64: Windows on ARM,
which upstream does not build for).  The version still decides what is newer and the tag is still what the folder
fast-forwards to; upstream's own newest release is reported beside it, as news, for the wait between builds."""
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "mw00/project-maya"
UPDATE_EXIT = 75                    # the server's exit code that tells maya.py: updated, start again (EX_TEMPFAIL)
RESTART_EXIT = 76                   # ... the dashboard saved a new port: start again with the saved config
SUPERVISED = "MAYA_RESTART_ON_UPDATE"   # set by maya.py for the server it starts: it starts it again after an update
CHECK_EVERY = 6 * 3600
RETRY_AFTER = 10 * 60               # GitHub not reached: ask again after this
TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(-[a-z][a-z0-9.]*)?$")   # a fork's own build of a release: v1.1.81-arm64


def vtuple(v):
    m = TAG.match(str(v or "").strip())
    return tuple(int(x) for x in m.groups()[:3]) if m else None    # the build's suffix is not part of the ordering


def build_of(tag):
    """A release tag's build suffix ("" for a plain release): v1.1.81-arm64 -> "-arm64".  A fork of Maya tags its own
    releases this way because they are one upstream version built for a platform upstream does not build for; the
    version still decides what is newer, and the suffix only says which build of it this is."""
    m = TAG.match(str(tag or "").strip())
    return (m.group(4) or "") if m else ""


def git(root, *args, timeout=60):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)


def last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else "no output"


def origin_repo(root=ROOT) -> str:
    """owner/name of the GitHub repository this folder was cloned from (a fork checks its own releases)."""
    try:
        r = git(root, "remote", "get-url", "origin", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return REPO
    m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", r.stdout.strip())
    return m.group(1) if r.returncode == 0 and m else REPO


def summary(notes: str) -> str:
    """A release's first sentence (the notes open with one bold summary sentence)."""
    for ln in (notes or "").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            return re.sub(r"[*_`]", "", ln)[:300]
    return ""


class Updater:
    def __init__(self, current: str | None, root: Path = ROOT):
        self.current = current
        self.root = Path(root)
        self.lock = threading.Lock()       # one check or one update at a time
        self.latest = None                 # {"version", "tag", "url", "summary", "published", "repo", "build"}
        self.upstream = None               # upstream's own latest release, when this folder updates from a fork
        self.checked = 0.0
        self.next_check = 0.0
        self.error = None
        self.state = None                  # {"state": "updating" | "failed", "to", "started", "error"}
        self.thread = None

    # ---------------------------------------------------------------- is there a newer release
    def enabled(self) -> bool:
        return os.environ.get("MAYA_UPDATE_CHECK", "1") != "0"

    def check(self, force: bool = False) -> dict:
        if self.enabled() and (force or time.time() >= self.next_check):
            with self.lock:
                if force or time.time() >= self.next_check:
                    self._fetch_latest()
        return self.info()

    def _release(self, repo: str) -> dict:
        """One repository's latest release, as the page wants it (raises when GitHub is not reached or the tag is not a
        version, which is what the six-hour pause is for)."""
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                     headers={"Accept": "application/vnd.github+json",
                                              "User-Agent": f"project-maya/{self.current or '?'}"})
        with urllib.request.urlopen(req, timeout=8) as r:
            rel = json.loads(r.read().decode("utf-8"))
        tag = str(rel.get("tag_name") or "")
        if not vtuple(tag):
            raise ValueError(f"the latest release's tag is not a version ({tag[:40]!r})")
        return {"version": tag.lstrip("v"), "tag": tag, "url": rel.get("html_url"),
                "summary": summary(rel.get("body") or ""), "published": rel.get("published_at"),
                "repo": repo, "build": build_of(tag)}

    def _fetch_latest(self):
        repo = origin_repo(self.root) if (self.root / ".git").exists() else REPO
        try:
            self.latest = self._release(repo)
            # A fork's releases are its own builds of one upstream version, so upstream's newest release is a second
            # piece of news the page can use: "upstream reached 1.1.83, the build for this PC is not made from it yet"
            # rather than silence.  It is a nicety: failing to ask for it does not fail the check.
            self.upstream = None
            if repo != REPO:
                try:
                    self.upstream = self._release(REPO)
                except (OSError, ValueError):
                    pass
            self.error = None
            self.checked = time.time()
            self.next_check = self.checked + CHECK_EVERY
        except (OSError, ValueError) as e:              # offline, GitHub's rate limit, a proxy: say so, ask later
            self.error = f"GitHub could not be reached ({getattr(e, 'reason', None) or e})"
            self.next_check = time.time() + RETRY_AFTER

    def newer(self) -> bool:
        cur, new = vtuple(self.current), vtuple(self.latest and self.latest["version"])
        return bool(cur and new and new > cur)

    # ---------------------------------------------------------------- can this folder update itself
    def blocker(self) -> str | None:
        """Why this install can't update itself from the page (None: it can)."""
        if not os.environ.get(SUPERVISED):
            return "this server was not started by ./setup.sh or START-MAYA.bat, which start it again after an update"
        if not (self.root / ".git").exists():
            return "this folder is not a git checkout (it was downloaded as an archive)"
        try:
            r = git(self.root, "status", "--porcelain", "--untracked-files=no", timeout=20)
        except (OSError, subprocess.SubprocessError):
            return "git is not installed"
        if r.returncode != 0:
            return f"git can't read this folder ({last_line(r.stderr)})"
        if r.stdout.strip():
            return "files in this folder were changed by hand (git status lists them)"
        return None

    def waiting(self) -> dict | None:
        """Upstream's newest release when it is newer than the release this folder updates from: a fork builds its
        platforms every few releases, and the page says so instead of calling the older build simply up to date."""
        up, new = self.upstream, self.latest
        cu, cn = vtuple((up or {}).get("version")), vtuple((new or {}).get("version"))
        return {"version": up["version"], "url": up.get("url")} if up and cu and cn and cu > cn else None

    def info(self) -> dict:
        newer = self.newer()
        why = self.blocker() if newer else None
        state = dict(self.state) if self.state else None
        tag = str((self.latest or {}).get("tag") or "")
        by_hand = ("download the new release from GitHub into a new folder" if not (self.root / ".git").exists()
                   else (f"git fetch --tags && git merge --ff-only refs/tags/{tag}, then start Maya again (./setup.sh or "
                         "START-MAYA.bat)" if build_of(tag)      # a build's tag is not what a plain git pull brings in
                         else "git pull, then start Maya again (./setup.sh or START-MAYA.bat)"))
        return {"current": self.current, "enabled": self.enabled(), "latest": self.latest, "newer": newer,
                "checked": self.checked or None, "error": self.error, "can_update": newer and why is None,
                "blocker": why, "state": state, "upstream": self.upstream, "waiting": self.waiting() if not newer else None,
                "by_hand": by_hand}

    def busy(self) -> bool:
        return bool(self.state) and self.state.get("state") == "updating"

    # ---------------------------------------------------------------- the update itself
    def start(self, svc) -> dict:
        """Update to the latest release on a thread (after the request in flight; new requests get a 503 meanwhile)."""
        with self.lock:                                 # two clicks: one update
            if not self.newer():
                raise ValueError("this is the latest version")
            why = self.blocker()
            if why:
                raise ValueError(f"Maya can't update itself here: {why}")
            if self.busy():
                raise ValueError("an update is already running")
            self.state = {"state": "updating", "to": self.latest["version"], "started": time.time(), "error": None}
            self.thread = threading.Thread(target=self._update, args=(svc, self.latest["tag"]), daemon=True)
            self.thread.start()
            return dict(self.state)

    def _update(self, svc, tag: str):
        with svc.fifo:                                  # the request in flight finishes first
            print(f"[maya] updating Project Maya to {tag} ...", flush=True)
            before = None
            try:
                before = git(self.root, "rev-parse", "HEAD", timeout=20).stdout.strip()
                r = git(self.root, "fetch", "--tags", "origin", timeout=600)
                if r.returncode:
                    raise RuntimeError(f"git fetch failed: {last_line(r.stderr)}")
                r = git(self.root, "merge", "--ff-only", f"refs/tags/{tag}", timeout=120)
                if r.returncode:
                    raise RuntimeError(f"this folder can't move straight to {tag} ({last_line(r.stderr)}); "
                                       "update it by hand: git pull")
            except (OSError, subprocess.SubprocessError, RuntimeError) as e:
                err = str(e)
                print(f"[maya] the update did not happen: {err}", flush=True)
                self.state.update(state="failed", error=err, ended=time.time())
                return
            print(f"[maya] updated the files {before[:9] if before else ''} -> {tag}; Maya starts again (maya.py "
                  "compiles what changed in the engine, then loads the model) ...", flush=True)
            svc.close_for_restart()
            os._exit(UPDATE_EXIT)
