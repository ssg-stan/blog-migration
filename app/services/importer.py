import asyncio
from bs4 import BeautifulSoup
from app.services.highlevel import DestinationError
from app.services.wordpress import WordPressClient, SimulatedWordPressClient
from app.services.preview import safe_url
from app.services.transformer import clean_html


def destination_settings(settings):
    return {"configured": settings.demo or bool(settings.wordpress_url and settings.wordpress_username and settings.wordpress_password),
            "simulated": settings.demo,
            "location_id": "demo-location" if settings.demo else settings.wordpress_url,
            "blog_id": "demo-blog" if settings.demo else settings.wordpress_url}


def default_mappings(post, catalog):
    def matched(name, choices):
        matches = [item["id"] for item in choices if item["name"].strip().casefold() == name.strip().casefold()]
        return matches[0] if len(matches) == 1 else ""
    return matched(post["author"], catalog["authors"]), [value for name in post["categories"]
        if (value := matched(name, catalog["categories"]))]


def draft_payload(post, catalog, settings, author_id, category_ids):
    destination = destination_settings(settings)
    if author_id not in {item["id"] for item in catalog["authors"]}:
        raise ValueError("Select a destination author from this location.")
    category_ids = list(dict.fromkeys(category_ids))
    if any(value not in {item["id"] for item in catalog["categories"]} for value in category_ids):
        raise ValueError("Select destination categories from this location.")
    if post["categories"] and not category_ids:
        raise ValueError("Map the working copy's categories before importing.")
    body = clean_html(post["content"], post["source_url"])
    words = len(BeautifulSoup(body, "html.parser").get_text(" ", strip=True).split())
    if not words:
        raise ValueError("The outgoing article has no readable content. Edit and approve it again.")
    payload = {"locationId": destination["location_id"], "blogId": destination["blog_id"],
        "title": post["title"], "urlSlug": post["slug"].strip("/"), "rawHTML": body,
        "description": post["meta_description"], "status": "DRAFT", "author": author_id,
        "categories": category_ids, "tags": post["tags"], "wordCount": words,
        "readTimeInMinutes": max(1, (words + 199) // 200), "archived": False}
    if post["featured_image"]:
        image = safe_url(post["featured_image"], post["source_url"])
        if not image:
            raise ValueError("Featured image must resolve to an HTTP or HTTPS URL.")
        payload.update(imageUrl=image, imageAltText=post["featured_image_alt"])
    # Intentionally omit publishedAt and canonicalLink: draft creation must not
    # schedule publication or point the new article's canonical URL back at HubSpot.
    return payload


class ImportManager:
    def __init__(self, db, settings, client_factory=None):
        self.db, self.settings = db, settings
        self.client_factory = client_factory or (SimulatedWordPressClient if settings.demo else WordPressClient)

    async def catalog(self):
        if not destination_settings(self.settings)["configured"]:
            raise DestinationError("Set WORDPRESS_URL, WORDPRESS_USERNAME and WORDPRESS_APPLICATION_PASSWORD in .env, then restart the app.")
        async with self.client_factory(self.settings) as client:
            return await client.catalog()

    async def run(self, post_id, request):
        destination = destination_settings(self.settings)
        if not destination["configured"]:
            raise ValueError("Configure the WordPress destination before importing.")
        attempt_id, operation_id, post = self.db.start_import(post_id, request.revision,
            destination["location_id"], destination["blog_id"], destination["simulated"])
        sending = False
        try:
            async with self.client_factory(self.settings) as client:
                catalog = await client.catalog()
                payload = draft_payload(post, catalog, self.settings, request.author_id, request.category_ids)
                if await client.slug_exists(payload["urlSlug"]):
                    raise ValueError("This slug already exists in WordPress. No draft was created. Edit the slug and approve again, or inspect the existing post.")
                self.db.sending_import(attempt_id, payload)
                sending = True
                destination_id = await client.create_draft(payload)
            message = ("Simulated draft created locally. Nothing was sent to WordPress." if destination["simulated"]
                       else "Draft created in WordPress. Review and publish it in the destination when ready.")
            return self.db.finish_import(attempt_id, "SUCCESS", message, destination_id)
        except asyncio.CancelledError:
            self.db.finish_import(attempt_id, "UNKNOWN" if sending else "ERROR",
                "Import interrupted during creation; check WordPress before any further action." if sending else "Preparation interrupted. No create request was sent.")
            raise
        except DestinationError as exc:
            return self.db.finish_import(attempt_id, "UNKNOWN" if exc.uncertain and sending else "ERROR", str(exc))
        except ValueError as exc:
            return self.db.finish_import(attempt_id, "UNKNOWN" if sending else "ERROR",
                "Creation result is uncertain. Check WordPress before any further action." if sending else str(exc))
        except Exception:
            return self.db.finish_import(attempt_id, "UNKNOWN" if sending else "ERROR",
                "Creation result is uncertain. Check WordPress before any further action." if sending
                else "Could not prepare the destination draft. No create request was sent.")
