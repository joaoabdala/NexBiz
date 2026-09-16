import json
import logging
import os
import secrets
from datetime import datetime, timedelta
from functools import wraps

import requests
from dotenv import load_dotenv
from flask import Flask, abort, redirect, render_template, request, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf import CSRFProtect
from werkzeug.security import check_password_hash, generate_password_hash

from consulta_inpi_marca_nome import possui_marca_por_nome
from consulta_inpi_marcas import possui_marca_no_inpi
from db import dict_cursor, get_conn

load_dotenv()

# 1) Defina aqui *todas* as substrings que você quer suprimir
IGNORE_STRINGS = [
    "GET",
    "POST",
]


# 2) Filtro para ignorar determinadas strings
class IgnoreStaticFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return not any(pat in msg for pat in IGNORE_STRINGS)


# 3) Logging só em stdout — a Vercel captura automaticamente, e o
# filesystem lá é efêmero/somente-leitura, então não faz sentido gravar
# em arquivo como antes (app_log.log).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%d/%m/%Y %H:%M:%S",
    handlers=[logging.StreamHandler()],
)
for h in logging.root.handlers:
    h.addFilter(IgnoreStaticFilter())

app = Flask(__name__)

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    logging.warning(
        "SECRET_KEY não configurada no ambiente — usando uma chave temporária "
        "gerada em memória (sessões serão invalidadas a cada reinício). "
        "Configure SECRET_KEY antes de ir para produção."
    )
app.secret_key = SECRET_KEY

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = not app.debug
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)

csrf = CSRFProtect(app)
limiter = Limiter(app=app, key_func=get_remote_address, default_limits=[])


def registrar_auditoria(acao: str, detalhes: str = "") -> None:
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO audit_log (actor_email, action, details) VALUES (%s, %s, %s)",
            (session.get("usuario", "desconhecido"), acao, detalhes),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        app.logger.exception("Falha ao registrar auditoria da ação '%s'", acao)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "usuario" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "usuario" not in session:
            return redirect(url_for("login"))
        if session.get("perfil") != "admin":
            abort(403)
        return view(*args, **kwargs)

    return wrapped


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def login():
    erro_login = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        senha = request.form.get("senha", "")

        try:
            conn = get_conn()
            cursor = dict_cursor(conn)
            cursor.execute(
                """
                SELECT u.id, u.name, u.email, u.password_hash, u.role, u.tenant_id,
                       t.name AS tenant_name
                FROM users u
                JOIN tenants t ON t.id = u.tenant_id
                WHERE u.email = %s AND u.active = true AND t.active = true
                """,
                (email,),
            )
            user = cursor.fetchone()
            cursor.close()
            conn.close()

            if user and check_password_hash(user["password_hash"], senha):
                session.clear()
                session.permanent = True
                session["usuario"] = user["email"]
                session["nome_exibicao"] = user["name"]
                session["perfil"] = user["role"]
                session["tenant_id"] = user["tenant_id"]
                session["tenant_nome"] = user["tenant_name"]
                app.logger.info(f"Usuário '{user['email']}' logou com sucesso")
                return redirect(url_for("index"))
            else:
                app.logger.warning(f"Falha no login para e-mail '{email}'")
                erro_login = "Usuário ou senha inválidos."
        except Exception:
            app.logger.exception("Erro ao processar login")
            erro_login = "Não foi possível concluir o login. Tente novamente em instantes."

    return render_template("login.html", erro=erro_login)


@app.route("/logout")
def logout():
    nome = session.get("nome_exibicao", session.get("usuario", "desconhecido"))
    session.clear()
    app.logger.info(f"Usuário: '{nome}' Deslogou com sucesso")
    return redirect(url_for("login"))


@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    resultado = None
    erro = None
    cnpj = ""
    msg_tp_tributacao = None
    msg_lei_do_bem = None
    nome_usuario = session["nome_exibicao"]
    is_admin = session.get("perfil") == "admin"

    if request.method == "POST":
        acao = request.form.get("acao")
        cnpj = (request.form.get("cnpj") or "").strip().replace(".", "").replace("/", "").replace("-", "")
        if not cnpj.isdigit() or len(cnpj) != 14:
            erro = "Informe um CNPJ válido: apenas números, com 14 dígitos."
        elif acao == "consultar":
            url = f"https://www.receitaws.com.br/v1/cnpj/{cnpj}"
            headers = {"User-Agent": "Mozilla/5.0"}

            try:
                response = requests.get(url, headers=headers, timeout=10)

                if response.status_code == 429 or "too many requests" in response.text.lower():
                    erro = "Limite de consultas à ReceitaWS excedido. Aguarde um pouco e tente novamente."
                    app.logger.warning(f"Limite atingido: {response.text}")
                elif "application/json" not in response.headers.get("Content-Type", ""):
                    erro = "Resposta da API não está em formato JSON."
                    app.logger.error(f"Status: {response.status_code}, Conteúdo: {response.text}")
                else:
                    try:
                        data = response.json()
                    except (ValueError, json.JSONDecodeError):
                        app.logger.exception("Erro ao decodificar resposta da ReceitaWS")
                        erro = "Não foi possível interpretar a resposta da ReceitaWS."
                        data = None

                    if data is not None:
                        if data.get("status") != "OK":
                            erro = data.get("message", "Erro desconhecido ao consultar a API da ReceitaWS.")
                            app.logger.warning(f"Resposta inválida: {data}")
                        else:
                            resultado = data
                            app.logger.info(
                                f"Usuário: '{nome_usuario}' Realizou uma consulta com sucesso. CNPJ: '{data['cnpj']}'"
                            )

                            conn = get_conn()
                            cursor = conn.cursor()

                            cursor.execute(
                                """
                                SELECT string_agg(
                                    CASE WHEN c.ano IS NULL THEN '' ELSE c.ano || ' - ' END || c.tipo_tributacao,
                                    ' | '
                                )
                                FROM cnpj_tipo_tributacao c
                                WHERE c.cnpj = %s
                                """,
                                (data.get("cnpj"),),
                            )
                            linha = cursor.fetchone()
                            msg_tp_tributacao = linha[0] if linha and linha[0] else "Tributação não identificada"

                            cursor.execute(
                                """
                                SELECT string_agg(
                                    CASE WHEN c.ano IS NULL THEN '' ELSE c.ano || ' - ' END || c.uf,
                                    ' | '
                                )
                                FROM cnpj_lei_do_bem c
                                WHERE c.cnpj = %s
                                """,
                                (data.get("cnpj"),),
                            )
                            linha_lei_do_bem = cursor.fetchone()
                            msg_lei_do_bem = (
                                linha_lei_do_bem[0] if linha_lei_do_bem and linha_lei_do_bem[0] else "Lei do Bem não identificada"
                            )

                            cursor.close()
                            conn.close()

                            if "simples" in resultado:
                                data_str = resultado["simples"].get("ultima_atualizacao", "").replace("Z", "")
                                try:
                                    resultado["simples"]["ultima_atualizacao_formatada"] = datetime.fromisoformat(
                                        data_str
                                    ).strftime("%d/%m/%Y %H:%M")
                                except ValueError:
                                    resultado["simples"]["ultima_atualizacao_formatada"] = data_str

                            # INPI é consultado de forma assíncrona pelo frontend
                            # via GET /consulta-inpi/<cnpj> — não bloqueia aqui

            except requests.exceptions.RequestException:
                app.logger.exception("Erro de conexão com a ReceitaWS")
                erro = "Não foi possível conectar à ReceitaWS. Tente novamente em instantes."
            except Exception:
                app.logger.exception("Erro inesperado ao consultar CNPJ")
                erro = "Ocorreu um erro inesperado ao consultar o CNPJ. Tente novamente."

    return render_template(
        "index.html",
        resultado=resultado,
        erro=erro,
        cnpj=cnpj,
        msg_tp_tributacao=msg_tp_tributacao,
        msg_lei_do_bem=msg_lei_do_bem,
        nome_usuario=nome_usuario,
        is_admin=is_admin,
    )


@app.route("/consulta-inpi/<cnpj>")
@login_required
def consulta_inpi(cnpj):
    """Rota chamada de forma assíncrona pelo frontend. Retorna JSON com o
    resultado da consulta no INPI por CNPJ."""
    try:
        encontrou = possui_marca_no_inpi(cnpj)
        return {"possui_marca": encontrou}
    except Exception as e:
        logging.warning(f"Falha na consulta INPI para CNPJ {cnpj}: {e}")
        return {"erro": "Não foi possível consultar o INPI no momento."}, 200


@app.route("/consulta-inpi-nome/<path:nome>")
@login_required
def consulta_inpi_nome(nome):
    """Rota chamada de forma assíncrona pelo frontend. Retorna JSON com o
    resultado da consulta no INPI por nome da marca."""
    if len(nome) > 200:
        return {"erro": "Nome muito longo."}, 400

    try:
        encontrou = possui_marca_por_nome(nome)
        return {"possui_marca": encontrou}
    except Exception as e:
        logging.warning(f"Falha na consulta INPI por nome '{nome}': {e}")
        return {"erro": "Não foi possível consultar o INPI no momento."}, 200


# ─────────────────────────────────────────────────────────────
# ADMIN — CRUD de tenants e usuários
# ─────────────────────────────────────────────────────────────

@app.route("/admin/tenants", methods=["GET", "POST"])
@admin_required
def admin_tenants():
    erro = None
    mensagem = None

    if request.method == "POST":
        acao = request.form.get("acao")
        try:
            conn = get_conn()
            cursor = conn.cursor()

            if acao == "criar":
                nome = (request.form.get("nome") or "").strip()
                slug = (request.form.get("slug") or "").strip().lower()
                if not nome or not slug:
                    erro = "Informe nome e identificador (slug) do tenant."
                else:
                    cursor.execute("INSERT INTO tenants (name, slug) VALUES (%s, %s)", (nome, slug))
                    conn.commit()
                    registrar_auditoria("criar_tenant", f"nome={nome} slug={slug}")
                    mensagem = f"Tenant '{nome}' criado."

            elif acao == "alternar_status":
                tenant_id = request.form.get("tenant_id")
                cursor.execute("UPDATE tenants SET active = NOT active WHERE id = %s", (tenant_id,))
                conn.commit()
                registrar_auditoria("alternar_status_tenant", f"tenant_id={tenant_id}")
                mensagem = "Status do tenant atualizado."

            cursor.close()
            conn.close()
        except Exception:
            app.logger.exception("Erro ao gerenciar tenants")
            erro = "Não foi possível concluir a operação."

    conn = get_conn()
    cursor = dict_cursor(conn)
    cursor.execute("SELECT id, name, slug, active, created_at FROM tenants ORDER BY name")
    tenants = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template(
        "admin/tenants.html",
        tenants=tenants,
        erro=erro,
        mensagem=mensagem,
        nome_usuario=session["nome_exibicao"],
        is_admin=True,
    )


@app.route("/admin/usuarios", methods=["GET", "POST"])
@admin_required
def admin_usuarios():
    erro = None
    mensagem = None

    if request.method == "POST":
        acao = request.form.get("acao")
        try:
            conn = get_conn()
            cursor = conn.cursor()

            if acao == "criar":
                nome = (request.form.get("nome") or "").strip()
                email = (request.form.get("email") or "").strip().lower()
                senha = request.form.get("senha") or ""
                role = request.form.get("role") if request.form.get("role") in ("admin", "user") else "user"
                tenant_id = request.form.get("tenant_id")

                if not nome or not email or not tenant_id:
                    erro = "Nome, e-mail e tenant são obrigatórios."
                elif len(senha) < 8:
                    erro = "A senha precisa ter pelo menos 8 caracteres."
                else:
                    cursor.execute(
                        "INSERT INTO users (tenant_id, name, email, password_hash, role) VALUES (%s, %s, %s, %s, %s)",
                        (tenant_id, nome, email, generate_password_hash(senha), role),
                    )
                    conn.commit()
                    registrar_auditoria("criar_usuario", f"email={email} tenant_id={tenant_id} role={role}")
                    mensagem = f"Usuário '{email}' criado."

            elif acao == "editar":
                user_id = request.form.get("user_id")
                nome = (request.form.get("nome") or "").strip()
                role = request.form.get("role") if request.form.get("role") in ("admin", "user") else "user"
                tenant_id = request.form.get("tenant_id")

                cursor.execute(
                    "UPDATE users SET name = %s, role = %s, tenant_id = %s WHERE id = %s",
                    (nome, role, tenant_id, user_id),
                )
                conn.commit()
                registrar_auditoria("editar_usuario", f"user_id={user_id}")
                mensagem = "Usuário atualizado."

            elif acao == "redefinir_senha":
                user_id = request.form.get("user_id")
                nova_senha = request.form.get("nova_senha") or ""
                if len(nova_senha) < 8:
                    erro = "A nova senha precisa ter pelo menos 8 caracteres."
                else:
                    cursor.execute(
                        "UPDATE users SET password_hash = %s WHERE id = %s",
                        (generate_password_hash(nova_senha), user_id),
                    )
                    conn.commit()
                    registrar_auditoria("redefinir_senha", f"user_id={user_id}")
                    mensagem = "Senha redefinida."

            elif acao == "alternar_status":
                user_id = request.form.get("user_id")
                cursor.execute("UPDATE users SET active = NOT active WHERE id = %s", (user_id,))
                conn.commit()
                registrar_auditoria("alternar_status_usuario", f"user_id={user_id}")
                mensagem = "Status do usuário atualizado."

            elif acao == "excluir":
                user_id = request.form.get("user_id")
                cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
                conn.commit()
                registrar_auditoria("excluir_usuario", f"user_id={user_id}")
                mensagem = "Usuário excluído."

            cursor.close()
            conn.close()
        except Exception:
            app.logger.exception("Erro ao gerenciar usuários")
            erro = "Não foi possível concluir a operação."

    conn = get_conn()
    cursor = dict_cursor(conn)
    cursor.execute(
        """
        SELECT u.id, u.name, u.email, u.role, u.active, u.created_at,
               t.id AS tenant_id, t.name AS tenant_name
        FROM users u
        JOIN tenants t ON t.id = u.tenant_id
        ORDER BY t.name, u.name
        """
    )
    usuarios = cursor.fetchall()

    cursor.execute("SELECT id, name FROM tenants WHERE active = true ORDER BY name")
    tenants = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template(
        "admin/usuarios.html",
        usuarios=usuarios,
        tenants=tenants,
        erro=erro,
        mensagem=mensagem,
        nome_usuario=session["nome_exibicao"],
        is_admin=True,
    )


# A app é servida via api/index.py na Vercel. Rodar direto (`python app.py`)
# é só um atalho de conveniência local — nunca com debug=True aqui; use
# main.py (debug=False) para rodar localmente com dados reais.
if __name__ == "__main__":
    app.run(debug=False)
