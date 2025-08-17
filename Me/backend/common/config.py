from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    env: str = "local"
    log_level: str = "INFO"

    jwt_secret: str = "change-this-in-prod"
    jwt_alg: str = "HS256"

    postgres_user: str = "medicore"
    postgres_password: str = "medicore_pw"
    postgres_db: str = "medicore"
    postgres_host: str = "postgres"
    postgres_port: int = 5432

    redis_host: str = "redis"
    redis_port: int = 6379

    kafka_brokers: str = "kafka:9092"

    class Config:
        env_file = ".env"

settings = Settings()

# --- Extended settings ---
Settings.mongo_uri = "mongodb://mongo:27017"
Settings.mongo_db = "medicore"

Settings.fhir_base_url = "https://example-fhir-server/fhir"
Settings.fhir_oauth_token_url = "https://example-fhir-server/oauth2/token"
Settings.fhir_client_id = "replace-me"
Settings.fhir_client_secret = "replace-me"

Settings.otel_exporter_otlp_endpoint = "http://jaeger:4318"
Settings.otel_service_namespace = "medicore"
