"""`sglab worker`: run the lab server's processing jobs on another (stronger) computer.

    sglab worker https://nbe.evm100.org --token-file /etc/nbe-worker/token [--name nbe-proc]

Polls /api/worker/claim (long poll, so new jobs start within a second or two), downloads
each stream's raw CSV, runs Layers 1-2 with the server's pipeline configs, uploads the
outputs and records the run on the server. Only outbound HTTPS is needed. Stdlib HTTP
only, so processing needs just the base package (numpy/scipy/pandas/matplotlib).

With the `model` extra installed (scikit-learn, Optuna) it also takes Layer-4 sweeps:
download the dataset, run sglab.sweep, upload the model run folder.

The token belongs to a user with the `worker` role (`sglab user add nbe-worker --role worker`,
then `sglab user token nbe-worker`), which can do nothing but this.
"""
from __future__ import annotations

import json
import socket
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import ppg
from ppg.config import Layer1Config, Layer2Config

from .processing import config_hash, process_stream, stream_configs


class Api:
    def __init__(self, base: str, token: str, name: str, timeout: float = 120):
        self.base, self.timeout = base.rstrip("/"), timeout
        self.headers = {"Authorization": f"Bearer {token}", "X-NBE-Worker": name,
                        "User-Agent": f"sglab-worker/{ppg.__version__}"}
        self.ctx = ssl.create_default_context()

    def _open(self, method: str, path: str, data=None, headers=None, timeout=None):
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={**self.headers, **(headers or {})})
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.timeout, context=self.ctx)
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("error")
            except Exception:
                detail = e.reason
            raise RuntimeError(f"{method} {path}: HTTP {e.code} {detail}") from None

    def json(self, method: str, path: str, body=None, timeout=None):
        data = None if body is None else json.dumps(body).encode()
        with self._open(method, path, data, {"Content-Type": "application/json"}, timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw and r.status != 204 else None

    def download(self, path: str, dest: Path) -> None:
        with self._open("GET", path) as r, dest.open("wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)

    def upload(self, path: str, src: Path) -> None:
        with src.open("rb") as f:
            self._open("PUT", path, f, {"Content-Type": "application/octet-stream",
                                        "Content-Length": str(src.stat().st_size)}).close()


def run_job(api: Api, job: dict, log=print) -> None:
    jid, sid = job["job_id"], job["session_id"]
    base1 = Layer1Config.from_dict(job["configs"]["layer1"])
    base2 = Layer2Config.from_dict(job["configs"]["layer2"])
    sites = job["configs"].get("sites") or {}
    done = {(r["stream"], r["config_hash"]) for r in job.get("ok_runs", [])}
    q = urllib.parse.quote
    errors = []
    for st in job["streams"]:
        name = st["stream"]
        cfg1, cfg2 = stream_configs(base1, base2, sites, st)
        h = config_hash(cfg1, cfg2)
        if not job["force"] and (name, h) in done:
            log(f"  [up-to-date] {sid}/{name}")
            continue
        with tempfile.TemporaryDirectory(prefix="sglab-worker-") as tmp:
            tmp = Path(tmp)
            csv = tmp / Path(st["file"]).name
            out = tmp / "out"
            t = time.monotonic()
            api.download(f"/api/worker/jobs/{jid}/raw/{q(name)}", csv)
            run = {"pipeline_version": ppg.__version__, "config_hash": h}
            try:
                summary = process_stream(csv, st, cfg1, cfg2, out)
            except Exception as e:  # record the failure, continue with other streams (as local processing)
                api.json("POST", f"/api/worker/jobs/{jid}/runs/{q(name)}",
                         {**run, "status": "error", "summary": {"error": f"{type(e).__name__}: {e}"}})
                log(f"  [error] {sid}/{name}: {e}")
                errors.append(f"{name}: {e}")
                continue
            for f in sorted(out.iterdir()):
                if f.is_file():
                    api.upload(f"/api/worker/jobs/{jid}/out/{q(name)}/{q(f.name)}", f)
            api.json("POST", f"/api/worker/jobs/{jid}/runs/{q(name)}", {**run, "status": "ok", "summary": summary})
            s2 = summary["layer2"]
            log(f"  [ok] {sid}/{name}: {s2.get('n_good_beats', 0)}/{s2.get('n_beats', 0)} good beats, "
                f"HR {s2.get('median_hr_bpm') or float('nan'):.1f} bpm ({time.monotonic() - t:.1f} s)")
    api.json("POST", f"/api/worker/jobs/{jid}/finish", {"error": "; ".join(errors) or None})


def run_sweep_job(api: Api, job: dict, log=print) -> None:
    from ppg.config import Layer4Config

    from .model import read_dataset
    from .sweep import sweep_to

    jid, pl = job["job_id"], job["payload"]
    last = [0.0]

    def progress(done, total):  # also keeps the lease alive
        if done == total or time.monotonic() - last[0] > 15:
            last[0] = time.monotonic()
            api.json("POST", f"/api/worker/jobs/{jid}/progress", {"done": done, "total": total})

    with tempfile.TemporaryDirectory(prefix="sglab-sweep-") as tmp:
        tmp = Path(tmp)
        ds = tmp / "dataset"
        ds.mkdir()
        api.download(f"/api/worker/jobs/{jid}/dataset/rows.csv", ds / "rows.csv")
        try:
            api.download(f"/api/worker/jobs/{jid}/dataset/manifest.json", ds / "manifest.json")
        except RuntimeError:
            pass
        df, raw, manifest = read_dataset(ds, pl["dataset"])
        try:
            meta = sweep_to(tmp / "runs", pl["name"], df, raw, manifest, pl["dataset"],
                            Layer4Config.from_dict(pl["layer4"]), pl["spec"], log, progress)
        except ValueError as e:  # not enough data for this config: a result, not a worker failure
            log(f"  [error] sweep {pl['name']}: {e}")
            api.json("POST", f"/api/worker/jobs/{jid}/finish", {"error": str(e)})
            return
        out = Path(meta["path"])
        for f in sorted(out.iterdir()):
            api.upload(f"/api/worker/jobs/{jid}/out/model/{urllib.parse.quote(f.name)}", f)
        api.json("POST", f"/api/worker/jobs/{jid}/model", {"run_id": meta["run_id"]})
    log(f"  [ok] sweep {pl['name']}/{meta['run_id']}")
    api.json("POST", f"/api/worker/jobs/{jid}/finish", {"error": None})


def can_sweep() -> bool:
    try:
        import optuna  # noqa: F401
        import sklearn  # noqa: F401
    except ImportError:
        return False
    return True


def serve(base: str, token: str, name: str | None = None, once: bool = False, log=print) -> int:
    name = name or socket.gethostname()
    api = Api(base, token, name)
    kinds = "process,sweep" if can_sweep() else "process"
    log(f"sglab worker {name} (ppg {ppg.__version__}) -> {api.base}  [jobs: {kinds}]"
        + ("" if can_sweep() else "  (pip install -e '.[model]' for Layer-4 sweeps)"))
    backoff = 5
    while True:
        try:
            job = api.json("POST", f"/api/worker/claim?wait=25&kinds={kinds}", timeout=60)
            backoff = 5
        except (OSError, RuntimeError) as e:  # server restarting, network down, ...
            log(f"  claim failed: {e}; retrying in {backoff} s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
            continue
        if job is None:
            if once:
                return 0
            continue
        try:
            if job.get("kind") == "sweep":
                log(f"job {job['job_id']}: sweep {job['payload']['name']} on dataset {job['payload']['dataset']}")
                run_sweep_job(api, job, log)
            else:
                log(f"job {job['job_id']}: session {job['session_id']}{' (force)' if job['force'] else ''}")
                run_job(api, job, log)
        except Exception as e:  # report it; if the server is unreachable, the lease expires and it is retried
            log(f"  job {job['job_id']} aborted: {type(e).__name__}: {e}")
            try:
                api.json("POST", f"/api/worker/jobs/{job['job_id']}/finish", {"error": f"worker: {e}"})
            except Exception:
                pass
            time.sleep(5)
