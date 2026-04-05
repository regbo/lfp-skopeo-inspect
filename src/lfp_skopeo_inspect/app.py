import hashlib
import json
import logging
import os
import pathlib
import subprocess
import tempfile
from typing import Any, Callable

import diskcache as dc
import filelock
from dynaconf import Dynaconf
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

app = FastAPI()

settings = Dynaconf(
    environments=True,
    envvar_prefix=False,
    load_dotenv=True,
    settings_files=[],
    secrets_dir=os.environ.get("DYNACONF_SECRETS_DIR", "/run/secrets"),
    env_switcher="ENV_FOR_DYNACONF",
)


def read_conf(
    request: Request | None,
    key: str,
    default=None,
    *mappers: Callable[[Any], [Any]],
):
    def _normalize_key(key):
        if key:
            key = key.lower().replace(".", "_").replace("-", "_").strip()
        return key or ""

    def _normalize_value(value):
        for v in [value, default]:
            for mapper in mappers:
                if v is None:
                    break
                v = mapper(v)
                if isinstance(v, str):
                    v = v.strip() or None
            if v is not None:
                return v
        return None

    _nk = None
    sources = [settings]
    if request:
        sources = [request.query_params] + sources
    for source in sources:
        value = _normalize_value(source.get(key))
        if value:
            return value
        if _nk is None:
            _nk = _normalize_key(key)
        for k, v in source.items():
            if _normalize_key(k) == _nk:
                value = _normalize_value(v)
                if value:
                    return value

    return _normalize_value(default)


def to_bool(value):
    if value is not None:
        if isinstance(value, bool):
            return value
        value = str(value).strip().lower()
        if value in {"1", "true", "yes", "y", "on"}:
            return True
    return False


logging.basicConfig(
    level=logging.DEBUG if read_conf(None, "debug", False, to_bool) else logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

disk_cache_expire = read_conf(None, "disk_cache_expire", 5, lambda x: float(x))
disk_cache_dir = (
    read_conf(
        None,
        "disk_cache_dir",
        pathlib.Path(tempfile.gettempdir()) / "skopeo-inspect-v2",
        pathlib.Path,
    )
    or ()
)
disk_cache = dc.Cache(disk_cache_dir)


def read_disk_cache(key: str, loader: Callable[[...], [Any]]) -> Any:
    value = disk_cache.get(key)
    if value is None:
        lock_file = disk_cache_dir / f"{hashlib.md5(key.encode()).hexdigest()}.lock"
        try:
            with filelock.FileLock(lock_file):
                value = disk_cache.get(key)
                if value is None:
                    value = loader()
                    disk_cache.set(key, value, expire=disk_cache_expire)

        finally:
            try:
                lock_file.unlink()
            except Exception:
                pass
    return value


@app.get("/")
async def root(request: Request):
    cmd = ["skopeo", "inspect"]
    try:
        image = read_conf(request, "image")
        if not image:
            return JSONResponse(status_code=400, content={"error": "image is required"})

        transport = read_conf(request, "transport", "docker")
        tls_verify = read_conf(request, "tls_verify", True, to_bool)
        creds = read_conf(request, "creds")
        token = read_conf(request, "registry_token")
        raw = read_conf(request, "raw", False, to_bool)
        debug = read_conf(request, "debug", False, to_bool)
        os_val = read_conf(request, "os", "linux")
        arch_val = read_conf(request, "architecture", "amd64")

        cmd += [f"--override-os={os_val}"]
        cmd += [f"--override-arch={arch_val}"]
        cmd += [f"--tls-verify={str(tls_verify).lower()}"]
        if creds:
            cmd += [f"--creds={creds}"]
        if token:
            cmd += [f"--registry-token={token}"]
        if raw:
            cmd += ["--raw"]
        if debug:
            cmd += ["--debug"]
        cmd.append(f"{transport}://{image}")
        cmd_str = " ".join(cmd)

        def _inspect() -> dict[str, Any]:
            logging.debug("inspect requested - commands:%s", cmd_str)
            proc = subprocess.run(cmd, capture_output=True, text=True)
            stdout = proc.stdout
            stderr = proc.stderr
            return_code = proc.returncode

            media_type_json = "application/json"

            if return_code != 0:
                logging.debug(
                    "error - cmd:%s return_code:%s stderr:%s stdout:%s",
                    cmd,
                    return_code,
                    stderr,
                    stdout,
                )
                result = {
                    "media_type": media_type_json,
                    "status_code": 502,
                    "content": {
                        "error": stderr.strip() or stdout.strip() or "inspect failed",
                        "return_code": return_code,
                    },
                }
            else:
                result = {
                    "media_type": "text/plain" if raw else media_type_json,
                    "content": stdout if raw else json.loads(stdout),
                }
            logging.debug(
                "inspect complete - commands:%s return_code:%s result:%s",
                cmd_str,
                return_code,
                result,
            )
            return result

        result = read_disk_cache(cmd_str, _inspect)
        return Response(**result) if raw else JSONResponse(**result)

    except Exception as e:
        logging.debug("error - cmd:%s", cmd, e)
        return JSONResponse(status_code=502, content={"error": str(e)})


if __name__ == "__main__":
    host = settings.get("host") or "0.0.0.0"
    port = int(settings.get("port") or 8000)

    import uvicorn

    logging.info(f"starting server on {host}:{port}")
    uvicorn.run("app:app", host=host, port=port)
