import os
from collections.abc import Mapping
from pathlib import Path


def resolve_worker_env_file(
    environ: Mapping[str, str] = os.environ,
    cwd: Path | None = None,
) -> Path | None:
    """Return the RCA Worker's dedicated environment file, if it is present."""
    override = environ.get("RCA_WORKER_ENV_FILE")
    if override is not None:
        env_file = Path(override).expanduser()
        if not env_file.is_file():
            raise FileNotFoundError(env_file)
        return env_file

    env_file = (cwd or Path.cwd()) / ".env.rca-worker"
    return env_file if env_file.is_file() else None
