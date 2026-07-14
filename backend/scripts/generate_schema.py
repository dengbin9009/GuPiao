from __future__ import annotations

from pathlib import Path

from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.dialects import mysql

from app.database import Base
from app import models  # noqa: F401


def render_schema() -> str:
    dialect = mysql.dialect()
    statements = [
        "-- Generated from app.models. Regenerate with: python scripts/generate_schema.py",
        "SET NAMES utf8mb4;",
        "SET FOREIGN_KEY_CHECKS = 0;",
    ]
    for table in Base.metadata.sorted_tables:
        statements.append(str(CreateTable(table).compile(dialect=dialect)).rstrip() + ";")
        for index in table.indexes:
            statements.append(str(CreateIndex(index).compile(dialect=dialect)).rstrip() + ";")
    statements.append("SET FOREIGN_KEY_CHECKS = 1;")
    rendered = "\n\n".join(statements) + "\n"
    return "\n".join(line.rstrip() for line in rendered.splitlines()) + "\n"


if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / "migrations" / "0001_initial.sql"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_schema(), encoding="utf-8")
    print(f"wrote {target}")
