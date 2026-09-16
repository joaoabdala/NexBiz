"""
Cria o primeiro tenant e o primeiro usuário admin no Postgres novo.
Roda uma única vez, na primeira configuração do ambiente (resolve o
"ovo e galinha": a tela de admin exige login de admin, mas ainda não existe
nenhum usuário).

Cada valor pode vir de variável de ambiente (útil pra não digitar tudo à
mão) ou, se não estiver definida, é pedido interativamente:

    SEED_TENANT_NAME, SEED_TENANT_SLUG, SEED_ADMIN_NAME,
    SEED_ADMIN_EMAIL, SEED_ADMIN_PASSWORD

    python scripts/seed_admin.py
"""
import getpass
import os
import re

import psycopg2
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash

load_dotenv()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def valor(env_var: str, prompt: str, obrigatorio: bool = True, senha: bool = False) -> str:
    """Lê de variável de ambiente; se ausente, pede interativamente."""
    v = os.getenv(env_var)
    if v:
        return v.strip()
    if senha:
        v = getpass.getpass(prompt)
    else:
        v = input(prompt).strip()
    if obrigatorio and not v:
        raise SystemExit(f"{env_var} não informado e campo é obrigatório.")
    return v


def main() -> None:
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    print("== Tenant (empresa cliente) ==")
    tenant_nome = valor("SEED_TENANT_NAME", "Nome do tenant: ")
    tenant_slug = valor("SEED_TENANT_SLUG", "Slug do tenant (ex: abdala-nexus): ").lower()

    cur.execute(
        """
        INSERT INTO tenants (name, slug) VALUES (%s, %s)
        ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
        """,
        (tenant_nome, tenant_slug),
    )
    tenant_id = cur.fetchone()[0]

    print("\n== Usuário admin ==")
    nome = valor("SEED_ADMIN_NAME", "Nome: ")
    email = valor("SEED_ADMIN_EMAIL", "E-mail: ").lower()
    if not EMAIL_RE.match(email):
        raise SystemExit(f"E-mail inválido: '{email}'.")

    senha = valor("SEED_ADMIN_PASSWORD", "Senha (mín. 8 caracteres): ", senha=True)
    if len(senha) < 8:
        raise SystemExit("Senha precisa ter pelo menos 8 caracteres.")

    cur.execute(
        """
        INSERT INTO users (tenant_id, name, email, password_hash, role)
        VALUES (%s, %s, %s, %s, 'admin')
        ON CONFLICT (email) DO UPDATE SET
            password_hash = EXCLUDED.password_hash,
            role = 'admin',
            active = true
        """,
        (tenant_id, nome, email, generate_password_hash(senha)),
    )

    conn.commit()
    cur.close()
    conn.close()
    print(f"\nAdmin '{email}' criado/atualizado no tenant '{tenant_nome}' ({tenant_slug}).")


if __name__ == "__main__":
    main()
