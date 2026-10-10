from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.flats import FLAT_NUMBERS

PORTAL_NAME = "Sri Krishna Mani Flat Owners Portal"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    admin_password: str = "nest5admin"
    secret_key: str = "dev-secret-change-me"
    database_url: str = "sqlite:///./nest5.db"
    maintenance_fee: int = 2000
    flat_numbers: list[str] = FLAT_NUMBERS

    admin_recovery_email: str = "vveerava@gmail.com"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    # Public URL used in emailed links; falls back to the URL the request came in on.
    app_base_url: str = ""
    reset_token_ttl_minutes: int = 60

    instance_dir: str = "instance"
    backup_dir: str = "backups"
    backup_keep: int = 30


settings = Settings()
