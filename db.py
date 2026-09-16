"""Acesso ao Postgres (Neon). Uma conexão por request - sem pool
persistente em memória, já que a app roda em funções serverless (Vercel),
onde cada invocação pode ser uma instância diferente."""
import os

import psycopg2
import psycopg2.extras


def get_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def dict_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
