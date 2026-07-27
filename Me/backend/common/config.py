
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

    redis_host: str = "redis"
    redis_port: int = 6379

    kafka_brokers: str = "kafka:9092"

    # --- MongoDB ---
    mongo_uri: str = "mongodb://mongo:27017"
    mongo_db: str = "medicore"

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
