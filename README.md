# Blog Migration Manager ? WordPress destination

Import selected HubSpot posts into WordPress as drafts.

## Start

```powershell
.\.venv\Scripts\python.exe run.py
```

Open http://127.0.0.1:8000. Restart an existing app process to load this update.
For an isolated simulation, run with `--demo --port 8769`.

## Configure

Keep existing HubSpot credentials. Add these values to your local `.env` (see `.env.example`):

```dotenv
WORDPRESS_URL=https://your-site.example
WORDPRESS_USERNAME=your-user
WORDPRESS_APPLICATION_PASSWORD=your-application-password
```

Use the site root, including any installation subdirectory, without `/wp-json`.
This connector uses the site's own WordPress REST API over HTTPS with an application
password from Users ? Profile. It does not use WordPress.com's centralized OAuth API.
The user needs permission to edit posts and read categories and tags; choosing other
authors requires permission to edit others' posts and list authors.
Credentials stay server-side; redirects are not followed and create requests are not retried.

## Choose a post

1. Sync from HubSpot, then open Blog Posts.
2. Choose a post and preview/edit it. Resolve validation errors and approve it.
3. Select **Import to WordPress** beside the approved post, or **Import as draft** in its preview.
4. Confirm the WordPress author and categories, then select **Create draft in WordPress**.
5. Review the draft in WordPress before publishing there.

Each action imports only the selected post. Successful imports save the destination
ID and lock the local copy against duplicate imports. An uncertain create response
blocks retries until the destination can be checked manually. Existing import history
and historical destination IDs are retained; switching destinations does not unlock
previously imported posts. Internal legacy `highlevel_id` columns and normalized audit
payload keys remain for compatibility. Saved payloads are normalized import snapshots;
the WordPress connector translates them into REST API fields.

## Field mapping and limits

- Title, slug and cleaned HTML become the WordPress title, slug and content.
- Description becomes the excerpt. SEO plugin metadata is not written.
- Existing destination authors and categories are selected by ID.
- Tags map to existing WordPress tags by name. Create missing tags in WordPress before importing; no post is created when tags are missing.
- Inline images retain their source URLs. Media upload is not included. Featured image URL and alt text remain in the audit snapshot; set the featured image in WordPress after importing.
- Original publication dates stay local. New posts always use `draft` status.
- Local and remote slug checks reduce duplicates, but WordPress does not guarantee unique draft slugs. Use one app process per database.
- Review, revision protection, HTML cleaning, URL checks, simulated imports and source refresh preservation remain available.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Connector tests use mocked HTTP; demo mode never contacts a destination.
