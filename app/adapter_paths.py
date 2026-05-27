from pathlib import Path

from app.config import settings


SYSTEM_ADAPTER_DIR = Path(__file__).resolve().parent / "adapter_templates"


def get_user_adapter_dir() -> Path:
    return settings.data_dir / "adapters"

