import ipaddress
import json
import logging
import os
import secrets
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

import psycopg2
import redis
import requests
from dotenv import load_dotenv
from flask import Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for
from flask_limiter import Limiter
from flask_wtf import CSRFProtect
from werkzeug.security import check_password_hash, generate_password_hash

from consulta_inpi_marca_nome import possui_marca_por_nome
from consulta_inpi_marcas import possui_marca_no_inpi
from db import dict_cursor, get_conn
from exportacoes import gerar_pdf, gerar_xlsx

load_dotenv()

# Logging só em stdout - a Vercel captura automaticamente, e o
# filesystem lá é efêmero/somente-leitura, então não faz sentido gravar
# em arquivo como antes (app_log.log).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%d/%m/%Y %H:%M:%S",
    handlers=[logging.StreamHandler()],
)

class SilenciarLogDeAcessoFilter(logging.Filter):
    """O Werkzeug loga uma linha por requisição (ex.: '"GET / HTTP/1.1" 200 -'),
    incluindo checagens automáticas de porta do editor - isso vira ruído, já
    que os eventos que importam (login, consulta de CNPJ, ações de admin) já
    são logados explicitamente pelo app.logger. Filtra só essas linhas de
    acesso, mantendo mensagens como a de start-up ("Running on http://...")."""

    def filter(self, record):
        return "HTTP/1." not in record.getMessage()


logging.getLogger("werkzeug").addFilter(SilenciarLogDeAcessoFilter())

app = Flask(__name__)

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    if os.getenv("VERCEL"):
        # Na Vercel isso nunca deveria cair aqui - falha alto e claro em vez
        # de subir rodando com uma chave que muda a cada cold start (o que
        # derrubaria sessões e o CSRF de forma imprevisível e silenciosa).
        raise RuntimeError(
            "SECRET_KEY não configurada. Defina essa variável de ambiente "
            "no painel da Vercel antes do deploy."
        )
    SECRET_KEY = secrets.token_hex(32)
    logging.warning(
        "SECRET_KEY não configurada no ambiente - usando uma chave temporária "
        "gerada em memória (sessões serão invalidadas a cada reinício). "
        "Configure SECRET_KEY antes de ir para produção."
    )
app.secret_key = SECRET_KEY

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = not app.debug
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)

csrf = CSRFProtect(app)

# Faixas de IP da Cloudflare (https://www.cloudflare.com/ips/). Só confiamos
# no CF-Connecting-IP quando a conexão chegou de uma delas - senão qualquer um
# poderia bater direto na Vercel (sem passar pela Cloudflare) e forjar o header
# pra escapar do rate limit.
CLOUDFLARE_IP_RANGES = [
    ipaddress.ip_network(faixa)
    for faixa in (
        "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
        "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
        "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
        "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
        "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
        "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
    )
]


def _ip_valido(valor):
    try:
        return ipaddress.ip_address((valor or "").strip())
    except ValueError:
        return None


def ip_do_cliente() -> str:
    """IP real de quem fez a requisição, usado no rate limit, no Turnstile e
    nos logs. Na Vercel o X-Real-IP é preenchido por ela (não dá pra forjar) e
    é o IP de quem conectou - que, vindo pela Cloudflare, é um IP da Cloudflare;
    aí o cliente de verdade está no CF-Connecting-IP. Fora da Vercel (dev
    local) usa só o endereço da conexão."""
    ip_conexao = (request.headers.get("X-Real-IP") if os.getenv("VERCEL") else None) or request.remote_addr or ""
    conexao = _ip_valido(ip_conexao)
    if conexao and any(conexao in faixa for faixa in CLOUDFLARE_IP_RANGES):
        cliente = _ip_valido(request.headers.get("CF-Connecting-IP"))
        if cliente:
            return str(cliente)
    return ip_conexao.strip() or "desconhecido"


REDIS_URL = os.getenv("REDIS_URL")
limiter = Limiter(
    app=app,
    key_func=ip_do_cliente,
    default_limits=[],
    storage_uri=REDIS_URL,  # None = volta pro armazenamento em memória (só serve p/ dev local)
)
if not REDIS_URL:
    logging.warning(
        "REDIS_URL não configurada - rate limiting usando memória local, que não é "
        "confiável em ambiente serverless (cada invocação pode cair numa instância "
        "diferente). Configure REDIS_URL (ex.: Upstash) antes de ir para produção."
    )

# Cache no Redis (o mesmo do rate limit) das consultas externas:
# - ReceitaWS: a API gratuita só permite ~3 consultas/minuto, e a exportação
#   (PDF/XLSX) precisa dos mesmos dados que acabaram de ser consultados.
# - INPI: é scraping, lento, e repetir a consulta de um CNPJ/marca refaz tudo.
# Dados cadastrais e registro de marca mudam pouco, então 24h é seguro. Só
# respostas válidas entram no cache (erros nunca). Sem REDIS_URL o cache fica
# desligado e tudo vai direto nas fontes.
CACHE_TTL_SEGUNDOS = 24 * 60 * 60
redis_cache = (
    redis.Redis.from_url(REDIS_URL, socket_timeout=2, socket_connect_timeout=2, decode_responses=True)
    if REDIS_URL
    else None
)


def _ler_cache(chave: str):
    if not redis_cache:
        return None
    try:
        bruto = redis_cache.get(chave)
        return json.loads(bruto) if bruto else None
    except Exception:
        # Redis fora do ar não pode derrubar a consulta - só perde o cache.
        app.logger.warning("Falha ao ler o cache '%s'", chave, exc_info=True)
        return None


def _gravar_cache(chave: str, valor) -> None:
    if not redis_cache:
        return
    try:
        redis_cache.set(chave, json.dumps(valor), ex=CACHE_TTL_SEGUNDOS)
    except Exception:
        app.logger.warning("Falha ao gravar o cache '%s'", chave, exc_info=True)


# CAPTCHA (Cloudflare Turnstile, widget invisível) no login. A site key é
# pública (vai pro HTML); a secret key só existe no servidor e valida o token
# no /siteverify. Sem as duas configuradas, em dev usa as chaves de teste do
# Cloudflare (sempre passam); na Vercel recusa subir, igual a SECRET_KEY.
TURNSTILE_SITE_KEY = os.getenv("TURNSTILE_SITE_KEY")
TURNSTILE_SECRET_KEY = os.getenv("TURNSTILE_SECRET_KEY")
if not (TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY):
    if os.getenv("VERCEL"):
        raise RuntimeError(
            "TURNSTILE_SITE_KEY/TURNSTILE_SECRET_KEY não configuradas. Defina "
            "essas variáveis de ambiente no painel da Vercel antes do deploy."
        )
    TURNSTILE_SITE_KEY = "1x00000000000000000000BB"  # teste: invisível, sempre passa
    TURNSTILE_SECRET_KEY = "1x0000000000000000000000000000000AA"  # teste: sempre passa
    logging.warning(
        "TURNSTILE_SITE_KEY/TURNSTILE_SECRET_KEY não configuradas - usando as "
        "chaves de teste do Cloudflare (o CAPTCHA do login sempre passa)."
    )


def captcha_valido(token: str) -> bool:
    """Valida o token do Turnstile no Cloudflare. O token é de uso único e
    cada POST do login recarrega a página, então o widget sempre gera outro.
    Qualquer falha (token vazio, rede, timeout) conta como inválido."""
    if not token:
        return False
    try:
        resp = requests.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={
                "secret": TURNSTILE_SECRET_KEY,
                "response": token,
                "remoteip": ip_do_cliente(),
            },
            timeout=5,
        )
        resultado = resp.json()
    except Exception:
        app.logger.exception("Falha ao validar o CAPTCHA no Cloudflare")
        return False
    if not resultado.get("success"):
        app.logger.warning(f"CAPTCHA recusado: {resultado.get('error-codes')}")
        return False
    return True


@app.before_request
def gerar_nonce_csp():
    # Nonce novo a cada resposta: só os <script> que o carregam (os nossos)
    # são executados pela CSP; um script injetado não tem como adivinhá-lo.
    g.csp_nonce = secrets.token_urlsafe(16)


@app.context_processor
def expor_nonce_csp():
    return {"csp_nonce": g.get("csp_nonce", "")}


def montar_csp(nonce: str) -> str:
    diretivas = {
        "default-src": "'self'",
        # static.cloudflareinsights.com: beacon do Cloudflare Web Analytics, que a
        # própria Cloudflare injeta no HTML (sem nonce) e reporta pra cloudflareinsights.com.
        "script-src": f"'self' 'nonce-{nonce}' https://cdn.jsdelivr.net https://challenges.cloudflare.com https://static.cloudflareinsights.com",
        # 'unsafe-inline' em estilo: os templates usam <style> e style="" inline;
        # o risco de CSS injetado é bem menor que o de script.
        "style-src": "'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com",
        "font-src": "'self' https://cdn.jsdelivr.net https://fonts.gstatic.com",
        "img-src": "'self' data:",
        "connect-src": "'self' https://cloudflareinsights.com",
        "frame-src": "https://challenges.cloudflare.com https://maps.google.com https://www.google.com",
        "object-src": "'none'",
        "base-uri": "'self'",
        "form-action": "'self'",
        "frame-ancestors": "'none'",
        "report-uri": "/csp-report",
    }
    return "; ".join(f"{nome} {valor}" for nome, valor in diretivas.items())


@app.after_request
def adicionar_headers_seguranca(response):
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    # Report-Only por enquanto: o navegador só avisa (via /csp-report) o que
    # bloquearia. Depois de um tempo sem violações legítimas nos logs, trocar
    # o nome do header para "Content-Security-Policy".
    response.headers["Content-Security-Policy-Report-Only"] = montar_csp(g.get("csp_nonce", ""))
    return response


@app.route("/csp-report", methods=["POST"])
@csrf.exempt
@limiter.limit("30 per minute")
def csp_report():
    relatorio = (request.get_json(force=True, silent=True) or {}).get("csp-report", {})
    app.logger.warning(
        "Violação de CSP: diretiva=%s bloqueado=%s página=%s",
        relatorio.get("violated-directive"),
        relatorio.get("blocked-uri"),
        relatorio.get("document-uri"),
    )
    return "", 204


@app.errorhandler(429)
def muitas_requisicoes(erro):
    # Só o login tem rate limit voltado ao usuário; o resto recebe só o status.
    if request.endpoint == "login":
        return render_template(
            "login.html",
            erro="Muitas tentativas de login. Aguarde alguns minutos e tente novamente.",
            turnstile_site_key=TURNSTILE_SITE_KEY,
        ), 429
    return "Muitas requisições. Tente novamente em instantes.", 429


TZ_BRASILIA = ZoneInfo("America/Sao_Paulo")

ACOES_AUDITORIA_LABELS = {
    "criar_usuario": "Criou usuário",
    "editar_usuario": "Editou usuário",
    "redefinir_senha": "Redefiniu senha",
    "trocar_senha_propria": "Trocou a própria senha",
    "ativar_usuario": "Ativou usuário",
    "desativar_usuario": "Desativou usuário",
    "excluir_usuario": "Excluiu usuário",
    "criar_tenant": "Criou tenant",
    "editar_tenant": "Editou tenant",
    "ativar_tenant": "Ativou tenant",
    "desativar_tenant": "Desativou tenant",
    "excluir_tenant": "Excluiu tenant",
    # mantidos só pra rótulo de registros antigos, gravados antes desta mudança
    "alternar_status_usuario": "Ativou/desativou usuário",
    "alternar_status_tenant": "Ativou/desativou tenant",
}


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


def eh_unico_admin_ativo(cursor, user_id) -> bool:
    """True se user_id é hoje um admin ativo e não existe nenhum outro
    admin ativo - usado pra impedir que a última pessoa com acesso ao
    admin se desative, se exclua ou perca a role sem querer.

    Usa FOR UPDATE pra travar as linhas de admin ativo até a transação
    terminar - evita que duas requisições concorrentes leiam a mesma
    contagem antes de qualquer uma das duas commitar (race condition)."""
    cursor.execute("SELECT role, active FROM users WHERE id = %s", (user_id,))
    row = cursor.fetchone()
    if not row or row[0] != "admin" or not row[1]:
        return False
    cursor.execute("SELECT id FROM users WHERE role = 'admin' AND active = true FOR UPDATE")
    return len(cursor.fetchall()) <= 1


def tenant_e_ultimo_acesso_admin(cursor, tenant_id) -> bool:
    """True se desativar tenant_id deixaria zero admins ativos com acesso
    ao sistema (nenhum admin ativo restaria em nenhum outro tenant ativo)."""
    cursor.execute("SELECT active FROM tenants WHERE id = %s", (tenant_id,))
    row = cursor.fetchone()
    if not row or not row[0]:
        return False  # já inativo (ou não existe) - não é uma desativação
    cursor.execute(
        """
        SELECT u.id FROM users u
        JOIN tenants t ON t.id = u.tenant_id
        WHERE u.role = 'admin' AND u.active = true AND t.active = true
          AND u.tenant_id != %s
        FOR UPDATE OF u
        """,
        (tenant_id,),
    )
    return len(cursor.fetchall()) == 0


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


def email_do_formulario() -> str:
    return request.form.get("email", "").strip().lower()


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")
# Por e-mail, pega ataque distribuído em vários IPs contra a mesma conta.
# Só conta tentativa que não logou (sucesso = redirect 302).
@limiter.limit(
    "5 per 15 minutes",
    key_func=lambda: "login-email:" + email_do_formulario(),
    methods=["POST"],
    deduct_when=lambda response: response.status_code != 302,
)
def login():
    erro_login = None
    if request.method == "POST":
        email = email_do_formulario()
        senha = request.form.get("senha", "")

        # CAPTCHA antes de tocar no banco - robô nem chega a testar a senha.
        if not captcha_valido(request.form.get("cf-turnstile-response", "")):
            app.logger.warning(f"Login bloqueado pelo CAPTCHA para e-mail '{email}' (ip {ip_do_cliente()})")
            erro_login = "A verificação anti-robô falhou ou expirou. Tente novamente."
            return render_template("login.html", erro=erro_login, turnstile_site_key=TURNSTILE_SITE_KEY)

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

                # Falha ao gravar o último login não pode derrubar um login
                # que já foi validado - registra à parte, sem propagar erro.
                try:
                    conn2 = get_conn()
                    cursor2 = conn2.cursor()
                    cursor2.execute(
                        "UPDATE users SET last_login_at = now() WHERE id = %s",
                        (user["id"],),
                    )
                    conn2.commit()
                    cursor2.close()
                    conn2.close()
                except Exception:
                    app.logger.exception("Falha ao gravar last_login_at")

                app.logger.info(f"Usuário '{user['email']}' logou com sucesso")
                return redirect(url_for("index"))
            else:
                app.logger.warning(f"Falha no login para e-mail '{email}' (ip {ip_do_cliente()})")
                erro_login = "Usuário ou senha inválidos."
        except Exception:
            app.logger.exception("Erro ao processar login")
            erro_login = "Não foi possível concluir o login. Tente novamente em instantes."

    return render_template("login.html", erro=erro_login, turnstile_site_key=TURNSTILE_SITE_KEY)


@app.route("/logout")
def logout():
    nome = session.get("nome_exibicao", session.get("usuario", "desconhecido"))
    session.clear()
    app.logger.info(f"Usuário: '{nome}' Deslogou com sucesso")
    return redirect(url_for("login"))


@app.route("/conta/trocar-senha", methods=["POST"])
@login_required
def trocar_senha():
    senha_atual = request.form.get("senha_atual") or ""
    nova_senha = request.form.get("nova_senha") or ""

    # "proximo" volta o usuário pra página de onde veio (qualquer navbar).
    # Só aceita caminho relativo do próprio site - nunca um destino
    # controlado por quem envia o formulário (evita open redirect).
    destino = request.form.get("proximo") or url_for("index")
    if not destino.startswith("/") or destino.startswith("//"):
        destino = url_for("index")

    conn = None
    try:
        conn = get_conn()
        cursor = dict_cursor(conn)
        cursor.execute("SELECT id, password_hash FROM users WHERE email = %s", (session["usuario"],))
        user = cursor.fetchone()

        if not user or not check_password_hash(user["password_hash"], senha_atual):
            flash("Senha atual incorreta.", "error")
        elif len(nova_senha) < 8:
            flash("A nova senha precisa ter pelo menos 8 caracteres.", "error")
        else:
            cursor.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s",
                (generate_password_hash(nova_senha), user["id"]),
            )
            conn.commit()
            registrar_auditoria("trocar_senha_propria", f"'{session['usuario']}'")
            flash("Senha atualizada com sucesso.", "success")
        cursor.close()
    except Exception:
        if conn:
            conn.rollback()
        app.logger.exception("Erro ao trocar a própria senha")
        flash("Não foi possível trocar a senha. Tente novamente.", "error")
    finally:
        if conn:
            conn.close()

    return redirect(destino)


def _consultar_receitaws(cnpj: str):
    """Chama a API da ReceitaWS. Retorna (data, erro) - "data" só vem
    preenchido quando a resposta é válida (status OK)."""
    url = f"https://www.receitaws.com.br/v1/cnpj/{cnpj}"
    headers = {"User-Agent": "Mozilla/5.0"}

    response = requests.get(url, headers=headers, timeout=10)

    if response.status_code == 429 or "too many requests" in response.text.lower():
        app.logger.warning(f"Limite atingido: {response.text}")
        return None, "Limite de consultas excedido temporariamente. Aguarde um pouco e tente novamente."
    if "application/json" not in response.headers.get("Content-Type", ""):
        app.logger.error(f"Status: {response.status_code}, Conteúdo: {response.text}")
        return None, "Resposta da API não está em formato JSON."

    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError):
        app.logger.exception("Erro ao decodificar resposta da ReceitaWS")
        return None, "Não foi possível interpretar a resposta da ReceitaWS."

    if data.get("status") != "OK":
        app.logger.warning(f"Resposta inválida: {data}")
        return None, data.get("message", "Erro desconhecido ao consultar a API da ReceitaWS.")

    return data, None


def buscar_dados_empresa(cnpj: str):
    """Busca os dados de um CNPJ na ReceitaWS (com cache no Redis) +
    tributação/Lei do Bem no Postgres. Usada tanto pela consulta principal
    (index) quanto pelas exportações (PDF/XLSX), pra não duplicar essa lógica.

    Retorna (resultado, erro, msg_tp_tributacao, msg_lei_do_bem) - "resultado"
    vem None se "erro" estiver preenchido.
    """
    resultado = None
    erro = None
    msg_tp_tributacao = None
    msg_lei_do_bem = None

    try:
        chave_cache = f"receitaws:{cnpj}"
        data = _ler_cache(chave_cache)
        if data is None:
            data, erro = _consultar_receitaws(cnpj)
            if data is not None:
                _gravar_cache(chave_cache, data)

        if data is not None:
            resultado = data

            # Busca separada dos dados de referência (tributação/Lei do
            # Bem): se o Postgres falhar aqui, os dados da Receita já
            # obtidos continuam sendo exibidos normalmente - só esses
            # dois campos caem num aviso, em vez de renderizar "None"
            # ou derrubar a consulta inteira.
            msg_tp_tributacao = "Não foi possível verificar a tributação."
            msg_lei_do_bem = "Não foi possível verificar a Lei do Bem."
            conn_ref = None
            try:
                conn_ref = get_conn()
                cursor_ref = conn_ref.cursor()

                cursor_ref.execute(
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
                linha = cursor_ref.fetchone()
                msg_tp_tributacao = linha[0] if linha and linha[0] else "Tributação não identificada"

                cursor_ref.execute(
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
                linha_lei_do_bem = cursor_ref.fetchone()
                msg_lei_do_bem = (
                    linha_lei_do_bem[0] if linha_lei_do_bem and linha_lei_do_bem[0] else "Lei do Bem não identificada"
                )

                cursor_ref.close()
            except Exception:
                app.logger.exception("Erro ao buscar tributação/Lei do Bem")
            finally:
                if conn_ref:
                    conn_ref.close()

            if "simples" in resultado:
                data_str = resultado["simples"].get("ultima_atualizacao", "").replace("Z", "")
                try:
                    resultado["simples"]["ultima_atualizacao_formatada"] = datetime.fromisoformat(
                        data_str
                    ).strftime("%d/%m/%Y %H:%M")
                except ValueError:
                    resultado["simples"]["ultima_atualizacao_formatada"] = data_str

    except requests.exceptions.RequestException:
        app.logger.exception("Erro de conexão com a ReceitaWS")
        erro = "Não foi possível conectar à ReceitaWS. Tente novamente em instantes."
    except Exception:
        app.logger.exception("Erro inesperado ao consultar CNPJ")
        erro = "Ocorreu um erro inesperado ao consultar o CNPJ. Tente novamente."

    return resultado, erro, msg_tp_tributacao, msg_lei_do_bem


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
            resultado, erro, msg_tp_tributacao, msg_lei_do_bem = buscar_dados_empresa(cnpj)
            if resultado:
                app.logger.info(
                    f"Usuário: '{nome_usuario}' Realizou uma consulta com sucesso. CNPJ: '{resultado['cnpj']}'"
                )
            # INPI é consultado de forma assíncrona pelo frontend
            # via GET /consulta-inpi/<cnpj> - não bloqueia aqui

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
    chave_cache = "inpi:cnpj:" + "".join(c for c in cnpj if c.isdigit())
    encontrou = _ler_cache(chave_cache)
    if encontrou is not None:
        return {"possui_marca": encontrou}

    try:
        encontrou = possui_marca_no_inpi(cnpj)
        _gravar_cache(chave_cache, encontrou)
        return {"possui_marca": encontrou}
    except Exception as e:
        logging.warning(f"Falha na consulta INPI para CNPJ {cnpj}: {e}")
        return {"erro": "Não foi possível consultar o INPI no momento."}, 200


@app.route("/consulta-inpi-nome")
@login_required
def consulta_inpi_nome():
    """Rota chamada de forma assíncrona pelo frontend. Retorna JSON com o
    resultado da consulta no INPI por nome da marca.

    O nome vem na query string (?nome=...), não no caminho da URL: na Vercel
    o caminho chega ao Flask ainda codificado ("ARANHA%20FERREIRA"), e o INPI
    acabava pesquisando o texto com "%20" - dando resultado errado. A query
    string é sempre decodificada, em qualquer ambiente."""
    nome = (request.args.get("nome") or "").strip()
    if not nome:
        return {"erro": "Nome não informado."}, 400
    if len(nome) > 200:
        return {"erro": "Nome muito longo."}, 400

    # Normaliza espaços/maiúsculas na chave pra "Empresa  X" e "empresa x"
    # reaproveitarem o mesmo resultado.
    chave_cache = "inpi:nome:" + " ".join(nome.split()).lower()
    encontrou = _ler_cache(chave_cache)
    if encontrou is not None:
        return {"possui_marca": encontrou}

    try:
        encontrou = possui_marca_por_nome(nome)
        _gravar_cache(chave_cache, encontrou)
        return {"possui_marca": encontrou}
    except Exception as e:
        logging.warning(f"Falha na consulta INPI por nome '{nome}': {e}")
        return {"erro": "Não foi possível consultar o INPI no momento."}, 200


def _preparar_exportacao():
    """Lê CNPJ + status do INPI (já resolvido no navegador, de forma
    assíncrona) do form da exportação, e busca de novo os dados da
    ReceitaWS/tributação/Lei do Bem - a ReceitaWS vem do cache no Redis
    (preenchido pela consulta), então não gasta cota da API; e não serializa
    o resultado num campo hidden pra não aceitar dados adulterados do
    navegador no relatório. O INPI é lento (é
    scraping) e por isso reaproveita o que o frontend já resolveu, em vez
    de consultar de novo.

    Retorna (resultado, msg_tp_tributacao, msg_lei_do_bem, inpi_cnpj_status,
    inpi_nome_status) ou None se o CNPJ for inválido ou a busca falhar -
    nesse caso já cuida do flash message."""
    cnpj = (request.form.get("cnpj") or "").strip().replace(".", "").replace("/", "").replace("-", "")
    if not cnpj.isdigit() or len(cnpj) != 14:
        flash("CNPJ inválido para exportação.", "error")
        return None

    resultado, erro, msg_tp_tributacao, msg_lei_do_bem = buscar_dados_empresa(cnpj)
    if not resultado:
        flash(erro or "Não foi possível gerar a exportação.", "error")
        return None

    inpi_cnpj_status = request.form.get("inpi_cnpj_status", "")
    inpi_nome_status = request.form.get("inpi_nome_status", "")
    return resultado, msg_tp_tributacao, msg_lei_do_bem, inpi_cnpj_status, inpi_nome_status


@app.route("/exportar/xlsx", methods=["POST"])
@login_required
def exportar_xlsx():
    dados = _preparar_exportacao()
    if dados is None:
        return redirect(url_for("index"))
    resultado, msg_tp_tributacao, msg_lei_do_bem, inpi_cnpj_status, inpi_nome_status = dados

    buffer, nome_arquivo = gerar_xlsx(resultado, msg_tp_tributacao, msg_lei_do_bem, inpi_cnpj_status, inpi_nome_status)
    app.logger.info(f"Usuário: '{session['nome_exibicao']}' exportou XLSX. CNPJ: '{resultado['cnpj']}'")

    return Response(
        buffer.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nome_arquivo}"'},
    )


@app.route("/exportar/pdf", methods=["POST"])
@login_required
def exportar_pdf():
    dados = _preparar_exportacao()
    if dados is None:
        return redirect(url_for("index"))
    resultado, msg_tp_tributacao, msg_lei_do_bem, inpi_cnpj_status, inpi_nome_status = dados

    caminho_logo = os.path.join(app.root_path, "static", "images", "logo-lockup.png")
    buffer, nome_arquivo = gerar_pdf(
        resultado, msg_tp_tributacao, msg_lei_do_bem, inpi_cnpj_status, inpi_nome_status, caminho_logo=caminho_logo
    )
    app.logger.info(f"Usuário: '{session['nome_exibicao']}' exportou PDF. CNPJ: '{resultado['cnpj']}'")

    return Response(
        buffer.getvalue(),
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{nome_arquivo}"'},
    )


# ─────────────────────────────────────────────────────────────
# ADMIN - CRUD de tenants e usuários
# ─────────────────────────────────────────────────────────────

@app.route("/admin/tenants", methods=["GET", "POST"])
@admin_required
def admin_tenants():
    erro = None
    mensagem = None

    if request.method == "POST":
        acao = request.form.get("acao")
        conn = None
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

            elif acao == "editar":
                tenant_id = request.form.get("tenant_id")
                nome = (request.form.get("nome") or "").strip()
                slug = (request.form.get("slug") or "").strip().lower()
                if not nome or not slug:
                    erro = "Informe nome e identificador (slug) do tenant."
                else:
                    cursor.execute(
                        "UPDATE tenants SET name = %s, slug = %s WHERE id = %s", (nome, slug, tenant_id)
                    )
                    conn.commit()
                    registrar_auditoria("editar_tenant", f"'{nome}' (slug={slug})")
                    mensagem = "Tenant atualizado."

            elif acao == "alternar_status":
                tenant_id = request.form.get("tenant_id")
                if tenant_e_ultimo_acesso_admin(cursor, tenant_id):
                    erro = "Não é possível desativar: nenhum admin ativo teria mais acesso ao sistema depois disso."
                else:
                    cursor.execute(
                        "UPDATE tenants SET active = NOT active WHERE id = %s RETURNING name, active",
                        (tenant_id,),
                    )
                    nome_tenant, ativo_novo = cursor.fetchone()
                    conn.commit()
                    acao_log = "ativar_tenant" if ativo_novo else "desativar_tenant"
                    registrar_auditoria(acao_log, f"'{nome_tenant}'")
                    mensagem = "Status do tenant atualizado."

            elif acao == "excluir":
                tenant_id = request.form.get("tenant_id")
                cursor.execute("DELETE FROM tenants WHERE id = %s RETURNING name", (tenant_id,))
                row = cursor.fetchone()
                conn.commit()
                registrar_auditoria("excluir_tenant", f"'{row[0]}'" if row else f"tenant_id={tenant_id}")
                mensagem = "Tenant excluído."

            cursor.close()
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            app.logger.warning("Slug de tenant duplicado")
            erro = "Já existe um tenant com esse identificador (slug)."
        except psycopg2.errors.ForeignKeyViolation:
            conn.rollback()
            app.logger.warning("Tentativa de excluir tenant com usuários vinculados")
            erro = "Não é possível excluir: existem usuários vinculados a este tenant. Exclua ou mova os usuários primeiro."
        except Exception:
            if conn:
                conn.rollback()
            app.logger.exception("Erro ao gerenciar tenants")
            erro = "Não foi possível concluir a operação."
        finally:
            if conn:
                conn.close()

    conn = get_conn()
    try:
        cursor = dict_cursor(conn)
        cursor.execute("SELECT id, name, slug, active, created_at FROM tenants ORDER BY name")
        tenants = cursor.fetchall()
        cursor.close()
    finally:
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
        conn = None
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
                    cursor.execute("SELECT name FROM tenants WHERE id = %s", (tenant_id,))
                    tenant_row = cursor.fetchone()
                    nome_tenant = tenant_row[0] if tenant_row else f"id={tenant_id}"

                    cursor.execute(
                        "INSERT INTO users (tenant_id, name, email, password_hash, role) VALUES (%s, %s, %s, %s, %s)",
                        (tenant_id, nome, email, generate_password_hash(senha), role),
                    )
                    conn.commit()
                    registrar_auditoria("criar_usuario", f"'{email}' em '{nome_tenant}' (role={role})")
                    mensagem = f"Usuário '{email}' criado."

            elif acao == "editar":
                user_id = request.form.get("user_id")
                nome = (request.form.get("nome") or "").strip()
                role = request.form.get("role") if request.form.get("role") in ("admin", "user") else "user"
                tenant_id = request.form.get("tenant_id")

                cursor.execute("SELECT name, active FROM tenants WHERE id = %s", (tenant_id,))
                tenant_row = cursor.fetchone()
                nome_tenant = tenant_row[0] if tenant_row else f"id={tenant_id}"
                tenant_novo_ativo = bool(tenant_row and tenant_row[1])

                if (role != "admin" or not tenant_novo_ativo) and eh_unico_admin_ativo(cursor, user_id):
                    erro = (
                        "Não é possível remover a permissão de admin (ou movê-lo para um "
                        "tenant inativo) sendo o único usuário admin ativo."
                    )
                else:
                    cursor.execute(
                        "UPDATE users SET name = %s, role = %s, tenant_id = %s WHERE id = %s RETURNING email",
                        (nome, role, tenant_id, user_id),
                    )
                    (email_usuario,) = cursor.fetchone()
                    conn.commit()
                    registrar_auditoria(
                        "editar_usuario", f"'{email_usuario}' -> nome={nome}, tenant='{nome_tenant}', role={role}"
                    )
                    mensagem = "Usuário atualizado."

            elif acao == "redefinir_senha":
                user_id = request.form.get("user_id")
                nova_senha = request.form.get("nova_senha") or ""
                if len(nova_senha) < 8:
                    erro = "A nova senha precisa ter pelo menos 8 caracteres."
                else:
                    cursor.execute(
                        "UPDATE users SET password_hash = %s WHERE id = %s RETURNING email",
                        (generate_password_hash(nova_senha), user_id),
                    )
                    row = cursor.fetchone()
                    conn.commit()
                    registrar_auditoria("redefinir_senha", f"'{row[0]}'" if row else f"user_id={user_id}")
                    mensagem = "Senha redefinida."

            elif acao == "alternar_status":
                user_id = request.form.get("user_id")
                if eh_unico_admin_ativo(cursor, user_id):
                    erro = "Não é possível desativar o único usuário admin ativo."
                else:
                    cursor.execute(
                        "UPDATE users SET active = NOT active WHERE id = %s RETURNING email, active",
                        (user_id,),
                    )
                    email_usuario, ativo_novo = cursor.fetchone()
                    conn.commit()
                    acao_log = "ativar_usuario" if ativo_novo else "desativar_usuario"
                    registrar_auditoria(acao_log, f"'{email_usuario}'")
                    mensagem = "Status do usuário atualizado."

            elif acao == "excluir":
                user_id = request.form.get("user_id")
                if eh_unico_admin_ativo(cursor, user_id):
                    erro = "Não é possível excluir o único usuário admin ativo."
                else:
                    cursor.execute("DELETE FROM users WHERE id = %s RETURNING email", (user_id,))
                    row = cursor.fetchone()
                    conn.commit()
                    registrar_auditoria("excluir_usuario", f"'{row[0]}'" if row else f"user_id={user_id}")
                    mensagem = "Usuário excluído."

            cursor.close()
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            app.logger.warning("E-mail de usuário duplicado")
            erro = "Já existe um usuário com esse e-mail."
        except Exception:
            if conn:
                conn.rollback()
            app.logger.exception("Erro ao gerenciar usuários")
            erro = "Não foi possível concluir a operação."
        finally:
            if conn:
                conn.close()

    # Filtros da listagem (GET, via querystring - sobrevivem aos forms POST
    # da própria tela porque eles não têm "action" e o navegador reenvia
    # pra URL atual, querystring incluída).
    filtro_tenant_id = request.args.get("tenant_id", "").strip()
    if filtro_tenant_id and not filtro_tenant_id.isdigit():
        filtro_tenant_id = ""
    filtro_status = request.args.get("status", "").strip()
    if filtro_status not in ("ativo", "inativo"):
        filtro_status = ""
    filtro_role = request.args.get("role", "").strip()
    if filtro_role not in ("admin", "user"):
        filtro_role = ""

    condicoes = []
    parametros = []
    if filtro_tenant_id:
        condicoes.append("t.id = %s")
        parametros.append(filtro_tenant_id)
    if filtro_status:
        condicoes.append("u.active = %s")
        parametros.append(filtro_status == "ativo")
    if filtro_role:
        condicoes.append("u.role = %s")
        parametros.append(filtro_role)
    where_sql = ("WHERE " + " AND ".join(condicoes)) if condicoes else ""

    conn = get_conn()
    try:
        cursor = dict_cursor(conn)
        cursor.execute(
            f"""
            SELECT u.id, u.name, u.email, u.role, u.active, u.created_at, u.last_login_at,
                   t.id AS tenant_id, t.name AS tenant_name
            FROM users u
            JOIN tenants t ON t.id = u.tenant_id
            {where_sql}
            ORDER BY t.name, u.name
            """,
            parametros,
        )
        usuarios = cursor.fetchall()
        for usuario in usuarios:
            if usuario["last_login_at"]:
                usuario["last_login_at"] = usuario["last_login_at"].astimezone(TZ_BRASILIA)

        cursor.execute("SELECT id, name FROM tenants WHERE active = true ORDER BY name")
        tenants = cursor.fetchall()
        cursor.close()
    finally:
        conn.close()

    return render_template(
        "admin/usuarios.html",
        usuarios=usuarios,
        tenants=tenants,
        filtro_tenant_id=filtro_tenant_id,
        filtro_status=filtro_status,
        filtro_role=filtro_role,
        erro=erro,
        mensagem=mensagem,
        nome_usuario=session["nome_exibicao"],
        is_admin=True,
    )


@app.route("/admin/auditoria")
@admin_required
def admin_auditoria():
    conn = get_conn()
    try:
        cursor = dict_cursor(conn)
        cursor.execute(
            "SELECT id, actor_email, action, details, created_at FROM audit_log "
            "ORDER BY created_at DESC LIMIT 200"
        )
        registros = cursor.fetchall()
        cursor.close()
    finally:
        conn.close()

    # O Postgres guarda em UTC (timestamptz) - converte pro horário de
    # Brasília só na hora de exibir.
    for registro in registros:
        registro["created_at"] = registro["created_at"].astimezone(TZ_BRASILIA)

    return render_template(
        "admin/auditoria.html",
        registros=registros,
        acoes_labels=ACOES_AUDITORIA_LABELS,
        nome_usuario=session["nome_exibicao"],
        is_admin=True,
    )


# A app é servida via api/index.py na Vercel. Rodar direto (`python app.py`)
# é só um atalho de conveniência local - nunca com debug=True aqui; use
# main.py (debug=False) para rodar localmente com dados reais.
if __name__ == "__main__":
    app.run(debug=False)
