"""Settings, read from environment variables / the .env file."""
import glob
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small.en")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "auto")  # auto | cuda | cpu
PIPER_VOICE = os.getenv("PIPER_VOICE", str(ROOT / "models/piper/en_US-lessac-medium.onnx"))

MAX_REPAIRS = int(os.getenv("MAX_REPAIRS", "2"))        # extra attempts after the first query
QUERY_TIMEOUT_S = int(os.getenv("QUERY_TIMEOUT_S", "20"))
MAX_ROWS = int(os.getenv("MAX_ROWS", "5000"))
TOP_TABLES = int(os.getenv("TOP_TABLES", "6"))

CACHE_DIR = ROOT / ".cache"
UPLOAD_DB = ROOT / "data" / "uploads.db"


def default_sources() -> dict[str, str]:
    """DEFAULT_SOURCES="olist=postgresql+psycopg://...;marketing=sqlite:///data/marketing.db" """
    out = {}
    for part in os.getenv("DEFAULT_SOURCES", "").split(";"):
        if "=" in part:
            name, url = part.split("=", 1)
            out[name.strip()] = url.strip()
    return out


def add_cuda_dlls():
    """On Windows, make the pip-installed CUDA libraries (cuBLAS / cuDNN) visible to faster-whisper."""
    if os.name != "nt":
        return
    for d in glob.glob(os.path.join(sys.prefix, "Lib", "site-packages", "nvidia", "*", "bin")):
        os.add_dll_directory(d)
        os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
