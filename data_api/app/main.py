"""zOrder ERP Data API: read-only REST endpoints over the three UNI stored procedures.

Auth: every /erp/* call needs the ERP headers  Z-Auth-Token  and  Instance_Id.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import transform as T
from .config import get_settings
from .security import Tenant, require_tenant
from .sources import SP_PROGRESS, SP_SALES, SP_STOCK, DataSourceError, fetch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
settings = get_settings()

app = FastAPI(
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
    title="zOrder ERP Data API",
    version="1.0.0",
    description="Read-only sales, stock and comparison data per company. "
                "Send the ERP login token as `Z-Auth-Token` and the company as `Instance_Id`.",
)
if settings.cors_origins.strip():
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_methods=["GET"],
        allow_headers=["Z-Auth-Token", "Instance_Id", "Instance-Id", "Content-Type"],
    )

TenantDep = Annotated[Tenant, Depends(require_tenant)]

_log = logging.getLogger("data_api")
if settings.sql_trust_server_certificate:
    _log.warning("DATA_API_SQL_TRUST_SERVER_CERTIFICATE=true: SQL Server certificates are NOT verified.")
if not settings.sql_encrypt:
    _log.warning("DATA_API_SQL_ENCRYPT=false: SQL traffic is NOT encrypted. Do not use in production.")
if settings.data_source == "fixture" and not settings.allow_dev_modes:
    raise RuntimeError("DATA_API_DATA_SOURCE=fixture is a development mode; set DATA_API_ALLOW_DEV_MODES=true.")


# --------------------------------------------------------------------------- envelope & errors
def ok(data, **meta) -> dict:
    return {"success": True, "data": data, "meta": meta}


@app.exception_handler(HTTPException)
async def _http_err(_: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"success": False, "message": exc.detail})


@app.exception_handler(RequestValidationError)
async def _val_err(_: Request, exc: RequestValidationError):
    msgs = [f"{'.'.join(str(p) for p in e['loc'][1:])}: {e['msg']}" for e in exc.errors()]
    return JSONResponse(status_code=400, content={"success": False, "message": "; ".join(msgs)})


@app.exception_handler(Exception)
async def _unhandled(_: Request, exc: Exception):
    logging.getLogger("data_api").exception("Unhandled error")
    return JSONResponse(status_code=500, content={"success": False,
                        "message": "Data API internal error. Please contact the provider."})


@app.exception_handler(DataSourceError)
async def _db_err(_: Request, exc: DataSourceError):
    return JSONResponse(status_code=502, content={"success": False, "message": str(exc)})


# --------------------------------------------------------------------------- param helpers
def date_range(from_date: date | None, to_date: date | None) -> tuple[date, date]:
    to_d = to_date or date.today()
    from_d = from_date or T.financial_year_start(to_d)
    if from_d > to_d:
        raise HTTPException(400, "from_date must be on or before to_date.")
    if (to_d - from_d).days > settings.max_range_days:
        raise HTTPException(400, f"Date range is limited to {settings.max_range_days} days.")
    return from_d, to_d


def parse_group_by(value: str, allowed: dict[str, str]) -> list[str]:
    parts = [p.strip().lower() for p in value.split(",") if p.strip()]
    if not parts or len(parts) > 2:
        raise HTTPException(400, "group_by takes one or two fields, e.g. 'branch' or 'branch,month'.")
    bad = [p for p in parts if p not in allowed]
    if bad:
        raise HTTPException(400, f"Unknown group_by '{', '.join(bad)}'. Allowed: {', '.join(allowed)}.")
    return parts


SALES_SORT = {"net_sale", "gross_margin", "quantity", "invoices", "sale_with_tax", "margin_pct"}
STOCK_SORT = {"stock_value_at_cost", "stock_qty", "avg_stock_days", "max_stock_days"}

FromQ = Annotated[date | None, Query(description="Start date (YYYY-MM-DD). Default: start of current financial year.")]
ToQ = Annotated[date | None, Query(description="End date (YYYY-MM-DD). Default: today.")]
BranchQ = Annotated[str | None, Query(description="BranchId. Omit for all branches.", max_length=50)]
TopQ = Annotated[int, Query(ge=1, le=200)]


def paginate(rows: list[dict], page: int, page_size: int) -> tuple[list[dict], dict]:
    page_size = min(page_size, settings.max_page_size)
    start = (page - 1) * page_size
    return rows[start:start + page_size], {"page": page, "page_size": page_size, "total_rows": len(rows),
                                           "total_pages": max(1, -(-len(rows) // page_size))}


# --------------------------------------------------------------------------- endpoints
@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok", "data_source": settings.data_source}


@app.get("/erp/branches", summary="Branches of the company")
async def branches(tenant: TenantDep):
    stock = await fetch(tenant, SP_STOCK, normalize=T.normalize_stock)
    today = date.today()
    sales = await fetch(tenant, SP_SALES, from_dt=today - timedelta(days=90), to_dt=today, normalize=T.normalize_sales)
    return ok(T.branches_view(stock, sales), company=tenant.name)


@app.get("/erp/sales", summary="Raw sales line items (ProcAPIUNISalesData)")
async def sales_raw(tenant: TenantDep, from_date: FromQ = None, to_date: ToQ = None, branch_id: BranchQ = None,
                    page: Annotated[int, Query(ge=1)] = 1, page_size: Annotated[int, Query(ge=1)] = 1000):
    f, t = date_range(from_date, to_date)
    df = await fetch(tenant, SP_SALES, from_dt=f, to_dt=t, branch_id=branch_id, normalize=T.normalize_sales)
    raw = df.drop(columns=[c for c in df.columns if c.startswith("_")]) if not df.empty else df
    rows, pg = paginate(T.records(raw), page, page_size)
    return ok(rows, from_date=str(f), to_date=str(t), branch_id=branch_id, **pg)


@app.get("/erp/sales/summary", summary="Sales grouped by any dimension")
async def sales_summary(tenant: TenantDep, from_date: FromQ = None, to_date: ToQ = None, branch_id: BranchQ = None,
                        group_by: Annotated[str, Query(description=f"One or two of: {', '.join(T.SALES_DIMENSIONS)}")] = "branch",
                        sort_by: Annotated[str, Query(description=f"One of: {', '.join(sorted(SALES_SORT))}")] = "net_sale",
                        top_n: TopQ = settings.default_top_n):
    if sort_by not in SALES_SORT:
        raise HTTPException(400, f"sort_by must be one of: {', '.join(sorted(SALES_SORT))}.")
    gb = parse_group_by(group_by, T.SALES_DIMENSIONS)
    f, t = date_range(from_date, to_date)
    df = await fetch(tenant, SP_SALES, from_dt=f, to_dt=t, branch_id=branch_id, normalize=T.normalize_sales)
    return ok(T.sales_summary(df, gb, sort_by, top_n), from_date=str(f), to_date=str(t), branch_id=branch_id,
              company=tenant.name, currency="INR")


@app.get("/erp/sales-progress", summary="Comparative sales progress, financial year vs previous (ProcAPIUNISalesProgress)")
async def sales_progress(tenant: TenantDep, from_date: date | None = None, to_date: date | None = None,
                         branch_id: BranchQ = None, raw: bool = False):
    df = await fetch(tenant, SP_PROGRESS, from_dt=from_date, to_dt=to_date, branch_id=branch_id,
                     normalize=T.normalize_progress)
    data = T.records(df) if raw else T.progress_view(df)
    return ok(data, branch_id=branch_id, company=tenant.name, currency="INR",
              note="Without dates the procedure compares the current financial year with the previous one.")


@app.get("/erp/stock", summary="Raw current stock rows (ProcAPIUNIStockData)")
async def stock_raw(tenant: TenantDep, branch_id: BranchQ = None,
                    page: Annotated[int, Query(ge=1)] = 1, page_size: Annotated[int, Query(ge=1)] = 1000):
    df = await fetch(tenant, SP_STOCK, branch_id=branch_id, normalize=T.normalize_stock)
    raw = df.drop(columns=[c for c in df.columns if c.startswith("_")]) if not df.empty else df
    rows, pg = paginate(T.records(raw), page, page_size)
    return ok(rows, branch_id=branch_id, **pg)


@app.get("/erp/stock/summary", summary="Current stock grouped by any dimension, with aging")
async def stock_summary(tenant: TenantDep, branch_id: BranchQ = None,
                        group_by: Annotated[str, Query(description=f"One or two of: {', '.join(T.STOCK_DIMENSIONS)}")] = "category",
                        sort_by: Annotated[str, Query(description=f"One of: {', '.join(sorted(STOCK_SORT))}")] = "stock_value_at_cost",
                        min_stock_days: Annotated[int | None, Query(ge=0, description="Only stock older than this many days")] = None,
                        top_n: TopQ = settings.default_top_n):
    if sort_by not in STOCK_SORT:
        raise HTTPException(400, f"sort_by must be one of: {', '.join(sorted(STOCK_SORT))}.")
    gb = parse_group_by(group_by, T.STOCK_DIMENSIONS)
    df = await fetch(tenant, SP_STOCK, branch_id=branch_id, normalize=T.normalize_stock)
    if min_stock_days is not None and not df.empty:
        df = df[df["StockDays"] >= min_stock_days]
    return ok(T.stock_summary(df, gb, sort_by, top_n), branch_id=branch_id, min_stock_days=min_stock_days,
              company=tenant.name, currency="INR")


@app.get("/erp/customers", summary="Customer lookup built from sales (name search)")
async def customers(tenant: TenantDep, search: Annotated[str | None, Query(max_length=100)] = None,
                    from_date: FromQ = None, to_date: ToQ = None, branch_id: BranchQ = None,
                    limit: Annotated[int, Query(ge=1, le=100)] = 20):
    f, t = date_range(from_date, to_date)
    df = await fetch(tenant, SP_SALES, from_dt=f, to_dt=t, branch_id=branch_id, normalize=T.normalize_sales)
    return ok(T.customers_view(df, search, limit), from_date=str(f), to_date=str(t), branch_id=branch_id,
              company=tenant.name, currency="INR",
              note="Customers are matched by name; the sales data has no mobile number or customer id.")


@app.get("/erp/suppliers", summary="Supplier performance: sales, margin and current stock")
async def suppliers(tenant: TenantDep, search: Annotated[str | None, Query(max_length=100)] = None,
                    from_date: FromQ = None, to_date: ToQ = None, branch_id: BranchQ = None,
                    top_n: TopQ = settings.default_top_n):
    f, t = date_range(from_date, to_date)
    sales = await fetch(tenant, SP_SALES, from_dt=f, to_dt=t, branch_id=branch_id, normalize=T.normalize_sales)
    stock = await fetch(tenant, SP_STOCK, branch_id=branch_id, normalize=T.normalize_stock)
    return ok(T.suppliers_view(sales, stock, search, top_n), from_date=str(f), to_date=str(t),
              branch_id=branch_id, company=tenant.name, currency="INR")
