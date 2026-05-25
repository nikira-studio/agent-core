from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.branding import DB_FILENAME, ENV_PREFIX


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX, env_file=".env", extra="ignore"
    )

    PORT: int = 3500
    DATA_PATH: str = "./data"
    ENCRYPTION_KEY: str = "auto"
    SESSION_DURATION_HOURS: int = 8
    INACTIVITY_TIMEOUT_MINUTES: int = 30
    SHARED_SCOPE_AGENTS: str = ""
    TRUSTED_PROXIES: str = ""
    STALE_THRESHOLD_MINUTES: int = 5
    COOKIE_SECURE: bool = False
    ALLOWED_INTERNAL_HOSTS: str = ""

    @property
    def data_dir(self) -> Path:
        path = Path(self.DATA_PATH)
        if not path.is_absolute():
            path = (Path(__file__).parent.parent / path).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_FILENAME

    @property
    def credential_key_path(self) -> Path:
        return self.data_dir / "credential.key"

    @property
    def shared_scope_agent_list(self) -> list[str]:
        if not self.SHARED_SCOPE_AGENTS:
            return []
        return [a.strip() for a in self.SHARED_SCOPE_AGENTS.split(",") if a.strip()]

    @property
    def trusted_proxy_list(self) -> list[str]:
        if not self.TRUSTED_PROXIES:
            return []
        return [
            proxy.strip() for proxy in self.TRUSTED_PROXIES.split(",") if proxy.strip()
        ]

    @property
    def allowed_internal_host_set(self) -> set[str]:
        if not self.ALLOWED_INTERNAL_HOSTS:
            return set()
        return {h.strip().lower() for h in self.ALLOWED_INTERNAL_HOSTS.split(",") if h.strip()}


settings = Settings()
