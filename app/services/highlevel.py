"""Local Titans uses the HighLevel Blogs API. No publish/update endpoints are used."""
import asyncio
import uuid
import httpx

BASE_URL = "https://services.leadconnectorhq.com"
API_VERSION = "v3"


class DestinationError(Exception):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class HighLevelClient:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.client = httpx.AsyncClient(base_url=BASE_URL, timeout=30,
            follow_redirects=False, trust_env=False, transport=transport,
            headers={"Authorization": "Bearer " + settings.highlevel_token,
                     "Version": API_VERSION, "Accept": "application/json"})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    async def _request(self, method, endpoint, **kwargs):
        creating = method == "POST"
        try:
            response = await self.client.request(method, endpoint, **kwargs)
        except httpx.TransportError:
            raise DestinationError("Destination could not be reached. " + (
                "The create result is uncertain; check Local Titans before any further action."
                if creating else "Check your connection and try again."), uncertain=creating) from None
        # Never echo provider bodies, request headers, or tokens in UI/logs.
        if response.status_code not in ({200, 201} if creating else {200}):
            messages = {401: "Destination token is invalid or expired.",
                        403: "Destination token is missing access to this location or the required blog scopes.",
                        404: "The destination blog or API resource was not found.",
                        429: "Destination rate limit reached. Wait before trying again."}
            raise DestinationError(messages.get(response.status_code,
                "Destination rejected the request (HTTP " + str(response.status_code) + ")."),
                uncertain=creating and (response.status_code >= 500 or 300 <= response.status_code < 400 or response.status_code == 408))
        try:
            return response.json()
        except ValueError:
            raise DestinationError("Destination returned an unreadable response.", uncertain=creating) from None

    async def _list(self, endpoint, key, offset_key="offset"):
        records, seen = [], set()
        for offset in range(0, 5000, 50):
            body = await self._request("GET", endpoint, params={
                "locationId": self.settings.highlevel_location_id, "limit": 50, offset_key: offset})
            batch = body.get(key) if isinstance(body, dict) else None
            if not isinstance(batch, list):
                raise DestinationError("Destination returned an unexpected catalog response.")
            for item in batch:
                if not isinstance(item, dict) or not isinstance(item.get("_id"), str) or not item["_id"]:
                    raise DestinationError("Destination catalog contains an invalid record.")
                if item["_id"] in seen:
                    raise DestinationError("Destination catalog pagination repeated a record.")
                if item.get("locationId") and item["locationId"] != self.settings.highlevel_location_id:
                    raise DestinationError("Destination returned a record belonging to another location.")
                seen.add(item["_id"])
                records.append({"id": item["_id"], "name": str(item.get("name") or item.get("label") or item["_id"])})
            if len(batch) < 50:
                return records
        raise DestinationError("Destination catalog exceeds the supported size. Narrow the destination first.")

    async def catalog(self):
        # Sequential calls avoid bursts and keep errors predictable.
        blogs = await self._list("/blogs/site/all", "data", "skip")
        if not any(blog["id"] == self.settings.highlevel_blog_id for blog in blogs):
            raise DestinationError("HIGHLEVEL_BLOG_ID does not belong to the configured destination location.")
        authors = await self._list("/blogs/authors", "authors")
        categories = await self._list("/blogs/categories", "categories")
        return {"blogs": blogs, "authors": authors, "categories": categories}

    async def slug_exists(self, slug):
        result = await self._request("GET", "/blogs/posts/url-slug-exists", params={
            "locationId": self.settings.highlevel_location_id, "urlSlug": slug})
        if not isinstance(result, dict) or type(result.get("exists")) is not bool:
            raise DestinationError("Destination returned an unexpected slug-check response.")
        return result["exists"]

    async def create_draft(self, payload):
        if payload.get("status") != "DRAFT":
            raise ValueError("Phase 3 only creates drafts.")
        result = await self._request("POST", "/blogs/posts", json=payload)
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("_id"), str) or not data["_id"]:
            raise DestinationError("Destination create response did not include a post ID. Check Local Titans before any further action.", uncertain=True)
        if data.get("status") != "DRAFT":
            raise DestinationError("Destination did not confirm DRAFT status. Check the destination post before any further action.", uncertain=True)
        return data["_id"]


class SimulatedHighLevelClient:
    """No HTTP requests. Creation is tracked in the separate local demo database."""
    def __init__(self, settings):
        self.settings = settings

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def catalog(self):
        return {"blogs": [{"id": "demo-blog", "name": "Local Titans · Simulated blog"}],
            "authors": [{"id": "demo-sarah", "name": "Sarah Williams"}, {"id": "demo-craig", "name": "Craig P."},
                        {"id": "demo-editor", "name": "Editorial team"}],
            "categories": [{"id": "demo-museum", "name": "Museum"}, {"id": "demo-storage", "name": "Storage"},
                           {"id": "demo-healthcare", "name": "Healthcare"}, {"id": "demo-general", "name": "General"}]}

    async def slug_exists(self, slug):
        return False

    async def create_draft(self, payload):
        await asyncio.sleep(.25)
        if payload["status"] != "DRAFT":
            raise ValueError("Only drafts are supported.")
        return "demo-draft-" + uuid.uuid4().hex
