"""Settings for the ERP Data API. Values come from environment variables / .env (prefix DATA_API_)."""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="DATA_API_", extra="ignore")

    # "sql"     -> run the stored procedures on the client database (production)
    # "fixture" -> read the sample Excel exports from fixture_dir (local development only)
    data_source: Literal["sql", "fixture"] = "sql"
    fixture_dir: str = "../dev/fixtures"

    # How Z-Auth-Token + Instance_Id are turned into a client DB connection.
    # "appfabric"  -> ask the ERP's connection API (validates the token, returns DB details)  [production]
    # "static"     -> instances.json mapping (development / single-client testing)
    resolver: Literal["appfabric", "static", "tokenstore"] = "appfabric"
    # The static resolver (instances.json) and fixture data are for development only and refuse to run
    # unless this is set explicitly.
    allow_dev_modes: bool = False
    instances_file: str = "instances.json"

    # ---- appfabric resolver: the ERP's internal "connection details" API
    conn_api_url: str = ""                 # full URL of the endpoint
    conn_api_method: Literal["GET", "POST"] = "GET"
    conn_api_key_header: str = "X-Internal-Key"
    conn_api_key: str = ""                 # shared service secret, if the endpoint requires one
    conn_api_timeout_seconds: float = 15
    conn_cache_seconds: int = 600          # how long a resolved connection is reused per token+company
    # Optional exact field names in the connection API response (blank = auto-detect common names)
    conn_field_server: str = ""
    conn_field_database: str = ""
    conn_field_user: str = ""
    conn_field_password: str = ""
    sql_encrypt: bool = True               # ODBC Encrypt=yes. Never turn off in production.
    # Verify the SQL Server's TLS certificate. Set true ONLY for servers with self-signed certificates;
    # it lets anyone on the network path impersonate the server.
    sql_trust_server_certificate: bool = False
    enable_docs: bool = False              # /docs and /openapi.json (keep off in production)

    odbc_driver: str = "ODBC Driver 18 for SQL Server"
    sql_timeout_seconds: int = 120

    cache_ttl_seconds: int = 300
    cache_max_entries: int = 64

    max_range_days: int = 731
    max_page_size: int = 5000
    default_top_n: int = 20

    # Comma-separated list of browser origins allowed to call the API (e.g. UniReport). Empty = no CORS.
    cors_origins: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
