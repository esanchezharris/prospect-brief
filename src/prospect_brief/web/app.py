"""Demo web page: a thin client over the same pipeline functions the CLI calls. Jobs run in
threads; progress lines stream to the page over server-sent events. No logic lives here."""

from __future__ import annotations

import asyncio
import csv
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import ROOT, Config
from ..pipeline import IdentityUnresolved, make_run_id, run_brief
from ..signals.pipeline import run_signals

STATIC = Path(__file__).parent / "static"


class Job:
    """A pipeline run in a thread. Log lines are kept so a page can reattach after a reload."""

    def __init__(self, kind: str, label: str) -> None:
        self.kind, self.label = kind, label
        self.lines: list[str] = []
        self.status = "running"
        self.result: dict[str, Any] = {}
        self.error: str | None = None
        self.cond = threading.Condition()
        self.awaiting_confirm = False
        self.confirm_event = threading.Event()
        self.confirm_answer = False
        self.started = time.time()

    def emit(self, line: str) -> None:
        with self.cond:
            self.lines.extend(str(line).splitlines())
            self.cond.notify_all()

    def finish(self) -> None:
        with self.cond:
            self.cond.notify_all()

    def confirm(self, card) -> bool:
        """Human-in-the-loop: block until the page answers, or 20 minutes."""
        self.awaiting_confirm = True
        self.emit("[confirm] waiting for you to confirm the identity card")
        ok = self.confirm_event.wait(timeout=1200) and self.confirm_answer
        self.awaiting_confirm = False
        self.emit("[confirm] identity confirmed" if ok else "[confirm] not confirmed; stopping")
        return ok


JOBS: dict[str, Job] = {}


def _config(mode: str) -> Config:
    cfg = Config.load()
    if mode == "fixture":
        cfg.root = cfg.root / ".fixture"
    return cfg


def _url_for(path: Path) -> str:
    rel = path.relative_to(ROOT).as_posix()
    return "/fixture/" + rel[len(".fixture/"):] if rel.startswith(".fixture/") else "/" + rel


class BriefRequest(BaseModel):
    name: str
    anchors: dict[str, str] = Field(default_factory=dict)
    institution: str = "University of Southern California"
    mode: Literal["live", "fixture"] = "live"
    confirm_identity: bool = True


class SignalsRequest(BaseModel):
    institution: str = "University of Southern California"
    days: int = 90
    forms: list[str] = Field(default_factory=lambda: ["S-1", "8-K", "DEF14A"])
    mode: Literal["live", "fixture"] = "live"


def _brief_worker(job: Job, req: BriefRequest) -> None:
    try:
        cfg = _config(req.mode)
        run_dir = cfg.runs_dir / make_run_id(req.name)
        run_dir.mkdir(parents=True, exist_ok=True)
        transport = None
        if req.mode == "fixture":
            from ..testing import build_fake_llm, fixture_transport, mock_search

            llm, search, transport = build_fake_llm(run_dir), mock_search(), fixture_transport()
        else:
            from ..llm import AnthropicProvider
            from ..search import TavilyProvider

            llm = AnthropicProvider(cfg, run_dir)
            search = TavilyProvider(exclude_domains=cfg.blocklist, user_agent=cfg.user_agent)

        async def go():
            try:
                return await run_brief(subject=req.name, anchors=req.anchors, institution=req.institution, config=cfg, llm=llm, search=search, run_dir=run_dir, transport=transport, log=job.emit, confirm=job.confirm, yes=not req.confirm_identity)
            finally:
                await search.aclose()

        brief, html_path = asyncio.run(go())
        r = brief.report
        job.result = {"run_id": r.run_id, "html_url": _url_for(html_path), "counts": r.counts, "cost_usd": r.cost_usd, "wall_seconds": r.wall_seconds, "sources": r.counts.get("sources_cited", 0), "mode": req.mode}
        job.status = "done"
    except IdentityUnresolved as e:
        job.status, job.error = "error", f"identity: {e}"
    except Exception as e:  # surfaced to the page, never swallowed
        job.status, job.error = "error", f"{type(e).__name__}: {e}"
    finally:
        job.finish()


def _signals_worker(job: Job, req: SignalsRequest) -> None:
    try:
        cfg = _config(req.mode)
        transport = None
        if req.mode == "fixture":
            from ..testing import build_fake_llm, fixture_transport

            llm, transport = build_fake_llm(None), fixture_transport()
        else:
            from ..llm import AnthropicProvider

            run_dir = cfg.runs_dir / f"signals-{req.institution.lower().replace(' ', '-')[:30]}"
            run_dir.mkdir(parents=True, exist_ok=True)
            llm = AnthropicProvider(cfg, run_dir)
        html_path, csv_path = asyncio.run(run_signals(institution=req.institution, days=req.days, forms=req.forms, config=cfg, llm=llm, transport=transport, log=job.emit))
        job.result = {"html_url": _url_for(html_path), "csv_url": _url_for(csv_path), "rows": _read_signal_rows(csv_path), "mode": req.mode}
        job.status = "done"
    except Exception as e:
        job.status, job.error = "error", f"{type(e).__name__}: {e}"
    finally:
        job.finish()


def _read_signal_rows(csv_path: Path) -> list[dict]:
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["filing_urls"] = r.get("filing_urls", "").split()
    return rows


def _sse(job: Job):
    """Replays every line so far, then follows the job. Any number of pages can attach."""

    def gen():
        i = 0
        while True:
            with job.cond:
                if i >= len(job.lines) and job.status == "running":
                    job.cond.wait(timeout=15)
                chunk = job.lines[i:]
                i = len(job.lines)
                status = job.status
            for ln in chunk:
                yield f"data: {json.dumps(ln)}\n\n"
                if ln.startswith("[confirm] waiting"):
                    yield f"event: confirm\ndata: {json.dumps({'job_id': job_id_of(job)})}\n\n"
            if status == "done":
                yield f"event: done\ndata: {json.dumps(job.result)}\n\n"
                return
            if status == "error":
                yield f"event: error\ndata: {json.dumps({'error': job.error})}\n\n"
                return
            if not chunk:
                yield ": keepalive\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def job_id_of(job: Job) -> str:
    return next(k for k, v in JOBS.items() if v is job)


def _job_or_404(job_id: str) -> Job:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    return job


app = FastAPI(title="prospect-brief", docs_url=None, redoc_url=None)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.post("/api/briefs")
def start_brief(req: BriefRequest) -> dict:
    if not req.name.strip():
        raise HTTPException(400, "name is required")
    req.anchors = {k.strip().lower(): v.strip() for k, v in req.anchors.items() if k.strip() and v.strip()}
    if not req.anchors:
        raise HTTPException(400, "at least one anchor is required")
    job_id = uuid.uuid4().hex[:12]
    job = JOBS[job_id] = Job("brief", req.name)
    threading.Thread(target=_brief_worker, args=(job, req), daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/briefs/{job_id}/confirm")
def confirm_brief(job_id: str, body: dict) -> dict:
    job = _job_or_404(job_id)
    if not job.awaiting_confirm:
        raise HTTPException(409, "job is not waiting for confirmation")
    job.confirm_answer = bool(body.get("ok", False))
    job.confirm_event.set()
    return {"ok": job.confirm_answer}


@app.get("/api/jobs")
def list_jobs() -> list[dict]:
    """Running and recent jobs, newest first, so a reloaded page can reattach."""
    out = [{"job_id": k, "kind": j.kind, "label": j.label, "status": j.status, "awaiting_confirm": j.awaiting_confirm, "started": j.started, "result": j.result if j.status == "done" else None, "error": j.error} for k, j in JOBS.items()]
    out.sort(key=lambda x: -x["started"])
    return out[:20]


@app.get("/api/briefs/{job_id}/events")
def brief_events(job_id: str):
    return _sse(_job_or_404(job_id))


@app.get("/api/briefs")
def list_briefs() -> list[dict]:
    out = []
    for base in (ROOT / "briefs", ROOT / ".fixture" / "briefs"):
        for rp in base.glob("*/run_report.json"):
            try:
                r = json.loads(rp.read_text())
            except Exception:
                continue
            out.append({"run_id": r["run_id"], "subject": r["subject"], "finished_at": r.get("finished_at"), "cost_usd": r.get("cost_usd", 0), "wall_seconds": r.get("wall_seconds", 0), "counts": r.get("counts", {}), "html_url": _url_for(rp.parent / "brief.html"), "fixture": ".fixture" in rp.parts})
    out.sort(key=lambda x: x["run_id"], reverse=True)
    return out


@app.get("/api/briefs/{run_id}/evidence")
def brief_evidence(run_id: str) -> dict:
    for base in (ROOT / "briefs", ROOT / ".fixture" / "briefs"):
        p = base / run_id / "brief.json"
        if p.exists():
            b = json.loads(p.read_text())
            rows = [{"id": e["id"], "status": e["status"], "drop_reason": e.get("drop_reason"), "claim": e["claim"], "quote": e["supporting_quote"], "source_url": e["source_url"], "publisher": e["publisher"], "tier": e["source_tier"], "identity_score": e.get("identity_score")} for e in b["evidence"]]
            return {"run_id": run_id, "subject": b["subject"], "html_url": _url_for(p.parent / "brief.html"), "rows": rows, "counts": b["report"]["counts"], "possibly_different_person": b.get("possibly_different_person", []), "conflicts": b.get("conflicts", [])}
    raise HTTPException(404, "unknown run")


@app.post("/api/signals")
def start_signals(req: SignalsRequest) -> dict:
    job_id = uuid.uuid4().hex[:12]
    job = JOBS[job_id] = Job("signals", req.institution)
    threading.Thread(target=_signals_worker, args=(job, req), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/signals/{job_id}/events")
def signal_events(job_id: str):
    return _sse(_job_or_404(job_id))


@app.get("/api/signals/latest")
def latest_signals(mode: str = "live") -> dict:
    base = ROOT / ".fixture" / "signals" if mode == "fixture" else ROOT / "signals"
    files = sorted(base.glob("*.csv"))
    if not files:
        return {"rows": [], "html_url": None}
    csv_path = files[-1]
    return {"rows": _read_signal_rows(csv_path), "html_url": _url_for(csv_path.with_suffix(".html")), "csv_url": _url_for(csv_path), "generated": csv_path.stem.rsplit("-", 3)[-3:]}


for _name, _dir in (("briefs", ROOT / "briefs"), ("signals", ROOT / "signals"), ("fixture", ROOT / ".fixture")):
    _dir.mkdir(parents=True, exist_ok=True)
    app.mount(f"/{_name}", StaticFiles(directory=_dir), name=_name)
