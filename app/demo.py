from app.database import now
from app.services.sync import normalize_post


def seed_demo(db):
    if db.stats()["total"]:
        return
    examples = [
        ("A smarter approach to museum collections", "Collections care", "PUBLISHED", "2026-09-24T09:00:00Z"),
        ("Making room for your next chapter", "Space planning", "PUBLISHED", "2026-09-12T09:00:00Z"),
        ("Mobile shelving: a practical guide", "Storage systems", "PUBLISHED", "2026-08-28T09:00:00Z"),
        ("Designing storage that works for everyone", "Space planning", "DRAFT", None),
        ("Behind the scenes of a better archive", "Collections care", "SCHEDULED", "2026-11-15T09:00:00Z"),
        ("Five questions to ask before you expand", "Storage systems", "DRAFT", None),
    ]
    posts = []
    for index, (title, tag, state, publish) in enumerate(examples):
        source = {"id": f"demo-{index + 1}", "name": title, "slug": title.lower().replace(":", "").replace(" ", "-"),
            "blogAuthorId": "1" if index % 2 == 0 else "2", "tagIds": [index % 3], "state": state,
            "publishDate": publish, "contentGroupId": "demo-blog", "updated": "2026-10-01T12:00:00Z",
            "metaDescription": "Explore thoughtful ideas for organizing collections, creating space, and planning for what comes next.",
            "postBody": """<p>Great spaces start with a simple question: what do the people using them need?</p>
            <h2>Start with what matters</h2><p>Before choosing a storage system, take time to understand your collection.
            Consider the items you use every day, the pieces you need to protect, and how your needs might change.</p>
            <blockquote>Make space for the work you want to do, and the collection you want to grow.</blockquote>
            <h2>Plan for everyday use</h2><p>A thoughtful layout balances capacity with access. These three principles help:</p>
            <ul><li>Keep frequently used items within easy reach.</li><li>Leave room for safe movement and future growth.</li>
            <li>Use consistent labels to help everyone find what they need.</li></ul>
            <h2>Compare your options</h2><table><thead><tr><th>Priority</th><th>What to consider</th></tr></thead>
            <tbody><tr><td>Access</td><td>Clear aisles and intuitive organization</td></tr>
            <tr><td>Capacity</td><td>Adjustable shelving and room to grow</td></tr></tbody></table>
            <h2>Take the next step</h2><p>Document your goals and walk through your space with the team who uses it.
            A little planning now can make a meaningful difference later.</p>"""}
        posts.append(normalize_post(source, {"1": "Sarah Williams", "2": "Alex Morgan"},
                                    {str(index % 3): tag}, now()))
    run_id = db.start_sync()
    db.save_snapshot(posts, run_id)
