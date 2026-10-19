"""Lazily created, cached AWS clients and DynamoDB tables.

Clients are created on first use (not at import time) so Lambda cold starts only
pay for what a route actually needs, and so unit tests can ``override`` any
handle with an in-memory fake.
"""
import os

try:  # botocore is always present in Lambda; the fallback only serves offline unit tests
    from botocore.exceptions import ClientError  # noqa: F401
except ImportError:  # pragma: no cover
    class ClientError(Exception):
        def __init__(self, error_response, operation_name):
            super().__init__(f"{operation_name}: {error_response.get('Error', {}).get('Code')}")
            self.response = error_response
            self.operation_name = operation_name

# logical table name -> environment variable holding the physical name
TABLE_ENV = {"docs": "TABLE_NAME", "app": "APP_TABLE_NAME", "chunks": "CHUNKS_TABLE_NAME"}

_cache = {}
_overrides = {}


def _bedrock_config():
    from botocore.config import Config
    # API-side functions must give up before API Gateway's 29 s integration timeout.
    timeout = int(os.environ.get("BEDROCK_READ_TIMEOUT", "25"))
    return Config(read_timeout=timeout, connect_timeout=5, retries={"max_attempts": 2, "mode": "adaptive"})


def client(service):
    if service in _overrides:
        return _overrides[service]
    if service not in _cache:
        import boto3
        if service == "bedrock-runtime":
            _cache[service] = boto3.client(service, config=_bedrock_config())
        else:
            _cache[service] = boto3.client(service)
    return _cache[service]


def table(logical):
    key = f"table:{logical}"
    if key in _overrides:
        return _overrides[key]
    if key not in _cache:
        import boto3
        _cache[key] = boto3.resource("dynamodb").Table(os.environ[TABLE_ENV[logical]])
    return _cache[key]


def new_table(logical):
    """A fresh (uncached) Table for use inside worker threads: boto3 *resources* are not thread-safe."""
    key = f"table:{logical}"
    if key in _overrides:
        return _overrides[key]
    import boto3
    return boto3.session.Session().resource("dynamodb").Table(os.environ[TABLE_ENV[logical]])


def bucket():
    return os.environ["BUCKET_NAME"]


def error_code(exc):
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "")


# ---- test hooks -------------------------------------------------------------
def override(name, obj):
    """``override('s3', fake)`` or ``override('table:docs', fake)``."""
    _overrides[name] = obj


def reset_overrides():
    _overrides.clear()
