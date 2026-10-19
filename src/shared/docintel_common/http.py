"""API Gateway (REST, Lambda proxy) helpers shared by the API-facing functions."""
import json
import logging
import os
import re

from .ddb import json_default

logger = logging.getLogger(__name__)

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def resp(code, body):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json",
                    "Access-Control-Allow-Origin": os.environ.get("CORS_ALLOWED_ORIGIN", "*")},
        "body": json.dumps(body, default=json_default),
    }


def body_of(event):
    raw = event.get("body") or "{}"
    data = json.loads(raw)       # JSONDecodeError handled by the router -> 400
    if not isinstance(data, dict):
        raise ApiError(400, "JSON body must be an object")
    return data


def path_param(event, name):
    value = (event.get("pathParameters") or {}).get(name, "")
    if not SAFE_ID.match(value or ""):
        raise ApiError(400, f"invalid {name}")
    return value


def query(event):
    return event.get("queryStringParameters") or {}


def actor(event, body=None):
    """Caller identity used for chat ownership and audit.

    The stack authenticates with an API key (shared secret), so this is a
    caller-asserted label, not a verified identity; see README "limitations".
    """
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    who = headers.get("x-user-id") or (body or {}).get("userId") or "anonymous"
    who = re.sub(r"[^A-Za-z0-9@._-]", "_", str(who))[:80]
    return who or "anonymous"


def request_id(event):
    return (event.get("requestContext") or {}).get("requestId")


def dispatch(event, routes):
    """Route on (httpMethod, resource). ``routes`` maps that tuple to ``fn(event)``."""
    route = (event.get("httpMethod"), event.get("resource"))
    try:
        fn = routes.get(route)
        if not fn:
            return resp(404, {"error": "not found"})
        return fn(event)
    except ApiError as e:
        return resp(e.status, {"error": e.message})
    except json.JSONDecodeError:
        return resp(400, {"error": "invalid JSON body"})
    except Exception:
        logger.exception("unhandled error on %s %s", *route)
        return resp(500, {"error": "internal error"})
