"""Customs Document Validation: upload PDFs, process them (AIDB pipeline + extractor agent), see the
validator's findings and ask the validator agent."""
import base64
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

import pandas as pd
from flask import Blueprint, request, send_from_directory
from sqlalchemy import text

from .common import (api, db_error, execute, get_engine, query, records, relation_exists, reply, start_job,
                     table)

bp = Blueprint("validation", __name__, url_prefix="/api/validation")

PIPELINE = os.getenv("CUSTOMS_PIPELINE", "customs_pdfs")
VALIDATOR_AGENT = os.getenv("CUSTOMS_VALIDATOR_AGENT", "customs_validator")
EXTRACTOR_AGENT = os.getenv("CUSTOMS_EXTRACTOR_AGENT", "customs_extractor")
# Folder the PGFS storage location points at (PDFs are uploaded here)
PDF_DIR = Path(os.getenv("CUSTOMS_PDF_DIR", "/tmp/pgfs/validation/"))
# How many documents are sent to the extractor agent at the same time (1 = one by one)
EXTRACT_WORKERS = max(1, int(os.getenv("CUSTOMS_EXTRACT_WORKERS", "4")))

# Jupyter checkpoint copies (".ipynb_checkpoints/<name>-checkpoint.pdf") are not real documents
CHECKPOINT_FILTER_SQL = "(source_id ILIKE '%.ipynb_checkpoints%' OR source_id ILIKE '%-checkpoint.pdf')"


def real_pdf_name(source_id) -> str:
    """File name without Jupyter's '-checkpoint' suffix."""
    return re.sub(r"-checkpoint(\.pdf)$", r"\1", Path(str(source_id)).name, flags=re.IGNORECASE)


def pdf_link(source_id):
    if source_id is None or pd.isna(source_id):
        return None
    return f"/api/validation/pdf/{quote(real_pdf_name(source_id))}"


def remove_checkpoint_dirs():
    if PDF_DIR.exists():
        for d in PDF_DIR.rglob(".ipynb_checkpoints"):
            shutil.rmtree(d, ignore_errors=True)


def purge_checkpoint_rows():
    for tbl in ("customs_extracted", "customs_parsed_text"):
        if relation_exists(tbl):
            execute(f"DELETE FROM {tbl} WHERE {CHECKPOINT_FILTER_SQL}")


def ensure_objects_if_needed():
    """The notebook creates customs_extracted; create an empty one if the app runs first."""
    if not relation_exists("customs_extracted"):
        execute("""CREATE TABLE IF NOT EXISTS customs_extracted (
                       source_id text PRIMARY KEY, fields jsonb, error text)""")
    purge_checkpoint_rows()


def list_documents() -> pd.DataFrame:
    """PDFs in the upload folder plus anything already extracted, one row per file."""
    names = sorted(p.name for p in PDF_DIR.glob("*.pdf")) if PDF_DIR.exists() else []
    files = pd.DataFrame({"file": names}, dtype="object")
    if relation_exists("customs_extracted"):
        ext = query("""SELECT regexp_replace(source_id, '^.*/', '') AS file,
                              fields->>'doc_type' AS doc_type,
                              fields->>'invoice_no' AS invoice_no,
                              CASE WHEN error IS NULL THEN 'Processed' ELSE 'Error' END AS status
                       FROM customs_extracted""")
    else:
        ext = pd.DataFrame(columns=["file", "doc_type", "invoice_no", "status"])
    df = files.merge(ext, on="file", how="outer")
    df["status"] = df["status"].fillna("Not processed")
    return df.sort_values(["invoice_no", "file"], na_position="last").reset_index(drop=True)


def delete_documents(names):
    safe = [Path(n).name for n in names]
    for n in safe:
        p = PDF_DIR / n
        if p.is_file():
            p.unlink()
    for tbl in ("customs_extracted", "customs_parsed_text"):
        if relation_exists(tbl):
            execute(f"DELETE FROM {tbl} WHERE regexp_replace(source_id, '^.*/', '') = ANY(:n)", {"n": safe})


def run_pipeline():
    remove_checkpoint_dirs()
    # pgfs must be loaded in this session so pgfs.allowed_local_fs_paths is populated
    with get_engine().connect() as c:
        c.execute(text("SELECT pgfs.pgfs_version()"))
        c.execute(text("SELECT aidb.run_pipeline(:p)"), {"p": PIPELINE})
    purge_checkpoint_rows()


def docs_to_extract(redo_all):
    if not relation_exists("customs_parsed_text"):
        return []
    sql = """SELECT s.source_id FROM customs_parsed_text s
             LEFT JOIN customs_extracted e USING (source_id)"""
    where = [f"NOT {CHECKPOINT_FILTER_SQL}"]
    if not redo_all:
        where.append("(e.source_id IS NULL OR e.fields IS NULL)")
    return query(sql + " WHERE " + " AND ".join(where) + " ORDER BY 1")["source_id"].tolist()


def extract_one(source_id):
    execute("""INSERT INTO customs_extracted (source_id, fields, error)
               SELECT s.source_id,
                      CASE WHEN a.error IS NULL THEN a.message::jsonb END,
                      a.error
               FROM customs_parsed_text s,
                    LATERAL aidb.agent_converse(:agent,
                            'Source file: ' || s.source_id || E'\\n\\n' || s.value) a
               WHERE s.source_id = :sid
               ON CONFLICT (source_id) DO UPDATE
                 SET fields = EXCLUDED.fields, error = EXCLUDED.error""",
            {"agent": EXTRACTOR_AGENT, "sid": source_id})


# ---------------------------------------------------------------- routes
@bp.get("/status")
def status():
    try:
        ensure_objects_if_needed()
        checks = {
            "Database connection": True,
            "Pipeline created": bool(query("SELECT 1 FROM aidb.pipelines WHERE name = :p", {"p": PIPELINE}).shape[0]),
            "Rules table": relation_exists("required_fields"),
            "Findings view": relation_exists("customs_findings"),
            "Validator agent": bool(query("SELECT 1 FROM aidb.agents WHERE name = :a",
                                          {"a": VALIDATOR_AGENT}).shape[0]),
        }
        error = None
    except Exception as e:  # noqa: BLE001
        checks, error = {"Database connection": False}, db_error(e)
    return reply({"ok": True, "checks": [{"label": k, "ok": v} for k, v in checks.items()],
                  "error": error, "pdf_dir": str(PDF_DIR)})


@bp.get("/documents")
@api
def documents():
    return reply({"ok": True, "rows": records(list_documents())})


@bp.post("/upload")
@api
def upload():
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    saved = 0
    for f in request.get_json().get("files", []):
        name = Path(f["name"]).name
        if not name.lower().endswith(".pdf"):
            continue
        (PDF_DIR / name).write_bytes(base64.b64decode(f["data"]))
        saved += 1
    return reply({"ok": True, "saved": saved})


@bp.post("/delete")
@api
def delete():
    names = request.get_json().get("names", [])
    delete_documents(names)
    return reply({"ok": True, "deleted": len(names)})


def process_job(job, redo):
    t0 = time.time()
    failed = []
    job.say("Step 1 of 2: parsing the PDFs with the AI pipeline (aidb reports no progress for this step, "
            "so only elapsed time is shown)")
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(run_pipeline)
        while not fut.done():
            time.sleep(1)
        fut.result()
    job.say(f"✅ Pipeline finished in {int(time.time() - t0)}s")
    pending = docs_to_extract(redo)
    if pending:
        job.say(f"Step 2 of 2: extracting fields from {len(pending)} document(s), {EXTRACT_WORKERS} at a time")
        job.progress = 0.0
        done = 0
        with ThreadPoolExecutor(max_workers=EXTRACT_WORKERS) as ex:
            futs = {ex.submit(extract_one, sid): sid for sid in pending}
            for fut in as_completed(futs):
                name = Path(futs[fut]).name
                done += 1
                try:
                    fut.result()
                    job.say(f"✅ {name}")
                except Exception as e:  # noqa: BLE001
                    failed.append(f"{name}: {db_error(e)}")
                    job.say(f"⚠️ {name} failed")
                job.progress = done / len(pending)
    else:
        job.say("Step 2 of 2: no new documents to extract.")
    job.say(f"Processing complete in {int(time.time() - t0)}s")
    errs = (query("SELECT regexp_replace(source_id, '^.*/', '') AS file, error FROM customs_extracted "
                  "WHERE error IS NOT NULL") if relation_exists("customs_extracted") else pd.DataFrame())
    return {"failed": failed, "errors": table(errs) if len(errs) else None}


@bp.post("/process")
def process():
    job = start_job(process_job, bool(request.get_json().get("redo")))
    return reply({"ok": True, "job": job.id})


@bp.get("/findings")
@api
def findings():
    if not relation_exists("customs_findings"):
        return reply({"ok": True, "available": False})
    f = query("SELECT * FROM customs_findings ORDER BY invoice_no, finding_type, doc_type")
    f["pdf_url"] = f["source_id"].map(pdf_link)
    ship = query("""SELECT d.invoice_no,
                           count(DISTINCT d.source_id) AS documents,
                           (SELECT count(*) FROM customs_findings x WHERE x.invoice_no = d.invoice_no) AS findings
                    FROM customs_docs d WHERE d.invoice_no IS NOT NULL
                    GROUP BY d.invoice_no ORDER BY 1""")
    ship["documents"] = pd.to_numeric(ship["documents"]).astype("int64")
    ship["findings"] = pd.to_numeric(ship["findings"]).astype("int64")
    ready = int((ship["findings"] == 0).sum()) if len(ship) else 0
    ship["status"] = ship["findings"].apply(lambda n: "✅ Ready to file" if n == 0 else "⚠️ Needs attention")
    doc_issues = f[f["source_id"].notna()]
    grouped = None
    if not doc_issues.empty:
        grouped = (doc_issues.groupby(["invoice_no", "doc_type", "pdf_url"], dropna=False)
                   .agg(problems=("finding_type", "count"),
                        details=("detail", lambda s: "; ".join(map(str, s)))).reset_index())
    return reply({"ok": True, "available": True,
                  "metrics": {"shipments": len(ship), "ready": ready, "attention": len(ship) - ready},
                  "shipments": table(ship), "doc_issues": table(grouped) if grouped is not None else None,
                  "findings": table(f.drop(columns=["source_id"]))})


@bp.get("/rules")
@api
def rules():
    if not relation_exists("required_fields"):
        return reply({"ok": True, "available": False})
    r = query("SELECT doc_type, field, rule FROM required_fields ORDER BY doc_type, field")
    return reply({"ok": True, "available": True, "rows": records(r)})


@bp.post("/ask")
def ask():
    q = request.get_json()["question"]
    try:
        r = query("SELECT message, error FROM aidb.agent_converse(:a, :q)", {"a": VALIDATOR_AGENT, "q": q})
        answer = r["message"].iloc[0] if len(r) and r["message"].iloc[0] else None
        if answer is None:
            answer = f"The agent returned an error: {r['error'].iloc[0] if len(r) else 'no response'}"
    except Exception as e:  # noqa: BLE001
        answer = f"Could not reach the agent: {db_error(e)}"
    return reply({"ok": True, "answer": answer})


@bp.get("/pdf/<path:name>")
def pdf(name):
    return send_from_directory(PDF_DIR, Path(name).name, mimetype="application/pdf")
