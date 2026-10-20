"""WordPress REST connector. Only draft posts are created; no publication calls."""
from html import unescape
from urllib.parse import urlsplit
import httpx
from app.services.highlevel import DestinationError, SimulatedHighLevelClient


class WordPressClient:
    def __init__(self, settings, transport=None):
        self.settings = settings
        url = settings.wordpress_url.rstrip('/')
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise DestinationError('WORDPRESS_URL must be an HTTPS site root without credentials, query, or fragment.')
        self.client = httpx.AsyncClient(base_url=url + '/wp-json/wp/v2/',
            auth=httpx.BasicAuth(settings.wordpress_username, settings.wordpress_password),
            timeout=30, follow_redirects=False, trust_env=False, transport=transport)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    async def _request(self, method, endpoint, include_headers=False, **kwargs):
        creating = method == 'POST'
        try:
            response = await self.client.request(method, endpoint, **kwargs)
        except httpx.TransportError:
            raise DestinationError('WordPress could not be reached. Check the destination before retrying.' if creating
                else 'WordPress could not be reached. Check your connection.', uncertain=creating) from None
        if response.status_code not in ({200, 201} if creating else {200}):
            raise DestinationError({401: 'WordPress authentication failed. Check the username and application password.',
                403: 'WordPress denied access. Check user permissions.',
                404: 'WordPress REST API was not found. Check the site URL.',
                429: 'WordPress rate limit reached. Try again later.'}.get(response.status_code,
                f'WordPress rejected the request (HTTP {response.status_code}).'),
                uncertain=creating and (response.status_code >= 500 or response.status_code in {408} or 300 <= response.status_code < 400))
        try:
            body = response.json()
            return (body, response.headers) if include_headers else body
        except ValueError:
            raise DestinationError('WordPress returned an unreadable response.', uncertain=creating) from None

    async def _list(self, endpoint, **params):
        records, seen = [], set()
        for page in range(1, 101):
            batch, headers = await self._request('GET', endpoint, include_headers=True, params=dict(params, per_page=100, page=page))
            if not isinstance(batch, list):
                raise DestinationError('WordPress returned an invalid catalog.')
            for item in batch:
                if not isinstance(item, dict) or type(item.get('id')) is not int or item['id'] <= 0 or item['id'] in seen:
                    raise DestinationError('WordPress returned an invalid or repeated catalog record.')
                seen.add(item['id'])
                records.append(item)
            if len(batch) < 100 or headers.get('X-WP-TotalPages') == str(page):
                return records
        raise DestinationError('WordPress catalog exceeds the supported size.')

    async def catalog(self):
        me = await self._request('GET', 'users/me', params={'context': 'edit'})
        if not isinstance(me, dict) or type(me.get('id')) is not int or not me.get('capabilities', {}).get('edit_posts'):
            raise DestinationError('The WordPress user must be allowed to edit posts.')
        users = await self._list('users', context='edit', who='authors') if me.get('capabilities', {}).get('edit_others_posts') else [me]
        categories = await self._list('categories', hide_empty='false')
        def choices(items):
            return [{'id': str(item['id']), 'name': unescape(str(item.get('name', item['id'])))} for item in items]
        return {'blogs': [{'id': self.settings.wordpress_url, 'name': self.settings.wordpress_url}],
                'authors': choices(users), 'categories': choices(categories)}

    async def slug_exists(self, slug):
        posts = await self._request('GET', 'posts', params={'slug': slug, 'status': 'any', 'context': 'edit', 'per_page': 1})
        if not isinstance(posts, list):
            raise DestinationError('WordPress returned an invalid slug check.')
        return bool(posts)

    async def create_draft(self, payload):
        if payload.get('status') != 'DRAFT':
            raise ValueError('WordPress connector only creates drafts.')
        # The saved normalized payload remains compatible with historical imports.
        tags = []
        for name in dict.fromkeys(payload.get('tags', [])):
            matches = [tag for tag in await self._list('tags', search=name, hide_empty='false')
                       if unescape(tag.get('name', '')).casefold() == name.casefold()]
            if not matches:
                raise DestinationError(f'Create the tag "{name}" in WordPress before importing.')
            tags.append(matches[0]['id'])
        outgoing = {'title': payload['title'], 'slug': payload['urlSlug'], 'content': payload['rawHTML'],
                    'excerpt': payload['description'], 'status': 'draft', 'author': int(payload['author']),
                    'categories': [int(value) for value in payload['categories']], 'tags': tags}
        result = await self._request('POST', 'posts', json=outgoing)
        if not isinstance(result, dict) or type(result.get('id')) is not int or result['id'] <= 0 or result.get('status') != 'draft':
            raise DestinationError('WordPress did not confirm a draft ID and status. Check WordPress before retrying.', uncertain=True)
        return str(result['id'])


class SimulatedWordPressClient(SimulatedHighLevelClient):
    async def catalog(self):
        catalog = await super().catalog()
        catalog['blogs'][0]['name'] = 'WordPress simulated blog'
        return catalog
