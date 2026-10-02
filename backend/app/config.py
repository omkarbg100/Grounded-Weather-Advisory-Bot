import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from root directory or backend directory
BASE_DIR = Path(__file__).resolve().parent.parent.parent
dotenv_path = BASE_DIR / ".env"
if dotenv_path.exists():
    load_dotenv(dotenv_path=dotenv_path)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-flash-latest")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.1"))
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "15.0"))

CORS_ORIGINS_RAW = os.getenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173")
CORS_ORIGINS = [o.strip() for o in CORS_ORIGINS_RAW.split(",") if o.strip()]

OPEN_METEO_FORECAST_URL = os.getenv("OPEN_METEO_FORECAST_URL", "https://api.open-meteo.com/v1/forecast")
OPEN_METEO_GEOCODE_URL = os.getenv("OPEN_METEO_GEOCODE_URL", "https://geocoding-api.open-meteo.com/v1/search")

SOPS_DIR = Path(__file__).resolve().parent / "sops"
