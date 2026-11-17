"""Upload a rendered short to YouTube (Shorts) and Instagram (Reels).

    cd ~/anime-studio
    uv run python remotion/scripts/shorts/publish.py auth-youtube                  # once: sign in, stores a refresh token
    uv run python remotion/scripts/shorts/publish.py check                         # are the credentials usable?
    uv run python remotion/scripts/shorts/publish.py upload <slug> <file.mp4> --youtube --instagram --confirm

Nothing is uploaded without --confirm. YouTube uploads are PRIVATE unless --public is given
(and unverified API projects are forced to private by Google anyway). Instagram has no private mode:
an Instagram upload is published publicly.

Credentials (never in the repo, chmod 600), in ~/.config/anime-studio/:
  youtube_client_secret.json   OAuth client of type "Desktop app" (Google Cloud console download)
  youtube_token.json           written by auth-youtube
  instagram.json               {"access_token": "...", "user_id": "..."}  (Instagram API with Instagram Login)
Every upload is logged in remotion/out/shorts/uploads.json; the same file is not uploaded twice to the same platform.
"""

from __future__ import annotations

import http.server as httpserver
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CFG = Path.home() / ".config" / "anime-studio"
ROOT = Path(__file__).resolve().parents[2]  # remotion/
LOG = ROOT / "out" / "shorts" / "uploads.json"
IG_API = "v23.0"
YT_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
REDIRECT_PORT = 8765


# ----------------------------------------------------------------------------- helpers
def call(method: str, url: str, data: bytes | None = None, headers: dict | None = None) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def form(d: dict) -> bytes:
    return urllib.parse.urlencode(d).encode()


def save_secret(p: Path, obj: dict) -> None:
    CFG.mkdir(parents=True, exist_ok=True)
    p.touch(mode=0o600, exist_ok=True)
    os.chmod(p, 0o600)
    p.write_text(json.dumps(obj, indent=1))


def die(msg: str) -> None:
    sys.exit(f"error: {msg}")


def log_load() -> list[dict]:
    return json.loads(LOG.read_text()) if LOG.exists() else []


def log_add(entry: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    items = log_load()
    items.append(entry)
    LOG.write_text(json.dumps(items, indent=1, ensure_ascii=False))


def metadata(slug: str) -> dict:
    topic = json.loads((ROOT / "shorts" / slug / "topic.json").read_text())
    pub = topic.get("publish", {})
    title = pub.get("title") or topic["title"]
    tags = pub.get("tags") or []
    hashtags = " ".join("#" + t.replace(" ", "") for t in (pub.get("hashtags") or ["shorts", "facts", "top10"]))
    desc = pub.get("description") or topic["hook"]["say"]
    sources = "\n".join("• " + s for s in topic.get("sources", []))
    full = f"{desc}\n\n{hashtags}" + (f"\n\nSources:\n{sources}" if sources else "")
    return {"title": title[:100], "description": full[:5000], "tags": tags, "caption": f"{title}\n\n{desc}\n\n{hashtags}"[:2200]}


# ----------------------------------------------------------------------------- YouTube
def yt_client() -> dict:
    p = CFG / "youtube_client_secret.json"
    if not p.exists():
        die(f"missing {p} (Google Cloud > Credentials > OAuth client ID > Desktop app > Download JSON)")
    d = json.loads(p.read_text())
    return d.get("installed") or d.get("web") or d


def auth_youtube() -> None:
    c = yt_client()
    redirect = f"http://127.0.0.1:{REDIRECT_PORT}"
    state = secrets.token_urlsafe(16)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": YT_SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
    })
    save_secret(CFG / "youtube_pending.json", {"state": state})
    got: dict = {}

    class H(httpserver.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>Done. You can close this tab.</h2>".encode())

        def log_message(self, *a) -> None:
            pass

    srv = httpserver.HTTPServer(("127.0.0.1", REDIRECT_PORT), H)
    srv.timeout = 300
    print("Open this link in your browser (Windows is fine) and allow access:\n\n" + url + "\n", flush=True)
    print(f"waiting for the browser to come back to {redirect} ...")
    while "code" not in got and "error" not in got:
        srv.handle_request()
    yt_finish(got)


def yt_finish(got: dict) -> None:
    """Exchange the code. Also usable by hand: auth-youtube-finish '<URL the browser landed on>'."""
    c = yt_client()
    redirect = f"http://127.0.0.1:{REDIRECT_PORT}"
    pend = CFG / "youtube_pending.json"
    state = json.loads(pend.read_text())["state"] if pend.exists() else None
    if got.get("error") or not state or got.get("state") != state:
        die(f"authorization failed: {got.get('error', 'state mismatch')}")
    st, _, body = call("POST", "https://oauth2.googleapis.com/token", form({
        "code": got["code"], "client_id": c["client_id"], "client_secret": c["client_secret"],
        "redirect_uri": redirect, "grant_type": "authorization_code",
    }), {"Content-Type": "application/x-www-form-urlencoded"})
    tok = json.loads(body)
    if st != 200 or "refresh_token" not in tok:
        die(f"token exchange failed ({st}): {tok.get('error_description') or tok.get('error')}")
    save_secret(CFG / "youtube_token.json", {"refresh_token": tok["refresh_token"]})
    pend.unlink(missing_ok=True)
    print("YouTube connected; refresh token saved to", CFG / "youtube_token.json")


def yt_access_token() -> str:
    c = yt_client()
    p = CFG / "youtube_token.json"
    if not p.exists():
        die("YouTube not connected: run  publish.py auth-youtube")
    rt = json.loads(p.read_text())["refresh_token"]
    st, _, body = call("POST", "https://oauth2.googleapis.com/token", form({
        "client_id": c["client_id"], "client_secret": c["client_secret"], "refresh_token": rt, "grant_type": "refresh_token",
    }), {"Content-Type": "application/x-www-form-urlencoded"})
    tok = json.loads(body)
    if st != 200:
        die(f"YouTube token refresh failed ({st}): {tok.get('error_description') or tok.get('error')} — run auth-youtube again")
    return tok["access_token"]


def yt_channel(at: str) -> str:
    st, _, body = call("GET", "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true", headers={"Authorization": f"Bearer {at}"})
    if st != 200:
        return f"(could not read channel: {st}; the upload scope cannot list channels, that is fine)"
    items = json.loads(body).get("items", [])
    return items[0]["snippet"]["title"] if items else "(no channel)"


def yt_upload(path: Path, meta: dict, public: bool, publish_at: str | None = None) -> str:
    at = yt_access_token()
    size = path.stat().st_size
    body = {
        "snippet": {"title": meta["title"], "description": meta["description"], "tags": meta["tags"], "categoryId": "27"},  # Education
        "status": {"privacyStatus": "public" if public and not publish_at else "private", "selfDeclaredMadeForKids": False},
    }
    if publish_at:  # YouTube scheduled publishing: stays private until this time, then goes public
        body["status"]["publishAt"] = publish_at
    st, hd, resp = call(
        "POST",
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        json.dumps(body).encode(),
        {"Authorization": f"Bearer {at}", "Content-Type": "application/json; charset=UTF-8",
         "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)},
    )
    loc = hd.get("Location") or hd.get("location")
    if st != 200 or not loc:
        die(f"YouTube upload init failed ({st}): {resp[:400].decode(errors='replace')}")
    st, _, resp = call("PUT", loc, path.read_bytes(), {"Content-Type": "video/mp4", "Content-Length": str(size)})
    if st not in (200, 201):
        die(f"YouTube upload failed ({st}): {resp[:400].decode(errors='replace')}")
    vid = json.loads(resp)
    return f"https://youtube.com/shorts/{vid['id']}  (status: {vid.get('status', {}).get('privacyStatus')})"


# ----------------------------------------------------------------------------- Instagram (API with Instagram Login)
def ig_creds() -> dict:
    p = CFG / "instagram.json"
    if not p.exists():
        die(f"missing {p} with {{\"access_token\": ..., \"user_id\": ...}}")
    return json.loads(p.read_text())


def ig_refresh() -> None:
    """Long-lived tokens last 60 days; refresh extends them (call any time after 24 h)."""
    c = ig_creds()
    st, _, body = call("GET", "https://graph.instagram.com/refresh_access_token?" + urllib.parse.urlencode(
        {"grant_type": "ig_refresh_token", "access_token": c["access_token"]}))
    if st == 200:
        c["access_token"] = json.loads(body)["access_token"]
        c["refreshed_at"] = int(time.time())
        save_secret(CFG / "instagram.json", c)


def ig_me() -> str:
    c = ig_creds()
    st, _, body = call("GET", f"https://graph.instagram.com/{IG_API}/me?" + urllib.parse.urlencode(
        {"fields": "user_id,username,account_type", "access_token": c["access_token"]}))
    d = json.loads(body)
    if st != 200:
        die(f"Instagram token check failed ({st}): {d.get('error', {}).get('message')}")
    if str(d.get("user_id")) != str(c["user_id"]):
        print(f"note: token belongs to user_id {d.get('user_id')}; updating instagram.json")
        c["user_id"] = str(d.get("user_id"))
        save_secret(CFG / "instagram.json", c)
    return f"@{d.get('username')} ({d.get('account_type')})"


def ig_upload(path: Path, meta: dict) -> str:
    c = ig_creds()
    tok, uid = c["access_token"], c["user_id"]
    base = f"https://graph.instagram.com/{IG_API}"
    st, _, body = call("POST", f"{base}/{uid}/media", form({
        "media_type": "REELS", "upload_type": "resumable", "caption": meta["caption"], "share_to_feed": "true", "access_token": tok,
    }))
    d = json.loads(body)
    if st != 200 or "id" not in d:
        die(f"Instagram container failed ({st}): {d.get('error', {}).get('message')}")
    cid = d["id"]
    size = path.stat().st_size
    st, _, body = call("POST", f"https://rupload.facebook.com/ig-api-upload/{IG_API}/{cid}", path.read_bytes(),
                       {"Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(size)})
    if st != 200:
        die(f"Instagram video upload failed ({st}): {body[:400].decode(errors='replace')}")
    for _ in range(60):  # processing, up to ~10 min
        st, _, body = call("GET", f"{base}/{cid}?" + urllib.parse.urlencode({"fields": "status_code,status", "access_token": tok}))
        s = json.loads(body)
        if s.get("status_code") == "FINISHED":
            break
        if s.get("status_code") in ("ERROR", "EXPIRED"):
            die(f"Instagram processing failed: {s.get('status')}")
        time.sleep(10)
    else:
        die("Instagram processing timed out")
    st, _, body = call("POST", f"{base}/{uid}/media_publish", form({"creation_id": cid, "access_token": tok}))
    d = json.loads(body)
    if st != 200:
        die(f"Instagram publish failed ({st}): {d.get('error', {}).get('message')}")
    st, _, body = call("GET", f"{base}/{d['id']}?" + urllib.parse.urlencode({"fields": "permalink", "access_token": tok}))
    return json.loads(body).get("permalink", f"media id {d['id']}")


# ----------------------------------------------------------------------------- CLI
def main() -> None:
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    cmd = a[0]
    if cmd == "auth-youtube":
        auth_youtube()
    elif cmd == "auth-youtube-finish":
        q = urllib.parse.parse_qs(urllib.parse.urlparse(a[1]).query)
        yt_finish({k: v[0] for k, v in q.items()})
    elif cmd == "check":
        if (CFG / "youtube_token.json").exists():
            print("YouTube  :", yt_channel(yt_access_token()))
        else:
            print("YouTube  : not connected")
        if (CFG / "instagram.json").exists():
            ig_refresh()
            print("Instagram:", ig_me())
        else:
            print("Instagram: not connected")
    elif cmd == "upload":
        pos = [x for x in a[1:] if not x.startswith("--")]
        if len(pos) != 2:
            die("usage: upload <slug> <file.mp4> [--youtube] [--instagram] [--public] [--publish-at=ISO8601] --confirm")
        slug, f = pos
        path = Path(f).resolve()
        if not path.exists() or path.stat().st_size == 0:
            die(f"{path} missing or empty")
        if "_preview" in path.name:
            die("refusing to upload a preview render")
        targets = [t for t in ("youtube", "instagram") if f"--{t}" in a]
        if not targets:
            die("choose --youtube and/or --instagram")
        meta = metadata(slug)
        print(f"title: {meta['title']}\nfile : {path} ({path.stat().st_size / 1e6:.1f} MB)\nto   : {', '.join(targets)}")
        if "--confirm" not in a:
            print("\n(dry run) add --confirm to upload")
            return
        done = {(e["file"], e["platform"]) for e in log_load()}
        for t in targets:
            if (str(path), t) in done:
                print(f"{t}: already uploaded, skipped")
                continue
            if t == "youtube":
                at = next((x.split("=", 1)[1] for x in a if x.startswith("--publish-at=")), None)
                url = yt_upload(path, meta, "--public" in a, at)
            else:
                ig_refresh()
                url = ig_upload(path, meta)
            log_add({"slug": slug, "file": str(path), "platform": t, "url": url, "at": time.strftime("%Y-%m-%d %H:%M")})
            print(f"{t}: {url}")
    else:
        die(f"unknown command {cmd}")


if __name__ == "__main__":
    main()
