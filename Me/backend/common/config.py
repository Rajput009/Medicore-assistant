
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Values shipped in the repository. If any of these survive into a production
# deployment the system is trivially compromised: the JWT secret in particular
# lets anyone mint a valid admin token.
INSECURE_DEFAULTS: frozenset[str] = frozenset(
    {
        "change-this-in-prod",
        "dev-change-me",
        "medicore_pw",
        "replace-me",
        "medicore-dev",
        "REPLACE_ME",
        "changeme",
        "secret",
        "password",
    }
)

MIN_SECRET_LENGTH = 32


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

    # --- Audit trail (HIPAA 164.312(b)) ---
    # Patient identifiers in audit records are pseudonymised by default so the
    # log stream carries no raw PHI. Enable raw ids only when the sink is an
    # access-controlled, HIPAA-compliant store.
    audit_log_raw_identifiers: bool = False
    # Dedicated salt; falls back to jwt_secret when unset.
    audit_log_salt: str = ""

    # --- Abuse protection ---
    rate_limit_per_minute: int = 120
    # Login is the credential-stuffing target, so it gets a tighter budget.
    login_rate_limit_per_minute: int = 10
    max_request_body_bytes: int = 1_048_576
    enable_hsts: bool = True
    allowed_origins: str = "http://localhost:5173"

    # --- Observability ---
    otel_exporter_otlp_endpoint: str = "http://jaeger:4318"
    otel_service_namespace: str = "medicore"
    otel_enabled: bool = True

    @model_validator(mode="after")
    def _reject_insecure_production_config(self) -> "Settings":
        """Refuse to start a production deployment with placeholder secrets.

        Failing at startup is the only safe option: a service that boots with
        the published default signing key silently accepts forged admin tokens,
        and nothing downstream would ever notice.
        """
        if not self.is_production:
            return self

        problems: list[str] = []

        def check(name: str, value: str, *, min_length: int = MIN_SECRET_LENGTH) -> None:
            if not value or value in INSECURE_DEFAULTS:
                problems.append(f"{name} is unset or still a placeholder value")
            elif len(value) < min_length:
                problems.append(
                    f"{name} must be at least {min_length} characters "
                    f"(got {len(value)})"
                )

        check("JWT_SECRET", self.jwt_secret)
        check("SESSION_SECRET", self.session_secret)
        # Only enforced when not supplied via DATABASE_URL.
        if not self.database_url:
            check("POSTGRES_PASSWORD", self.postgres_password, min_length=12)

        if self.fhir_client_secret in INSECURE_DEFAULTS:
            problems.append("FHIR_CLIENT_SECRET is still a placeholder value")

        # A wildcard origin with credentials allows any site to drive the API
        # using a signed-in clinician's session.
        if "*" in self.cors_origins:
            problems.append("ALLOWED_ORIGINS must not be '*' in production")

        if problems:
            raise ValueError(
                "Refusing to start with an insecure production configuration:\n  - "
                + "\n  - ".join(problems)
            )
        return self

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
