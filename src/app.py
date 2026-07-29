"""Parsed Document Viewer — a light, self-contained Databricks App.

Side-by-side view of an original PDF (streamed from a Unity Catalog volume) and the
output of `ai_parse_document`, read from a Delta table. Everything is chosen at runtime
from the UI: catalog, volume, PDF, and the parsed table — nothing is hard-coded.

Expected parsed-table shape (the "consolidated elements" layout produced by the
ai_parse_document_eval notebook):

    document      STRING   -- source file name, e.g. 'fidelity'
    page          INT      -- 1-indexed global page number
    seq           INT      -- reading order within a page
    element_type  STRING   -- text | table | page_header | footnote | figure | ...
    content       STRING   -- element content (HTML for tables)
    element       VARIANT  -- full raw element (bbox, confidence, etc.)

The app validates these columns before rendering and returns a clear error otherwise.
"""
import os
import threading
from functools import lru_cache

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from databricks import sql as dbsql
from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config

# On a deployed Databricks App these are injected automatically. Locally, fall back to a
# CLI profile so the same code runs with `uvicorn app:app` during development.
IS_APP = bool(os.environ.get("DATABRICKS_APP_NAME"))
PROFILE = os.environ.get("DATABRICKS_PROFILE", "DEFAULT")
WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")
DEFAULT_CATALOG = os.environ.get("DEFAULT_CATALOG", "")

# Columns the viewer relies on. `element` (raw VARIANT) is optional.
REQUIRED_COLS = {"document", "page", "seq", "element_type", "content"}

app = FastAPI(title="Parsed Document Viewer")

_cfg = Config() if IS_APP else Config(profile=PROFILE)
_ws = WorkspaceClient(config=_cfg)
_conn_lock = threading.Lock()
_conn = None


def _connection():
    global _conn
    with _conn_lock:
        if _conn is None:
            if not WAREHOUSE_ID:
                raise HTTPException(500, "DATABRICKS_WAREHOUSE_ID is not set. Bind a SQL "
                                         "warehouse resource to the app or set the env var.")
            _conn = dbsql.connect(
                server_hostname=_cfg.host.replace("https://", ""),
                http_path=f"/sql/1.0/warehouses/{WAREHOUSE_ID}",
                credentials_provider=lambda: _cfg.authenticate,
            )
        return _conn


def _query(q, params=None):
    """Run a query, resetting the session once on a stale-connection error."""
    global _conn
    try:
        with _connection().cursor() as cur:
            cur.execute(q, params or {})
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except HTTPException:
        raise
    except Exception:
        with _conn_lock:
            if _conn is not None:
                try:
                    _conn.close()
                except Exception:
                    pass
                _conn = None
        with _connection().cursor() as cur:
            cur.execute(q, params or {})
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---- Unity Catalog browsing --------------------------------------------------
# The SDK is used (not SHOW ... SQL) so results honor the caller's UC permissions
# and paginate cleanly.

@app.get("/api/catalogs")
def list_catalogs():
    names = sorted(c.name for c in _ws.catalogs.list() if c.name)
    return {"catalogs": names, "default": DEFAULT_CATALOG or (names[0] if names else None)}


@app.get("/api/schemas")
def list_schemas(catalog: str):
    return {"schemas": sorted(s.name for s in _ws.schemas.list(catalog_name=catalog) if s.name)}


@app.get("/api/volumes")
def list_volumes(catalog: str, schema: str):
    vols = _ws.volumes.list(catalog_name=catalog, schema_name=schema)
    return {"volumes": sorted(v.name for v in vols if v.name)}


@app.get("/api/pdfs")
def list_pdfs(catalog: str, schema: str, volume: str, path: str = ""):
    """List PDF files (and sub-directories) under a volume path."""
    base = f"/Volumes/{catalog}/{schema}/{volume}"
    target = f"{base}/{path.strip('/')}" if path.strip("/") else base
    dirs, pdfs = [], []
    for entry in _ws.files.list_directory_contents(target):
        rel = entry.path[len(base):].lstrip("/")
        if entry.is_directory:
            dirs.append(rel)
        elif entry.name and entry.name.lower().endswith(".pdf"):
            pdfs.append({"name": entry.name, "path": rel, "size": entry.file_size})
    return {"base": base, "dirs": sorted(dirs), "pdfs": sorted(pdfs, key=lambda p: p["name"])}


@app.get("/api/tables")
def list_tables(catalog: str, schema: str):
    """List tables in a schema, flagging which ones match the expected parsed shape."""
    out = []
    for t in _ws.tables.list(catalog_name=catalog, schema_name=schema):
        cols = {c.name for c in (t.columns or [])}
        out.append({"name": t.name, "compatible": REQUIRED_COLS.issubset(cols)})
    # compatible tables first, then alphabetical
    return {"tables": sorted(out, key=lambda t: (not t["compatible"], t["name"]))}


# ---- PDF streaming -----------------------------------------------------------

_pdf_cache = {}


@app.get("/api/pdf")
def get_pdf(catalog: str, schema: str, volume: str, path: str):
    full = f"/Volumes/{catalog}/{schema}/{volume}/{path.strip('/')}"
    if full not in _pdf_cache:
        try:
            resp = _ws.files.download(full)
            _pdf_cache[full] = resp.contents.read()
        except Exception as e:
            raise HTTPException(404, f"could not read {full}: {str(e)[:200]}")
    return Response(content=_pdf_cache[full], media_type="application/pdf",
                    headers={"Cache-Control": "public, max-age=3600"})


# ---- Parsed elements ---------------------------------------------------------

def _fq(catalog, schema, table):
    # backtick-quote each identifier; reject backticks to avoid breaking out of the quotes
    for part in (catalog, schema, table):
        if "`" in part:
            raise HTTPException(400, "invalid identifier")
    return f"`{catalog}`.`{schema}`.`{table}`"


@lru_cache(maxsize=8)
def _table_meta(fq: str):
    """Distinct documents and page bounds in the parsed table (cached per table)."""
    docs = _query(f"SELECT DISTINCT document FROM {fq} ORDER BY document")
    bounds = _query(
        f"SELECT document, min(page) AS min_page, max(page) AS max_page, count(*) AS elements "
        f"FROM {fq} GROUP BY document")
    return {
        "documents": [d["document"] for d in docs],
        "bounds": {b["document"]: {"min_page": b["min_page"], "max_page": b["max_page"],
                                   "elements": b["elements"]} for b in bounds},
    }


@app.get("/api/parsed_meta")
def parsed_meta(catalog: str, schema: str, table: str):
    fq = _fq(catalog, schema, table)
    # validate shape first for a friendly error instead of a raw SQL failure
    try:
        cols = {r["col_name"] for r in _query(f"DESCRIBE {fq}") if r.get("col_name")}
    except Exception as e:
        raise HTTPException(400, f"cannot read table: {str(e)[:200]}")
    missing = REQUIRED_COLS - cols
    if missing:
        raise HTTPException(
            422, f"table is missing required column(s): {', '.join(sorted(missing))}. "
                 f"Expected the consolidated elements shape "
                 f"({', '.join(sorted(REQUIRED_COLS))}).")
    return {"table": fq, "has_raw": "element" in cols, **_table_meta(fq)}


@app.get("/api/parsed_page")
def parsed_page(catalog: str, schema: str, table: str, page: int,
                document: str = "", include_raw: bool = False):
    fq = _fq(catalog, schema, table)
    raw_sel = ", to_json(element) AS raw" if include_raw else ""
    where = "page = %(p)s" + (" AND document = %(d)s" if document else "")
    rows = _query(
        f"SELECT seq, element_type, content{raw_sel} FROM {fq} WHERE {where} ORDER BY seq",
        {"p": page, "d": document})
    return {"page": page, "document": document, "elements": rows}


# ---- Static frontend (no build step) -----------------------------------------

_here = os.path.dirname(__file__)
_static = os.path.join(_here, "static")


@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True, "warehouse_configured": bool(WAREHOUSE_ID)})


if os.path.isdir(_static):
    @app.get("/")
    def index():
        return FileResponse(os.path.join(_static, "index.html"))

    app.mount("/", StaticFiles(directory=_static), name="static")
