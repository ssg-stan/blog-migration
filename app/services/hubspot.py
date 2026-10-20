import asyncio
import httpx

API_ROOT = "https://api.hubapi.com"


class HubSpotError(Exception):
    pass


class HubSpotClient:
    def __init__(self, token, transport=None):
        self.client = httpx.AsyncClient(
            base_url=API_ROOT,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
            transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    async def _get(self, endpoint, params):
        for attempt in range(4):
            try:
                response = await self.client.get(endpoint, params=params)
            except httpx.RequestError:
                if attempt < 3:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise HubSpotError("Could not reach HubSpot. Check your internet connection and try again.") from None
            if response.status_code == 401:
                raise HubSpotError("HubSpot rejected the access token. Update HUBSPOT_ACCESS_TOKEN in .env and restart the app.")
            if response.status_code == 403:
                raise HubSpotError("HubSpot denied access. Check the private app's content scope and your account's CMS access.")
            if response.status_code == 429 or 500 <= response.status_code <= 599:
                if attempt < 3:
                    try:
                        delay = float(response.headers.get("Retry-After", 2 ** attempt))
                    except ValueError:
                        delay = 2 ** attempt
                    await asyncio.sleep(min(30, max(0, delay)))
                    continue
                raise HubSpotError("HubSpot is busy or rate limiting requests. Wait a moment and sync again.")
            if response.status_code != 200:
                # Never propagate provider response bodies or request headers, which can contain secrets.
                raise HubSpotError(f"HubSpot returned HTTP {response.status_code}. Check your app permissions and try again.")
            try:
                payload = response.json()
            except ValueError:
                raise HubSpotError("HubSpot returned an invalid JSON response. Sync again later.") from None
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                raise HubSpotError("HubSpot returned an unexpected response format.")
            return payload

    async def collection(self, endpoint, extra_params=None, progress=None):
        params = {"limit": 100, "archived": "false", **(extra_params or {})}
        items, seen = [], set()
        while True:
            data = await self._get(endpoint, params)
            if not all(isinstance(item, dict) for item in data["results"]):
                raise HubSpotError("HubSpot returned invalid collection records.")
            items.extend(data["results"])
            if progress:
                progress(len(items))
            paging = data.get("paging") or {}
            next_page = paging.get("next") or {}
            after = next_page.get("after")
            if after is None:
                return items
            after = str(after)
            if after in seen:
                raise HubSpotError("HubSpot repeated a pagination cursor. Sync stopped to avoid an endless loop.")
            seen.add(after)
            # Only use the cursor; never follow an untrusted paging link with our bearer token.
            params["after"] = after

    async def fetch_posts(self, blog_id="", progress=None):
        authors = await self.collection("/cms/v3/blogs/authors")
        tags = await self.collection("/cms/v3/blogs/tags")
        params = {"contentGroupId__eq": blog_id} if blog_id else {}
        posts = await self.collection("/cms/v3/blogs/posts", params, progress)
        # Also filter locally, so a provider ignoring the filter cannot mix blogs.
        if blog_id:
            posts = [post for post in posts if str(post.get("contentGroupId", "")) == blog_id]
        return posts, authors, tags
