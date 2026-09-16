"""Acesso ao Postgres (Neon). Uma conexão por request - sem pool
persistente em memória, já que a app roda em funções serverless (Vercel),
onde cada invocação pode ser uma instância diferente."""
import os

import psycopg2
import psycopg2.extras


def get_conn():
    # connect_timeout evita que uma conexão travada com o Neon prenda a
    # invocação inteira (importante em função serverless, com tempo limite).
    return psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=10)


def dict_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
