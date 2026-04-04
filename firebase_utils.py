# -*- coding: utf-8 -*-
"""
firebase_utils.py  –  Capa de base de datos: Excel local + sincronización automática a GitHub
Optimizada con Caché en Memoria RAM y Sincronización Asíncrona (Background Threading)
"""

import logging
import json
import uuid
import time
import base64
import requests
import threading
from datetime import datetime, timezone
from threading import Lock
from streamlit.runtime.scriptrunner import add_script_run_ctx

import pandas as pd
import streamlit as st

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- OPTIMIZACIÓN 2: Caché en RAM para evitar lecturas constantes de disco ---
_cached_dfs = None

# ---------------------------------------------------------------------------
# Configuración del archivo Excel
# ---------------------------------------------------------------------------
EXCEL_PATH = "SAVA_DB.xlsx"

# Columnas esperadas por hoja
SHEET_COLUMNS = {
    "inventory": [
        "id", "name", "quantity", "purchase_price", "sale_price",
        "min_stock_alert", "supplier_id", "supplier_name", "updated_at"
    ],
    "inventory_history": [
        "id", "item_id", "timestamp", "type", "quantity_change", "details"
    ],
    "orders": [
        "id", "title", "price", "status", "timestamp", "completed_at",
        "payment_method", "customer_name", "is_direct_sale", "ingredients_json"
    ],
    "suppliers": [
        "id", "name", "contact_person", "email", "phone"
    ],
}

_excel_lock = Lock()  # Semáforo para evitar escrituras concurrentes


# ---------------------------------------------------------------------------
# Sincronización con GitHub
# ---------------------------------------------------------------------------

def _github_headers() -> dict:
    """Retorna los headers de autenticación para la API de GitHub."""
    token = st.secrets.get("GITHUB_TOKEN", "")
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }


def _github_api_url() -> str:
    """Construye la URL de la API de GitHub para el archivo SAVA_DB.xlsx."""
    repo = st.secrets.get("GITHUB_REPO", "")
    db_path = st.secrets.get("GITHUB_DB_PATH", "SAVA_DB.xlsx")
    return f"https://api.github.com/repos/{repo}/contents/{db_path}"


def _is_github_configured() -> bool:
    """Verifica que los secrets de GitHub estén presentes y muestra advertencia si faltan."""
    token = st.secrets.get("GITHUB_TOKEN", "")
    repo  = st.secrets.get("GITHUB_REPO", "")
    if not token or not repo:
        logger.warning(
            "⚠️  GitHub sync DESACTIVADO. "
            "Agrega GITHUB_TOKEN y GITHUB_REPO en los Secrets de Streamlit."
        )
        return False
    return True


def _github_pull() -> None:
    """
    Descarga SAVA_DB.xlsx desde GitHub al sistema de archivos local.
    Se llama al arrancar la aplicación para tener siempre la versión más reciente.
    """
    if not _is_github_configured():
        return

    try:
        url = _github_api_url()
        logger.info(f"GitHub pull → {url}")
        resp = requests.get(url, headers=_github_headers(), timeout=30)

        if resp.status_code == 404:
            logger.info("SAVA_DB.xlsx no existe en GitHub todavía. Se creará al primer guardado.")
            return

        if resp.status_code != 200:
            msg = f"GitHub pull falló: HTTP {resp.status_code} — {resp.text[:300]}"
            logger.error(msg)
            st.warning(f"⚠️ {msg}")
            return

        data = resp.json()

        # Archivos >1 MB: GitHub devuelve download_url en lugar de content inline
        if data.get("encoding") == "base64" and data.get("content"):
            file_bytes = base64.b64decode(data["content"])
        elif data.get("download_url"):
            dl_resp = requests.get(data["download_url"], timeout=60)
            dl_resp.raise_for_status()
            file_bytes = dl_resp.content
        else:
            logger.warning("GitHub pull: respuesta inesperada, sin contenido.")
            return

        with open(EXCEL_PATH, "wb") as f:
            f.write(file_bytes)

        logger.info(f"✅ SAVA_DB.xlsx descargado desde GitHub ({len(file_bytes)/1024:.1f} KB).")

    except Exception as e:
        logger.error(f"GitHub pull error: {e}")
        st.warning(f"⚠️ Error al descargar la base de datos desde GitHub: {e}")


def _github_push() -> None:
    """
    Sube SAVA_DB.xlsx al repositorio de GitHub después de cada escritura.
    Muestra feedback visible al usuario (st.toast) con éxito o error.
    """
    if not _is_github_configured():
        st.warning(
            "⚠️ **GitHub Sync desactivado.** "
            "Para que los datos no se pierdan, agrega los secrets: "
            "`GITHUB_TOKEN`, `GITHUB_REPO` y `GITHUB_DB_PATH` en Streamlit → Settings → Secrets."
        )
        return

    try:
        url = _github_api_url()
        headers = _github_headers()

        # ---------- 1. Leer archivo local ----------
        with open(EXCEL_PATH, "rb") as f:
            raw_bytes = f.read()
        content_b64 = base64.b64encode(raw_bytes).decode("utf-8")
        logger.info(f"GitHub push → {url}  ({len(raw_bytes)/1024:.1f} KB)")

        # ---------- 2. Obtener SHA actual del archivo en GitHub ----------
        sha = ""
        get_resp = requests.get(url, headers=headers, timeout=15)
        if get_resp.status_code == 200:
            sha = get_resp.json().get("sha", "")
            logger.info(f"SHA actual en GitHub: {sha[:10]}...")
        elif get_resp.status_code == 404:
            logger.info("El archivo no existe aún en GitHub; se creará ahora.")
        else:
            logger.warning(f"No se pudo obtener SHA: HTTP {get_resp.status_code} — {get_resp.text[:200]}")

        # ---------- 3. Subir el archivo ----------
        commit_msg = (
            f"Auto-sync SAVA_DB.xlsx "
            f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC]"
        )
        payload = {"message": commit_msg, "content": content_b64}
        if sha:
            payload["sha"] = sha

        put_resp = requests.put(url, headers=headers, json=payload, timeout=60)

        if put_resp.status_code in (200, 201):
            logger.info("✅ SAVA_DB.xlsx sincronizado con GitHub correctamente.")
            st.toast("☁️ Datos guardados y sincronizados con GitHub", icon="✅")
        else:
            error_detail = put_resp.text[:500]
            msg = f"GitHub push falló: HTTP {put_resp.status_code} — {error_detail}"
            logger.error(msg)
            st.warning(f"⚠️ Error de sincronización: HTTP {put_resp.status_code}\n\n{error_detail}")

    except FileNotFoundError:
        logger.error(f"No se encontró el archivo local {EXCEL_PATH} para subir a GitHub.")
        st.warning(f"⚠️ No se encontró {EXCEL_PATH} para sincronizar.")
    except Exception as e:
        logger.error(f"GitHub push error inesperado: {e}")
        st.warning(f"⚠️ Error inesperado al sincronizar con GitHub: {e}")


# ---------------------------------------------------------------------------
# Helpers internos de bajo nivel (Optimizados en RAM)
# ---------------------------------------------------------------------------

def _read_excel() -> dict[str, pd.DataFrame]:
    """Lee todas las hojas del Excel. Devuelve el caché en memoria si existe."""
    global _cached_dfs
    if _cached_dfs is not None:
        return {k: v.copy() for k, v in _cached_dfs.items()}

    try:
        xls = pd.ExcelFile(EXCEL_PATH, engine="openpyxl")
        dfs = {}
        for sheet, cols in SHEET_COLUMNS.items():
            if sheet in xls.sheet_names:
                df = xls.parse(sheet, dtype=str)
                for col in cols:
                    if col not in df.columns:
                        df[col] = ""
                dfs[sheet] = df[cols]
            else:
                dfs[sheet] = pd.DataFrame(columns=cols)
        
        _cached_dfs = {k: v.copy() for k, v in dfs.items()}
        return dfs
    except FileNotFoundError:
        dfs = {sheet: pd.DataFrame(columns=cols) for sheet, cols in SHEET_COLUMNS.items()}
        _cached_dfs = {k: v.copy() for k, v in dfs.items()}
        return dfs


def _write_excel(dfs: dict[str, pd.DataFrame]) -> None:
    """Escribe todas las hojas al archivo Excel local y actualiza el caché."""
    global _cached_dfs
    _cached_dfs = {k: v.copy() for k, v in dfs.items()}

    with pd.ExcelWriter(EXCEL_PATH, engine="openpyxl", mode="w") as writer:
        for sheet, df in dfs.items():
            df.to_excel(writer, sheet_name=sheet, index=False)


def _write_and_sync(dfs: dict[str, pd.DataFrame]) -> None:
    """
    Escribe el Excel localmente y luego lo sincroniza con GitHub de forma asíncrona.
    """
    _write_excel(dfs)
    
    # --- OPTIMIZACIÓN 1: Hilo en segundo plano para evitar congelar la UI ---
    t = threading.Thread(target=_github_push, daemon=True)
    add_script_run_ctx(t) # Permite que st.toast se muestre desde el hilo
    t.start()


def _new_id() -> str:
    """Genera un ID único corto (20 chars)."""
    return uuid.uuid4().hex[:20]


def _now_str() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ingredients(row) -> list:
    """Deserializa el campo JSON de ingredientes almacenado en la hoja 'orders'."""
    raw = row.get("ingredients_json", "")
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except Exception:
            return []
    return []


def _row_to_order(row: pd.Series) -> dict:
    """Convierte una fila de la hoja 'orders' al dict que usa app.py."""
    order = row.to_dict()
    order["ingredients"] = _parse_ingredients(order)
    order.pop("ingredients_json", None)

    ts_raw = order.get("timestamp", "")
    try:
        ts = datetime.fromisoformat(str(ts_raw))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        order["timestamp_obj"] = ts
        order["timestamp"] = ts
    except Exception:
        order["timestamp_obj"] = datetime.min.replace(tzinfo=timezone.utc)

    ca_raw = order.get("completed_at", "")
    try:
        if ca_raw and str(ca_raw).strip() and str(ca_raw) != "nan":
            ca = datetime.fromisoformat(str(ca_raw))
            if ca.tzinfo is None:
                ca = ca.replace(tzinfo=timezone.utc)
            order["completed_at"] = ca
        else:
            order["completed_at"] = None
    except Exception:
        order["completed_at"] = None

    try:
        order["price"] = float(order.get("price", 0) or 0)
    except Exception:
        order["price"] = 0.0

    order["is_direct_sale"] = str(order.get("is_direct_sale", "")).lower() in ("true", "1", "yes")
    return order


def _row_to_inventory(row: pd.Series) -> dict:
    """Convierte una fila de la hoja 'inventory' al dict que usa app.py."""
    item = row.to_dict()
    for num_col in ("quantity", "min_stock_alert"):
        try:
            v = item.get(num_col)
            item[num_col] = int(float(v)) if v not in (None, "", "nan", "None") else 0
        except Exception:
            item[num_col] = 0
    for float_col in ("purchase_price", "sale_price"):
        try:
            v = item.get(float_col)
            item[float_col] = float(v) if v not in (None, "", "nan", "None") else 0.0
        except Exception:
            item[float_col] = 0.0
    for k, v in item.items():
        if str(v) in ("nan", "None", "NaT"):
            item[k] = None
    return item


# ---------------------------------------------------------------------------
# Decorador de reintentos
# ---------------------------------------------------------------------------

def firestore_retry(func):
    """Reintenta la función hasta 3 veces con backoff exponencial."""
    def wrapper(*args, **kwargs):
        max_retries = 3
        delay = 1
        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.warning(f"Attempt {attempt + 1} failed for {func.__name__}: {e}. Retrying...")
                time.sleep(delay)
                delay *= 2
        logger.error(f"All retries failed for {func.__name__}.")
        raise
    return wrapper


# ---------------------------------------------------------------------------
# Clase principal Manager
# ---------------------------------------------------------------------------

class FirebaseManager:
    """
    Capa de base de datos sobre Excel local con sincronización automática a GitHub.
    """

    def __init__(self):
        self._ensure_excel_exists()

    def _ensure_excel_exists(self):
        """
        Al arrancar: descarga SAVA_DB.xlsx desde GitHub (versión más reciente),
        y si no existe en ningún lado, crea uno vacío.
        """
        import os
        _github_pull()

        if not os.path.exists(EXCEL_PATH):
            logger.info(f"Creando archivo Excel nuevo: {EXCEL_PATH}")
            dfs = {sheet: pd.DataFrame(columns=cols) for sheet, cols in SHEET_COLUMNS.items()}
            _write_and_sync(dfs)
        else:
            try:
                existing_sheets = pd.ExcelFile(EXCEL_PATH, engine="openpyxl").sheet_names
                if set(SHEET_COLUMNS.keys()) - set(existing_sheets):
                    dfs = _read_excel()
                    _write_excel(dfs)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # INVENTARIO
    # ------------------------------------------------------------------

    @firestore_retry
    def save_inventory_item(self, data: dict, custom_id: str, is_new: bool = False, details: str = None):
        """Crea o actualiza un ítem de inventario y agrega entrada al historial."""
        with _excel_lock:
            dfs = _read_excel()
            df_inv = dfs["inventory"]
            df_hist = dfs["inventory_history"]

            history_type = "Stock Inicial" if is_new else "Ajuste Manual"
            details = details or ("Item created in the system." if is_new else "Item updated manually.")

            row = {
                "id": custom_id,
                "name": data.get("name", ""),
                "quantity": data.get("quantity", 0),
                "purchase_price": data.get("purchase_price", 0.0),
                "sale_price": data.get("sale_price", 0.0),
                "min_stock_alert": data.get("min_stock_alert", 0),
                "supplier_id": data.get("supplier_id", "") or "",
                "supplier_name": data.get("supplier_name", "") or "",
                "updated_at": data.get("updated_at", _now_str()),
            }

            idx = df_inv.index[df_inv["id"] == custom_id].tolist()
            if idx:
                for k, v in row.items():
                    df_inv.at[idx[0], k] = v
            else:
                df_inv = pd.concat([df_inv, pd.DataFrame([row])], ignore_index=True)

            hist_row = {
                "id": _new_id(),
                "item_id": custom_id,
                "timestamp": _now_str(),
                "type": history_type,
                "quantity_change": data.get("quantity", 0),
                "details": details,
            }
            df_hist = pd.concat([df_hist, pd.DataFrame([hist_row])], ignore_index=True)

            dfs["inventory"] = df_inv
            dfs["inventory_history"] = df_hist
            _write_and_sync(dfs)  # ← Guarda + sube a GitHub (en hilo secundario)
            logger.info(f"Inventory item saved/updated: {custom_id}")

    def delete_inventory_item(self, doc_id: str):
        """Elimina un ítem de inventario y todo su historial."""
        try:
            with _excel_lock:
                dfs = _read_excel()
                dfs["inventory"] = dfs["inventory"][dfs["inventory"]["id"] != doc_id]
                dfs["inventory_history"] = dfs["inventory_history"][dfs["inventory_history"]["item_id"] != doc_id]
                _write_and_sync(dfs)
            logger.info(f"Inventory item {doc_id} and history deleted.")
        except Exception as e:
            logger.error(f"Error deleting inventory item {doc_id}: {e}")
            raise e

    @firestore_retry
    def get_inventory_item_details(self, doc_id: str):
        """Devuelve el dict de un ítem por su ID, o None si no existe."""
        dfs = _read_excel()
        df = dfs["inventory"]
        rows = df[df["id"] == doc_id]
        if rows.empty:
            return None
        return _row_to_inventory(rows.iloc[0])

    @firestore_retry
    def get_all_inventory_items(self) -> list:
        """Devuelve todos los ítems de inventario ordenados por nombre."""
        dfs = _read_excel()
        df = dfs["inventory"]
        items = [_row_to_inventory(row) for _, row in df.iterrows()]
        return sorted(items, key=lambda x: (x.get("name") or "").lower())

    # ------------------------------------------------------------------
    # ÓRDENES / VENTAS
    # ------------------------------------------------------------------

    @firestore_retry
    def create_order(self, order_data: dict):
        """Crea una nueva orden."""
        with _excel_lock:
            dfs = _read_excel()
            df_orders = dfs["orders"]

            enriched_ingredients = []
            for ing in order_data.get("ingredients", []):
                item_details = self.get_inventory_item_details(ing["id"])
                if item_details:
                    ing["purchase_price"] = item_details.get("purchase_price", 0)
                    ing["sale_price"] = item_details.get("sale_price", 0)
                enriched_ingredients.append(ing)

            order_id = _new_id()
            ts = order_data.get("timestamp", datetime.now(timezone.utc))
            ts_str = ts.isoformat() if isinstance(ts, datetime) else str(ts)

            row = {
                "id": order_id,
                "title": order_data.get("title", ""),
                "price": order_data.get("price", 0.0),
                "status": order_data.get("status", "processing"),
                "timestamp": ts_str,
                "completed_at": "",
                "payment_method": order_data.get("payment_method", "efectivo"),
                "customer_name": order_data.get("customer_name", "Cliente General"),
                "is_direct_sale": str(order_data.get("is_direct_sale", False)),
                "ingredients_json": json.dumps(enriched_ingredients, ensure_ascii=False, default=str),
            }
            dfs["orders"] = pd.concat([df_orders, pd.DataFrame([row])], ignore_index=True)
            _write_and_sync(dfs)
            logger.info(f"New order created: {order_id}")

    @firestore_retry
    def get_order_count(self) -> int:
        dfs = _read_excel()
        return len(dfs["orders"])

    @firestore_retry
    def get_orders(self, status: str = None) -> list:
        """Devuelve órdenes ordenadas por timestamp descendente."""
        dfs = _read_excel()
        df = dfs["orders"]
        if df.empty:
            return []
        if status:
            df = df[df["status"] == status]
        orders = [_row_to_order(row) for _, row in df.iterrows()]
        return sorted(
            orders,
            key=lambda x: x.get("timestamp_obj", datetime.min.replace(tzinfo=timezone.utc)),
            reverse=True
        )

    @firestore_retry
    def get_orders_in_date_range(self, start_date: datetime, end_date: datetime) -> list:
        """Devuelve órdenes completadas dentro de un rango de fechas."""
        dfs = _read_excel()
        df = dfs["orders"]
        if df.empty:
            return []

        result = []
        for _, row in df.iterrows():
            order = _row_to_order(row)
            if order.get("status") != "completed":
                continue
            ca = order.get("completed_at")
            if ca is None:
                continue
            if ca.tzinfo is None:
                ca = ca.replace(tzinfo=timezone.utc)
            if start_date.tzinfo is None:
                start_date = start_date.replace(tzinfo=timezone.utc)
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
            if start_date <= ca < end_date:
                result.append(order)
        return result

    @firestore_retry
    def cancel_order(self, order_id: str):
        """Cancela (elimina) una orden."""
        with _excel_lock:
            dfs = _read_excel()
            dfs["orders"] = dfs["orders"][dfs["orders"]["id"] != order_id]
            _write_and_sync(dfs)
            logger.info(f"Order {order_id} cancelled.")

    def complete_order(self, order_id: str):
        """
        Completa una orden: descuenta stock, registra historial,
        y actualiza el estado. Devuelve (bool, str, list).
        """
        with _excel_lock:
            try:
                dfs = _read_excel()
                df_orders = dfs["orders"]
                df_inv = dfs["inventory"]
                df_hist = dfs["inventory_history"]

                order_rows = df_orders[df_orders["id"] == order_id]
                if order_rows.empty:
                    return False, "El pedido no existe.", []

                order_data = _row_to_order(order_rows.iloc[0])
                ingredients = order_data.get("ingredients", [])

                # Validar stock primero
                for ing in ingredients:
                    inv_rows = df_inv[df_inv["id"] == ing["id"]]
                    if inv_rows.empty:
                        return False, f"Ingrediente '{ing.get('name')}' no encontrado.", []
                    current_qty = int(float(inv_rows.iloc[0].get("quantity") or 0))
                    if current_qty < ing.get("quantity", 0):
                        return False, f"Stock insuficiente para '{ing.get('name')}'.", []

                low_stock_alerts = []
                now_str = _now_str()

                for ing in ingredients:
                    idx = df_inv.index[df_inv["id"] == ing["id"]].tolist()
                    if not idx:
                        continue
                    i = idx[0]
                    current_qty = int(float(df_inv.at[i, "quantity"] or 0))
                    new_qty = current_qty - ing["quantity"]
                    df_inv.at[i, "quantity"] = new_qty

                    hist_row = {
                        "id": _new_id(),
                        "item_id": ing["id"],
                        "timestamp": now_str,
                        "type": "Venta (Pedido)",
                        "quantity_change": -ing["quantity"],
                        "details": f"Pedido ID: {order_id}",
                    }
                    df_hist = pd.concat([df_hist, pd.DataFrame([hist_row])], ignore_index=True)

                    try:
                        min_alert = int(float(df_inv.at[i, "min_stock_alert"] or 0))
                    except Exception:
                        min_alert = 0
                    if min_alert and 0 < new_qty <= min_alert:
                        low_stock_alerts.append(
                            f"'{df_inv.at[i, 'name']}' ha alcanzado el umbral mínimo ({new_qty}/{min_alert})."
                        )

                ord_idx = df_orders.index[df_orders["id"] == order_id].tolist()[0]
                df_orders.at[ord_idx, "status"] = "completed"
                df_orders.at[ord_idx, "completed_at"] = now_str

                dfs["inventory"] = df_inv
                dfs["inventory_history"] = df_hist
                dfs["orders"] = df_orders
                _write_and_sync(dfs)

                return True, f"Pedido '{order_data.get('title')}' completado.", low_stock_alerts

            except Exception as e:
                logger.error(f"Error completing order {order_id}: {e}")
                return False, f"Error: {str(e)}", []

    def process_direct_sale(self, items_sold: list, sale_id: str, payment_data: dict = None):
        """
        Procesa una venta directa: descuenta stock, historial y crea orden.
        Devuelve (bool, str, list).
        """
        with _excel_lock:
            try:
                dfs = _read_excel()
                df_inv = dfs["inventory"]
                df_hist = dfs["inventory_history"]
                df_orders = dfs["orders"]

                if payment_data is None:
                    payment_data = {"method": "efectivo", "customer": "Cliente General"}

                total_sale_amount = 0.0
                enriched_ingredients = []

                for sold_item in items_sold:
                    inv_rows = df_inv[df_inv["id"] == sold_item["id"]]
                    if inv_rows.empty:
                        return False, f"Producto '{sold_item.get('name')}' no encontrado.", []
                    item_data = _row_to_inventory(inv_rows.iloc[0])
                    current_qty = item_data.get("quantity", 0)
                    if current_qty < sold_item["quantity"]:
                        return False, f"Stock insuficiente para '{sold_item.get('name')}'.", []

                    sale_price = item_data.get("sale_price", 0.0)
                    total_sale_amount += sale_price * sold_item["quantity"]
                    enriched_ingredients.append({
                        "id": sold_item["id"],
                        "name": item_data.get("name"),
                        "quantity": sold_item["quantity"],
                        "sale_price": sale_price,
                        "purchase_price": item_data.get("purchase_price", 0.0),
                    })

                low_stock_alerts = []
                now_str = _now_str()

                for sold_item in items_sold:
                    idx = df_inv.index[df_inv["id"] == sold_item["id"]].tolist()
                    if not idx:
                        continue
                    i = idx[0]
                    current_qty = int(float(df_inv.at[i, "quantity"] or 0))
                    new_qty = current_qty - sold_item["quantity"]
                    df_inv.at[i, "quantity"] = new_qty

                    hist_row = {
                        "id": _new_id(),
                        "item_id": sold_item["id"],
                        "timestamp": now_str,
                        "type": "Venta Directa",
                        "quantity_change": -sold_item["quantity"],
                        "details": f"ID de Venta: {sale_id}",
                    }
                    df_hist = pd.concat([df_hist, pd.DataFrame([hist_row])], ignore_index=True)

                    try:
                        min_alert = int(float(df_inv.at[i, "min_stock_alert"] or 0))
                    except Exception:
                        min_alert = 0
                    if min_alert and 0 < new_qty <= min_alert:
                        low_stock_alerts.append(
                            f"'{df_inv.at[i, 'name']}' ha alcanzado el umbral mínimo ({new_qty}/{min_alert})."
                        )

                title_suffix = sale_id.split("-")[-1] if "-" in sale_id else sale_id
                order_row = {
                    "id": sale_id,
                    "title": f"Venta Directa {title_suffix}",
                    "price": total_sale_amount,
                    "status": "completed",
                    "timestamp": now_str,
                    "completed_at": now_str,
                    "payment_method": payment_data.get("method", "efectivo"),
                    "customer_name": payment_data.get("customer", "Cliente General"),
                    "is_direct_sale": "True",
                    "ingredients_json": json.dumps(enriched_ingredients, ensure_ascii=False, default=str),
                }
                df_orders = pd.concat([df_orders, pd.DataFrame([order_row])], ignore_index=True)

                dfs["inventory"] = df_inv
                dfs["inventory_history"] = df_hist
                dfs["orders"] = df_orders
                _write_and_sync(dfs)

                return True, f"Venta '{sale_id}' procesada y stock actualizado.", low_stock_alerts

            except Exception as e:
                logger.error(f"Error processing direct sale {sale_id}: {e}")
                return False, f"Error: {str(e)}", []

    # ------------------------------------------------------------------
    # PROVEEDORES
    # ------------------------------------------------------------------

    @firestore_retry
    def add_supplier(self, supplier_data: dict):
        """Agrega un nuevo proveedor."""
        with _excel_lock:
            dfs = _read_excel()
            row = {
                "id": _new_id(),
                "name": supplier_data.get("name", ""),
                "contact_person": supplier_data.get("contact_person", ""),
                "email": supplier_data.get("email", ""),
                "phone": supplier_data.get("phone", ""),
            }
            dfs["suppliers"] = pd.concat([dfs["suppliers"], pd.DataFrame([row])], ignore_index=True)
            _write_and_sync(dfs)
            logger.info("New supplier added.")

    @firestore_retry
    def get_all_suppliers(self) -> list:
        """Devuelve todos los proveedores ordenados por nombre."""
        dfs = _read_excel()
        df = dfs["suppliers"]
        suppliers = []
        for _, row in df.iterrows():
            s = row.to_dict()
            for k, v in s.items():
                if str(v) in ("nan", "None", "NaT"):
                    s[k] = ""
            suppliers.append(s)
        return sorted(suppliers, key=lambda x: (x.get("name") or "").lower())
