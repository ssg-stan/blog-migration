"""Bounded public-URL checks with DNS pinning and redirect revalidation."""
import asyncio
import http.client
import ipaddress
import socket
import ssl
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit
import certifi
from app.services.preview import safe_url
from app.services.validation import fingerprint, url_targets


class BlockedURL(ValueError):
    pass


def public_address(url):
    target = safe_url(url)
    if not target:
        raise BlockedURL("Only credential-free HTTP(S) URLs can be checked.")
    parsed = urlsplit(target)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in {80, 443}:
        raise BlockedURL("Only standard public web ports are checked.")
    host = parsed.hostname
    if not host or host.lower() == "localhost" or host.lower().endswith((".localhost", ".local", ".internal")):
        raise BlockedURL("Local or internal addresses are not checked.")
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ips = list(dict.fromkeys(item[4][0] for item in addresses))
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise BlockedURL("Private, loopback, and reserved addresses are not checked.")
    return parsed, port, ips[0]


def public_request(url, method="HEAD"):
    parsed, port, ip = public_address(url)
    if parsed.scheme == "https":
        connection = http.client.HTTPSConnection(parsed.hostname, port, timeout=6,
            context=ssl.create_default_context(cafile=certifi.where()))
    else:
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=6)
    # Connect only to the validated IP. HTTPS still uses the original host for SNI and certificate verification.
    connection._create_connection = lambda address, timeout=6, source_address=None: socket.create_connection((ip, port), timeout, source_address)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    try:
        connection.request(method, path, headers={"User-Agent": "BlogMigrationManager/0.2 URL-check", "Range": "bytes=0-0"})
        response = connection.getresponse()
        return response.status, response.getheader("Location"), response.getheader("Content-Type", "")
    finally:
        connection.close()


def check_url(target, request_fn=public_request):
    original = target["url"]
    current = original
    try:
        for _ in range(4):
            status, redirect, content_type = request_fn(current)
            if status in {405, 501}:
                status, redirect, content_type = request_fn(current, "GET")
            if status in {301, 302, 303, 307, 308} and redirect:
                current = urljoin(current, redirect)
                continue
            if status in {404, 410}:
                state, message = "broken", f"Broken {target['kind']}: HTTP {status}."
            elif 200 <= status < 300:
                if target["kind"] == "image" and content_type and not content_type.lower().startswith("image/"):
                    state, message = "unverified", "Image URL responded, but the server did not identify it as an image."
                else:
                    state, message = "ok", f"{target['kind'].title()} responded successfully (HTTP {status})."
            else:
                state, message = "unverified", f"Could not verify {target['kind']}: HTTP {status}. Review manually."
            return {**target, "state": state, "message": message}
        return {**target, "state": "unverified", "message": "Too many redirects; review this URL manually."}
    except (BlockedURL, ValueError):
        return {**target, "state": "blocked", "message": "URL was not checked because it is unsafe, private, or uses an unsupported port."}
    except (OSError, http.client.HTTPException):
        return {**target, "state": "unverified", "message": "Connection or certificate verification failed; review this URL manually."}


class URLCheckManager:
    def __init__(self, db, checker=check_url):
        self.db = db
        self.checker = checker
        self.tasks = {}
        self.states = {}
        self.semaphore = asyncio.Semaphore(4)

    def start(self, post):
        post_id = post["id"]
        task = self.tasks.get(post_id)
        if task and not task.done():
            return False
        targets = url_targets(post)
        self.states[post_id] = {"status": "RUNNING", "revision": post["revision"], "total": min(len(targets), 50), "completed": 0, "message": "Checking public image and internal-link URLs…"}
        self.tasks[post_id] = asyncio.create_task(self._run(post, targets))
        return True

    async def _one(self, post_id, target):
        async with self.semaphore:
            result = await asyncio.to_thread(self.checker, target)
            self.states[post_id]["completed"] += 1
            return result

    async def _run(self, post, targets):
        post_id = post["id"]
        try:
            results = await asyncio.gather(*(self._one(post_id, target) for target in targets[:50]))
            report = {"fingerprint": fingerprint(post), "checked_at": datetime.now(timezone.utc).isoformat(),
                      "results": results, "limited": len(targets) > 50}
            saved = self.db.save_link_report(post_id, post["revision"], report)
            self.states[post_id].update(status="SUCCESS" if saved else "STALE", message="URL checks saved." if saved else "Post changed during URL checks. Run the check again for the latest content.")
        except asyncio.CancelledError:
            self.states[post_id].update(status="ERROR", message="URL check interrupted. Run it again.")
            raise
        except Exception:
            self.states[post_id].update(status="ERROR", message="URL check could not complete. Try again later.")

    def status(self, post_id):
        return self.states.get(post_id, {"status": "IDLE"})

    async def close(self):
        tasks = [task for task in self.tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
