"""Remote processing: job queue, /api/worker/*, and the `sglab worker` client against the app."""
import json
import shutil
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from sglab.auth import AuthStore  # noqa: E402
from sglab.catalog import Catalog  # noqa: E402
from sglab.jobs import JobQueue  # noqa: E402
from sglab.recorder import Recording  # noqa: E402
from sglab.settings import load_settings  # noqa: E402
from sglab.worker import Api, run_job  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
ORIGIN = {"Origin": "http://testserver"}
PW = "correct horse battery"


class ClientApi(Api):
    """sglab.worker.Api over a TestClient instead of urllib."""

    def __init__(self, client, token, name):
        super().__init__("http://testserver", token, name)
        self.c = client

    def _check(self, r):
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} {r.text}")
        return r

    def json(self, method, path, body=None, timeout=None):
        r = self._check(self.c.request(method, path, json=body, headers=self.headers))
        return None if r.status_code == 204 else r.json()

    def download(self, path, dest):
        dest.write_bytes(self._check(self.c.get(path, headers=self.headers)).content)

    def upload(self, path, src):
        self._check(self.c.put(path, content=src.read_bytes(), headers=self.headers))


@pytest.fixture
def remote(tmp_path):
    shutil.copytree(REPO / "config", tmp_path / "config")
    (tmp_path / "config" / "server.toml").write_text('[auth]\nenabled = true\n[processing]\nmode = "remote"\n')
    s = load_settings(tmp_path)
    s.data_root.mkdir(parents=True, exist_ok=True)
    a = AuthStore(s.db_path)
    a.add_user("ada", PW, "admin")
    a.add_user("nbe-worker", PW, "worker")
    tok = a.create_token("nbe-worker", "test")
    cat = Catalog(s.db_path, s.data_root)
    rec = Recording(s, [s.device("sim")])
    rec.start({"subject_id": "S001", "protocol": "rest"})
    time.sleep(25)
    rec.stop()
    dest, _ = rec.finalize({"subject_id": "S001", "protocol": "rest", "trial": 1, "keep": True})
    sid = cat.upsert_session(dest)
    from sglab.web.app import create_app
    return s, create_app(s), tok, sid


def test_remote_processing_end_to_end(remote):
    s, app, tok, sid = remote
    ada = TestClient(app)
    ada.post("/login", data={"username": "ada", "password": PW}, headers=ORIGIN)
    assert ada.post(f"/sessions/{sid}/process", headers=ORIGIN, follow_redirects=False).status_code == 303
    assert ada.post(f"/sessions/{sid}/process", headers=ORIGIN, follow_redirects=False).status_code == 303
    assert [j["state"] for j in JobQueue(s.db_path).recent()] == ["queued"]  # deduplicated
    assert "no processing worker online" in ada.get(f"/sessions/{sid}").text

    c = TestClient(app)
    w = ClientApi(c, tok, "big-box")
    # the worker role can't browse anything, and needs to name itself
    assert c.get("/api/sessions", headers=w.headers).status_code == 403
    assert c.post("/api/worker/claim", headers={"Authorization": f"Bearer {tok}"}).status_code == 400

    assert w.json("GET", "/api/worker/ping")["pending"] == 1
    job = w.json("POST", "/api/worker/claim")
    assert job["session_id"] == sid and len(job["streams"]) == 1 and job["configs"]["layer1"]
    assert w.json("POST", "/api/worker/claim") is None  # nothing else queued
    stream = job["streams"][0]["stream"]
    other = ClientApi(c, tok, "someone-else")
    with pytest.raises(RuntimeError, match="409"):
        other.download(f"/api/worker/jobs/{job['job_id']}/raw/{stream}", s.data_root / "x.csv")
    assert "running on big-box" in ada.get(f"/api/sessions/{sid}").json()["processing"]

    run_job(w, job, log=lambda *_: None)
    x = ada.get(f"/api/sessions/{sid}").json()
    assert x["processing"] is None
    r = x["runs"][0]
    assert r["status"] == "ok" and r["summary"]["worker"] == "big-box"
    assert abs(r["summary"]["layer2"]["median_hr_bpm"] - 72) < 3
    assert (s.data_root / r["out_dir"] / f"{r['summary']['stem']}_signals.csv").is_file()
    assert not (s.processed_dir / "_staging").exists() or not any((s.processed_dir / "_staging").iterdir())
    sig = ada.get(f"/api/sessions/{sid}/signal", params={"stream": stream}).json()
    assert len(sig["t"]) > 1000
    assert ada.get(f"/sessions/{sid}").status_code == 200

    # same configs again -> the worker skips the stream; force re-runs it
    ada.post(f"/sessions/{sid}/process", headers=ORIGIN)
    logs = []
    run_job(w, w.json("POST", "/api/worker/claim"), log=logs.append)
    assert any("up-to-date" in m for m in logs)
    ada.post(f"/sessions/{sid}/process?force=true", headers=ORIGIN)
    job = w.json("POST", "/api/worker/claim")
    assert job["force"]
    run_job(w, job, log=lambda *_: None)
    n_runs = Catalog(s.db_path, s.data_root).q("SELECT COUNT(*) AS n FROM runs WHERE session_id=?", (sid,))[0]["n"]
    assert n_runs == 2
    jobs = ada.get("/api/worker/jobs").json()
    assert [j["state"] for j in jobs["jobs"]] == ["done"] * 3 and "big-box" in jobs["workers_seen"]


def test_stale_job_goes_back_to_the_queue(tmp_path):
    q = JobQueue(tmp_path / "c.sqlite")
    jid = q.enqueue("S1")
    assert q.claim("a")["job_id"] == jid and q.claim("b") is None
    q.con.execute("UPDATE jobs SET claimed_at='2000-01-01T00:00:00+00:00'")
    j = q.claim("b")
    assert j["job_id"] == jid and j["worker"] == "b"
    q.finish(jid, "boom")
    assert q.state("S1") == "error: boom" and q.pending() == 0


def test_cancel_drops_queued_and_running_jobs(tmp_path):
    q = JobQueue(tmp_path / "c.sqlite")
    running, queued = q.enqueue("S1"), q.enqueue("S2")
    sweep = q.enqueue_sweep({"name": "m"})
    assert q.claim("a")["job_id"] == running
    assert q.counts() == {"queued": 1, "running": 1}
    assert sorted(q.cancel()) == [running, queued]
    assert q.counts() == {"queued": 0, "running": 0}
    assert q.get(sweep)["state"] == "queued"  # sweeps untouched
    assert q.claim("b") is None and q.state("S1") is None
    q.con.execute("UPDATE jobs SET claimed_at='2000-01-01T00:00:00+00:00'")
    assert q.claim("b") is None  # a cancelled job never comes back


def test_cancel_processing_stops_the_worker(remote):
    s, app, tok, sid = remote
    ada = TestClient(app)
    ada.post("/login", data={"username": "ada", "password": PW}, headers=ORIGIN)
    ada.post("/sessions/process-all", data={}, headers=ORIGIN)
    w = ClientApi(TestClient(app), tok, "big-box")
    job = w.json("POST", "/api/worker/claim")
    assert "1 running, 0 queued" in ada.get("/").text
    r = ada.post("/sessions/cancel-processing", headers=ORIGIN, follow_redirects=False)
    assert r.headers["location"] == "/?cancelled=1"
    with pytest.raises(RuntimeError, match="409"):
        run_job(w, job, log=lambda *_: None)
    assert JobQueue(s.db_path).get(job["job_id"])["state"] == "cancelled"


def test_process_all_queues_every_kept_session(remote):
    s, app, tok, sid = remote
    ada = TestClient(app)
    ada.post("/login", data={"username": "ada", "password": PW}, headers=ORIGIN)
    assert "Process all" in ada.get("/").text
    r = ada.post("/sessions/process-all", data={"subject": "", "protocol": ""}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?queued=1"
    jobs = JobQueue(s.db_path).recent()
    assert [(j["session_id"], j["state"], j["force"]) for j in jobs] == [(sid, "queued", 0)]
    assert "1 session queued for the worker" in ada.get("/?queued=1").text
    # nothing matches this filter
    r = ada.post("/sessions/process-all", data={"subject": "S999"}, headers=ORIGIN, follow_redirects=False)
    assert r.headers["location"] == "/?subject=S999&queued=0"
    # discarded sessions are skipped
    with Catalog(s.db_path, s.data_root).con as con:
        con.execute("UPDATE sessions SET status='discard'")
        con.execute("DELETE FROM jobs")
    ada.post("/sessions/process-all", data={}, headers=ORIGIN)
    assert JobQueue(s.db_path).recent() == []


# ---------------------------------------------------------------- Layer-4 sweeps
FAST = {"trials": 8, "features": ["hr_mean_bpm", "pad_ms_rel"],
        "space": [["model.kind", "categorical", ["ridge"]], ["model.alpha", "float", [0.1, 10, "log"]]]}


def _model_lab(tmp_path, remote_mode: bool):
    from test_layer4 import _dataset

    shutil.copytree(REPO / "config", tmp_path / "config")
    (tmp_path / "config" / "server.toml").write_text(
        '[auth]\nenabled = true\n' + ('[processing]\nmode = "remote"\n' if remote_mode else ""))
    (tmp_path / "config" / "sweep_layer4.json").write_text(json.dumps(FAST))
    (tmp_path / "config" / "pipeline_layer4.json").write_text(json.dumps({"role": "glasses"}))
    s = load_settings(tmp_path)
    ds = s.data_root / "datasets" / "bp"
    ds.mkdir(parents=True)
    _dataset(n_subjects=4).to_csv(ds / "rows.csv", index=False)
    (ds / "manifest.json").write_text(json.dumps({"name": "bp", "built_at": "2026-10-30T00:00:00+00:00",
                                                  "summary": {"n_rows": 66}}))
    a = AuthStore(s.db_path)
    a.add_user("ada", PW, "admin")
    a.add_user("vic", PW, "viewer")
    a.add_user("nbe-worker", PW, "worker")
    from sglab.web.app import create_app
    return s, create_app(s), a.create_token("nbe-worker", "test")


def _login(app, who="ada"):
    c = TestClient(app)
    c.post("/login", data={"username": who, "password": PW}, headers=ORIGIN)
    return c


def test_remote_sweep_end_to_end(tmp_path):
    pytest.importorskip("optuna")
    from sglab.model import list_runs
    from sglab.worker import run_sweep_job

    s, app, tok = _model_lab(tmp_path, remote_mode=True)
    ada = _login(app)
    assert "How to read these results" in ada.get("/models").text
    assert _login(app, "vic").post("/models/sweep", data={"trials": 8}, headers=ORIGIN).status_code == 403
    r = ada.post("/models/sweep", data={"role": "glasses", "trials": 8}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    page = ada.get("/models?msg=sweep").text
    assert "sweep bp-glasses-sweep-cal" in page and "no worker online" in page

    c = TestClient(app)
    w = ClientApi(c, tok, "gpu-box")
    assert w.json("POST", "/api/worker/claim") is None  # an older worker (process only) never gets a sweep
    job = w.json("POST", "/api/worker/claim?kinds=process,sweep")
    assert job["kind"] == "sweep" and job["payload"]["layer4"]["role"] == "glasses"
    assert job["payload"]["spec"]["trials"] == 8
    # only the worker holding the job may fetch its dataset
    assert c.get(f"/api/worker/jobs/{job['job_id']}/dataset/rows.csv",
                 headers={**w.headers, "X-NBE-Worker": "other"}).status_code == 409
    assert c.get(f"/api/worker/jobs/{job['job_id']}/dataset/catalog.sqlite", headers=w.headers).status_code == 404
    run_sweep_job(w, job, log=lambda *_: None)

    j = JobQueue(s.db_path).get(job["job_id"])
    assert j["state"] == "done" and j["progress"].startswith(("40/", "32/"))  # 8 + 4 nested searches of 8
    runs = list_runs(s)
    assert len(runs) == 1 and runs[0]["sweep"]["nested"]["n_outer"] == 4
    name, rid = runs[0]["name"], runs[0]["run_id"]
    assert not (s.processed_dir / "_staging" / str(job["job_id"])).exists()
    page = ada.get("/models").text
    assert f"/models/{name}/{rid}" in page and "pill good" in page
    detail = ada.get(f"/models/{name}/{rid}").text
    assert "beats baseline" in detail and "honest check" in detail and "pad_ms_rel" in detail
    assert ada.get(f"/files/models/{name}/{rid}/report.png").headers["content-type"] == "image/png"
    assert ada.get("/models/nope/123").status_code == 404
    assert ada.get("/files/models/../catalog.sqlite").status_code == 404
    assert ada.get("/api/models").json()[0]["run_id"] == rid


def test_local_train_and_sweep_from_the_page(tmp_path):
    pytest.importorskip("optuna")
    from sglab.model import list_runs

    s, app, _ = _model_lab(tmp_path, remote_mode=False)
    ada = _login(app)
    ada.post("/models/train", headers=ORIGIN)
    ada.post("/models/sweep", data={"trials": 6}, headers=ORIGIN)
    ctx = app.state.ctx
    for _ in range(300):
        if all(t["state"] != "running" for t in ctx.model_tasks):
            break
        time.sleep(0.1)
    assert [t["state"] for t in ctx.model_tasks] == ["done", "done"], ctx.model_tasks
    assert {r["name"] for r in list_runs(s)} == {"bp-glasses-ridge-cal", "bp-glasses-sweep-cal"}
    page = ada.get("/models").text
    assert "Scores over time" in page and "NaN" not in page.split("const H =")[1].split(";")[0]
