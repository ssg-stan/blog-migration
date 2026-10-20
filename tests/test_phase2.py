import asyncio
import json
import socket
import sqlite3
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.database import Database, now
from app.main import create_app
from app.services.validation import EDIT_FIELDS, fingerprint, validate_post, url_targets
from app.services.sync import normalize_post
from app.services.link_checker import BlockedURL, public_address, public_request, check_url, URLCheckManager
from app.services.transformer import clean_html


def source(post_id="one", **updates):
    return {"id": post_id, "name": "Original title", "slug": "original-title", "postBody": "<h2>Original article</h2><p>A useful post.</p>",
            "authorName": "Publisher", "blogAuthorId": "9", "tagIds": [3], "publishDate": "2026-01-01T12:00:00Z",
            "metaDescription": "A clear SEO description", "contentGroupId": "blog-1", "state": "PUBLISHED", **updates}


def save_source(db, items):
    run_id = db.start_sync()
    db.save_snapshot([normalize_post(item, {"9": "Blog author"}, {"3": "Storage"}, now()) for item in items], run_id)


@pytest.fixture
def db(tmp_path):
    db = Database(tmp_path / "local.db")
    db.initialize()
    save_source(db, [source()])
    return db


def fields(post, **updates):
    return {**{key: post.get(key) for key in EDIT_FIELDS}, **updates}


def test_migration_backs_up_existing_library_without_losing_posts(tmp_path):
    db = Database(tmp_path / "legacy.db")
    db.initialize()
    save_source(db, [source()])
    # Produce the original Phase 1 posts table while preserving its data.
    extra = {'local_edits','revision','validation_json','validation_status','link_report','review_notes','reviewed_at','approved_at','source_changed'}
    with db.connect() as conn:
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(posts)") if row["name"] not in extra]
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP TABLE review_events")
        conn.execute("DROP TABLE post_revisions")
        conn.execute("ALTER TABLE posts RENAME TO modern_posts")
        conn.execute("CREATE TABLE posts AS SELECT " + ",".join(columns) + " FROM modern_posts")
        conn.execute("DROP TABLE modern_posts")
    db.initialize()
    assert db.get_post(1)["title"] == "Original title"
    assert db.get_post(1)["revision"] == 1
    assert db.get_post(1)["validation"]["total"] == 8
    backups = list(tmp_path.glob("legacy.phase1-backup-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1
    db.initialize()
    assert len(list(tmp_path.glob("legacy.phase1-backup-*.db"))) == 1


def test_editor_preserves_source_and_survives_resync(db):
    post = db.get_post(1)
    edited = db.edit_post(1, post["revision"], fields(post, title="Local title", content="<p>Local content</p>", categories=["Local category"]))
    assert edited["title"] == "Local title"
    assert edited["source"]["title"] == "Original title"
    assert json.loads(edited["raw_json"])["name"] == "Original title"
    db.review_post(1, edited["revision"], "APPROVED", True)
    save_source(db, [source(name="Upstream title", postBody="<p>Changed upstream</p>")])
    refreshed = db.get_post(1)
    assert refreshed["title"] == "Local title"
    assert refreshed["content"] == "<p>Local content</p>"
    assert refreshed["categories"] == ["Local category"]
    assert refreshed["source"]["title"] == "Upstream title"
    assert refreshed["migration_status"] == "PENDING"
    assert refreshed["source_changed"] == 1
    assert any(issue["code"] == "source_changed" for issue in refreshed["validation"]["issues"])


def test_edit_and_source_changes_clear_approval_but_unchanged_sync_does_not(db):
    approved = db.review_post(1, 1, "APPROVED", True)
    assert approved["approved_at"]
    save_source(db, [source()])
    assert db.get_post(1)["migration_status"] == "APPROVED"
    edited = db.edit_post(1, approved["revision"], fields(approved, meta_description="Local metadata"))
    assert edited["migration_status"] == "PENDING" and edited["approved_at"] is None
    db.review_post(1, edited["revision"], "APPROVED", True)
    save_source(db, [source(name="Changed source title")])
    assert db.get_post(1)["migration_status"] == "PENDING"


def test_stale_save_and_review_are_rejected_without_overwriting(db):
    old = db.get_post(1)
    db.edit_post(1, 1, fields(old, title="New title"))
    with pytest.raises(ValueError, match="another tab"):
        db.edit_post(1, 1, fields(old, title="Stale title"))
    with pytest.raises(ValueError, match="another tab"):
        db.review_post(1, 1, "REJECTED")
    assert db.get_post(1)["title"] == "New title"


def test_approval_requires_no_errors_and_acknowledged_warnings(db):
    with pytest.raises(ValueError, match="Acknowledge"):
        db.review_post(1, 1, "APPROVED")
    post = db.edit_post(1, 1, fields(db.get_post(1), title=""))
    with pytest.raises(ValueError, match="Resolve validation errors"):
        db.review_post(1, post["revision"], "APPROVED", True)
    rejected = db.review_post(1, post["revision"], "REJECTED", notes="Missing title")
    assert rejected["migration_status"] == "REJECTED"
    assert db.review_history(1)[0]["notes"] == "Missing title"


def test_duplicate_slugs_block_approval_and_recheck_other_posts(db):
    save_source(db, [source("two", slug="original-title", name="Second post")])
    assert db.get_post(1)["validation"]["errors"] == 1
    other = db.get_post(2)
    assert other["validation"]["errors"] == 1
    db.edit_post(2, other["revision"], fields(other, slug="unique-slug"))
    assert db.get_post(1)["validation"]["errors"] == 0
    assert db.get_post(2)["validation"]["errors"] == 0


def test_search_uses_effective_local_edits_and_review_filters(db):
    edited = db.edit_post(1, 1, fields(db.get_post(1), title="Migration-ready copy", author="Local writer"))
    db.review_post(1, edited["revision"], "APPROVED", True)
    assert db.list_posts(query="Migration-ready")[1] == 1
    assert db.list_posts(query="Local writer")[1] == 1
    assert db.list_posts(review="APPROVED")[1] == 1
    assert db.list_posts(review="REJECTED")[1] == 0
    assert db.review_stats()["approved"] == 1


def test_reset_restores_latest_source_and_saves_previous_revision(db):
    post = db.edit_post(1, 1, fields(db.get_post(1), title="Local title"))
    reset = db.reset_edits(1, post["revision"])
    assert reset["title"] == "Original title"
    assert reset["local_edits"] == {}
    assert reset["migration_status"] == "PENDING"
    with db.connect() as conn:
        snapshots = conn.execute("SELECT snapshot_json FROM post_revisions ORDER BY id DESC").fetchall()
    assert json.loads(snapshots[0][0])["title"] == "Local title"


def test_active_html_ctas_forms_and_invalid_images_are_detected(db):
    content = '<div class="hs_cos_wrapper"><script>bad()</script><p onclick="bad()">Hello</p><form>Form</form><a class="hs-cta-wrapper" href="javascript:bad()">CTA</a><img src=""></div>'
    post = db.edit_post(1, 1, fields(db.get_post(1), content=content))
    codes = {issue["code"] for issue in post["validation"]["issues"]}
    assert {'active_html','hubspot_form','hubspot_classes','hubspot_cta','invalid_image','invalid_link'} <= codes
    assert post["validation"]["status"] == "ERROR"
    cleaned = clean_html(content)
    assert "script" not in cleaned and "form" not in cleaned and "onclick" not in cleaned and "javascript:" not in cleaned
    assert "Hello" in cleaned and "CTA" in cleaned


def test_link_report_is_discarded_if_content_changes(db):
    post = db.get_post(1)
    report = {"fingerprint": fingerprint(post), "results": [], "checked_at": now()}
    edited = db.edit_post(1, 1, fields(post, content="<p>New content</p>"))
    assert not db.save_link_report(1, post["revision"], report)
    assert edited["link_report"] == {}


def test_broken_url_check_clears_approval(db):
    post = db.get_post(1)
    post = db.edit_post(1, 1, fields(post, featured_image="https://example.com/image.jpg"))
    post = db.review_post(1, post["revision"], "APPROVED", True)
    report = {"fingerprint": fingerprint(post), "checked_at": now(), "results": [{"url": "https://example.com/image.jpg", "state": "broken", "message": "Broken image: HTTP 404."}]}
    assert db.save_link_report(1, post["revision"], report)
    current = db.get_post(1)
    assert current["migration_status"] == "PENDING"
    assert current["validation"]["errors"] == 1
    assert current["revision"] == post["revision"] + 1


@pytest.mark.parametrize('url',["http://localhost/","http://127.0.0.1/","http://10.0.0.1/","http://169.254.169.254/","http://[::1]/","http://example.com:8080/","http://user:password@example.com/"])
def test_url_guard_blocks_private_reserved_credentials_and_ports(monkeypatch, url):
    def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    with pytest.raises(BlockedURL):
        public_address(url)


def test_dns_guard_rejects_mixed_public_private_answers(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443)),(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))])
    with pytest.raises(BlockedURL):
        public_address('https://example.com/')


def test_redirects_are_checked_and_head_can_fallback_to_get():
    calls=[]
    def request(url, method='HEAD'):
        calls.append((url,method))
        if url.endswith('/start'):
            return 302, '/final', ''
        if method == 'HEAD':
            return 405,None,''
        return 200,None,'image/jpeg'
    result=check_url({'url':'https://example.com/start','kind':'image'}, request)
    assert result['state']=='ok'
    assert calls == [('https://example.com/start','HEAD'),('https://example.com/final','HEAD'),('https://example.com/final','GET')]
    def blocked_redirect(url, method='HEAD'):
        if url == 'https://example.com/':
            return 302, 'http://127.0.0.1/private', ''
        raise BlockedURL()
    assert check_url({'url':'https://example.com/','kind':'image'}, blocked_redirect)['state']=='blocked'


def test_dns_pinning_keeps_original_tls_host(monkeypatch):
    monkeypatch.setattr('app.services.link_checker.public_address', lambda url: (__import__('urllib.parse',fromlist=['urlsplit']).urlsplit(url),443,'93.184.216.34'))
    connections=[]
    class Connection:
        def __init__(self, host, port, **kwargs):
            connections.append(self);self.host=host
        def request(self, *args, **kwargs):
            self._create_connection((self.host,443), 6)
        def getresponse(self):
            class Response:
                status=200
                def getheader(self, key, default=None):
                    return 'image/jpeg' if key=='Content-Type' else default
            return Response()
        def close(self):
            pass
    destinations=[]
    monkeypatch.setattr('app.services.link_checker.http.client.HTTPSConnection',Connection)
    monkeypatch.setattr(socket,'create_connection',lambda address,*args: destinations.append(address))
    assert public_request('https://example.com/image.jpg')[0]==200
    assert connections[0].host=='example.com'
    assert destinations==[('93.184.216.34',443)]


def test_url_check_manager_saves_mock_results_and_limits_work(db):
    post=db.edit_post(1,1,fields(db.get_post(1), featured_image='https://example.com/image.jpg'))
    def checker(target):
        return {**target,'state':'ok','message':'Responded'}
    async def run():
        manager=URLCheckManager(db,checker)
        assert manager.start(post)
        assert not manager.start(post)
        await manager.tasks[1]
        assert manager.status(1)['status']=='SUCCESS'
    asyncio.run(run())
    assert db.get_post(1)['link_report']['results'][0]['state']=='ok'


def test_phase2_http_edit_review_original_preview_and_security(tmp_path):
    app=create_app(Settings(tmp_path/'api.db'))
    with TestClient(app,base_url='http://localhost') as client:
        save_source(app.state.db,[source()])
        post=app.state.db.get_post(1)
        payload={**fields(post,title='Locally edited',categories=['Museum']), 'revision':1,'review_notes':'Looks good'}
        assert client.put('/api/posts/1',json=payload).status_code==403
        headers={'X-Migration-Request':'1'}
        assert client.put('/api/posts/1',json=payload,headers={**headers,'Origin':'https://evil.test'}).status_code==403
        response=client.put('/api/posts/1',json=payload,headers=headers)
        assert response.status_code==200
        revision=response.json()['revision']
        assert client.put('/api/posts/1',json=payload,headers=headers).status_code==409
        assert 'Locally edited' in client.get('/posts/1/preview').text
        assert 'Original title' in client.get('/posts/1/preview?version=source').text
        assert client.get('/posts/1/edit').status_code==200
        assert 'Content validation' in client.get('/posts/1').text
        approved=client.post('/api/posts/1/review',json={'revision':revision,'action':'APPROVED','acknowledge_warnings':True},headers=headers)
        assert approved.status_code==200
        assert app.state.db.get_post(1)['migration_status']=='APPROVED'
        assert 'Locally edited' in client.get('/posts?review=APPROVED').text
        assert client.post('/api/posts/1/review',json={'revision':revision,'action':'PUBLISHED'},headers=headers).status_code==422
        assert client.get('/posts/999/edit').status_code==404
        assert client.get('/health').json()['phase']==3


def test_unsupported_publish_date_and_malformed_input_are_rejected_on_approval(db):
    post=db.edit_post(1,1,fields(db.get_post(1),publish_date='not-a-date'))
    assert post['validation']['errors']==1
    with pytest.raises(ValueError,match='Resolve'):
        db.review_post(1,post['revision'],'APPROVED',True)


def test_validation_detects_missing_anchors_and_only_checks_internal_links(db):
    post=fields(db.get_post(1),content='<p id="intro">Text</p><a href="#missing">Missing</a><a href="/next">Next</a><a href="https://outside.example.com">External</a><img src="/photo.jpg" alt="Photo">')
    post['source_url']='https://example.com/blog/article'
    assert any(issue['code']=='missing_anchor' for issue in validate_post(post)['issues'])
    urls=url_targets(post)
    assert {target['url'] for target in urls}=={'https://example.com/next','https://example.com/photo.jpg'}


def test_imported_posts_are_protected(db):
    with db.connect() as conn:
        conn.execute("UPDATE posts SET highlevel_id='destination-id', migration_status='IMPORTED' WHERE id=1")
    with pytest.raises(ValueError,match='Imported'):
        db.edit_post(1,1,fields(db.get_post(1),title='Change'))


def test_url_checks_stop_at_fifty_and_report_limit(db):
    html='<p>Many images</p>'+''.join(f'<img src="https://example.com/{i}.jpg" alt="Image">' for i in range(55))
    post=db.edit_post(1,1,fields(db.get_post(1),content=html))
    calls=[]
    def checker(target):
        calls.append(target['url'])
        return {**target,'state':'ok','message':'Responded'}
    async def run():
        manager=URLCheckManager(db,checker)
        manager.start(post)
        await manager.tasks[1]
    asyncio.run(run())
    assert len(calls)==50
    assert db.get_post(1)['link_report']['limited'] is True
    assert any(issue['code']=='url_limit' for issue in db.get_post(1)['validation']['issues'])


def test_ready_post_can_be_approved_without_warning_acknowledgement(db):
    raw=source(url='https://example.com/blog/article',featuredImage='https://example.com/image.jpg',featuredImageAltText='A useful image')
    save_source(db,[raw])
    post=db.get_post(1)
    assert any(issue['code']=='urls_unchecked' for issue in post['validation']['issues'])
    report={'fingerprint':fingerprint(post),'checked_at':now(),'results':[{'url':post['featured_image'],'state':'ok','message':'Image responded'}]}
    assert db.save_link_report(1,post['revision'],report)
    checked=db.get_post(1)
    assert checked['validation']['status']=='READY'
    assert db.review_post(1,checked['revision'],'APPROVED')['migration_status']=='APPROVED'


def test_reapproval_acknowledges_upstream_change(db):
    post=db.edit_post(1,1,fields(db.get_post(1),title='Local'))
    save_source(db,[source(name='Upstream changed')])
    post=db.get_post(1)
    assert post['source_changed']==1
    approved=db.review_post(1,post['revision'],'APPROVED',True)
    assert approved['source_changed']==0
    assert not any(issue['code']=='source_changed' for issue in approved['validation']['issues'])


def test_inaccessible_urls_are_not_mislabeled_broken():
    target={'url':'https://example.com/image.jpg','kind':'image'}
    assert check_url(target,lambda *args:(403,None,''))['state']=='unverified'
    assert check_url(target,lambda *args:(404,None,''))['state']=='broken'
