import os
import shutil
from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_name: str = "LocalEC2"
    app_env: str = "development"
    api_prefix: str = "/api/v1"
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_database: str = "stratus"
    mongodb_server_selection_timeout_ms: int = 10000
    jwt_secret: str = "development-only-change-this-secret-before-production"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 15
    refresh_token_days: int = 7
    private_key_encryption_key: str = ""
    frontend_origin: str = "http://localhost:5173"
    admin_frontend_origin: str = "http://localhost:5174"
    platform_admin_email: str = ""
    development_default_admin: bool = True
    multipass_executable: str = "multipass"
    vagrant_executable: str = "vagrant"
    vagrant_provider: str = "libvirt"
    vagrant_instances_root: Path = Path("runtime/vagrant")
    multipass_default_image: str = "24.04"
    max_instance_cpus: int = 8
    max_instance_memory_mb: int = 16384
    max_instance_disk_gb: int = 100
    max_total_instances: int = 20
    operation_poll_seconds: float = 1
    state_reconcile_seconds: float = 15
    autoscaling_interval_seconds: float = 30
    load_balancer_health_interval_seconds: float = 5
    load_balancer_request_timeout_seconds: float = 30
    one_time_secret_minutes: int = 10
    log_level: str = "INFO"
    vm_provider: str = "multipass"
    node_offline_seconds: int = 45
    volumes_root: Path = Path(".stratus-volumes")
    runtime_root: Path = Path("runtime")

    @model_validator(mode="after")
    def secure_production(self) -> "Settings":
        if os.name == "nt" and not shutil.which(self.multipass_executable):
            installed = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / (
                "Multipass/bin/multipass.exe"
            )
            if (
                self.multipass_executable.lower() in {"multipass", "multipass.exe"}
                and installed.is_file()
            ):
                self.multipass_executable = str(installed)
        if self.app_env == "production":
            if self.development_default_admin:
                raise ValueError("DEVELOPMENT_DEFAULT_ADMIN must be false in production")
            if len(self.jwt_secret) < 32 or "development" in self.jwt_secret:
                raise ValueError("JWT_SECRET must be a strong production secret")
            if not self.private_key_encryption_key:
                raise ValueError("PRIVATE_KEY_ENCRYPTION_KEY is required in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
