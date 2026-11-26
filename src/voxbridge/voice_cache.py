"""Local Cartesia metadata cache; never stores audio, embeddings or API keys."""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
from .cartesia_voices import OwnedVoice, valid_voice_id
from .config import config_path, write_json_atomic


def _scope(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest() if isinstance(key,str) and key.strip() else ""


def _validate(voices):
    if not isinstance(voices,list) or len(voices)>400:
        raise ValueError("VOICE_CACHE_INVALID")
    ids=set()
    for v in voices:
        if (not isinstance(v,OwnedVoice) or not valid_voice_id(v.voice_id)
                or not isinstance(v.name,str) or not 1<=len(v.name)<=100
                or not isinstance(v.language,str) or not 1<=len(v.language)<=20
                or v.voice_id in ids or any(ord(c)<32 for c in v.name+v.language)):
            raise ValueError("VOICE_CACHE_INVALID")
        ids.add(v.voice_id)
    return voices


def load_voice_cache(path=None, *, key=None):
    path=Path(path) if path is not None else config_path().with_name("cartesia_voices.json")
    scope=_scope(os.environ.get("CARTESIA_API_KEY","") if key is None else key)
    if not scope or not path.is_file():
        return []
    try:
        with path.open("rb") as f:
            raw=f.read(262145)
        if len(raw)>262144:
            return []
        data=json.loads(raw)
        if not isinstance(data,dict) or data.get("scope")!=scope:
            return []
        rows=data.get("voices")
        if not isinstance(rows,list) or len(rows)>400:
            return []
        return _validate([OwnedVoice(**row) for row in rows])
    except (OSError,ValueError,TypeError,UnicodeError):
        return []


def save_voice_cache(voices,path=None,*,key=None):
    scope=_scope(os.environ.get("CARTESIA_API_KEY","") if key is None else key)
    if not scope:
        return
    _validate(voices)
    path=Path(path) if path is not None else config_path().with_name("cartesia_voices.json")
    write_json_atomic(path,{"scope":scope,"voices":[asdict(v) for v in voices]})
