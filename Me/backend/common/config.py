
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
    # Short-lived access tokens limit the blast radius of a stolen JWT.
    # 15 minutes is the default; raise only with a documented risk acceptance.
    access_token_ttl_minutes: int = 15

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
    redis_db: int = 0
    redis_password: str = ""
    # When False, rate-limit and revocation stay in-process (tests / single pod).
    # Default False so a missing Redis never blocks local boot; production
    # ConfigMap sets REDIS_ENABLED=true so budgets and denylists are shared.
    redis_enabled: bool = False
    redis_socket_timeout_seconds: float = 0.5

    kafka_brokers: str = "kafka:9092"
    # httpOnly session cookie name used by the auth service. The SPA never
    # needs to read the raw JWT when cookie sessions are enabled.
    auth_cookie_name: str = "medicore_session"
    # Emit the access token as an httpOnly Secure cookie on login/OIDC callback.
    # The JSON body still carries access_token for non-browser clients.
    auth_set_cookie: bool = True

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

    # --- Queryable audit index ---
    # Mirrors audit records into Postgres so "who viewed MRN-X?" is answerable
    # without a log-aggregation round trip. Writes are queued and batched off
    # the request path; see backend/common/audit_store.py.
    audit_index_enabled: bool = True
    # Bounded queue: an unbounded one would trade a counted drop for an OOM.
    audit_index_queue_size: int = 5000
    audit_index_batch_size: int = 200
    audit_index_flush_interval_seconds: float = 1.0
    # HIPAA 164.316(b)(2) requires six years of documentation retention.
    # 0 disables purging entirely (retain forever / handled externally).
    audit_retention_days: int = 2555
    audit_purge_interval_seconds: int = 86_400

    # --- Break-glass (emergency scope override) ---
    # Lets an in-scope clinician reach a patient outside their ward/department
    # in an emergency, with a mandatory reason recorded at WARNING and indexed
    # for review. Widens scope only — never role. Set false to disable, in
    # which case the header is rejected rather than ignored.
    break_glass_enabled: bool = True

    # --- Abuse protection ---
    rate_limit_per_minute: int = 120
    # Login is the credential-stuffing target, so it gets a tighter budget.
    login_rate_limit_per_minute: int = 10
    max_request_body_bytes: int = 1_048_576
    enable_hsts: bool = True
    allowed_origins: str = "http://localhost:5173"
    # Comma-separated Host header allow-list. Empty disables the check (local
    # only). In production this MUST be set so Host-header attacks cannot
    # poison absolute URL generation or cache keys.
    trusted_hosts: str = ""
    # Interactive OpenAPI docs (/docs, /redoc, /openapi.json). Forced off in
    # production regardless of this flag — the schema is a free attack map.
    expose_api_docs: bool = False
    # Username/password demo login. Forced off in production. Outside
    # production it still requires an explicit opt-in via ENABLE_DEMO_LOGIN
    # or ENV=local/test so a mis-set ENV cannot re-enable a shared password.
    enable_demo_login: bool = False

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

        if not self.trusted_hosts_list:
            problems.append(
                "TRUSTED_HOSTS must be set in production "
                "(comma-separated hostnames, e.g. 'api.hospital.example,localhost')"
            )

        if self.enable_demo_login:
            problems.append(
                "ENABLE_DEMO_LOGIN cannot be true in production; use OIDC SSO"
            )

        if self.access_token_ttl_minutes <= 0 or self.access_token_ttl_minutes > 60:
            problems.append(
                "ACCESS_TOKEN_TTL_MINUTES must be between 1 and 60 in production "
                f"(got {self.access_token_ttl_minutes})"
            )

        # Production without OIDC leaves no legitimate way to obtain a token
        # once demo login is forced off. Fail closed rather than shipping a
        # locked-out auth service.
        oidc_ready = bool(
            (self.oidc_issuer or self.oidc_jwks_uri)
            and self.oidc_client_id
            and self.oidc_client_secret
        )
        if not oidc_ready:
            problems.append(
                "OIDC_ISSUER (or OIDC_JWKS_URI), OIDC_CLIENT_ID and "
                "OIDC_CLIENT_SECRET must be configured in production"
            )

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

    @property
    def trusted_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.trusted_hosts.split(",") if h.strip()]

    @property
    def demo_login_allowed(self) -> bool:
        """True only when the username/password stub is safe to expose.

        Production is always False. Outside production the operator must still
        opt in (ENV=local/test, or ENABLE_DEMO_LOGIN=true) so a forgotten flag
        on a staging box does not silently re-enable a shared password.
        """
        if self.is_production:
            return False
        if self.env.lower() in ("local", "test"):
            return True
        return bool(self.enable_demo_login)


settings = Settings()
