"""User-configured standing cloud permissions. Never triggers requests."""
from dataclasses import asdict, dataclass
import json
from pathlib import Path

from .config import config_path, write_json_atomic


@dataclass(frozen=True)
class CloudPermissions:
    dictation_openai: bool = False
    analysis_openai: bool = False
    analysis_anthropic: bool = False
    vision_openai: bool = False
    tts_cartesia: bool = False
    captions_openai_audio: bool = False
    captions_openai_translation: bool = False

    def validate(self):
        if any(type(v) is not bool for v in asdict(self).values()):
            raise ValueError("CLOUD_PERMISSION_INVALID")
        return self


SCOPES = tuple(CloudPermissions.__dataclass_fields__)


def permission_path():
    return config_path().with_name("cloud_permissions.json")


def load_permissions(path: Path | None = None) -> CloudPermissions:
    path = permission_path() if path is None else path
    if not path.exists():
        return CloudPermissions()
    with path.open("rb") as reader:
        raw = reader.read(4097)
    if len(raw) > 4096:
        raise ValueError("CLOUD_PERMISSION_FILE_TOO_LARGE")
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) - set(SCOPES):
        raise ValueError("CLOUD_PERMISSION_INVALID")
    return CloudPermissions(**data).validate()


def save_permissions(value: CloudPermissions, path: Path | None = None):
    value.validate()
    write_json_atomic(permission_path() if path is None else path, asdict(value))


def standing_permission(scope: str) -> bool:
    """Read on each user action, so revocation applies without restart."""
    if scope not in SCOPES:
        return False
    try:
        return getattr(load_permissions(), scope) is True
    except (OSError, ValueError, TypeError):
        return False


def approved(checker, scope: str) -> bool:
    if checker is None or scope not in SCOPES:
        return False
    try:
        return checker(scope) is True
    except Exception:
        return False
