"""Offline content validation shared by sync, editing, and approval."""
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from urllib.parse import urlsplit, unquote
from bs4 import BeautifulSoup
from app.services.preview import safe_url

EDIT_FIELDS = ("title", "slug", "content", "meta_description", "featured_image", "featured_image_alt", "author", "tags", "categories", "publish_date")


def fingerprint(post):
    data = {key: post.get(key) for key in EDIT_FIELDS}
    data["source_url"] = post.get("source_url", "")
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def slug_key(slug):
    return slug.strip("/").casefold()


def url_targets(post):
    """Images and internal HTTP(S) links only; no public requests happen here."""
    soup = BeautifulSoup(post.get("content", ""), "html.parser")
    base = safe_url(post.get("source_url", ""))
    host = urlsplit(base).hostname if base else None
    urls = {}
    if post.get("featured_image"):
        target = safe_url(post["featured_image"], base)
        if target:
            urls[target] = "image"
    for image in soup.find_all("img"):
        target = safe_url(image.get("src", ""), base)
        if target:
            urls[target] = "image"
    for anchor in soup.find_all("a", href=True):
        if anchor["href"].startswith("#"):
            continue
        target = safe_url(anchor["href"], base)
        if target and host and urlsplit(target).hostname == host:
            urls.setdefault(target.split("#")[0], "internal link")
    return [{"url": url, "kind": kind} for url, kind in urls.items()]


def validate_post(post, duplicate_slug=False):
    issues, checks = [], []
    def issue(level, code, field, message, url=""):
        issues.append({"level": level, "code": code, "field": field, "message": message, "url": url})
    def check(field, label, state, detail):
        checks.append({"field": field, "label": label, "state": state, "detail": detail})
        if state != "passed":
            issue(state, f"field_{field}", field, detail)
    title = post.get("title", "").strip()
    check("title", "Title", "passed" if title else "error", "Title is present." if title else "Add a post title.")
    slug = post.get("slug", "").strip()
    valid_slug = bool(slug_key(slug)) and bool(re.fullmatch(r"[\w/-]+", slug)) and "//" not in slug and not duplicate_slug
    detail = "URL slug is present and unique." if valid_slug else ("This slug is used by another local post." if duplicate_slug else "Use a URL path with letters, numbers, hyphens, underscores, and single slashes.")
    check("slug", "URL slug", "passed" if valid_slug else "error", detail)
    soup = BeautifulSoup(post.get("content", ""), "html.parser")
    substantive = BeautifulSoup(str(soup), "html.parser")
    for hidden in substantive.select("script, style, form, iframe, object, embed"):
        hidden.decompose()
    has_body = bool(substantive.get_text(strip=True) or substantive.find("img"))
    check("content", "Article body", "passed" if has_body else "error", "Article content is present." if has_body else "Add article body content.")
    meta = post.get("meta_description", "").strip()
    meta_state = "passed" if meta and len(meta) <= 160 else "warning"
    check("meta_description", "SEO meta description", meta_state, "Meta description is present." if meta_state == "passed" else ("Meta description exceeds 160 characters; review the search snippet." if meta else "Add an SEO meta description."))
    author = post.get("author", "").strip()
    author_ok = bool(author) and not author.startswith("Author #")
    check("author", "Author", "passed" if author_ok else "warning", "Author name is resolved." if author_ok else "Set a resolved author name.")
    taxonomy = post.get("tags", []) + post.get("categories", [])
    check("taxonomy", "Categories & tags", "passed" if taxonomy else "warning", "Categories or tags are present." if taxonomy else "Add at least one category or tag.")
    image = post.get("featured_image", "").strip()
    image_url = safe_url(image, post.get("source_url", "")) if image else ""
    check("featured_image", "Featured image", "passed" if image_url else ("error" if image else "warning"), "Featured image URL is valid (reachability is checked separately)." if image_url else ("Featured image needs a valid HTTP(S) URL." if image else "No featured image is set."))
    date = post.get("publish_date")
    try:
        if date:
            datetime.fromisoformat(date.replace("Z", "+00:00"))
        date_state = "passed" if date else "warning"
    except (ValueError, AttributeError):
        date_state = "error"
    check("publish_date", "Publish date", date_state, "Publish date is valid." if date_state == "passed" else ("Publish date is missing. The local post can still be reviewed." if not date else "Use a valid ISO 8601 publish date."))
    if soup.find("script") or any(any(str(key).lower().startswith("on") for key in tag.attrs) for tag in soup.find_all(True)):
        issue("error", "active_html", "content", "Active scripts or event handlers detected. Use Clean HTML or remove them before approval.")
    for selector, code, label in [("form", "hubspot_form", "form"), ("iframe,object,embed", "embed", "embedded media")]:
        if soup.select(selector):
            issue("warning", code, "content", f"Source {label} detected; it is disabled in previews and needs migration review.")
    if soup.select('[class*="hs_cos"], [class*="hs-"], [class*="hubspot"]'):
        issue("warning", "hubspot_classes", "content", "HubSpot-specific CSS classes found. Preview styles may differ from the source.")
    if soup.select('[class*="cta"], [id*="cta"], [data-hs-cta-url]'):
        issue("warning", "hubspot_cta", "content", "HubSpot CTA detected. Check its behavior or replace it with a normal link.")
    base = safe_url(post.get("source_url", ""))
    source_host = urlsplit(base).hostname if base else None
    external_images = 0
    missing_alt = 0
    for img in soup.find_all("img"):
        target = safe_url(img.get("src", ""), base)
        if not target:
            issue("error", "invalid_image", "content", "An image has a missing, unsafe, or unresolved source URL.")
        elif not source_host or urlsplit(target).hostname != source_host:
            external_images += 1
        if not img.get("alt", "").strip():
            missing_alt += 1
    if image_url and (not source_host or urlsplit(image_url).hostname != source_host):
        external_images += 1
    if external_images:
        issue("warning", "external_images", "content", f"{external_images} image(s) rely on an external host; confirm they will remain available.")
    if missing_alt or (image_url and not post.get("featured_image_alt", "").strip()):
        issue("warning", "image_alt", "featured_image_alt", "Some images lack alt text. Review accessibility before import.")
    ids = {tag.get("id") for tag in soup.find_all(id=True)}
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if href.startswith("#"):
            if unquote(href[1:]) and unquote(href[1:]) not in ids:
                issue("warning", "missing_anchor", "content", f"Link points to a missing article anchor: {href}.")
        elif href.lower().startswith(("mailto:", "tel:")):
            issue("warning", "contact_link", "content", "Email or telephone link detected; it is omitted from the safe preview.")
        elif not safe_url(href, base):
            issue("error", "invalid_link", "content", "An article link has an unsafe or unresolved URL.")
    if post.get("source_changed"):
        issue("warning", "source_changed", "source", "HubSpot changed after local edits were made. Compare Original / Working before approving.")
    targets = url_targets(post)
    report = post.get("link_report") or {}
    if report.get("fingerprint") != fingerprint(post):
        report = {}
    if targets and not report:
        issue("warning", "urls_unchecked", "links", f"{len(targets)} image/internal-link URL(s) have not been checked. Select Check URLs.")
    for result in report.get("results", []):
        if result["state"] != "ok":
            issue("error" if result["state"] == "broken" else "warning", "url_" + result["state"], "links", result["message"], result["url"])
    if report.get("limited"):
        issue("warning", "url_limit", "links", "Only the first 50 unique URLs were checked; review the remaining URLs manually.")
    counts = Counter(item["level"] for item in issues)
    return {"status": "ERROR" if counts["error"] else ("WARNING" if counts["warning"] else "READY"),
            "errors": counts["error"], "warnings": counts["warning"], "checks": checks, "issues": issues,
            "passed": sum(check["state"] == "passed" for check in checks), "total": len(checks),
            "image_count": len(soup.find_all("img")) + bool(image), "url_count": len(targets),
            "urls_checked_at": report.get("checked_at"), "fingerprint": fingerprint(post)}
