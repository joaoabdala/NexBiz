"""
Consulta de Marcas no INPI por CNPJ
-------------------------------------
Verifica se uma empresa possui marca registrada no INPI.
Retorna apenas SIM ou NÃO.

Dependências:
    pip install requests beautifulsoup4

Uso:
    python consulta_inpi_marcas.py
    python consulta_inpi_marcas.py --cnpj 07.827.213/0001-30
"""

import argparse
import re
import time

import requests
from bs4 import BeautifulSoup

BASE_URL        = "https://busca.inpi.gov.br"
URL_LOGIN       = "https://busca.inpi.gov.br/pePI/servlet/LoginController"
URL_BUSCA       = "https://busca.inpi.gov.br/pePI/servlet/MarcasServletController"

MSG_SEM_RESULTADO = "Nenhum Nome de Titular foi encontrado"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": BASE_URL + "/pePI/",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded",
}

# Valores baixos de propósito: essa consulta roda dentro de uma função
# serverless (Vercel) com tempo de execução limitado (10s no plano Hobby).
# Preferimos falhar rápido (o frontend já trata isso como "não foi possível
# consultar o INPI") a estourar o timeout da própria função no meio de
# várias tentativas.
MAX_TENTATIVAS  = 1
DELAY_RETRY     = 1
TIMEOUT_REQUISICAO = 6


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def limpar_cnpj(cnpj: str) -> str:
    return re.sub(r"\D", "", cnpj)

def formatar_cnpj(cnpj: str) -> str:
    c = limpar_cnpj(cnpj)
    if len(c) != 14:
        raise ValueError(f"CNPJ inválido: '{cnpj}'. Deve ter 14 dígitos.")
    return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}"

def eh_pagina_login(texto: str) -> bool:
    return "T_Login" in texto or ("Continuar" in texto and "T_Senha" in texto)


# ──────────────────────────────────────────────
# Sessão com login anônimo
# ──────────────────────────────────────────────

def iniciar_sessao() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    # Pega cookie de sessão
    session.get(BASE_URL + "/pePI/", timeout=TIMEOUT_REQUISICAO)
    # Login anônimo (clica em "Continuar")
    session.post(
        URL_LOGIN + "?action=login",
        data={"T_Login": "", "T_Senha": "", "action": "login"},
        timeout=TIMEOUT_REQUISICAO,
        allow_redirects=True,
    )
    return session


# ──────────────────────────────────────────────
# Consulta principal
# ──────────────────────────────────────────────

def possui_marca_no_inpi(cnpj: str) -> bool:
    cnpj_num = limpar_cnpj(cnpj)

    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            if tentativa > 1:
                print(f"  Tentativa {tentativa}/{MAX_TENTATIVAS}...")
                time.sleep(DELAY_RETRY)

            session = iniciar_sessao()

            # Submete o formulário exatamente como o navegador faz
            r = session.post(
                URL_BUSCA,
                data={
                    "cpf_cgc_numINPI": cnpj_num,
                    "nomeTitular":     "",
                    "Action":          "searchNome",
                    "precisao":        "aproximacao",
                    "tipoPesquisa":    "BY_CNPJ_NOME",
                    "registerPerPage": "20",
                    "botao":           "pesquisar",
                },
                headers={**HEADERS, "Referer": BASE_URL + "/pePI/jsp/marcas/Pesquisa_titular.jsp"},
                timeout=TIMEOUT_REQUISICAO,
            )
            r.raise_for_status()
            r.encoding = "utf-8"

            texto = BeautifulSoup(r.text, "html.parser").get_text()

            if eh_pagina_login(texto):
                print("  Portal redirecionou para login, reiniciando sessão...")
                continue

            if MSG_SEM_RESULTADO in texto:
                return False

            if "Foram encontrados" in texto:
                return True

            # Fallback
            soup = BeautifulSoup(r.text, "html.parser")
            tabela = soup.find("table")
            if tabela and tabela.find("a"):
                return True

            return False

        except requests.exceptions.ConnectionError:
            print(f"  Falha de conexão na tentativa {tentativa}/{MAX_TENTATIVAS}.")
            if tentativa == MAX_TENTATIVAS:
                raise

        except requests.exceptions.HTTPError as e:
            print(f"  Erro HTTP na tentativa {tentativa}/{MAX_TENTATIVAS}: {e}")
            if tentativa == MAX_TENTATIVAS:
                raise

    raise RuntimeError("Número máximo de tentativas atingido sem resposta válida.")


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Verifica se uma empresa tem marca registrada no INPI."
    )
    parser.add_argument("--cnpj", type=str, default=None)
    args = parser.parse_args()

    cnpj_input = args.cnpj or input("Digite o CNPJ da empresa: ").strip()

    try:
        cnpj_fmt = formatar_cnpj(cnpj_input)
        print(f"\nCNPJ: {cnpj_fmt}")

        encontrou = possui_marca_no_inpi(cnpj_input)

        if encontrou:
            print("✅ Possui marca registrada no INPI.")
        else:
            print("❌ Nenhuma marca registrada encontrada no INPI.")

    except ValueError as e:
        print(f"\nErro: {e}")
    except requests.exceptions.ConnectionError:
        print("\nErro de conexão. Verifique sua internet e tente novamente.")
    except requests.exceptions.HTTPError as e:
        print(f"\nErro HTTP: {e}")
    except RuntimeError as e:
        print(f"\n{e}")
