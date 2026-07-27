
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings.

    Every attribute below is a real pydantic field, so it can be overridden via
    an environment variable of the same name (case-insensitive), e.g.
    ``FHIR_BASE_URL`` -> ``settings.fhir_base_url``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "local"
    log_level: str = "INFO"

    jwt_secret: str = "change-this-in-prod"
    jwt_alg: str = "HS256"

    postgres_user: str = "medicore"
    postgres_password: str = "medicore_pw"
    postgres_db: str = "medicore"
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    database_url: str | None = None
    postgres_min_pool_size: int = 1
    postgres_max_pool_size: int = 10
    postgres_command_timeout_seconds: float = 10.0
    # Cached FHIR rows older than this are purged by the janitor task.
    cache_max_age_seconds: int = 3600
    cache_cleanup_interval_seconds: int = 300

    redis_host: str = "redis"
    redis_port: int = 6379

    kafka_brokers: str = "kafka:9092"

    # --- MongoDB ---
    mongo_uri: str = "mongodb://mongo:27017"
    mongo_db: str = "medicore"
    # Bounded timeouts: the driver otherwise waits ~30s to select a server,
    # turning a brief outage into cascading upstream timeouts.
    mongo_server_selection_timeout_ms: int = 5000
    mongo_connect_timeout_ms: int = 5000
    mongo_socket_timeout_ms: int = 10000
    mongo_min_pool_size: int = 0
    mongo_max_pool_size: int = 50

    # Ward layout seeded on first start. Format: "WARD:COUNT,WARD:COUNT".
    bed_layout: str = "A:8,B:8,ICU:4"

    # --- FHIR ---
    fhir_base_url: str = "https://example-fhir-server/fhir"
    fhir_oauth_token_url: str = "https://example-fhir-server/oauth2/token"
    fhir_client_id: str = "replace-me"
    fhir_client_secret: str = "replace-me"
    fhir_timeout_seconds: float = 10.0

    # --- OIDC / JWKS ---
    oidc_issuer: str = ""
    oidc_jwks_uri: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = "http://localhost:8081/oidc/callback"
    # Optional strict claim validation; empty means "do not verify".
    oidc_audience: str = ""
    oidc_issuer_claim: str = ""

    session_secret: str = "dev-change-me"
    allowed_origins: str = "http://localhost:5173"

    # --- Observability ---
    otel_exporter_otlp_endpoint: str = "http://jaeger:4318"
    otel_service_namespace: str = "medicore"
    otel_enabled: bool = True

    @property
    def parsed_bed_layout(self) -> list[tuple[str, int]]:
        """Parse ``bed_layout`` into (ward, count) pairs."""
        wards: list[tuple[str, int]] = []
        for chunk in self.bed_layout.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            ward, _, raw_count = chunk.partition(":")
            ward = ward.strip()
            if not ward:
                continue
            try:
                count = int(raw_count)
            except ValueError as exc:
                raise ValueError(
                    f"Invalid BED_LAYOUT entry {chunk!r}; expected 'WARD:COUNT'"
                ) from exc
            if count < 0:
                raise ValueError(f"Bed count for ward {ward!r} cannot be negative")
            wards.append((ward, count))
        return wards

    @property
    def is_production(self) -> bool:
        return self.env.lower() not in ("local", "test", "dev", "development")

    @property
    def sqlalchemy_dsn(self) -> str:
        """Postgres DSN, preferring an explicit DATABASE_URL when provided."""
        if self.database_url:
            return self.database_url
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


settings = Settings()
