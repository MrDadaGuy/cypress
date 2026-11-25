"""Logging setup.

On Cloud Run, one JSON object per line on stdout: Cloud Logging reads `severity` and `message`,
and Error Reporting groups any ERROR entry that carries a stack trace. Locally, plain text.
"""

import json
import logging
import os
import sys
import traceback


class _CloudJson(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {"severity": record.levelname, "logger": record.name, "message": record.getMessage()}
        if record.exc_info:
            # Error Reporting wants the traceback inside `message` (or `stack_trace`).
            entry["message"] += "\n" + "".join(traceback.format_exception(*record.exc_info))
        try:  # group the line with its request's trace in Cloud Logging
            from .tracing import current_ids, project
            tid, sid, sampled = current_ids()
            if tid:
                entry["logging.googleapis.com/trace"] = f"projects/{project()}/traces/{tid}"
                entry["logging.googleapis.com/spanId"] = sid
                entry["logging.googleapis.com/trace_sampled"] = sampled
        except Exception:
            pass
        extra = getattr(record, "json_fields", None)
        if extra:
            entry.update(extra)
        return json.dumps(entry, default=str)


def setup() -> None:
    handler = logging.StreamHandler(sys.stdout)
    if os.environ.get("K_SERVICE") or os.environ.get("CLOUD_RUN_JOB"):
        handler.setFormatter(_CloudJson())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
