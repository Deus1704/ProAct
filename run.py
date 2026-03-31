"""Entry point for the Proactive Ride Assistant."""
from pathlib import Path

from dotenv import load_dotenv
import uvicorn


load_dotenv(Path(__file__).with_name(".env"))


if __name__ == "__main__":
    uvicorn.run(
        "proactive_assistant_app.app:app",
        host="0.0.0.0",
        port=8001,
        reload=True,
    )
