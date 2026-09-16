"""
Aplica o schema (db/schema.sql) e carrega os CSVs extraídos em data/ para o
Postgres novo (DATABASE_URL).

    python scripts/load_reference_data.py
"""
import csv
import os
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
SCHEMA_PATH = BASE_DIR / "db" / "schema.sql"

TABELAS = {
    "cnpj_tipo_tributacao": ["cnpj", "ano", "tipo_tributacao"],
    "cnpj_lei_do_bem": ["cnpj", "razao_social", "ano", "uf"],
}


def aplicar_schema(conn) -> None:
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        cur = conn.cursor()
        cur.execute(f.read())
        cur.close()
    conn.commit()


def carregar_tabela(conn, tabela: str, colunas: list[str]) -> None:
    csv_path = DATA_DIR / f"{tabela}.csv"
    if not csv_path.exists():
        print(f"[aviso] {csv_path} não encontrado, pulando {tabela}. Rode scripts/extract_reference_data.py antes.")
        return

    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        linhas = [tuple((row.get(c) or None) for c in colunas) for row in reader]

    if not linhas:
        print(f"{tabela}: CSV vazio, nada a carregar.")
        return

    cur = conn.cursor()
    cur.execute(f"TRUNCATE TABLE {tabela} RESTART IDENTITY")
    colunas_sql = ", ".join(colunas)
    # execute_values agrupa várias linhas por INSERT - muito mais rápido que
    # executemany (uma linha por vez) para tabelas grandes como esta.
    psycopg2.extras.execute_values(
        cur, f"INSERT INTO {tabela} ({colunas_sql}) VALUES %s", linhas, page_size=2000
    )
    conn.commit()
    cur.close()
    print(f"{tabela}: {len(linhas)} linhas carregadas.")


def main() -> None:
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        aplicar_schema(conn)
        for tabela, colunas in TABELAS.items():
            carregar_tabela(conn, tabela, colunas)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
