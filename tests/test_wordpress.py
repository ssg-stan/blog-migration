import asyncio
import json
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.services.wordpress import WordPressClient
from app.services.highlevel import DestinationError


def settings():
    return Settings(Path('unused.db'), wordpress_url='https://example.com/blog',
                    wordpress_username='editor', wordpress_password='secret')


def payload():
    return dict(status='DRAFT', title='Chosen post', urlSlug='chosen', rawHTML='<p>Body</p>',
                description='Excerpt', author='2', categories=['3'], tags=['Museum'])


def test_wordpress_connection_mapping_and_draft_request():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.headers['authorization'].startswith('Basic ')
        assert request.url.path.startswith('/blog/wp-json/wp/v2/')
        endpoint = request.url.path.split('/v2/')[1]
        if endpoint == 'users/me':
            return httpx.Response(200, json={'id': 2, 'name': 'Editor', 'capabilities': {'edit_posts': True}})
        if endpoint == 'categories':
            return httpx.Response(200, json=[{'id': 3, 'name': 'Museum'}])
        if endpoint == 'tags':
            return httpx.Response(200, json=[{'id': 4, 'name': 'Museum'}])
        if request.method == 'GET':
            assert request.url.params['status'] == 'any'
            return httpx.Response(200, json=[])
        assert json.loads(request.content) == dict(title='Chosen post', slug='chosen', content='<p>Body</p>',
            excerpt='Excerpt', status='draft', author=2, categories=[3], tags=[4])
        return httpx.Response(201, json={'id': 42, 'status': 'draft'})
    async def check():
        async with WordPressClient(settings(), httpx.MockTransport(handler)) as client:
            assert (await client.catalog())['authors'] == [{'id': '2', 'name': 'Editor'}]
            assert not await client.slug_exists('chosen')
            assert await client.create_draft(payload()) == '42'
    asyncio.run(check())
    assert sum(call.method == 'POST' for call in calls) == 1


@pytest.mark.parametrize('status,uncertain', [(401, False), (403, False), (429, False), (500, True), (302, True), (408, True)])
def test_wordpress_failures_are_redacted_and_not_retried(status, uncertain):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text='secret provider details')
    async def check():
        async with WordPressClient(settings(), httpx.MockTransport(handler)) as client:
            with pytest.raises(DestinationError) as caught:
                await client.create_draft(dict(payload(), tags=[]))
            assert caught.value.uncertain is uncertain
            assert 'secret' not in str(caught.value)
    asyncio.run(check())
    assert len(calls) == 1


def test_missing_tag_and_non_draft_never_create_post():
    def handler(request):
        assert request.method == 'GET'
        return httpx.Response(200, json=[])
    async def check():
        async with WordPressClient(settings(), httpx.MockTransport(handler)) as client:
            with pytest.raises(DestinationError, match='Create the tag'):
                await client.create_draft(payload())
            with pytest.raises(ValueError, match='only creates drafts'):
                await client.create_draft(dict(payload(), status='PUBLISHED'))
    asyncio.run(check())


@pytest.mark.parametrize('body', [{}, {'id': 42, 'status': 'publish'}, {'status': 'draft'}])
def test_unconfirmed_wordpress_creation_blocks_retry(body):
    async def check():
        async with WordPressClient(settings(), httpx.MockTransport(lambda request: httpx.Response(201, json=body))) as client:
            with pytest.raises(DestinationError) as caught:
                await client.create_draft(dict(payload(), tags=[]))
            assert caught.value.uncertain
    asyncio.run(check())


def test_exact_page_boundary_uses_wordpress_total_pages():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=[{'id': n + 1} for n in range(100)], headers={'X-WP-TotalPages': '1'})
    async def check():
        async with WordPressClient(settings(), httpx.MockTransport(handler)) as client:
            assert len(await client._list('categories')) == 100
    asyncio.run(check())
    assert len(calls) == 1


def test_user_can_choose_one_post_from_library(tmp_path):
    app = create_app(Settings(tmp_path / 'demo.db', demo=True))
    with TestClient(app, base_url='http://localhost') as client:
        post = app.state.db.get_post(1)
        app.state.db.review_post(1, post['revision'], 'APPROVED', True)
        page = client.get('/posts?review=APPROVED').text
        assert 'Import to WordPress' in page and 'href="/posts/1/import"' in page
        assert 'WordPress' in client.get('/destination').text
        post = app.state.db.get_post(1)
        response = client.post('/api/posts/1/import', headers={'X-Migration-Request': '1'},
            json={'revision': post['revision'], 'author_id': 'demo-sarah', 'category_ids': ['demo-museum']})
        assert response.json()['status'] == 'SUCCESS'
        assert app.state.db.get_post(2)['import_state'] == 'IDLE'
