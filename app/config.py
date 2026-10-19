from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.flats import FLAT_NUMBERS

PORTAL_NAME = "Sri Krishna Mani Flat Owners Portal"
APP_VERSION = "nest5-v7.4"


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

    upi_vpa: str = "9940255076@ybl"
    upi_payee_name: str = "Sri Krishna Mani Flats"
    upi_mobile: str = "9940255076"

    # In-app monthly job (single uvicorn worker): creates the current IST month and recurring-task occurrences.
    monthly_job_enabled: bool = True
    monthly_job_interval_seconds: int = 3600

    instance_dir: str = "instance"
    backup_dir: str = "backups"
    backup_keep: int = 30


settings = Settings()
