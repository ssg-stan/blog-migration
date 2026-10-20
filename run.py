"""Local development entry point. Run python run.py, or python run.py --demo."""
import argparse
import os
from dotenv import load_dotenv
from app.config import ROOT

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start the local Blog Migration Manager")
    parser.add_argument("--demo", action="store_true", help="Use an isolated sample library and simulated destination; no provider access")
    parser.add_argument("--port", type=int, help="Local port (default: PORT from .env or 8000)")
    parser.add_argument("--reload", action="store_true", help="Restart automatically when code changes")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    # Command-line mode always wins over an inherited environment variable.
    os.environ["BLOG_MIGRATION_DEMO"] = "1" if args.demo else "0"
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=args.port or int(os.getenv("PORT", "8000")),
                reload=args.reload, workers=1)
