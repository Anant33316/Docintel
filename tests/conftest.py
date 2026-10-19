"""Test setup: import paths, environment, and in-memory AWS fakes.

Run from the project root:  ``pytest tests``
(no AWS account, credentials or network needed; boto3 is optional for the unit tests).
"""
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for sub in ("src/shared", "src/api", "src/export", "src/privacy", "tests"):
    p = os.path.join(ROOT, sub)
    if p not in sys.path:
        sys.path.append(p)

os.environ.update(
    TABLE_NAME="docs", APP_TABLE_NAME="app", CHUNKS_TABLE_NAME="chunks", BUCKET_NAME="bucket",
    BEDROCK_MODEL_ID="test-model", EMBEDDING_MODEL_ID="test-embed", CONFIDENCE_THRESHOLD="90",
    AWS_DEFAULT_REGION="us-east-1", CHAT_HISTORY_TTL_DAYS="30", AUDIT_TTL_DAYS="365",
)

from helpers import Env  # noqa: E402


@pytest.fixture
def env():
    e = Env()
    e.install()
    yield e
    e.uninstall()


