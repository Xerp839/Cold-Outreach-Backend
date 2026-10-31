"""Run the pipeline once from your machine:  python -m app.run_once"""

import json

from app import models  # noqa: F401
from app.db import Base, engine, session_scope
from app.models import Run
from app.pipeline.runner import run_pipeline, start_run

if __name__ == "__main__":
    Base.metadata.create_all(engine)
    run_id = start_run("manual")
    if run_id is None:
        raise SystemExit("a run is already in progress")
    run_pipeline(run_id)
    with session_scope() as s:
        run = s.get(Run, run_id)
        print(json.dumps({"run_id": run_id, "status": run.status, "error": run.error, "stats": run.stats}, indent=2))
