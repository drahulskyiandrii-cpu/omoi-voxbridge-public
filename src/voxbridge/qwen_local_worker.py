"""Authenticated VoxBridge launcher for the pinned, read-only Mini worker."""
import hashlib
import hmac
import os
from pathlib import Path
import sys
import types

DISCONNECT_SAFE_SHA = "f82a2ab2dc259ea29494a5d76a907c45268af18c53b86ff8e42c1527189b46b7"

WORKER_SHAS = (
    DISCONNECT_SAFE_SHA,
    "9cb114adee5764abca171f923b93e5a40abad00b607340297d3d836883eda9a9",
    "06096361f93e0f60e20dae68b2c6b69326ae7f14af48a97627a4a34f4b158338",
    "b9b0bfc1edeb077e48fd8d57f735ce6b5eb0aa9f414c12d4e3dbc5e6b14cc642",
    "c6bb704070b4c6cce4fa046d2b13086252ac6ebc829ae0e54341534ef23f280f",
    "210d6daea3f7d3847a1c40a3bb79be384c2c91339f44eee627858b9b7e75fb15",
)

# Import works both as a package and as a standalone isolated launcher.
if __package__:
    from .windows_job_membership import in_named_job
else:
    sys.path.insert(0, str(Path(__file__).parent))
    from windows_job_membership import in_named_job


class AuthenticatedApp:
    def __init__(self, app, token):
        self.app, self.token = app, ("Bearer " + token).encode("ascii")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            if scope["type"] == "lifespan":
                return await self.app(scope, receive, send)
            return
        headers = [v for k,v in scope.get("headers", []) if k.lower() == b"authorization"]
        if len(headers) != 1 or not hmac.compare_digest(headers[0], self.token):
            status = 401
        elif (scope.get("method"), scope.get("path")) not in {
            ("GET", "/health"), ("POST", "/warmup"), ("POST", "/v1/audio/stream")
        }:
            status = 404
        else:
            return await self.app(scope, receive, send)
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-length", b"0"), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": b""})

def main():
    # The venv launcher may spawn another interpreter. Do not serve unless this
    # exact process belongs to the parent's kill-on-close job.
    if sys.stdin.readline() != "GO\n" or not in_named_job(os.environ["VOXBRIDGE_QWEN_JOB"]):
        raise SystemExit("QWEN_JOB_REQUIRED")
    token = os.environ.pop("VOXBRIDGE_QWEN_TOKEN")
    if len(token) < 32:
        raise SystemExit("QWEN_TOKEN_REQUIRED")
    source = Path(sys.argv[1])
    code = source.read_bytes()
    if not any(hmac.compare_digest(hashlib.sha256(code).hexdigest(), sha) for sha in WORKER_SHAS):
        raise SystemExit("QWEN_WORKER_CHANGED")
    module = types.ModuleType("voxbridge_pinned_mini_worker")
    module.__file__ = str(source)
    sys.modules[module.__name__] = module
    exec(compile(code, str(source), "exec"), vars(module))
    import uvicorn
    uvicorn.run(AuthenticatedApp(module.app, token), host="127.0.0.1",
                port=18190, access_log=False, log_level="warning", workers=1,
                ws="none")

if __name__ == "__main__":
    main()
