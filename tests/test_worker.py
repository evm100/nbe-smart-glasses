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
