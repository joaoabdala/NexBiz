# Consultar CNPJ - Abdala Nexus

Aplicação Flask para consultar dados de CNPJ (via [ReceitaWS](https://www.receitaws.com.br/)), cruzando com informações internas de regime tributário e Lei do Bem, e verificando marcas registradas no INPI. Multi-tenant, com autenticação e administração de usuários próprias (sem dependência de sistemas de terceiros).

## Stack

- **Backend:** Flask (Python)
- **Banco de dados:** PostgreSQL ([Neon](https://neon.tech))
- **Hospedagem:** [Vercel](https://vercel.com) (função serverless Python)
- **Frontend:** Jinja2 + Bootstrap 5, sem framework JS - forms tradicionais com POST

## Estrutura do projeto

```
app.py                  Aplicação Flask - rotas, autenticação, CRUD de admin
db.py                   Helper de conexão com o Postgres
db/schema.sql           Schema do banco (tenants, users, dados de referência, audit_log)
main.py                 Entrypoint para rodar localmente (python main.py)
api/index.py            Entrypoint usado pela Vercel (serverless)
vercel.json             Configuração de rotas/build da Vercel

templates/              Páginas Jinja2 (login, consulta, admin/)
static/                 CSS (tema dark/light) e imagens

consulta_inpi_marcas.py         Consulta de marca no INPI por CNPJ
consulta_inpi_marca_nome.py     Consulta de marca no INPI por nome

scripts/
  extract_reference_data.py     Extrai regime tributário/Lei do Bem de uma fonte legada (MySQL) para CSV
  load_reference_data.py        Aplica o schema e carrega os CSVs extraídos no Postgres
  seed_admin.py                 Cria o primeiro tenant + usuário admin

help/                    Notas de build (PyInstaller) e requirements do build local
```

## Configuração

Crie um `.env` na raiz com:

```
DATABASE_URL=postgresql://usuario:senha@host/banco?sslmode=require
SECRET_KEY=<gerado com: python -c "import secrets; print(secrets.token_hex(32))">
REDIS_URL=<URL de conexão do banco criado no Upstash>
TURNSTILE_SITE_KEY=<site key do widget Turnstile no Cloudflare>
TURNSTILE_SECRET_KEY=<secret key do mesmo widget>
```

Use o endpoint **pooled** do Neon em `DATABASE_URL` (recomendado para ambientes serverless). `REDIS_URL` é usado pelo rate limit do login (`Flask-Limiter`); sem ela a aplicação ainda funciona (cai para armazenamento em memória), mas isso não é confiável em produção na Vercel - ver seção Segurança. `TURNSTILE_SITE_KEY`/`TURNSTILE_SECRET_KEY` são do CAPTCHA do login (Cloudflare Turnstile, widget em modo Invisible); sem elas, em dev o app usa as chaves de teste do Cloudflare (que sempre passam).

Instale as dependências:

```bash
pip install -r requirements.txt
```

Aplique o schema no banco (uma vez):

```bash
python -c "import psycopg2, os; from dotenv import load_dotenv; load_dotenv(); psycopg2.connect(os.environ['DATABASE_URL']).cursor().execute(open('db/schema.sql', encoding='utf-8').read())"
```

Ou rode `scripts/load_reference_data.py`, que já aplica o schema antes de carregar dados.

Crie o primeiro tenant + admin:

```bash
python scripts/seed_admin.py
```

O script pede tenant, nome, e-mail e senha interativamente, ou lê de variáveis de ambiente (`SEED_TENANT_NAME`, `SEED_TENANT_SLUG`, `SEED_ADMIN_NAME`, `SEED_ADMIN_EMAIL`, `SEED_ADMIN_PASSWORD`) se estiverem definidas no `.env`.

## Rodando localmente

```bash
python main.py
```

Acessa em `http://localhost:5000`.

## Deploy na Vercel

O deploy usa `api/index.py` (que importa o `app` do `app.py`) e as rotas definidas em `vercel.json`. Configure `DATABASE_URL`, `SECRET_KEY`, `REDIS_URL`, `TURNSTILE_SITE_KEY` e `TURNSTILE_SECRET_KEY` como variáveis de ambiente no painel da Vercel (nunca commitadas no repositório). Sem `SECRET_KEY` ou sem as chaves do Turnstile configuradas, a aplicação recusa subir na Vercel (falha rápido em vez de rodar com uma chave instável entre invocações).

## Migração de dados de referência (regime tributário / Lei do Bem)

Essas duas tabelas (`cnpj_tipo_tributacao` e `cnpj_lei_do_bem`) são dados baixados manualmente da internet (ver `help/fonte.txt`), armazenados no banco próprio - não vêm mais de nenhum sistema de terceiros. Para uma extração pontual de uma fonte legada:

```bash
python scripts/extract_reference_data.py   # gera data/*.csv
python scripts/load_reference_data.py      # aplica o schema e carrega os CSVs no Postgres
```

O primeiro script lê credenciais de um `.env.migration` (não commitado) - só é necessário se você tiver uma fonte legada para migrar.

## Papéis de usuário

- **admin:** acesso à tela `/admin` (CRUD de usuários e tenants).
- **user:** acesso apenas à consulta de CNPJ.

Todo usuário pertence a um tenant (empresa). Não é permitido desativar, excluir ou remover a role `admin` do único usuário admin ativo do sistema, nem mover esse admin para um tenant inativo, nem desativar o tenant que contém o único admin ativo - qualquer uma dessas ações causaria perda total de acesso à administração.

## Segurança

- Senhas com hash (`werkzeug.security`), nunca em texto puro.
- CSRF (`Flask-WTF`) em todos os formulários POST.
- Rate limiting no login (`Flask-Limiter`) com backend Redis (Upstash, via `REDIS_URL`) - necessário porque o armazenamento em memória padrão não é compartilhado entre invocações serverless.
- CAPTCHA invisível (Cloudflare Turnstile) no login, validado no servidor (`/siteverify`) antes de consultar o banco.
- Headers `X-Frame-Options`, `X-Content-Type-Options` e `Referrer-Policy` em todas as respostas.
- Erros internos (banco, APIs externas) são logados no servidor e nunca expostos ao usuário.
- Trilha de auditoria (`audit_log`) para ações administrativas.
- As consultas ao INPI (scraping) usam timeout curto e uma única tentativa, para não estourar o tempo limite de execução da função serverless na Vercel.
