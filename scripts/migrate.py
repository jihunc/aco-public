"""Explicit local migration; never resets existing data or prints the connection secret."""

import os
from pathlib import Path

from alembic.config import Config
from dotenv import dotenv_values
from sqlalchemy import URL

from alembic import command

values = {**dotenv_values(Path(".env")), **os.environ}
url = (
    values.get("ACO_TEST_DATABASE_URL")
    or values.get("DATABASE_URL")
    or URL.create(
        "postgresql+psycopg",
        username="aco",
        password=values.get("DATABASE_PASSWORD"),
        host="127.0.0.1",
        port=55432,
        database="aco",
    ).render_as_string(hide_password=False)
)
config = Config("alembic.ini")
config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
command.upgrade(config, "head")
