from __future__ import annotations

import json

from app.config import get_settings
from app.database import Base, SessionLocal, apply_runtime_migrations, engine
from app.simulation_runtime import prepare_simulation_runtime


def main() -> None:
    Base.metadata.create_all(bind=engine)
    apply_runtime_migrations()
    with SessionLocal() as db:
        result = prepare_simulation_runtime(db, get_settings())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
