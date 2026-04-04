# ... existing code ...
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

_cached_dfs = None

# ---------------------------------------------------------------------------
# Configuración del archivo Excel
# ---------------------------------------------------------------------------
# ... existing code ...
# ---------------------------------------------------------------------------
# Helpers internos de bajo nivel
# ---------------------------------------------------------------------------

def _read_excel() -> dict[str, pd.DataFrame]:
    """Lee todas las hojas del Excel y devuelve un dict sheet→DataFrame."""
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
    """Escribe todas las hojas al archivo Excel local."""
    global _cached_dfs
    _cached_dfs = {k: v.copy() for k, v in dfs.items()}

    with pd.ExcelWriter(EXCEL_PATH, engine="openpyxl", mode="w") as writer:
        for sheet, df in dfs.items():
            df.to_excel(writer, sheet_name=sheet, index=False)


def _write_and_sync(dfs: dict[str, pd.DataFrame]) -> None:
    """
    Escribe el Excel localmente y luego lo sincroniza con GitHub.
    Todos los métodos de escritura deben usar esta función en lugar de _write_excel().
    """
    _write_excel(dfs)
    
    # Optimización 1: Hilo en segundo plano para evitar congelar la app
    t = threading.Thread(target=_github_push, daemon=True)
    add_script_run_ctx(t) # Conectar contexto para permitir los mensajes st.toast
    t.start()


def _new_id() -> str:
# ... existing code ...
