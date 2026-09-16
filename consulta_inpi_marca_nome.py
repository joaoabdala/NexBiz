"""
Consulta de Marcas no INPI por Nome
-------------------------------------
Verifica se existe alguma marca registrada no INPI com o nome informado.
Retorna apenas SIM ou NÃO.

Dependências:
    pip install requests beautifulsoup4

Uso:
    python consulta_inpi_marca_nome.py
    python consulta_inpi_marca_nome.py --nome "ARANHA FERREIRA"
    python consulta_inpi_marca_nome.py --nome "XYZABCDEF123" --debug
"""

import argparse
import time

import requests
from bs4 import BeautifulSoup

BASE_URL  = "https://busca.inpi.gov.br"
URL_LOGIN = "https://busca.inpi.gov.br/pePI/servlet/LoginController"
URL_FORM  = "https://busca.inpi.gov.br/pePI/jsp/marcas/Pesquisa_classe_basica.jsp"
URL_BUSCA = "https://busca.inpi.gov.br/pePI/servlet/MarcasServletController"

# Campos fixos do formulário (confirmados pelo debug)
PAYLOAD_BASE = {
    "buscaExata":      "sim",   # primeiro radio - pesquisa exata
    "txt":             "",
    "classeInter":     "",
    "registerPerPage": "20",    # obrigatório - evita NumberFormatException
    "botao":           "pesquisar",
    "Action":          "searchMarca",
    "tipoPesquisa":    "BY_MARCA_CLASSIF_BASICA",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": URL_FORM,
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded",
}

MAX_TENTATIVAS = 3
DELAY_RETRY    = 3


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def eh_pagina_login(texto: str) -> bool:
    return "T_Login" in texto or ("Continuar" in texto and "T_Senha" in texto)

def eh_erro_servidor(texto: str) -> bool:
    return "NumberFormatException" in texto or "Erro na informa" in texto


# ──────────────────────────────────────────────
# Sessão com login anônimo
# ──────────────────────────────────────────────

def iniciar_sessao() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    session.get(BASE_URL + "/pePI/", timeout=20)
    session.post(
        URL_LOGIN + "?action=login",
        data={"T_Login": "", "T_Senha": "", "action": "login"},
        timeout=20,
        allow_redirects=True,
    )
    return session


# ──────────────────────────────────────────────
# Debug
# ──────────────────────────────────────────────

def debug_busca(nome: str):
    """Mostra o texto bruto retornado pelo portal para o nome informado."""
    print(f"\n=== DEBUG: buscando '{nome}' ===")
    session = iniciar_sessao()
    payload = {**PAYLOAD_BASE, "marca": nome}
    print(f"Payload: {payload}")
    r = session.post(URL_BUSCA, data=payload, headers=HEADERS, timeout=20)
    r.encoding = "utf-8"
    soup = BeautifulSoup(r.text, "html.parser")
    print(f"\nStatus: {r.status_code} | URL: {r.url}")
    print("\n=== TEXTO RETORNADO ===")
    print(soup.get_text())


# ──────────────────────────────────────────────
# Consulta principal
# ──────────────────────────────────────────────

def _buscar(session: requests.Session, nome: str, exata: bool) -> bool:
    """
    Executa uma única busca (exata ou radical) e retorna True se encontrou.
    """
    payload = {
        **PAYLOAD_BASE,
        "marca":      nome,
        "buscaExata": "sim" if exata else "nao",
    }

    r = session.post(URL_BUSCA, data=payload, headers=HEADERS, timeout=20)
    r.raise_for_status()
    r.encoding = "utf-8"

    soup = BeautifulSoup(r.text, "html.parser")
    texto = soup.get_text()

    if eh_pagina_login(texto):
        return None  # sinaliza que precisa renovar sessão

    if eh_erro_servidor(texto):
        raise RuntimeError("Erro no servidor do INPI: verifique os campos enviados.")

    if "Foram encontrados" in texto:
        return True

    tabela = soup.find("table")
    if tabela and tabela.find("a"):
        return True

    return False


def possui_marca_por_nome(nome: str) -> bool:
    """
    Realiza busca exata E radical.
    Retorna True se qualquer uma das duas encontrar resultado.
    """
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            if tentativa > 1:
                print(f"  Tentativa {tentativa}/{MAX_TENTATIVAS}...")
                time.sleep(DELAY_RETRY)

            session = iniciar_sessao()

            # ── Busca exata ──
            print("  Busca exata...")
            resultado_exata = _buscar(session, nome, exata=True)
            if resultado_exata is None:
                print("  Portal redirecionou para login, reiniciando sessão...")
                continue
            if resultado_exata:
                return True

            # ── Busca radical ──
            time.sleep(1)  # pequena pausa entre as duas requisições
            print("  Busca radical...")
            resultado_radical = _buscar(session, nome, exata=False)
            if resultado_radical is None:
                print("  Portal redirecionou para login, reiniciando sessão...")
                continue

            return resultado_radical

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
        description="Verifica se existe marca registrada no INPI pelo nome."
    )
    parser.add_argument("--nome", type=str, default=None)
    parser.add_argument("--debug", action="store_true", help="Mostra o HTML bruto retornado")
    args = parser.parse_args()

    nome_input = args.nome or input("Digite o nome da marca: ").strip()

    if args.debug:
        debug_busca(nome_input)
    else:
        try:
            print(f"\nMarca: {nome_input}")
            encontrou = possui_marca_por_nome(nome_input)

            if encontrou:
                print("✅ Marca encontrada no INPI.")
            else:
                print("❌ Nenhuma marca encontrada no INPI.")

        except requests.exceptions.ConnectionError:
            print("\nErro de conexão. Verifique sua internet e tente novamente.")
        except requests.exceptions.HTTPError as e:
            print(f"\nErro HTTP: {e}")
        except RuntimeError as e:
            print(f"\n{e}")
