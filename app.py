import json
import os
import re
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd
import requests
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
PROCESSED_DIR = BASE_DIR / "processed"
STATE_PATH = BASE_DIR / "data" / "state.json"
SUPPLIER_URL = "http://sexoptovik.ru/files/all_prod_prices__.csv"
PACKAGING_COST_RUB = 45.0
PROFIT_SHARE = 0.33

REQUIRED_STAT_COLUMNS = {
    "Артикул поставщика",
    "Кол-во",
    "К перечислению Продавцу за реализованный Товар",
    "Код номенклатуры",
    "Баркод",
    "Обоснование для оплаты",
    "Услуги по доставке товара покупателю",
}

DEFAULT_COLUMN_INDICES = list(range(34)) + [36]


def ensure_directories() -> None:
    """Make sure all working directories exist."""
    for folder in (UPLOAD_DIR, PROCESSED_DIR, STATE_PATH.parent):
        folder.mkdir(parents=True, exist_ok=True)


def load_state() -> Dict[str, str]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(state: Dict[str, str]) -> None:
    ensure_directories()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def column_letters_to_indices(columns: Iterable[str], indices: Iterable[int]) -> List[str]:
    available = list(columns)
    result: List[str] = []
    for idx in indices:
        if 0 <= idx < len(available):
            result.append(available[idx])
    return result


def read_statistics(path: Path) -> pd.DataFrame:
    return pd.read_excel(path)


def read_price_catalog(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, sep=";", encoding="cp1251", index_col=False)


def normalise_price_map(price_df: pd.DataFrame) -> Dict[str, float]:
    mapping: Dict[str, float] = {}
    id_column_candidates = [
        "id товара",
        "id товара ".strip(),
    ]
    price_column_candidates = ["цена", "закупка", "цена, руб."]

    id_column = next((c for c in id_column_candidates if c in price_df.columns), None)
    price_column = next((c for c in price_column_candidates if c in price_df.columns), None)
    if not id_column or not price_column:
        return mapping

    for _, row in price_df[[id_column, price_column]].dropna(subset=[price_column]).iterrows():
        try:
            key = str(int(float(row[id_column])))
        except (ValueError, TypeError):
            continue
        try:
            price = float(str(row[price_column]).replace(",", "."))
        except ValueError:
            continue
        mapping[key] = price
    return mapping


def collect_numeric_tokens(value) -> List[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    tokens: List[str] = []
    for item in re.findall(r"\d{3,}", str(value)):
        normalized = item.lstrip("0") or "0"
        if normalized not in tokens:
            tokens.append(normalized)
    return tokens


def resolve_purchase_price(row: pd.Series, price_map: Dict[str, float]) -> Optional[float]:
    candidates: List[str] = []

    article = row.get("Артикул поставщика")
    if isinstance(article, str):
        lowered = article.lower()
        if lowered.startswith(("id-", "id_")):
            match = re.match(r"(?i)id[-_](\d+)", article)
            if match:
                value = match.group(1).lstrip("0") or "0"
                candidates.append(value)

    for field in ("Код номенклатуры", "Баркод"):
        for token in collect_numeric_tokens(row.get(field)):
            if token not in candidates:
                candidates.append(token)

    for candidate in candidates:
        if candidate in price_map:
            return price_map[candidate]
    return None


def allocate_logistics(
    statistics: pd.DataFrame,
    reason_column: str,
    logistics_column: str,
) -> Tuple[pd.DataFrame, Dict[str, float], float]:
    sales = statistics[statistics[reason_column] == "Продажа"].copy()
    logistics = statistics[statistics[reason_column] == "Логистика"].copy()

    logistics_totals = (
        logistics.groupby("Артикул поставщика")[logistics_column].sum().to_dict()
        if not logistics.empty
        else {}
    )
    quantity_totals = (
        sales.groupby("Артикул поставщика")["Кол-во"].sum().to_dict()
        if not sales.empty
        else {}
    )

    logistics_per_unit: Dict[str, float] = {}
    for article, total in logistics_totals.items():
        qty = quantity_totals.get(article)
        if qty and qty > 0:
            logistics_per_unit[article] = float(total) / float(qty)
    total_logistics = float(logistics[logistics_column].sum()) if not logistics.empty else 0.0
    return sales, logistics_per_unit, total_logistics


def compute_profit_table(
    statistics_path: Path,
    price_path: Path,
    selected_columns: Optional[List[str]] = None,
) -> Tuple[pd.DataFrame, Dict[str, float], Dict[str, float]]:
    stats_df = read_statistics(statistics_path)
    price_df = read_price_catalog(price_path)
    price_map = normalise_price_map(price_df)

    missing_columns = [col for col in REQUIRED_STAT_COLUMNS if col not in stats_df.columns]
    if missing_columns:
        formatted = ", ".join(missing_columns)
        raise ValueError(f"В отчете Wildberries отсутствуют столбцы: {formatted}")

    if selected_columns:
        base_columns = [col for col in selected_columns if col in stats_df.columns]
    else:
        base_columns = column_letters_to_indices(stats_df.columns, DEFAULT_COLUMN_INDICES)

    reason_column = "Обоснование для оплаты"
    logistics_column = "Услуги по доставке товара покупателю"
    article_column = "Артикул поставщика"
    quantity_column = "Кол-во"
    payout_column = "К перечислению Продавцу за реализованный Товар"
    fines_column = "Общая сумма штрафов"
    storage_column = "Хранение"

    sale_reason = "Продажа"
    logistics_reason = "Логистика"
    fines_reason = "Штраф"
    storage_reason = "Хранение"

    sales_df = stats_df[stats_df[reason_column] == sale_reason].copy()
    if sales_df.empty:
        raise ValueError("Не найдены строки с продажами в отчете Wildberries.")

    columns_to_use = [col for col in base_columns if col in sales_df.columns]
    for mandatory in (quantity_column, payout_column, article_column, reason_column):
        if mandatory in sales_df.columns and mandatory not in columns_to_use:
            columns_to_use.append(mandatory)
    base_df = sales_df[columns_to_use].copy()

    working_df = sales_df.loc[base_df.index]
    quantity = pd.to_numeric(working_df[quantity_column], errors="coerce").fillna(0.0)
    payout = pd.to_numeric(working_df[payout_column], errors="coerce").fillna(0.0)

    purchase_prices = (
        working_df.apply(lambda row: resolve_purchase_price(row, price_map), axis=1)
        .fillna(0.0)
        .astype(float)
    )
    article_series = working_df[article_column].astype(str)
    quantity_by_article = working_df.groupby(article_column)[quantity_column].sum().to_dict()

    logistics_df = stats_df[stats_df[reason_column] == logistics_reason]
    logistics_by_article = (
        logistics_df.groupby(article_column)[logistics_column].sum().to_dict()
    )

    logistics_per_unit: Dict[str, float] = {}
    matched_logistics_total = 0.0
    for article, total in logistics_by_article.items():
        qty = quantity_by_article.get(article)
        if qty and qty > 0:
            per_unit = float(total) / float(qty)
            logistics_per_unit[article] = per_unit
            matched_logistics_total += float(total)
    total_logistics = float(pd.to_numeric(logistics_df[logistics_column], errors="coerce").fillna(0.0).sum())
    unmatched_logistics = total_logistics - matched_logistics_total
    if abs(unmatched_logistics) < 1e-6:
        unmatched_logistics = 0.0

    fines_df = stats_df[stats_df[reason_column] == fines_reason]
    fines_by_article = fines_df.groupby(article_column)[fines_column].sum().to_dict()
    fines_per_unit: Dict[str, float] = {}
    matched_fines_total = 0.0
    for article, total in fines_by_article.items():
        qty = quantity_by_article.get(article)
        if qty and qty > 0:
            per_unit = float(total) / float(qty)
            fines_per_unit[article] = per_unit
            matched_fines_total += float(total)
    total_fines = float(pd.to_numeric(fines_df[fines_column], errors="coerce").fillna(0.0).sum())
    unmatched_fines = total_fines - matched_fines_total
    if abs(unmatched_fines) < 1e-6:
        unmatched_fines = 0.0

    storage_df = stats_df[stats_df[reason_column] == storage_reason]
    storage_total = float(pd.to_numeric(storage_df[storage_column], errors="coerce").fillna(0.0).sum())
    if abs(storage_total) < 1e-6:
        storage_total = 0.0

    logistics_unit = article_series.map(logistics_per_unit).fillna(0.0).astype(float)
    logistics_total = logistics_unit * quantity
    fines_unit = article_series.map(fines_per_unit).fillna(0.0).astype(float)
    fines_total = fines_unit * quantity

    packaging_unit = pd.Series(PACKAGING_COST_RUB, index=base_df.index)
    packaging_unit = packaging_unit.where(quantity > 0, 0.0)
    packaging_total = packaging_unit * quantity

    accrued = payout - logistics_total - fines_total
    accrued_per_unit = accrued.divide(quantity.replace(0, pd.NA)).fillna(0.0)

    cost_unit = purchase_prices + packaging_unit
    cost_total = cost_unit * quantity

    profit_total = accrued - cost_total
    profit_per_unit = profit_total.divide(quantity.replace(0, pd.NA)).fillna(0.0)
    margin = profit_total.divide(accrued.replace(0, pd.NA)).fillna(0.0) * 100.0

    base_df["Закупочная цена, руб"] = purchase_prices
    base_df["Логистика, руб/ед"] = logistics_unit
    base_df["Логистика, руб итого"] = logistics_total
    base_df["Штрафы, руб"] = fines_total
    base_df["Хранение, руб"] = 0.0
    base_df["Упаковка, руб/ед"] = packaging_unit
    base_df["Упаковка, руб итого"] = packaging_total
    base_df["К перечислению, руб"] = payout
    base_df["Начислено ВБ, руб"] = accrued
    base_df["Начислено ВБ на ед., руб"] = accrued_per_unit
    base_df["Себестоимость на ед., руб"] = cost_unit
    base_df["Себестоимость итого, руб"] = cost_total
    base_df["Прибыль на ед., руб"] = profit_per_unit
    base_df["Прибыль итого, руб"] = profit_total
    base_df["Маржа, %"] = margin

    columns_to_remove = [reason_column, payout_column, logistics_column, fines_column, storage_column]
    base_df = base_df.drop(columns=[col for col in columns_to_remove if col in base_df.columns], errors="ignore")

    adjustment_rows: List[Dict[str, object]] = []
    adjustment_template = {col: "" for col in base_df.columns}

    def make_adjustment(
        label: str,
        *,
        logistics_value: float = 0.0,
        fines_value: float = 0.0,
        storage_value: float = 0.0,
    ) -> Dict[str, object]:
        row = adjustment_template.copy()
        row[article_column] = label
        row[quantity_column] = 0.0
        row["К перечислению, руб"] = 0.0
        row["Логистика, руб/ед"] = 0.0
        row["Логистика, руб итого"] = logistics_value
        row["Штрафы, руб"] = fines_value
        row["Хранение, руб"] = storage_value
        row["Упаковка, руб/ед"] = 0.0
        row["Упаковка, руб итого"] = 0.0
        row["Закупочная цена, руб"] = 0.0
        row["Начислено ВБ, руб"] = -(logistics_value + fines_value + storage_value)
        row["Начислено ВБ на ед., руб"] = 0.0
        row["Себестоимость на ед., руб"] = 0.0
        row["Себестоимость итого, руб"] = 0.0
        row["Прибыль на ед., руб"] = 0.0
        row["Прибыль итого, руб"] = -(logistics_value + fines_value + storage_value)
        row["Маржа, %"] = 0.0
        return row

    if unmatched_logistics:
        adjustment_rows.append(
            make_adjustment("Логистика (без продаж)", logistics_value=float(unmatched_logistics))
        )
    if unmatched_fines:
        adjustment_rows.append(
            make_adjustment("Штрафы (без продаж)", fines_value=float(unmatched_fines))
        )
    if storage_total:
        adjustment_rows.append(
            make_adjustment("Хранение", storage_value=float(storage_total))
        )

    if adjustment_rows:
        adjustments_df = pd.DataFrame(adjustment_rows)
        base_df = pd.concat([base_df, adjustments_df], ignore_index=True)

    preferred_order = [
        article_column,
        quantity_column,
        "К перечислению, руб",
        "Начислено ВБ, руб",
        "Начислено ВБ на ед., руб",
        "Логистика, руб/ед",
        "Логистика, руб итого",
        "Штрафы, руб",
        "Хранение, руб",
        "Упаковка, руб/ед",
        "Упаковка, руб итого",
        "Закупочная цена, руб",
        "Себестоимость на ед., руб",
        "Себестоимость итого, руб",
        "Прибыль на ед., руб",
        "Прибыль итого, руб",
        "Маржа, %",
    ]
    ordered = [col for col in preferred_order if col in base_df.columns]
    base_df = base_df[ordered]

    summary = build_summary(base_df)

    return base_df, price_map, summary



def build_summary(df: pd.DataFrame) -> Dict[str, float]:
    total_qty = float(df["Кол-во"].sum()) if "Кол-во" in df.columns else 0.0
    wb_total = float(df["К перечислению, руб"].sum()) if "К перечислению, руб" in df.columns else 0.0
    total_revenue = float(df["Начислено ВБ, руб"].sum()) if "Начислено ВБ, руб" in df.columns else wb_total
    total_cost = float(df["Себестоимость итого, руб"].sum()) if "Себестоимость итого, руб" in df.columns else 0.0
    total_profit = float(df["Прибыль итого, руб"].sum()) if "Прибыль итого, руб" in df.columns else 0.0
    avg_margin = (total_profit / total_revenue * 100.0) if total_revenue else 0.0
    logistic_total = float(df["Логистика, руб итого"].sum()) if "Логистика, руб итого" in df.columns else 0.0
    packaging_total = float(df["Упаковка, руб итого"].sum()) if "Упаковка, руб итого" in df.columns else 0.0
    fines_total = float(df["Штрафы, руб"].sum()) if "Штрафы, руб" in df.columns else 0.0
    storage_total = float(df["Хранение, руб"].sum()) if "Хранение, руб" in df.columns else 0.0
    our_share = total_profit * PROFIT_SHARE
    return {
        "total_qty": round(total_qty, 2),
        "total_revenue": round(total_revenue, 2),
        "wb_total": round(wb_total, 2),
        "total_cost": round(total_cost, 2),
        "total_profit": round(total_profit, 2),
        "avg_margin": round(avg_margin, 2),
        "our_share": round(our_share, 2),
        "logistics_total": round(logistic_total, 2),
        "packaging_total": round(packaging_total, 2),
        "fines_total": round(fines_total, 2),
        "storage_total": round(storage_total, 2),
    }



def append_totals_row(df: pd.DataFrame) -> pd.DataFrame:
    totals = {
        col: df[col].sum()
        for col in [
            "Кол-во",
            "К перечислению, руб",
            "Начислено ВБ, руб",
            "Себестоимость итого, руб",
            "Прибыль итого, руб",
            "Упаковка, руб итого",
            "Логистика, руб итого",
            "Штрафы, руб",
            "Хранение, руб",
        ]
        if col in df.columns
    }
    totals_row = {col: "" for col in df.columns}
    totals_row.update(totals)
    if "Артикул поставщика" in totals_row:
        totals_row["Артикул поставщика"] = "ИТОГО"
    return pd.concat([df, pd.DataFrame([totals_row])], ignore_index=True)



def save_processed_report(
    df: pd.DataFrame,
    summary: Dict[str, float],
    *,
    output_dir: Optional[Path] = None,
    store_latest_alias: bool = True,
) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target_dir = output_dir or PROCESSED_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    output_path = target_dir / f"wb_profit_{timestamp}.xlsx"
    df_with_totals = append_totals_row(df)
    summary_df = pd.DataFrame(
        [
            {"Показатель": "Начислено ВБ (к оплате), руб", "Значение": summary["total_revenue"]},
            {"Показатель": "К перечислению за товар, руб", "Значение": summary["wb_total"]},
            {"Показатель": "Себестоимость итого, руб", "Значение": summary["total_cost"]},
            {"Показатель": "Прибыль итого, руб", "Значение": summary["total_profit"]},
            {"Показатель": "Наша доля прибыли (33%), руб", "Значение": summary["our_share"]},
            {"Показатель": "Логистика всего, руб", "Значение": summary["logistics_total"]},
            {"Показатель": "Штрафы всего, руб", "Значение": summary.get("fines_total", 0.0)},
            {"Показатель": "Хранение всего, руб", "Значение": summary.get("storage_total", 0.0)},
            {"Показатель": "Упаковка всего, руб", "Значение": summary["packaging_total"]},
            {"Показатель": "Количество, шт", "Значение": summary["total_qty"]},
            {"Показатель": "Маржа, %", "Значение": summary["avg_margin"]},
        ]
    )
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df_with_totals.to_excel(writer, index=False, sheet_name="Продажи")
        summary_df.to_excel(writer, index=False, sheet_name="Сводка")
    if store_latest_alias:
        (target_dir / "latest.xlsx").write_bytes(output_path.read_bytes())
    return output_path


app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET_KEY", "wb-calculator-secret")
app.jinja_env.filters["basename"] = lambda value: Path(value).name if value else ""


@app.before_request
def _ensure_setup() -> None:
    ensure_directories()



def gather_columns(df: pd.DataFrame) -> List[str]:
    return [col for col in df.columns if isinstance(col, str)]


@app.route("/", methods=["GET"])
def index() -> str:
    state = load_state()
    statistics_path = state.get("statistics_path")
    price_path = state.get("price_path")
    selected_columns = state.get("selected_columns") or []
    last_processed = state.get("last_processed")
    last_summary = state.get("last_summary")
    available_columns: List[str] = []
    preview_rows: List[Dict[str, str]] = []

    if statistics_path and Path(statistics_path).exists():
        try:
            df = read_statistics(Path(statistics_path))
            available_columns = gather_columns(df)
            if not selected_columns:
                selected_columns = column_letters_to_indices(df.columns, DEFAULT_COLUMN_INDICES)
            preview_rows = df[selected_columns].head(10).fillna("").to_dict(orient="records")
        except Exception as exc:  # pragma: no cover - defensive for UI
            flash(f"Не удалось открыть файл статистики: {exc}", "danger")

    return render_template(
        "index.html",
        state=state,
        available_columns=available_columns,
        selected_columns=selected_columns,
        preview_rows=preview_rows,
        statistics_ready=bool(statistics_path),
        price_ready=bool(price_path),
        last_processed=last_processed,
        summary=last_summary,
    )


def handle_upload(file_storage, target_dir: Path) -> Optional[Path]:
    if not file_storage or not file_storage.filename:
        return None
    filename = Path(file_storage.filename).name
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = f"{timestamp}_{filename}"
    dest = target_dir / safe_name
    file_storage.save(dest)
    return dest


@app.route("/upload", methods=["POST"])
def upload() -> str:
    state = load_state()

    stat_file = request.files.get("statistics")
    price_file = request.files.get("prices")
    selected_columns = request.form.getlist("columns")

    stat_path = handle_upload(stat_file, UPLOAD_DIR) or state.get("statistics_path")
    price_path = handle_upload(price_file, UPLOAD_DIR) or state.get("price_path")

    if not stat_path or not Path(stat_path).exists():
        flash("Загрузите отчет Wildberries (xlsx).", "warning")
        return redirect(url_for("index"))

    if not price_path or not Path(price_path).exists():
        flash("Загрузите прайс поставщика (csv).", "warning")
        return redirect(url_for("index"))

    try:
        df, price_map, summary = compute_profit_table(Path(stat_path), Path(price_path), selected_columns)
    except Exception as exc:
        flash(f"Ошибка обработки: {exc}", "danger")
        return redirect(url_for("index"))

    output_path = save_processed_report(df, summary)

    state.update(
        {
            "statistics_path": str(stat_path),
            "price_path": str(price_path),
            "selected_columns": selected_columns,
            "last_processed": datetime.now().isoformat(),
            "latest_output": str(output_path),
            "price_items": len(price_map),
            "last_summary": summary,
        }
    )
    save_state(state)
    flash("Файлы обработаны, отчет готов к скачиванию.", "success")
    return redirect(url_for("index"))


@app.route("/download", methods=["GET"])
def download() -> BytesIO:
    state = load_state()
    latest = state.get("latest_output")
    if not latest or not Path(latest).exists():
        flash("Нет готового отчета для скачивания. Сначала загрузите файлы.", "warning")
        return redirect(url_for("index"))

    buffer = BytesIO()
    buffer.write(Path(latest).read_bytes())
    buffer.seek(0)
    filename = Path(latest).name
    return send_file(
        buffer,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def download_latest_price_catalog(url: str = SUPPLIER_URL) -> Path:
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_all_prod_prices__.csv"
    destination = UPLOAD_DIR / filename
    destination.write_bytes(response.content)
    return destination


@app.route("/refresh-price", methods=["POST"])
def refresh_price() -> str:
    try:
        price_path = download_latest_price_catalog()
    except requests.HTTPError as exc:
        flash(f"Не удалось скачать прайс: {exc.response.status_code}", "danger")
        return redirect(url_for("index"))
    except requests.RequestException as exc:
        flash(f"Ошибка сети при загрузке прайса: {exc}", "danger")
        return redirect(url_for("index"))

    state = load_state()
    state.update(
        {
            "price_path": str(price_path),
            "price_updated": datetime.now().isoformat(),
        }
    )
    save_state(state)
    flash("Прайс Sexoptovik обновлен.", "success")
    return redirect(url_for("index"))


if __name__ == "__main__":
    ensure_directories()
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
