"""Vercel serverless function entry point for the FastAPI backend."""

import os

# Set upload dir to /tmp for serverless environment
os.environ.setdefault("UPLOAD_DIR", "/tmp/uploads")

from backend.main import app  # noqa: E402, F401
