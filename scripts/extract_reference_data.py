"""
Extrai as tabelas de referência (regime tributário e Lei do Bem) do banco
legado (MySQL, schema "services" hospedado na mesma instância do CPJ) para
arquivos CSV em data/.

Uso único, durante a migração para o banco novo (Postgres/Neon). Precisa de
acesso de rede ao host legado (DB_HOST_RPL) com as credenciais guardadas em
.env.migration.

    python scripts/extract_reference_data.py
"""
import csv
from pathlib import Path

import mysql.connector
from dotenv import dotenv_values

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

# Nome da coluna -> colunas desejadas na tabela nova. Se a origem não tiver
# alguma dessas colunas (ex.: razao_social ainda não existir lá), ela é
# gravada vazia no CSV em vez de quebrar a extração.
TABELAS = {
    "cnpj_tipo_tributacao": ["cnpj", "ano", "tipo_tributacao"],
    "cnpj_lei_do_bem": ["cnpj", "razao_social", "ano", "uf"],
}


def extrair_tabela(cursor, tabela: str, colunas_desejadas: list[str]) -> None:
    cursor.execute(f"SELECT * FROM {tabela}")
    colunas_disponiveis = [d[0] for d in cursor.description]
    linhas = cursor.fetchall()

    faltando = [c for c in colunas_desejadas if c not in colunas_disponiveis]
    if faltando:
        print(f"[aviso] {tabela}: colunas não encontradas na origem, serão gravadas vazias: {faltando}")

    destino = DATA_DIR / f"{tabela}.csv"
    with open(destino, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(colunas_desejadas)
        for linha in linhas:
            registro = dict(zip(colunas_disponiveis, linha))
            writer.writerow([registro.get(c, "") for c in colunas_desejadas])

    print(f"{tabela}: {len(linhas)} linhas exportadas para {destino}")


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)

    credenciais = dotenv_values(BASE_DIR / ".env.migration")
    faltando = [k for k in ("DB_HOST_RPL", "DB_USER", "DB_PASSWORD", "DB_NAME_SERVICES") if not credenciais.get(k)]
    if faltando:
        raise SystemExit(f"Faltam variáveis em .env.migration: {', '.join(faltando)}")

    conn = mysql.connector.connect(
        host=credenciais["DB_HOST_RPL"],
        user=credenciais["DB_USER"],
        password=credenciais["DB_PASSWORD"],
        database=credenciais["DB_NAME_SERVICES"],
    )
    cursor = conn.cursor()

    for tabela, colunas in TABELAS.items():
        extrair_tabela(cursor, tabela, colunas)

    cursor.close()
    conn.close()


if __name__ == "__main__":
    main()
