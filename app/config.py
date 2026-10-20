from dataclasses import dataclass, field
from pathlib import Path
import os
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

@dataclass(frozen=True)
class Settings:
    database_path: Path
    hubspot_token: str = field(default="", repr=False)
    blog_id: str = ""
    demo: bool = False
    highlevel_token: str = field(default="", repr=False)
    highlevel_location_id: str = ""
    highlevel_blog_id: str = ""

    wordpress_url: str = ""
    wordpress_username: str = ""
    wordpress_password: str = field(default="", repr=False)

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env")
        demo = os.getenv("BLOG_MIGRATION_DEMO", "") == "1"
        configured = Path(os.getenv("DATABASE_PATH", "data/migration.db"))
        database = ROOT / "data/demo.db" if demo else configured
        if not database.is_absolute():
            database = ROOT / database
        return cls(database, os.getenv("HUBSPOT_ACCESS_TOKEN", "").strip(),
        os.getenv("HUBSPOT_BLOG_ID", "").strip(), demo,
        os.getenv("HIGHLEVEL_ACCESS_TOKEN", "").strip(),
        os.getenv("HIGHLEVEL_LOCATION_ID", "").strip(),
        os.getenv("HIGHLEVEL_BLOG_ID", "").strip(),
        os.getenv("WORDPRESS_URL", "").strip().rstrip("/"),
        os.getenv("WORDPRESS_USERNAME", "").strip(),
        os.getenv("WORDPRESS_APPLICATION_PASSWORD", "").strip())
