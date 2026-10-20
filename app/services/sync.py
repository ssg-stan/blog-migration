import asyncio
import json
from datetime import datetime, timezone
from app.database import now
from app.services.hubspot import HubSpotClient, HubSpotError


def date_value(value):
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            stamp = float(value)
            if stamp > 100_000_000_000:
                stamp /= 1000
            return datetime.fromtimestamp(stamp, timezone.utc).isoformat()
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def text(value):
    return "" if value is None else str(value)


def normalize_post(post, authors, tags, synced_at):
    if not post.get("id"):
        raise HubSpotError("HubSpot returned a post without an ID. No posts were saved; sync again later.")
    author_id = text(post.get("blogAuthorId"))
    tag_ids = post.get("tagIds") or []
    state = text(post.get("state") or post.get("currentState")).upper()
    published = post.get("currentlyPublished") is True or state == "PUBLISHED"
    source_state = "PUBLISHED" if published else ("SCHEDULED" if "SCHEDULE" in state else "DRAFT")
    return {
        "hubspot_id": text(post["id"]),
        "title": text(post.get("name") or post.get("htmlTitle")),
        "slug": text(post.get("slug")),
        "content": text(post.get("postBody")),
        "meta_description": text(post.get("metaDescription")),
        "featured_image": text(post.get("featuredImage")),
        "featured_image_alt": text(post.get("featuredImageAltText")),
        "author": authors.get(author_id) or (f"Author #{author_id}" if author_id else ""),
        "tags": json.dumps([tags.get(text(tag_id), f"Tag #{tag_id}") for tag_id in tag_ids]),
        "publish_date": date_value(post.get("publishDate")),
        "source_state": source_state,
        "source_url": text(post.get("url")),
        "blog_id": text(post.get("contentGroupId")),
        "source_updated_at": date_value(post.get("updated") or post.get("updatedAt")),
        "raw_json": json.dumps(post, ensure_ascii=False),
        "synced_at": synced_at,
    }


class SyncManager:
    def __init__(self, db, settings, client_factory=HubSpotClient):
        self.db = db
        self.settings = settings
        self.client_factory = client_factory
        self.task = None

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    def start(self):
        if self.running:
            return None
        run_id = self.db.start_sync()
        self.task = asyncio.create_task(self._run(run_id))
        return run_id

    async def _run(self, run_id):
        try:
            async with self.client_factory(self.settings.hubspot_token) as client:
                posts, authors, tags = await client.fetch_posts(
                    self.settings.blog_id, lambda count: self.db.progress(run_id, count))
            author_map = {text(item.get("id")): text(item.get("fullName") or item.get("displayName") or item.get("name")) for item in authors}
            tag_map = {text(item.get("id")): text(item.get("name")) for item in tags}
            stamp = now()
            # A stable ID is the sole identity. Preserve future local review/import columns on refresh.
            normalized = {text(post.get("id")): normalize_post(post, author_map, tag_map, stamp) for post in posts}
            self.db.save_snapshot(list(normalized.values()), run_id)
        except asyncio.CancelledError:
            self.db.finish_error(run_id, "Sync interrupted by application shutdown. Sync again to retry.")
            raise
        except HubSpotError as exc:
            self.db.finish_error(run_id, str(exc))
        except Exception:
            self.db.finish_error(run_id, "Sync could not be completed. Existing posts are unchanged. Check local database access and try again.")

    async def close(self):
        if self.running:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
