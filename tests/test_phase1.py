import asyncio
import json
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.database import Database, now
from app.main import create_app
from app.services.hubspot import HubSpotClient, HubSpotError
from app.services.preview import preview_html, safe_url
from app.services.sync import SyncManager, date_value, normalize_post


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "migration.db")
    database.initialize()
    return database


def source(post_id="1", **overrides):
    return {"id": post_id, "name": "A useful article", "slug": "blog/useful-article", "postBody": "<h2>Good ideas</h2><p>Article body.</p>",
            "blogAuthorId": "9", "authorName": "Publisher, not author", "tagIds": [3], "contentGroupId": "blog-1",
            "state": "PUBLISHED", "publishDate": 1704067200000, "updatedAt": "2026-10-01T12:30:00Z", **overrides}


def save(db, items):
    run_id = db.start_sync()
    return db.save_snapshot([normalize_post(post, {"9": "Actual author"}, {"3": "Storage"}, now()) for post in items], run_id)


def test_normalization_uses_blog_author_and_preserves_source():
    original = source()
    post = normalize_post(original, {"9": "Actual author"}, {"3": "Storage"}, now())
    assert post["author"] == "Actual author"
    assert json.loads(post["tags"]) == ["Storage"]
    assert post["publish_date"] == "2024-01-01T00:00:00+00:00"
    assert post["source_updated_at"] == "2026-10-01T12:30:00+00:00"
    assert json.loads(post["raw_json"]) == original
    assert post["content"] == original["postBody"]
    assert normalize_post(original, {}, {}, now())["author"] == "Author #9"


def test_upsert_keeps_identity_and_review_state(db):
    assert save(db, [source()]) == (1, 0)
    with db.connect() as conn:
        conn.execute("UPDATE posts SET migration_status='APPROVED', highlevel_id='future-id' WHERE hubspot_id='1'")
    assert save(db, [source(name="Revised title")]) == (0, 1)
    assert db.stats()["total"] == 1
    post = db.get_post(1)
    assert post["title"] == "Revised title"
    assert post["migration_status"] == "APPROVED"
    assert post["highlevel_id"] == "future-id"


def test_search_filter_pagination_and_literal_wildcards(db):
    save(db, [source(str(i), name=f"Post {i}", state="DRAFT" if i % 2 else "PUBLISHED") for i in range(25)])
    save(db, [source("percent", name="100% useful")])
    assert db.list_posts(query="%")[1] == 1
    assert db.list_posts(query="Actual author")[1] == 26
    posts, total, page, pages = db.list_posts(page=2)
    assert (len(posts), total, page, pages) == (6, 26, 2, 2)
    assert db.list_posts(state="DRAFT")[1] == 12
    assert db.list_posts(query="no match")[1] == 0
    assert db.list_posts(page=999)[2] == 2


def test_preview_removes_active_content_and_resolves_urls():
    body = '<script>alert(1)</script><form>unsafe form</form><iframe src="https://evil.test"></iframe>' +            '<h2 onclick="alert(1)">Title</h2><img src="/image.jpg" onerror="alert(1)"><a href="javascript:alert(1)">Bad</a>' +            '<a href="../next">Next</a><table><tr><td>Data</td></tr></table>'
    clean = preview_html(body, "https://example.com/blog/article")
    assert "script" not in clean and "onclick" not in clean and "onerror" not in clean
    assert "form" not in clean and "iframe" not in clean and "javascript:" not in clean
    assert 'src="https://example.com/image.jpg"' in clean
    assert 'href="https://example.com/next"' in clean
    assert "<h2>Title</h2>" in clean and "<table>" in clean
    assert safe_url("javascript:alert(1)") == ""
    assert safe_url("https://user:secret@example.com") == ""


def test_paginated_client_only_uses_get_and_ignores_paging_link():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.host == "api.hubapi.com"
        assert request.headers["Authorization"] == "Bearer fake-token"
        if request.url.path.endswith("authors"):
            return httpx.Response(200, json={"results": [{"id": "9", "fullName": "Actual author"}]})
        if request.url.path.endswith("tags"):
            return httpx.Response(200, json={"results": [{"id": "3", "name": "Storage"}]})
        if "after" not in request.url.params:
            return httpx.Response(200, json={"results": [source()], "paging": {"next": {"after": "cursor-2", "link": "https://evil.test/steal"}}})
        assert request.url.params["after"] == "cursor-2"
        assert request.url.params["contentGroupId__eq"] == "blog-1"
        return httpx.Response(200, json={"results": [source("2"), source("other", contentGroupId="different-blog")]})
    async def fetch():
        async with HubSpotClient("fake-token", httpx.MockTransport(handler)) as client:
            return await client.fetch_posts("blog-1")
    posts, authors, tags = asyncio.run(fetch())
    assert [post["id"] for post in posts] == ["1", "2"]
    assert len(calls) == 4


@pytest.mark.parametrize("status, phrase", [(401, "access token"), (403, "content scope"), (400, "HTTP 400")])
def test_provider_errors_are_actionable_and_do_not_leak_secrets(status, phrase):
    async def fetch():
        transport = httpx.MockTransport(lambda request: httpx.Response(status, text="SECRET_TOKEN"))
        async with HubSpotClient("SECRET_TOKEN", transport) as client:
            await client.collection("/cms/v3/blogs/posts")
    with pytest.raises(HubSpotError) as caught:
        asyncio.run(fetch())
    assert phrase in str(caught.value)
    assert "SECRET_TOKEN" not in str(caught.value)


def test_repeated_pagination_cursor_stops():
    async def fetch():
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"results": [], "paging": {"next": {"after": "same"}}}))
        async with HubSpotClient("token", transport) as client:
            await client.collection("/cms/v3/blogs/posts")
    with pytest.raises(HubSpotError, match="pagination"):
        asyncio.run(fetch())


def test_rate_limit_retries(monkeypatch):
    attempts = []
    async def no_wait(delay):
        pass
    monkeypatch.setattr("app.services.hubspot.asyncio.sleep", no_wait)
    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"results": []})
    async def fetch():
        async with HubSpotClient("token", httpx.MockTransport(handler)) as client:
            return await client.collection("/cms/v3/blogs/posts")
    assert asyncio.run(fetch()) == []
    assert len(attempts) == 2


def test_failed_sync_keeps_existing_snapshot_and_prevents_overlap(db):
    save(db, [source()])
    class FailingClient:
        def __init__(self, token):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def fetch_posts(self, blog_id, progress):
            progress(100)
            raise HubSpotError("Page two failed")
    async def sync():
        manager = SyncManager(db, Settings(db.path, "token"), FailingClient)
        assert manager.start() is not None
        assert manager.start() is None
        await manager.task
    asyncio.run(sync())
    assert db.stats()["total"] == 1
    assert db.get_post(1)["title"] == "A useful article"
    assert db.latest_sync()["status"] == "ERROR"
    assert db.latest_sync()["fetched"] == 100


def test_restart_marks_abandoned_runs(db):
    db.start_sync()
    db.initialize()
    assert db.latest_sync()["status"] == "ERROR"
    assert "restart" in db.latest_sync()["message"]


def test_routes_empty_setup_preview_and_request_protection(tmp_path):
    app = create_app(Settings(tmp_path / "test.db"))
    with TestClient(app, base_url="http://localhost") as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Connect HubSpot to get started" in response.text
        assert "No syncs yet" in response.text
        assert client.post("/api/sync").status_code == 403
        headers = {"X-Migration-Request": "1"}
        assert client.post("/api/sync", headers=headers).status_code == 400
        assert client.post("/api/sync", headers={**headers, "Origin": "https://evil.test"}).status_code == 403
        assert client.get("/", headers={"Host": "evil.test"}).status_code == 400
        save(app.state.db, [source(name='<script>bad</script> & title')])
        review = client.get("/posts/1")
        assert review.status_code == 200
        assert "&lt;script&gt;bad&lt;/script&gt;" in review.text
        assert 'sandbox="allow-popups allow-popups-to-escape-sandbox"' in review.text
        preview = client.get("/posts/1/preview")
        assert preview.status_code == 200
        assert "<h2>Good ideas</h2>" in preview.text
        assert "default-src 'none'" in preview.headers["Content-Security-Policy"]
        assert "sandbox" in preview.headers["Content-Security-Policy"]
        assert client.get("/posts/999").status_code == 404
        assert client.get("/posts/999/preview").status_code == 404
        assert client.get("/health").json()["phase"] == 3
        assert client.get("/static/styles.css").status_code == 200


def test_sync_endpoint_completes_with_mocked_hubspot(tmp_path):
    class FakeClient:
        def __init__(self, token):
            assert token == "test-token"
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def fetch_posts(self, blog_id, progress):
            progress(1)
            return [source()], [{"id": "9", "fullName": "Actual author"}], [{"id": "3", "name": "Storage"}]
    app = create_app(Settings(tmp_path / "test.db", "test-token"), FakeClient)
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post("/api/sync", headers={"X-Migration-Request": "1"})
        assert response.status_code == 202
        # Barrier: wait on the actual background task rather than a timing-dependent sleep.
        async def wait_for_sync():
            await app.state.sync_manager.task
        client.portal.call(wait_for_sync)
        status = client.get("/api/sync/status").json()
        assert status["run"]["status"] == "SUCCESS"
        assert status["run"]["created"] == 1
        assert "Actual author" in client.get("/").text
        assert "test-token" not in client.get("/").text
        assert "test-token" not in client.get("/api/sync/status").text


def test_demo_uses_sample_library_and_disables_real_sync(tmp_path):
    app = create_app(Settings(tmp_path / "demo.db", demo=True))
    with TestClient(app, base_url="http://localhost") as client:
        assert app.state.db.stats()["total"] == 6
        assert "Demo workspace" in client.get("/").text
        assert client.post("/api/sync", headers={"X-Migration-Request": "1"}).status_code == 400
        assert client.get("/posts/1/preview").status_code == 200


def test_invalid_post_id_rolls_back_entire_sync(db):
    save(db, [source()])
    class InvalidClient:
        def __init__(self, token):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def fetch_posts(self, blog_id, progress):
            return [source(name="Should not save"), {"name": "No ID"}], [], []
    async def sync():
        manager = SyncManager(db, Settings(db.path, "token"), InvalidClient)
        manager.start()
        await manager.task
    asyncio.run(sync())
    assert db.get_post(1)["title"] == "A useful article"
    assert db.latest_sync()["status"] == "ERROR"


def test_date_parsing_and_source_states():
    assert date_value("2026-10-07T12:00:00Z") == "2026-10-07T12:00:00+00:00"
    assert date_value("not a date") is None
    assert normalize_post(source(state="SCHEDULED", currentlyPublished=False), {}, {}, now())["source_state"] == "SCHEDULED"
    assert normalize_post(source(state="PUBLISHED_OR_SCHEDULED", currentlyPublished=True), {}, {}, now())["source_state"] == "PUBLISHED"
