"""Cleaning and summarising the stored-procedure output.

Raw endpoints return SP columns unchanged; summaries use friendly field names
(Para1..4 = color, size, style, brand).
"""
from __future__ import annotations

import json
import re
from datetime import date

import numpy as np
import pandas as pd

PLACEHOLDERS = {"[none]", "[default]", "none", "nan", "", "-"}
NOT_SET = "(not set)"

# friendly group-by name -> column in the normalised frame
SALES_DIMENSIONS = {
    "branch": "BranchName", "branch_city": "BranchCity", "branch_state": "BranchState",
    "month": "_Month", "day": "_Day", "weekday": "_Weekday",
    "department": "InvDepartmentName", "category": "InvCategoryName", "sub_category": "InvSubCategoryName",
    "article": "ArticleNo", "uom": "UomName",
    "color": "Para1Name", "size": "Para2Name", "style": "Para3Name", "brand": "Para4Name",
    "supplier": "SupplierName", "supplier_city": "SupplierCity", "supplier_state": "SupplierState",
    "customer": "CustomerName", "customer_city": "CustomerCity", "customer_state": "CustomerState",
    "customer_type": "_CustomerType", "salesperson": "SalesPersonName",
}
STOCK_DIMENSIONS = {
    "branch": "BranchName", "branch_city": "BranchCity", "branch_state": "BranchState",
    "department": "InvDepartmentName", "category": "InvCategoryName", "sub_category": "InvSubCategoryName",
    "article": "ArticleNo", "uom": "UomName",
    "color": "Para1Name", "size": "Para2Name", "style": "Para3Name", "brand": "Para4Name",
    "supplier": "SupplierName", "supplier_city": "SupplierCity", "supplier_state": "SupplierState",
    "aging": "_Aging", "purchase_month": "_PurchaseMonth",
}
AGING_BINS = [-1, 30, 90, 180, 365, float("inf")]
AGING_LABELS = ["0-30 days", "31-90 days", "91-180 days", "181-365 days", "365+ days"]

_TEXT_COLS = [
    "InvDepartmentName", "InvCategoryName", "InvSubCategoryName", "ArticleNo", "UomName",
    "Para1Name", "Para2Name", "Para3Name", "Para4Name", "SupplierName", "SupplierCity", "SupplierState",
    "CustomerName", "CustomerCity", "CustomerState", "CustomerCountry", "SalesPersonName",
    "BranchName", "BranchCity", "BranchState",
]
_PLACE_COLS = {"SupplierCity", "SupplierState", "CustomerCity", "CustomerState", "CustomerCountry",
               "BranchCity", "BranchState"}


# --------------------------------------------------------------------------- helpers
def financial_year_start(today: date | None = None) -> date:
    today = today or date.today()
    return date(today.year if today.month >= 4 else today.year - 1, 4, 1)


MAX_TEXT = 80
# control characters, zero-width and bidi-override characters
_ODD_CHARS = re.compile("[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")


def _clean_text(s: pd.Series, place: bool) -> pd.Series:
    """Trim, strip odd characters and cap length: ERP free text goes to the model as data."""
    s = s.astype("string").str.replace(_ODD_CHARS, " ", regex=True).str.replace(r"\s+", " ", regex=True)
    s = s.str.strip().str.slice(0, MAX_TEXT)
    if place:  # "DELHI." / "UTTAR PRADESH." -> "Delhi" / "Uttar Pradesh"
        s = s.str.rstrip(".").str.strip().str.title()
    return s.where(~s.str.lower().isin(PLACEHOLDERS) & s.notna(), NOT_SET)


def _num(df: pd.DataFrame, cols: list[str]) -> None:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)


def _to_date(s: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(s):  # Excel serial dates
        return pd.to_datetime(s, unit="D", origin="1899-12-30", errors="coerce")
    return pd.to_datetime(s, errors="coerce")


def records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> JSON-safe list of dicts (NaN -> null, dates -> ISO)."""
    if df.empty:
        return []
    return json.loads(df.to_json(orient="records", date_format="iso", double_precision=2))


def _r(x: float) -> float:
    return float(round(x, 2)) if x is not None and not pd.isna(x) else 0.0


# --------------------------------------------------------------------------- normalisation
def normalize_sales(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if df.empty:
        return df
    for c in _TEXT_COLS:
        if c in df.columns:
            df[c] = _clean_text(df[c], c in _PLACE_COLS)
    _num(df, ["Quantity", "SaleAtCost", "SaleAmount", "TaxAmount", "SaleAmountWithTax", "IsNewCustomer"])
    df["InvoiceDt"] = _to_date(df["InvoiceDt"])
    df["BranchId"] = df["BranchId"].astype(str)
    df["_Month"] = df["InvoiceDt"].dt.strftime("%Y-%m")
    df["_Day"] = df["InvoiceDt"].dt.strftime("%Y-%m-%d")
    df["_Weekday"] = df["InvoiceDt"].dt.day_name()
    df["_CustomerType"] = np.where(df["IsNewCustomer"] > 0, "New", "Repeat")
    return df


def normalize_stock(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if df.empty:
        return df
    for c in _TEXT_COLS:
        if c in df.columns:
            df[c] = _clean_text(df[c], c in _PLACE_COLS)
    _num(df, ["StockDays", "StockQty", "StockAtCost"])
    df["BranchId"] = df["BranchId"].astype(str)
    df["PurchaseDt"] = _to_date(df["PurchaseDt"])
    df["StockAsOnDt"] = _to_date(df["StockAsOnDt"])
    df["_Aging"] = pd.cut(df["StockDays"], bins=AGING_BINS, labels=AGING_LABELS).astype(str)
    df["_PurchaseMonth"] = df["PurchaseDt"].dt.strftime("%Y-%m")
    return df


def normalize_progress(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if not df.empty:
        _num(df, ["NetAmount", "CTAmount", "CompareIndex"])
        df["CompareCol"] = df["CompareCol"].astype(str)
    return df


# --------------------------------------------------------------------------- sales
def _sales_metrics(g) -> pd.DataFrame:
    out = g.agg(
        net_sale=("SaleAmount", "sum"), tax=("TaxAmount", "sum"), sale_with_tax=("SaleAmountWithTax", "sum"),
        cost=("SaleAtCost", "sum"), quantity=("Quantity", "sum"), invoices=("InvoiceId", "nunique"),
        lines=("InvoiceId", "size"),
    )
    out["gross_margin"] = out["net_sale"] - out["cost"]
    out["margin_pct"] = np.where(out["net_sale"] != 0, out["gross_margin"] / out["net_sale"] * 100, 0.0)
    return out


def sales_totals(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"net_sale": 0, "tax": 0, "sale_with_tax": 0, "cost": 0, "gross_margin": 0, "margin_pct": 0,
                "quantity": 0, "invoices": 0, "lines": 0, "customers": 0, "zero_value_lines": 0}
    net, cost = df["SaleAmount"].sum(), df["SaleAtCost"].sum()
    return {
        "net_sale": _r(net), "tax": _r(df["TaxAmount"].sum()), "sale_with_tax": _r(df["SaleAmountWithTax"].sum()),
        "cost": _r(cost), "gross_margin": _r(net - cost), "margin_pct": _r((net - cost) / net * 100 if net else 0),
        "quantity": _r(df["Quantity"].sum()), "invoices": int(df["InvoiceId"].nunique()), "lines": int(len(df)),
        "customers": int(df.loc[df["CustomerName"] != NOT_SET, "CustomerName"].nunique()),
        "zero_value_lines": int((df["SaleAmount"] == 0).sum()),
        "first_invoice_date": df["InvoiceDt"].min().strftime("%Y-%m-%d"),
        "last_invoice_date": df["InvoiceDt"].max().strftime("%Y-%m-%d"),
    }


def _top_with_others(out: pd.DataFrame, keys: list[str], sort_by: str, top_n: int, sum_cols: list[str]):
    out = out.sort_values(sort_by, ascending=False)
    total_groups = len(out)
    if top_n and total_groups > top_n:
        head, rest = out.iloc[:top_n], out.iloc[top_n:]
        other = {k: f"(other {len(rest)})" for k in keys}
        other.update({c: rest[c].sum() for c in sum_cols if c in rest.columns})
        out = pd.concat([head, pd.DataFrame([other])], ignore_index=True)
    return out, total_groups


def sales_summary(df: pd.DataFrame, group_by: list[str], sort_by: str, top_n: int) -> dict:
    totals = sales_totals(df)
    if df.empty:
        return {"totals": totals, "group_by": group_by, "groups": [], "total_groups": 0}
    cols = [SALES_DIMENSIONS[g] for g in group_by]
    out = _sales_metrics(df.groupby(cols, dropna=False)).reset_index()
    out = out.rename(columns=dict(zip(cols, group_by)))
    if group_by in (["month"], ["day"]):  # time series: keep chronological, no "others"
        out = out.sort_values(group_by[0])
        total_groups = len(out)
    else:
        out, total_groups = _top_with_others(
            out, group_by, sort_by, top_n,
            ["net_sale", "tax", "sale_with_tax", "cost", "quantity", "invoices", "lines", "gross_margin"])
        mask = out["net_sale"] != 0
        out["margin_pct"] = np.where(mask, out["gross_margin"] / out["net_sale"].where(mask, 1) * 100, 0.0)
    out["share_pct"] = out["net_sale"] / totals["net_sale"] * 100 if totals["net_sale"] else 0.0
    return {"totals": totals, "group_by": group_by, "sort_by": sort_by,
            "groups": records(out.round(2)), "total_groups": int(total_groups)}


# --------------------------------------------------------------------------- stock
def stock_totals(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"stock_qty": 0, "stock_value_at_cost": 0, "rows": 0, "avg_stock_days": 0}
    qty = df["StockQty"].sum()
    return {
        "stock_qty": _r(qty), "stock_value_at_cost": _r(df["StockAtCost"].sum()), "rows": int(len(df)),
        "avg_stock_days": _r((df["StockDays"] * df["StockQty"]).sum() / qty if qty else df["StockDays"].mean()),
        "zero_cost_rows": int((df["StockAtCost"] == 0).sum()),
        "negative_stock_rows": int((df["StockQty"] < 0).sum()),
        "stock_as_on": df["StockAsOnDt"].max().strftime("%Y-%m-%d %H:%M") if df["StockAsOnDt"].notna().any() else None,
    }


def stock_summary(df: pd.DataFrame, group_by: list[str], sort_by: str, top_n: int) -> dict:
    totals = stock_totals(df)
    if df.empty:
        return {"totals": totals, "group_by": group_by, "groups": [], "total_groups": 0}
    cols = [STOCK_DIMENSIONS[g] for g in group_by]
    work = df.assign(_qd=df["StockDays"] * df["StockQty"])
    out = work.groupby(cols, dropna=False).agg(
        stock_qty=("StockQty", "sum"), stock_value_at_cost=("StockAtCost", "sum"), rows=("StockQty", "size"),
        _qd=("_qd", "sum"), max_stock_days=("StockDays", "max"),
    ).reset_index().rename(columns=dict(zip(cols, group_by)))
    out["avg_stock_days"] = np.where(out["stock_qty"] != 0, out["_qd"] / out["stock_qty"].where(out["stock_qty"] != 0, 1), 0)
    out = out.drop(columns="_qd")
    if group_by == ["aging"]:
        out["_o"] = out["aging"].map({l: i for i, l in enumerate(AGING_LABELS)})
        out = out.sort_values("_o").drop(columns="_o")
        total_groups = len(out)
    else:
        out, total_groups = _top_with_others(out, group_by, sort_by, top_n, ["stock_qty", "stock_value_at_cost", "rows"])
    tv = totals["stock_value_at_cost"]
    out["value_share_pct"] = out["stock_value_at_cost"] / tv * 100 if tv else 0.0
    return {"totals": totals, "group_by": group_by, "sort_by": sort_by,
            "groups": records(out.round(2)), "total_groups": int(total_groups)}


# --------------------------------------------------------------------------- progress
def progress_view(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"periods": [], "months": [], "totals": {}}
    periods = sorted(df["CompareCol"].unique())
    months = (df.sort_values("CompareIndex").drop_duplicates("CompareIndex")[["CompareIndex", "DisplayLabel"]])
    rows = []
    for _, m in months.iterrows():
        row = {"month": m["DisplayLabel"]}
        for p in periods:
            hit = df[(df["CompareCol"] == p) & (df["CompareIndex"] == m["CompareIndex"])]
            row[p] = _r(hit["NetAmount"].sum()) if not hit.empty else None
            row[f"{p} cumulative"] = _r(hit["CTAmount"].max()) if not hit.empty else None
        if len(periods) >= 2:
            cur, prev = row[periods[-1]], row[periods[-2]]
            row["growth_pct"] = _r((cur - prev) / prev * 100) if cur is not None and prev else None
        rows.append(row)
    totals = {p: _r(df.loc[df["CompareCol"] == p, "NetAmount"].sum()) for p in periods}
    result = {"periods": periods, "months": rows, "totals": totals}
    if len(periods) >= 2:  # like-for-like: only months present in the latest period
        latest = df[df["CompareCol"] == periods[-1]]["CompareIndex"]
        prev_same = df[(df["CompareCol"] == periods[-2]) & df["CompareIndex"].isin(latest)]["NetAmount"].sum()
        cur = totals[periods[-1]]
        result["like_for_like"] = {
            "months_compared": int(latest.nunique()), periods[-1]: cur, periods[-2]: _r(prev_same),
            "growth_pct": _r((cur - prev_same) / prev_same * 100) if prev_same else None,
        }
    return result


# --------------------------------------------------------------------------- customers / suppliers / branches
def customers_view(df: pd.DataFrame, search: str | None, limit: int) -> dict:
    d = df[df["CustomerName"] != NOT_SET] if not df.empty else df
    if search:
        d = d[d["CustomerName"].str.contains(re.escape(search.strip()), case=False, na=False)]
    if d.empty:
        return {"customers": [], "matched": 0}
    g = d.groupby(["CustomerName", "CustomerCity", "CustomerState"], dropna=False)
    out = g.agg(
        net_sale=("SaleAmount", "sum"), quantity=("Quantity", "sum"), invoices=("InvoiceId", "nunique"),
        first_purchase=("InvoiceDt", "min"), last_purchase=("InvoiceDt", "max"),
        new_customer=("IsNewCustomer", "max"),
    ).reset_index().sort_values("net_sale", ascending=False)
    matched = len(out)
    out = out.head(limit)
    cats = (d.groupby(["CustomerName", "InvCategoryName"])["SaleAmount"].sum().reset_index()
              .sort_values("SaleAmount", ascending=False).groupby("CustomerName")["InvCategoryName"]
              .apply(lambda s: list(s.head(3))))
    branches = d.groupby("CustomerName")["BranchName"].apply(lambda s: sorted(s.unique().tolist()))
    out["top_categories"] = out["CustomerName"].map(cats)
    out["branches"] = out["CustomerName"].map(branches)
    out["avg_bill_value"] = out["net_sale"] / out["invoices"].where(out["invoices"] > 0, 1)
    out["new_customer"] = out["new_customer"] > 0
    out = out.rename(columns={"CustomerName": "customer", "CustomerCity": "city", "CustomerState": "state"})
    out["first_purchase"] = out["first_purchase"].dt.strftime("%Y-%m-%d")
    out["last_purchase"] = out["last_purchase"].dt.strftime("%Y-%m-%d")
    return {"customers": records(out.round(2)), "matched": int(matched)}


def suppliers_view(sales: pd.DataFrame, stock: pd.DataFrame, search: str | None, top_n: int) -> dict:
    def filt(d: pd.DataFrame) -> pd.DataFrame:
        if d.empty or not search:
            return d
        return d[d["SupplierName"].str.contains(re.escape(search.strip()), case=False, na=False)]

    s, k = filt(sales), filt(stock)
    keys = ["SupplierName", "SupplierCity"]
    parts = []
    if not s.empty:
        sm = _sales_metrics(s.groupby(keys, dropna=False))[["net_sale", "cost", "gross_margin", "margin_pct", "quantity"]]
        parts.append(sm.rename(columns={"quantity": "sold_qty"}))
    if not k.empty:
        km = k.assign(_qd=k["StockDays"] * k["StockQty"]).groupby(keys, dropna=False).agg(
            stock_qty=("StockQty", "sum"), stock_value_at_cost=("StockAtCost", "sum"), _qd=("_qd", "sum"))
        km["avg_stock_days"] = np.where(km["stock_qty"] != 0, km["_qd"] / km["stock_qty"].where(km["stock_qty"] != 0, 1), 0)
        parts.append(km.drop(columns="_qd"))
    if not parts:
        return {"suppliers": [], "matched": 0}
    out = pd.concat(parts, axis=1).reset_index().fillna(0)
    for c in ["net_sale", "stock_value_at_cost"]:
        if c not in out:
            out[c] = 0.0
    out = out.sort_values(["net_sale", "stock_value_at_cost"], ascending=False)
    matched = len(out)
    out = out.head(top_n).rename(columns={"SupplierName": "supplier", "SupplierCity": "city"})
    return {"suppliers": records(out.round(2)), "matched": int(matched)}


def branches_view(stock: pd.DataFrame, sales: pd.DataFrame) -> list[dict]:
    cols = ["BranchId", "BranchName", "BranchCity", "BranchState"]
    frames = [d[cols] for d in (stock, sales) if not d.empty and set(cols) <= set(d.columns)]
    if not frames:
        return []
    out = pd.concat(frames).drop_duplicates("BranchId").sort_values("BranchName")
    return records(out.rename(columns={"BranchId": "branch_id", "BranchName": "name",
                                       "BranchCity": "city", "BranchState": "state"}))
