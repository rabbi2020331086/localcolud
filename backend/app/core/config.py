from functools import lru_cache

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
    max_instance_cpus: int = 8
    max_instance_memory_mb: int = 16384
    max_instance_disk_gb: int = 100
    max_total_instances: int = 20
    operation_poll_seconds: float = 1
    operation_worker_concurrency: int = 8
    state_reconcile_seconds: float = 15
    autoscaling_interval_seconds: float = 30
    load_balancer_health_interval_seconds: float = 5
    load_balancer_request_timeout_seconds: float = 30
    tailscale_reusable_auth_key: str = ""
    # Optional. Set only after defining a matching tagOwner in the tailnet policy
    # and generating an auth key permitted to apply that tag.
    tailscale_instance_tag: str = ""
    tailscale_funnel_target_port: int = 80
    one_time_secret_minutes: int = 10
    agent_deploy_code_hours: int = 72
    log_level: str = "INFO"
    node_offline_seconds: int = 45
    node_job_lease_seconds: int = 1200
    node_job_poll_seconds: float = 0.5
    node_job_timeout_seconds: int = 1200
    volume_storage_root: str = "./data/volumes"
    ami_storage_root: str = "./data/amis"
    agent_release_root: str = "./data/agent-releases"

    @model_validator(mode="after")
    def secure_production(self) -> "Settings":
        if self.operation_worker_concurrency < 1:
            raise ValueError("OPERATION_WORKER_CONCURRENCY must be at least 1")
        if not 1 <= self.tailscale_funnel_target_port <= 65535:
            raise ValueError("TAILSCALE_FUNNEL_TARGET_PORT must be a valid TCP port")
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
