-- Abdala Nexus - Consultar CNPJ
-- Schema Postgres (Neon). Idempotente - pode ser rodado mais de uma vez.

CREATE TABLE IF NOT EXISTS tenants (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL UNIQUE,
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id             SERIAL PRIMARY KEY,
    tenant_id      INTEGER NOT NULL REFERENCES tenants(id),
    name           TEXT NOT NULL,
    email          TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    role           TEXT NOT NULL DEFAULT 'user',
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_users_tenant_id ON users (tenant_id);

-- Último login bem-sucedido (NULL = nunca logou). Gravado em login() no
-- app.py. Instalações que já rodaram uma versão anterior deste schema
-- ganham a coluna aqui.
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ;

-- Roles: 'admin' = super-admin (global), 'gestor' = administra só os
-- usuários do próprio tenant, 'user' = usuário comum. A constraint é
-- recriada pra instalações antigas, que só aceitavam 'admin' e 'user'.
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('admin', 'gestor', 'user'));

-- Sessão única: token da sessão vigente do usuário. Cada login gera um novo
-- e toda requisição compara com o do cookie - login em outro dispositivo
-- derruba o anterior. NULL = nenhuma sessão válida.
ALTER TABLE users ADD COLUMN IF NOT EXISTS session_token TEXT;

-- Limite de usuários ativos por tenant (gestores contam, super-admins não).
-- NULL = sem limite - só tenants criados antes desta coluna; a tela de
-- admin exige o valor ao criar.
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS max_active_users INTEGER CHECK (max_active_users > 0);

-- Dados de referência baixados do governo (regime tributário e Lei do Bem).
-- Globais - compartilhados entre todos os tenants, não têm tenant_id.

-- cnpj fica como TEXT (não VARCHAR(14)) porque a origem grava o CNPJ
-- formatado (ex.: "04.252.011/0001-10", 18 caracteres) - mesmo formato que
-- a ReceitaWS devolve e que o app usa para o lookup (WHERE cnpj = %s).
CREATE TABLE IF NOT EXISTS cnpj_tipo_tributacao (
    id               SERIAL PRIMARY KEY,
    cnpj             TEXT NOT NULL,
    ano              INTEGER,
    tipo_tributacao  TEXT
);

CREATE INDEX IF NOT EXISTS idx_cnpj_tipo_tributacao_cnpj ON cnpj_tipo_tributacao (cnpj);

CREATE TABLE IF NOT EXISTS cnpj_lei_do_bem (
    id            SERIAL PRIMARY KEY,
    cnpj          TEXT NOT NULL,
    razao_social  VARCHAR(255),
    ano           INTEGER,
    uf            TEXT
);

CREATE INDEX IF NOT EXISTS idx_cnpj_lei_do_bem_cnpj ON cnpj_lei_do_bem (cnpj);

-- Normaliza instalações que já rodaram uma versão anterior deste schema
-- com cnpj VARCHAR(14) (curto demais para o formato com pontuação).
ALTER TABLE cnpj_tipo_tributacao ALTER COLUMN cnpj TYPE TEXT;
ALTER TABLE cnpj_lei_do_bem ALTER COLUMN cnpj TYPE TEXT;

-- Trilha de auditoria das ações feitas na tela de admin.

CREATE TABLE IF NOT EXISTS audit_log (
    id           SERIAL PRIMARY KEY,
    actor_email  TEXT NOT NULL,
    action       TEXT NOT NULL,
    details      TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log (created_at DESC);
