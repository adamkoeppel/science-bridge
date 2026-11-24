"""Client for the local Claude Science daemon's HTTP API.

Auth mirrors the browser: `claude-science url` mints a single-use nonce, which
we POST to /api/auth/nonce to receive the `operon_auth` session cookie. Writes
also need the Origin header and an `x-operon-csrf` header matching the
`operon_csrf` cookie. Cookies persist (mode 0600) in ~/.science-bridge so a
fresh login is only needed when the session expires.

Only the endpoints the web UI itself uses are called, and only for actions the
user asked for.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import paths


class DaemonError(RuntimeError):
    pass


class DaemonDown(DaemonError):
    pass


def daemon_status() -> dict:
    """`claude-science status` as a dict (always exits 0)."""
    try:
        out = subprocess.run(
            [str(paths.CS_BIN), "status"], capture_output=True, text=True, timeout=20
        ).stdout
        return json.loads(out)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as e:
        return {"running": False, "error": str(e)}


class Client:
    def __init__(self) -> None:
        st = daemon_status()
        if not st.get("running"):
            raise DaemonDown("Claude Science daemon is not running (start the app, or `claude-science serve`).")
        host = (st.get("daemon") or {}).get("url_host") or "localhost"
        self.base = f"http://{host}:{st['port']}"
        self.origin = self.base
        self.version = st.get("version")
        self._jar_path = paths.state_dir() / "cookies.txt"
        self.jar = http.cookiejar.MozillaCookieJar(str(self._jar_path))
        if self._jar_path.exists():
            try:
                self.jar.load(ignore_discard=True, ignore_expires=True)
            except (OSError, http.cookiejar.LoadError):
                pass
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    # ------------------------------------------------------------------ auth

    def _save(self) -> None:
        self.jar.save(ignore_discard=True, ignore_expires=True)
        os.chmod(self._jar_path, 0o600)

    def _cookie(self, name: str) -> str | None:
        for c in self.jar:
            if c.name == name:
                return c.value
        return None

    def login(self) -> None:
        r = subprocess.run([str(paths.CS_BIN), "url"], capture_output=True, text=True, timeout=30)
        url = (r.stdout.strip().splitlines() or [""])[0]
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if "nonce" not in q:
            raise DaemonError(f"could not mint a login nonce (exit {r.returncode}): {r.stderr.strip()[:200]}")
        self._raw("POST", "/api/auth/nonce", {"nonce": q["nonce"][0]}, auth_retry=False, raw=True)
        if not self._cookie("operon_auth"):
            raise DaemonError("login did not return a session cookie")
        if not self._cookie("operon_csrf"):
            # The UI page sets the CSRF cookie that writes must echo back.
            self._raw("GET", "/", auth_retry=False, raw=True)
        self._save()

    # ------------------------------------------------------------------ http

    def _raw(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        query: dict | None = None,
        auth_retry: bool = True,
        raw: bool = False,
    ) -> Any:
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Origin", self.origin)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if csrf := self._cookie("operon_csrf"):
            req.add_header("x-operon-csrf", csrf)
        try:
            with self.opener.open(req, timeout=60) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 401 and auth_retry:
                self.login()
                return self._raw(method, path, body, query=query, auth_retry=False, raw=raw)
            detail = e.read().decode(errors="replace")[:500]
            raise DaemonError(f"{method} {path} -> HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise DaemonDown(f"cannot reach daemon at {self.base}: {e.reason}") from None
        self._save()
        if raw:
            return payload
        return json.loads(payload) if payload else None

    def get(self, path: str, **query: Any) -> Any:
        return self._raw("GET", path, query=query or None)

    def post(self, path: str, body: Any = None, **query: Any) -> Any:
        return self._raw("POST", path, body if body is not None else {}, query=query or None)

    # ------------------------------------------------------------- actions

    def auth_status(self) -> dict:
        return self.get("/api/auth/status")

    def frame(self, frame_id: str) -> dict:
        return self.get(f"/api/frames/{frame_id}", shallow="true")

    def send_message(self, frame_id: str, text: str, agent: str = "OPERON") -> dict:
        """Send a user message, the way the web UI does (POST /api/request).

        Queues if the session is running; starts a new turn if it is completed.
        The daemon refuses while an approval card or question is open.
        (POST /api/frames/<id>/message expects internal input_data keys; sending
        {"text": ...} there dispatches an empty turn, so don't use it.)"""
        return self.post("/api/request", {
            "target_agent": agent,
            "root_frame_id": frame_id,
            "input_data": {"request": text},
            "task_source": "user",
        })

    def new_session(self, project_id: str, text: str, agent: str = "OPERON") -> dict:
        """Start a new session in a project with its first message."""
        r = self.post("/api/request", {
            "target_agent": agent,
            "project_id": project_id,
            "input_data": {"request": text},
            "task_source": "user",
        })
        fid = r.get("root_frame_id") or r.get("frame_id")
        if not fid:
            raise DaemonError(f"unexpected new-session response: {str(r)[:300]}")
        return {"session_id": fid, **r}

    def resolve_input(self, frame_id: str, responses: list[dict]) -> dict:
        """Answer approval cards / questions on a frame (same call the UI's card buttons make)."""
        return self.post(f"/api/frames/{frame_id}/resolve-input", {"responses": responses})

    def approve_plan(self, frame_id: str, edited_plan: str | None = None) -> dict:
        body = {"edited_plan": edited_plan} if edited_plan else {}
        return self.post(f"/api/frames/{frame_id}/approve-plan", body)

    def discard_plan(self, frame_id: str) -> dict:
        return self.post(f"/api/frames/{frame_id}/discard-plan")

    def cancel(self, root_id: str, reason: str | None = None) -> dict:
        return self.post(f"/api/frames/{root_id}/cancel", reason=reason)

    def resume(self, root_id: str) -> dict:
        return self.post(f"/api/frames/{root_id}/resume")
