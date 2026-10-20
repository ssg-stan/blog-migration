from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import urlencode
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware
from app.config import ROOT, Settings
from app.database import Database
from app.services.hubspot import HubSpotClient
from app.services.preview import preview_html, safe_url
from app.services.sync import SyncManager
from app.models import PostEdit, ReviewAction, RevisionAction, CleanRequest, ImportRequest
from app.services.validation import EDIT_FIELDS
from app.services.link_checker import URLCheckManager, check_url
from app.services.transformer import clean_html
from app.services.importer import ImportManager, destination_settings, default_mappings
from app.services.highlevel import DestinationError


def format_date(value):
    if not value:
        return "Not set"
    try:
        return datetime.fromisoformat(value).strftime("%b %d, %Y")
    except ValueError:
        return value


def create_app(settings=None, client_factory=HubSpotClient, url_checker=check_url, destination_factory=None):
    settings = settings or Settings.from_env()
    db = Database(settings.database_path)
    manager = SyncManager(db, settings, client_factory)
    url_checks = URLCheckManager(db, url_checker)
    importer = ImportManager(db, settings, destination_factory)

    @asynccontextmanager
    async def lifespan(app):
        db.initialize()
        if settings.demo:
            from app.demo import seed_demo
            seed_demo(db)
        yield
        await manager.close()
        await url_checks.close()

    app = FastAPI(title="Blog Migration Manager", version="0.3.0", lifespan=lifespan)
    app.state.db = db
    app.state.sync_manager = manager
    app.state.settings = settings
    app.state.url_checks = url_checks
    app.state.importer = importer
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])
    app.mount("/static", StaticFiles(directory=ROOT / "app/static"), name="static")
    templates = Jinja2Templates(directory=ROOT / "app/templates")
    templates.env.filters["date"] = format_date
    templates.env.filters["safe_url"] = safe_url

    @app.middleware("http")
    async def local_request_protection(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if request.headers.get("X-Migration-Request") != "1" or (origin and origin != str(request.base_url).rstrip("/")):
                return JSONResponse({"detail": "Use the local app to perform this action."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if request.url.path.startswith("/static/fonts/"):
            response.headers["Access-Control-Allow-Origin"] = "*"
        if "Content-Security-Policy" not in response.headers:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https: http:; "
                "frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'; form-action 'self'"
            )
        return response

    def context(**extra):
        return {"configured": bool(settings.hubspot_token), "demo": settings.demo,
                "destination": destination_settings(settings), "blog_id": settings.blog_id, "sync": db.latest_sync(), "stats": db.stats(), "review_stats": db.review_stats(), **extra}

    @app.get("/posts", response_class=HTMLResponse)
    def posts_library(request: Request, q: str = Query("", max_length=200),
                  state: str = Query("", pattern="^(|PUBLISHED|DRAFT|SCHEDULED)$"),
                  page: int = Query(1, ge=1),
                  review: str = Query("", pattern="^(|PENDING|APPROVED|REJECTED|IMPORTED)$"),
                  validation: str = Query("", pattern="^(|ERROR|WARNING|READY)$")):
        posts, total, page, pages = db.list_posts(q.strip(), state, page, review=review, validation=validation)
        def page_url(number):
            return "/posts?" + urlencode({"q": q, "state": state, "review": review, "validation": validation, "page": number})
        return templates.TemplateResponse(request=request, name="posts.html", context=context(
            active="posts", posts=posts, stats=db.stats(), total=total, q=q, state=state,
            page=page, pages=pages, page_url=page_url, review=review, validation=validation))

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        posts, _, _, _ = db.list_posts(page_size=5)
        stats = db.stats()
        published_percent = round(stats["published"] / stats["total"] * 100) if stats["total"] else 0
        return templates.TemplateResponse(request=request, name="dashboard.html", context=context(
            active="dashboard", posts=posts, published_percent=published_percent, history=db.sync_history()))

    @app.get("/field-mapping", response_class=HTMLResponse)
    def field_mapping(request: Request):
        posts, _, _, _ = db.list_posts(page_size=1)
        sample = {**posts[0], **posts[0]["source"]} if posts else {}
        fields = [
            ("name / htmlTitle", "Title", "title"),
            ("postBody", "Body (HTML)", "content"),
            ("slug", "URL slug", "slug"),
            ("publishDate", "Published date", "publish_date"),
            ("blogAuthorId", "Author (resolved name)", "author"),
            ("tagIds", "Tags (resolved names)", "tags"),
            ("metaDescription", "SEO meta description", "meta_description"),
            ("featuredImage", "Featured image URL", "featured_image"),
            ("featuredImageAltText", "Image alt text", "featured_image_alt"),
            ("url", "Source URL", "source_url"),
            ("contentGroupId", "HubSpot blog ID", "blog_id"),
        ]
        return templates.TemplateResponse(request=request, name="mapping.html", context=context(
            active="mapping", sample=sample, fields=fields))

    @app.get("/posts/{post_id}", response_class=HTMLResponse)
    def review(request: Request, post_id: int):
        post = db.get_post(post_id)
        if not post:
            return templates.TemplateResponse(request=request, name="error.html", status_code=404,
                context=context(message="This post could not be found in your local library."))
        return templates.TemplateResponse(request=request, name="review.html", context=context(active="posts", post=post, history=db.review_history(post_id), import_attempt=db.latest_import(post_id)))

    @app.get("/posts/{post_id}/preview", response_class=HTMLResponse)
    def preview(request: Request, post_id: int, version: str = Query("working", pattern="^(working|source)$")):
        post = db.get_post(post_id)
        if not post:
            raise HTTPException(404, "Post not found")
        if version == "source":
            post = {**post, **post["source"]}
        image = safe_url(post["featured_image"], post["source_url"])
        response = templates.TemplateResponse(request=request, name="article.html", context={
            "post": post, "body": preview_html(post["content"], post["source_url"]), "image": image})
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; img-src https: http:; font-src 'self'; style-src 'unsafe-inline'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'self'; sandbox allow-popups allow-popups-to-escape-sandbox"
        )
        return response

    def require_post(post_id):
        post = db.get_post(post_id)
        if not post:
            raise HTTPException(404, "Post not found.")
        return post

    def review_operation(operation):
        try:
            return operation()
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/posts/{post_id}/edit", response_class=HTMLResponse)
    def edit_page(request: Request, post_id: int):
        post = require_post(post_id)
        if post["highlevel_id"] or post["import_state"] in {"PREPARING", "SENDING", "UNKNOWN"}:
            return RedirectResponse("/posts/" + str(post_id) + "/import", status_code=303)
        return templates.TemplateResponse(request=request, name="edit.html", context=context(active="posts", post=post))

    @app.put("/api/posts/{post_id}")
    def save_edit(post_id: int, payload: PostEdit):
        values = payload.model_dump()
        for field in ("title", "slug", "meta_description", "featured_image", "featured_image_alt", "author"):
            values[field] = values[field].strip()
        for field in ("tags", "categories"):
            if any(len(value) > 120 for value in values[field]):
                raise HTTPException(422, "Category and tag names must be 120 characters or fewer.")
            values[field] = list(dict.fromkeys(value.strip() for value in values[field] if value.strip()))
        values["publish_date"] = values["publish_date"].strip() if values["publish_date"] else None
        post = review_operation(lambda: db.edit_post(post_id, payload.revision,
            {key: values[key] for key in EDIT_FIELDS}, payload.review_notes))
        return {"revision": post["revision"], "validation": post["validation"], "status": post["migration_status"]}

    @app.post("/api/posts/{post_id}/review")
    def review_action(post_id: int, payload: ReviewAction):
        post = review_operation(lambda: db.review_post(post_id, payload.revision, payload.action, payload.acknowledge_warnings, payload.notes))
        return {"revision": post["revision"], "status": post["migration_status"]}

    @app.post("/api/posts/{post_id}/reset")
    def reset_post(post_id: int, payload: RevisionAction):
        post = review_operation(lambda: db.reset_edits(post_id, payload.revision))
        return {"revision": post["revision"], "status": post["migration_status"]}

    @app.post("/api/clean-html")
    def clean_content(payload: CleanRequest):
        return {"content": clean_html(payload.content, payload.source_url)}

    @app.post("/api/posts/{post_id}/check-urls", status_code=202)
    async def check_post_urls(post_id: int, payload: RevisionAction):
        post = require_post(post_id)
        if post["highlevel_id"] or post["import_state"] not in {"IDLE", "ERROR"}:
            raise HTTPException(409, "URL checks are locked while this post has an active, completed or uncertain import.")
        if post["revision"] != payload.revision:
            raise HTTPException(409, "Post changed. Reload before checking URLs.")
        if not url_checks.start(post):
            raise HTTPException(409, "URL checks are already running for this post.")
        return url_checks.status(post_id)

    @app.get("/api/posts/{post_id}/check-urls")
    def url_check_status(post_id: int):
        require_post(post_id)
        return url_checks.status(post_id)

    @app.get("/destination", response_class=HTMLResponse)
    def destination_page(request: Request):
        return templates.TemplateResponse(request=request, name="destination.html", context=context(active="destination"))

    @app.post("/api/destination/check")
    async def destination_check():
        try:
            catalog = await importer.catalog()
        except DestinationError as exc:
            raise HTTPException(502, str(exc)) from None
        destination = destination_settings(settings)
        blog = next(item for item in catalog["blogs"] if item["id"] == destination["blog_id"])
        return {"simulated": destination["simulated"], "blog": blog, "authors": len(catalog["authors"]), "categories": len(catalog["categories"])}

    @app.get("/posts/{post_id}/import", response_class=HTMLResponse)
    async def import_page(request: Request, post_id: int):
        post = require_post(post_id)
        catalog, error = None, ""
        if post["migration_status"] == "APPROVED" and post["import_state"] in {"IDLE", "ERROR"}:
            try:
                catalog = await importer.catalog()
            except DestinationError as exc:
                error = str(exc)
        attempt = db.latest_import(post_id)
        import_copy = attempt["approved_copy"] if attempt and attempt["payload"] and post["import_state"] in {"SUCCESS", "UNKNOWN", "SENDING"} else post
        author_id, category_ids = default_mappings(post, catalog) if catalog else ("", [])
        return templates.TemplateResponse(request=request, name="import.html", context=context(active="posts", post=post,
            catalog=catalog, destination_error=error, selected_author=author_id, selected_categories=category_ids,
            outgoing_html=clean_html(post["content"], post["source_url"]), import_attempt=attempt, import_copy=import_copy))

    @app.post("/api/posts/{post_id}/import")
    async def import_post(post_id: int, payload: ImportRequest):
        require_post(post_id)
        try:
            result = await importer.run(post_id, payload)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"revision": result["revision"], "status": result["import_state"],
                "message": result["import_message"], "destination_id": result["highlevel_id"], "simulated": settings.demo}

    @app.get("/api/posts/{post_id}/import")
    def import_status(post_id: int):
        post = require_post(post_id)
        attempt = db.latest_import(post_id)
        return {"revision": post["revision"], "status": post["import_state"], "message": post["import_message"],
                "destination_id": post["highlevel_id"], "simulated": bool(attempt and attempt["simulated"])}

    @app.get("/api/sync/status")
    def sync_status():
        return {"running": manager.running, "configured": bool(settings.hubspot_token),
                "demo": settings.demo, "run": db.latest_sync()}

    @app.post("/api/sync", status_code=202)
    async def sync_posts():
        if settings.demo:
            raise HTTPException(400, "Demo mode uses a separate sample database. Restart without --demo to sync HubSpot.")
        if not settings.hubspot_token:
            raise HTTPException(400, "Add HUBSPOT_ACCESS_TOKEN to .env and restart the app before syncing.")
        run_id = manager.start()
        if run_id is None:
            raise HTTPException(409, "A sync is already running. Wait for it to finish.")
        return {"run_id": run_id, "message": "HubSpot sync started."}

    @app.get("/health")
    def health():
        return {"status": "ok", "phase": 3, "demo": settings.demo}

    return app


app = create_app()
