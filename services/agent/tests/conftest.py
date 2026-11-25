import os
import subprocess

import pytest

TEST_DB = "cypress_test"
os.environ["DATABASE_URL"] = f"postgresql:///{TEST_DB}"
os.environ["CYPRESS_ENV"] = "dev"
os.environ["CYPRESS_DEV_USER"] = "tester@example.com"


@pytest.fixture(scope="session")
def client():
    subprocess.run(["dropdb", "--if-exists", TEST_DB], check=True)
    subprocess.run(["createdb", TEST_DB], check=True)
    from fastapi.testclient import TestClient

    from cypress.main import app

    with TestClient(app) as c:
        yield c
