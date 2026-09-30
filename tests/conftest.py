import os
import tempfile

import pytest
from fastapi.testclient import TestClient

# 必须在导入 app 之前指向可写的临时路径（startup 时会初始化该库）。
_TMP = tempfile.mkdtemp(prefix="museum-default-")
os.environ.setdefault("DATABASE_PATH", os.path.join(_TMP, "default.db"))

from app import db as db_mod  # noqa: E402
from app.db import get_conn  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    db_mod.init_db(db_path)

    def override_get_conn():
        conn = db_mod.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_conn] = override_get_conn
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
