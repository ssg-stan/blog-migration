import asyncio
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.database import Database
from app.main import create_app
from app.models import ImportRequest
from app.services.highlevel import HighLevelClient, SimulatedHighLevelClient, DestinationError
from app.services.importer import ImportManager, draft_payload, default_mappings
from test_phase2 import source, save_source, fields


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "imports.db")
    database.initialize()
    save_source(database, [source()])
    database.review_post(1, 1, "APPROVED", True)
    return database


def settings(db):
    return Settings(db.path, demo=True)


def request(db, **updates):
    return ImportRequest(revision=db.get_post(1)["revision"], author_id="demo-editor", **updates)


def run(db, factory=None, payload=None):
    return asyncio.run(ImportManager(db, settings(db), factory).run(1, payload or request(db)))


def test_simulated_import_saves_id_payload_and_locks_copy(db):
    result = run(db)
    assert result["migration_status"] == "IMPORTED"
    assert result["import_state"] == "SUCCESS"
    assert result["highlevel_id"].startswith("demo-draft-")
    attempt = db.latest_import(1)
    assert attempt["simulated"] == 1 and attempt["status"] == "SUCCESS"
    assert attempt["payload"]["status"] == "DRAFT"
    assert attempt["payload"]["author"] == "demo-editor"
    assert attempt["payload"]["description"] == db.get_post(1)["meta_description"]
    assert "publishedAt" not in attempt["payload"] and "canonicalLink" not in attempt["payload"]
    assert result["destination_blog_id"] == "demo-blog"
    assert result["imported_at"]
    with pytest.raises(ValueError, match="Imported"):
        run(db)
    with pytest.raises(ValueError, match="Imported"):
        db.review_post(1, result["revision"], "PENDING")
    assert db.review_stats()["imported"] == 1


def test_only_current_approved_valid_copy_can_be_imported(db):
    with pytest.raises(ValueError, match="another tab"):
        run(db, payload=ImportRequest(revision=1, author_id="demo-editor"))
    post = db.review_post(1, db.get_post(1)["revision"], "REJECTED")
    with pytest.raises(ValueError, match="Only an approved"):
        run(db)
    db.review_post(1, post["revision"], "APPROVED", True)
    db.edit_post(1, db.get_post(1)["revision"], fields(db.get_post(1), title=""))
    with pytest.raises(ValueError, match="Only an approved"):
        run(db)
    assert db.latest_import(1) is None


def test_mappings_use_destination_ids_and_unknown_values_fail_before_create(db):
    class Client(SimulatedHighLevelClient):
        async def create_draft(self, payload):
            pytest.fail("Invalid mappings must never create a draft")
    result = run(db, Client, ImportRequest(revision=2, author_id="foreign-author"))
    assert result["import_state"] == "ERROR"
    assert "Select a destination author" in result["import_message"]
    result = run(db, Client, request(db, category_ids=["foreign-category"]))
    assert result["import_state"] == "ERROR"
    assert "Select destination categories" in result["import_message"]


def test_category_mappings_required_and_duplicate_ids_removed(db):
    post=db.get_post(1)
    edited=db.edit_post(1,post["revision"],fields(post,categories=["Museum"],author="Sarah Williams"))
    db.review_post(1,edited["revision"],"APPROVED",True)
    result=run(db)
    assert result["import_state"] == "ERROR" and "Map" in result["import_message"]
    result=run(db,payload=request(db, category_ids=["demo-museum","demo-museum"]))
    assert result["import_state"] == "SUCCESS"
    assert db.latest_import(1)["payload"]["categories"] == ["demo-museum"]


def test_duplicate_slug_at_destination_never_posts(db):
    class Client(SimulatedHighLevelClient):
        async def slug_exists(self, slug): return True
        async def create_draft(self, payload): pytest.fail("Existing slug must not be posted")
    result=run(db,Client)
    assert result["import_state"] == "ERROR"
    assert "already exists" in result["import_message"]
    assert db.latest_import(1)["payload"] == {}
    assert result["migration_status"] == "APPROVED"


def test_known_create_failure_can_be_manually_retried(db):
    class Client(SimulatedHighLevelClient):
        async def create_draft(self,payload): raise DestinationError("Missing write scope.")
    result=run(db,Client)
    assert result["import_state"] == "ERROR"
    assert result["migration_status"] == "APPROVED"
    assert run(db)["import_state"] == "SUCCESS"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM import_attempts").fetchone()[0] == 2


def test_uncertain_creation_blocks_reimport_and_edits(db):
    class Client(SimulatedHighLevelClient):
        async def create_draft(self,payload): raise DestinationError("Timed out during creation.",uncertain=True)
    result=run(db,Client)
    assert result["import_state"] == "UNKNOWN"
    assert db.latest_import(1)["payload"]["status"] == "DRAFT"
    with pytest.raises(ValueError,match="uncertain"):
        run(db)
    with pytest.raises(ValueError,match="uncertain"):
        db.edit_post(1,result["revision"],fields(result,title="Another title"))


def test_unexpected_exception_during_create_is_uncertain(db):
    class Client(SimulatedHighLevelClient):
        async def create_draft(self,payload): raise RuntimeError("sensitive-provider-detail")
    result=run(db,Client)
    assert result["import_state"] == "UNKNOWN"
    assert "sensitive-provider-detail" not in result["import_message"]


def test_restart_marks_preparing_retryable_and_sending_uncertain(db):
    attempt,operation,post=db.start_import(1,2,"demo-location","demo-blog",True)
    db.initialize()
    assert db.get_post(1)["import_state"] == "ERROR"
    attempt,operation,post=db.start_import(1,db.get_post(1)["revision"],"demo-location","demo-blog",True)
    catalog=asyncio.run(SimulatedHighLevelClient(settings(db)).catalog())
    db.sending_import(attempt,draft_payload(post,catalog,settings(db),"demo-editor",[]))
    db.initialize()
    assert db.get_post(1)["import_state"] == "UNKNOWN"
    assert db.latest_import(1)["status"] == "UNKNOWN"
    with pytest.raises(ValueError,match="uncertain"):
        run(db)


def test_active_import_blocks_sync_edits_review_and_other_import(db):
    db.start_import(1,2,"demo-location","demo-blog",True)
    with pytest.raises(ValueError,match="being imported"):
        save_source(db,[source(name="Refreshed")])
    post=db.get_post(1)
    with pytest.raises(ValueError,match="active"):
        db.edit_post(1,post["revision"],fields(post,title="Changed"))
    with pytest.raises(ValueError,match="active"):
        db.review_post(1,post["revision"],"REJECTED")
    save_source(db,[source("two",slug="second")])
    other=db.get_post(2)
    db.review_post(2,other["revision"],"APPROVED",True)
    with pytest.raises(ValueError,match="Another post"):
        db.start_import(2,db.get_post(2)["revision"],"demo-location","demo-blog",True)


def test_payload_cleans_source_and_resolves_image_urls(db):
    post=db.get_post(1)
    post.update(content='<div class="hs_cos_wrapper"><h2>Article</h2><p>Body</p><img src="/image.jpg" alt="Image"></div>',
                featured_image="/cover.jpg",source_url="https://example.com/blog/article",tags=["Museum"])
    catalog=asyncio.run(SimulatedHighLevelClient(settings(db)).catalog())
    payload=draft_payload(post,catalog,settings(db),"demo-editor",[])
    assert "hs_cos_wrapper" not in payload["rawHTML"]
    assert 'src="https://example.com/image.jpg"' in payload["rawHTML"]
    assert payload["imageUrl"] == "https://example.com/cover.jpg"
    assert payload["tags"] == ["Museum"]
    assert payload["wordCount"] > 0
    assert post["source"]["content"] != payload["rawHTML"]


def test_default_mapping_only_unique_names_are_matched():
    post={"author":" Sarah Williams ","categories":["museum","Unknown"]}
    catalog={"authors":[{"id":"a","name":"Sarah Williams"}],"categories":[{"id":"c","name":"Museum"}]}
    assert default_mappings(post,catalog)==("a",["c"])
    catalog["authors"].append({"id":"duplicate","name":"Sarah Williams"})
    assert default_mappings(post,catalog)[0] == ""


def live_settings():
    return Settings(__import__('pathlib').Path('unused.db'),highlevel_token='secret-token',highlevel_location_id='location',highlevel_blog_id='blog')


def test_live_connector_uses_documented_host_version_and_draft_payload():
    calls=[]
    def handler(request):
        calls.append(request)
        assert request.url.host=='services.leadconnectorhq.com'
        assert request.headers['Authorization']=='Bearer secret-token'
        assert request.headers['Version']=='v3'
        if request.url.path=='/blogs/site/all': return httpx.Response(200,json={'data':[{'_id':'blog','name':'Blog'}]})
        if request.url.path=='/blogs/authors': return httpx.Response(200,json={'authors':[{'_id':'author','name':'Editor','locationId':'location'}],'count':1})
        if request.url.path=='/blogs/categories': return httpx.Response(200,json={'categories':[{'_id':'category','label':'Museum'}],'count':1})
        if request.url.path=='/blogs/posts/url-slug-exists': return httpx.Response(200,json={'exists':False})
        assert json.loads(request.content)['status']=='DRAFT'
        return httpx.Response(201,json={'data':{'_id':'destination','status':'DRAFT'}})
    async def check():
        async with HighLevelClient(live_settings(),httpx.MockTransport(handler)) as client:
            catalog=await client.catalog()
            assert catalog['categories'][0]['name']=='Museum'
            assert not await client.slug_exists('article')
            assert await client.create_draft({'status':'DRAFT'})=='destination'
    asyncio.run(check())
    assert len(calls)==5 and sum(item.method=='POST' for item in calls)==1


@pytest.mark.parametrize('status,uncertain',[(401,False),(403,False),(422,False),(429,False),(500,True),(502,True),(408,True),(302,True)])
def test_destination_http_failures_are_redacted_and_not_retried(status,uncertain):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(status,text='secret-token sensitive-body')
    async def check():
        async with HighLevelClient(live_settings(),httpx.MockTransport(handler)) as client:
            with pytest.raises(DestinationError) as caught:
                await client.create_draft({'status':'DRAFT'})
            assert caught.value.uncertain is uncertain
            assert 'secret-token' not in str(caught.value) and 'sensitive-body' not in str(caught.value)
    asyncio.run(check())
    assert len(calls)==1


@pytest.mark.parametrize('body',[{}, {'data':{'_id':'id','status':'PUBLISHED'}}, {'data':{'status':'DRAFT'}}])
def test_unconfirmed_destination_creation_is_uncertain(body):
    async def check():
        async with HighLevelClient(live_settings(),httpx.MockTransport(lambda request:httpx.Response(201,json=body))) as client:
            with pytest.raises(DestinationError) as caught:
                await client.create_draft({'status':'DRAFT'})
            assert caught.value.uncertain
    asyncio.run(check())


def test_network_timeout_is_uncertain_only_for_creation():
    def handler(request): raise httpx.ReadTimeout('secret-token',request=request)
    async def check():
        async with HighLevelClient(live_settings(),httpx.MockTransport(handler)) as client:
            for operation,uncertain in [(client.create_draft({'status':'DRAFT'}),True),(client.slug_exists('article'),False)]:
                with pytest.raises(DestinationError) as caught: await operation
                assert caught.value.uncertain is uncertain
                assert 'secret-token' not in str(caught.value)
    asyncio.run(check())


def test_wrong_destination_blog_and_malformed_catalog_fail_safely():
    async def check():
        for body in [{'data':[{'_id':'another-blog'}]}, {'unexpected':[]}, {'data':[{'_id':'blog','locationId':'another-location'}]}]:
            async with HighLevelClient(live_settings(),httpx.MockTransport(lambda request:httpx.Response(200,json=body))) as client:
                with pytest.raises(DestinationError): await client.catalog()
    asyncio.run(check())


def test_destination_catalog_paginates_all_records():
    offsets=[]
    def handler(request):
        offset=int(request.url.params['offset']);offsets.append(offset)
        return httpx.Response(200,json={'authors':[{'_id':str(i),'name':'Author '+str(i)} for i in range(offset,min(offset+50,53))],'count':53})
    async def check():
        async with HighLevelClient(live_settings(),httpx.MockTransport(handler)) as client:
            assert len(await client._list('/blogs/authors','authors'))==53
    asyncio.run(check())
    assert offsets==[0,50]


def test_phase3_http_simulation_approval_security_and_imported_filter(tmp_path):
    app=create_app(Settings(tmp_path/'demo.db',demo=True))
    headers={'X-Migration-Request':'1'}
    with TestClient(app,base_url='http://localhost') as client:
        assert client.get('/health').json()['phase']==3
        assert client.get('/destination').status_code==200
        assert client.post('/api/destination/check',headers=headers,json={}).json()['simulated'] is True
        assert client.post('/api/posts/1/import',json={'revision':1}).status_code==403
        assert client.post('/api/posts/1/import',headers=headers,json={'revision':1,'author_id':'demo-sarah'}).status_code==409
        post=app.state.db.get_post(1)
        app.state.db.review_post(1,post['revision'],'APPROVED',True)
        post=app.state.db.get_post(1)
        assert 'Create simulated draft' in client.get('/posts/1/import').text
        response=client.post('/api/posts/1/import',headers=headers,json={'revision':post['revision'],'author_id':'demo-sarah','category_ids':['demo-museum']})
        assert response.status_code==200 and response.json()['status']=='SUCCESS'
        assert response.json()['simulated'] is True
        assert 'Simulated draft created' in client.get('/posts/1/import').text
        assert 'View import' in client.get('/posts/1').text
        assert 'A smarter approach' in client.get('/posts?review=IMPORTED').text
        assert client.get('/api/posts/1/import').json()['status']=='SUCCESS'
        assert client.post('/api/posts/1/import',headers=headers,json={'revision':app.state.db.get_post(1)['revision'],'author_id':'demo-sarah'}).status_code==409
        assert client.post('/api/posts/1/check-urls',headers=headers,json={'revision':app.state.db.get_post(1)['revision']}).status_code==409
        assert client.get('/posts/999/import').status_code==404


def test_missing_config_never_attempts_destination_connection(tmp_path):
    class Client:
        def __init__(self,*args): pytest.fail('Missing credentials must never connect')
    app=create_app(Settings(tmp_path/'db.db'),destination_factory=Client)
    headers={'X-Migration-Request':'1'}
    with TestClient(app,base_url='http://localhost') as client:
        save_source(app.state.db,[source()])
        app.state.db.review_post(1,1,'APPROVED',True)
        assert 'WORDPRESS_URL' in client.get('/posts/1/import').text
        assert client.post('/api/destination/check',headers=headers).status_code==502
        assert client.post('/api/posts/1/import',headers=headers,json={'revision':2,'author_id':'author'}).status_code==409


def test_phase2_library_is_backed_up_before_phase3_columns_are_added(db):
    # Reproduce the Phase 2 schema without losing its existing approval.
    with db.connect() as conn:
        conn.execute("DROP INDEX one_active_import")
        conn.execute("DROP TABLE import_attempts")
        for column in ['import_state','import_message','destination_location_id','destination_blog_id']:
            conn.execute("ALTER TABLE posts DROP COLUMN " + column)
    db.initialize()
    backups=list(db.path.parent.glob('imports.phase2-backup-*.db'))
    assert len(backups)==1
    assert db.get_post(1)['migration_status']=='APPROVED'
    assert db.get_post(1)['import_state']=='IDLE'
    db.initialize()
    assert len(list(db.path.parent.glob('imports.phase2-backup-*.db')))==1


def test_duplicate_local_destination_slug_is_blocked_without_second_create(db):
    run(db)
    # Importing a different source ID with a slug already used by a successful
    # local attempt must be blocked even if the remote slug check is delayed.
    save_source(db,[source('two',slug='unique-at-review')])
    second=db.get_post(2)
    db.review_post(2,second['revision'],'APPROVED',True)
    attempt,operation,post=db.start_import(2,db.get_post(2)['revision'],'demo-location','demo-blog',True)
    catalog=asyncio.run(SimulatedHighLevelClient(settings(db)).catalog())
    payload=draft_payload(post,catalog,settings(db),'demo-editor',[])
    payload['urlSlug']='original-title'
    with pytest.raises(ValueError,match='destination slug'):
        db.sending_import(attempt,payload)
    assert db.latest_import(2)['status']=='PREPARING'


def test_cancelled_create_records_uncertain_result(db):
    class Client(SimulatedHighLevelClient):
        async def create_draft(self,payload): raise asyncio.CancelledError()
    async def check():
        with pytest.raises(asyncio.CancelledError):
            await ImportManager(db,settings(db),Client).run(1,request(db))
    asyncio.run(check())
    assert db.get_post(1)['import_state']=='UNKNOWN'
    assert db.latest_import(1)['status']=='UNKNOWN'


def test_concurrent_duplicate_click_reserves_only_one_import(db):
    class Client(SimulatedHighLevelClient):
        async def create_draft(self,payload):
            await asyncio.sleep(.05)
            return 'demo-draft-one'
    async def check():
        manager=ImportManager(db,settings(db),Client)
        payload=request(db)
        results=await asyncio.gather(manager.run(1,payload),manager.run(1,payload),return_exceptions=True)
        assert sum(isinstance(value,ValueError) for value in results)==1
        assert sum(isinstance(value,dict) and value['import_state']=='SUCCESS' for value in results)==1
    asyncio.run(check())
    with db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM import_attempts').fetchone()[0]==1


def test_live_connector_refuses_non_draft_before_request():
    def handler(request): pytest.fail('Publication must never reach the API')
    async def check():
        async with HighLevelClient(live_settings(),httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError,match='only creates drafts'):
                await client.create_draft({'status':'PUBLISHED'})
    asyncio.run(check())


def test_import_audit_preserves_approved_copy_after_source_refresh(db):
    run(db)
    save_source(db,[source(name='Later HubSpot title',postBody='<p>Later source content</p>')])
    imported=db.get_post(1)
    assert imported['highlevel_id'] and imported['migration_status']=='IMPORTED'
    attempt=db.latest_import(1)
    assert attempt['approved_copy']['title']=='Original title'
    assert 'Original article' in attempt['approved_copy']['content']
    assert attempt['payload']['title']=='Original title'


def test_imported_editor_url_redirects_to_import_details(tmp_path):
    app=create_app(Settings(tmp_path/'demo.db',demo=True))
    with TestClient(app,base_url='http://localhost') as client:
        app.state.db.review_post(1,1,'APPROVED',True)
        asyncio.run(app.state.importer.run(1,ImportRequest(revision=2,author_id='demo-sarah')))
        response=client.get('/posts/1/edit',follow_redirects=False)
        assert response.status_code==303 and response.headers['location']=='/posts/1/import'
