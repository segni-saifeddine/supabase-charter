import re
from datetime import UTC, date, datetime

# One folder level of a Vault KV path: lowercase, digits, and - _ . inside.
PATH_SEGMENT = re.compile(r"^[a-z0-9]([-a-z0-9_.]*[a-z0-9])?$")
MAX_NAME = 63
MAX_SECRET_PATH = 200


def validate_secret_path(secret_path: str) -> str:
    """The folder under the Vault mount, as in <mount>/<secret_path>/<name>.

    May be nested (`acme/staging`). Rejects anything that could climb out of
    the configured mount or land the secret somewhere other than where it was
    reported to the requester.
    """
    if not secret_path or len(secret_path) > MAX_SECRET_PATH:
        raise ValueError(
            f"secret_path must be 1 to {MAX_SECRET_PATH} characters, got {secret_path!r}."
        )
    if secret_path.startswith("/") or secret_path.endswith("/"):
        raise ValueError(f"secret_path {secret_path!r} must not start or end with '/'.")
    segments = secret_path.split("/")
    for segment in segments:
        if segment in {"", ".", ".."} or not PATH_SEGMENT.fullmatch(segment):
            raise ValueError(
                f"secret_path {secret_path!r} is not a valid Vault path: each folder "
                "must be lowercase letters, digits, '-', '_' or '.'."
            )
    return secret_path


def project_name(kind: str, purpose: str, when: date | None = None) -> str:
    stamp = (when or datetime.now(UTC).date()).strftime("%Y%m%d")
    slug = re.sub(r"[^a-z0-9]+", "-", purpose.lower()).strip("-")
    prefix = f"{kind}-"
    room = MAX_NAME - len(prefix) - 1 - len(stamp)
    if room < 1:
        raise ValueError(f"kind {kind!r} is too long to form a project name.")
    slug = slug[:room].strip("-") or "db"
    return f"{prefix}{slug}-{stamp}"
