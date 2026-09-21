"""
exportacoes.py - Geração dos arquivos de exportação (XLSX e PDF) da
consulta de CNPJ, chamados pelas rotas /exportar/xlsx e /exportar/pdf
em app.py.

Recebem sempre os mesmos dados já calculados pela consulta (o dict
"resultado" da ReceitaWS + tributação/Lei do Bem + status do INPI que o
frontend já resolveu de forma assíncrona) - não fazem nenhuma chamada de
rede, só formatam o que já foi buscado.
"""

import io
from datetime import datetime
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.lib.styles import ParagraphStyle

# Paleta - mesmas cores de static/css/theme.css (tema dark), pra manter a
# identidade visual do site também nos arquivos exportados.
COR_NAVY = "0D1425"
COR_NAVY_ESCURO = "0A0F1E"
COR_ACENTO = "00D4FF"
COR_SUCESSO = "5DCAA5"
COR_ERRO = "E24B4A"
COR_TEXTO_SECUNDARIO = "7A8BA0"
COR_CINZA_CLARO = "F0F4FA"
COR_BORDA = "DCE3EC"

# Servidor roda em UTC (Vercel) - "Gerado em ..." precisa do horário de
# Brasília, senão o timestamp mostrado fica 3h à frente do real.
TZ_BRASILIA = ZoneInfo("America/Sao_Paulo")


def _agora_brasilia() -> datetime:
    return datetime.now(TZ_BRASILIA)


def _formatar_capital_social(valor) -> str:
    try:
        v = float(valor)
    except (TypeError, ValueError):
        return str(valor or "-")
    return "R$ " + "{:,.2f}".format(v).replace(",", "X").replace(".", ",").replace("X", ".")


def _texto_inpi(status: str, encontrada: str, nao_encontrada: str) -> str:
    return {
        "sim": encontrada,
        "nao": nao_encontrada,
        "erro": "Não foi possível consultar",
    }.get(status, "Consulta não concluída")


def _linhas_identificacao(resultado, msg_tp_tributacao, msg_lei_do_bem, inpi_cnpj_status, inpi_nome_status):
    simples = resultado.get("simples") or {}
    return [
        ("Nome", resultado.get("nome") or "-"),
        ("CNPJ", resultado.get("cnpj") or "-"),
        ("Fantasia", resultado.get("fantasia") or "-"),
        ("Situação", resultado.get("situacao") or "-"),
        ("Telefone", resultado.get("telefone") or "-"),
        ("E-mail", resultado.get("email") or "-"),
        ("Tributação", msg_tp_tributacao or "-"),
        ("Lei do Bem", msg_lei_do_bem or "-"),
        ("INPI por CNPJ", _texto_inpi(inpi_cnpj_status, "Possui marca registrada", "Sem marca registrada")),
        ("INPI por Nome", _texto_inpi(inpi_nome_status, "Marca encontrada pelo nome", "Nenhuma marca encontrada pelo nome")),
        ("Abertura", resultado.get("abertura") or "-"),
        ("UF", resultado.get("uf") or "-"),
        ("Capital Social", _formatar_capital_social(resultado.get("capital_social"))),
        ("Natureza Jurídica", resultado.get("natureza_juridica") or "-"),
        ("Simples - Optante", "Sim" if simples.get("optante") else "Não"),
        ("Simples - Início", simples.get("data_opcao") or "-"),
        ("Simples - Saída", simples.get("data_exclusao") or "-"),
        ("Simples - Atualização", simples.get("ultima_atualizacao_formatada") or "-"),
    ]


def _endereco_formatado(resultado) -> str:
    if not resultado.get("logradouro"):
        return ""
    partes = [resultado.get("logradouro", "")]
    if resultado.get("numero"):
        partes[0] += f", {resultado['numero']}"
    if resultado.get("complemento"):
        partes.append(resultado["complemento"])
    partes.append(f"{resultado.get('bairro', '')}, {resultado.get('municipio', '')}/{resultado.get('uf', '')}")
    partes.append(f"CEP {resultado.get('cep', '')}")
    return " · ".join(p for p in partes if p)


def _nome_arquivo(resultado, extensao: str) -> str:
    cnpj = (resultado.get("cnpj") or "cnpj").replace(".", "").replace("/", "").replace("-", "")
    return f"consulta-cnpj-{cnpj}.{extensao}"


# ─────────────────────────────────────────────────────────────
# XLSX
# ─────────────────────────────────────────────────────────────

def gerar_xlsx(resultado, msg_tp_tributacao, msg_lei_do_bem, inpi_cnpj_status, inpi_nome_status):
    wb = Workbook()
    ws = wb.active
    ws.title = "Consulta CNPJ"
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 60

    fonte_titulo = Font(name="Calibri", size=16, bold=True, color="FFFFFF")
    fonte_subtitulo = Font(name="Calibri", size=10, italic=True, color=COR_TEXTO_SECUNDARIO)
    fonte_secao = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    fonte_label = Font(name="Calibri", size=10, bold=True, color="333333")
    fonte_valor = Font(name="Calibri", size=10, color="111111")
    fonte_cabecalho_tabela = Font(name="Calibri", size=10, bold=True, color="FFFFFF")

    preenchimento_titulo = PatternFill("solid", fgColor=COR_NAVY)
    preenchimento_secao = PatternFill("solid", fgColor=COR_NAVY)
    preenchimento_zebra = PatternFill("solid", fgColor=COR_CINZA_CLARO)
    borda_fina = Border(bottom=Side(style="thin", color=COR_BORDA))

    linha = 1

    def escrever_titulo():
        nonlocal linha
        ws.merge_cells(start_row=linha, start_column=1, end_row=linha, end_column=2)
        celula = ws.cell(row=linha, column=1, value="Relatório de Consulta CNPJ")
        celula.font = fonte_titulo
        celula.fill = preenchimento_titulo
        celula.alignment = Alignment(vertical="center", horizontal="left", indent=1)
        ws.row_dimensions[linha].height = 32
        linha += 1

        ws.merge_cells(start_row=linha, start_column=1, end_row=linha, end_column=2)
        agora = _agora_brasilia().strftime("%d/%m/%Y %H:%M")
        empresa = resultado.get("fantasia") or resultado.get("nome") or ""
        celula = ws.cell(row=linha, column=1, value=f"{empresa} · Gerado por NexBiz (Abdala Nexus) em {agora}")
        celula.font = fonte_subtitulo
        celula.alignment = Alignment(vertical="center", horizontal="left", indent=1)
        linha += 2

    def escrever_secao(titulo):
        nonlocal linha
        ws.merge_cells(start_row=linha, start_column=1, end_row=linha, end_column=2)
        celula = ws.cell(row=linha, column=1, value=titulo)
        celula.font = fonte_secao
        celula.fill = preenchimento_secao
        celula.alignment = Alignment(vertical="center", horizontal="left", indent=1)
        ws.row_dimensions[linha].height = 22
        linha += 1

    def escrever_par(label, valor, zebra):
        nonlocal linha
        c1 = ws.cell(row=linha, column=1, value=label)
        c2 = ws.cell(row=linha, column=2, value=valor)
        c1.font = fonte_label
        c2.font = fonte_valor
        c1.border = borda_fina
        c2.border = borda_fina
        c1.alignment = Alignment(vertical="center", wrap_text=True, indent=1)
        c2.alignment = Alignment(vertical="center", wrap_text=True, indent=1)
        if zebra:
            c1.fill = preenchimento_zebra
            c2.fill = preenchimento_zebra
        linha += 1

    def escrever_tabela(titulo, cabecalhos, linhas_dados):
        nonlocal linha
        escrever_secao(titulo)
        if not linhas_dados:
            escrever_par("—", "Nenhum registro encontrado.", False)
            linha += 1
            return
        for col, texto in enumerate(cabecalhos, start=1):
            c = ws.cell(row=linha, column=col, value=texto)
            c.font = fonte_cabecalho_tabela
            c.fill = preenchimento_secao
            c.alignment = Alignment(vertical="center", indent=1)
        linha += 1
        for i, dados in enumerate(linhas_dados):
            for col, texto in enumerate(dados, start=1):
                c = ws.cell(row=linha, column=col, value=texto)
                c.font = fonte_valor
                c.border = borda_fina
                c.alignment = Alignment(vertical="center", wrap_text=True, indent=1)
                if i % 2 == 1:
                    c.fill = preenchimento_zebra
            linha += 1
        linha += 1

    escrever_titulo()

    escrever_secao("Identificação e dados societários")
    for i, (label, valor) in enumerate(
        _linhas_identificacao(resultado, msg_tp_tributacao, msg_lei_do_bem, inpi_cnpj_status, inpi_nome_status)
    ):
        escrever_par(label, valor, i % 2 == 1)
    linha += 1

    endereco = _endereco_formatado(resultado)
    if endereco:
        escrever_secao("Endereço")
        escrever_par("Endereço completo", endereco, False)
        linha += 1

    socios = [(s.get("nome", "-"), s.get("qual", "-")) for s in (resultado.get("qsa") or [])]
    escrever_tabela("Sócios", ["Nome", "Qualificação"], socios)

    principal = [(a.get("code", "-"), a.get("text", "-")) for a in (resultado.get("atividade_principal") or [])]
    escrever_tabela("Atividade principal", ["Código", "Descrição"], principal)

    secundarias = [(a.get("code", "-"), a.get("text", "-")) for a in (resultado.get("atividades_secundarias") or [])]
    escrever_tabela("Atividades secundárias", ["Código", "Descrição"], secundarias)

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer, _nome_arquivo(resultado, "xlsx")


# ─────────────────────────────────────────────────────────────
# PDF
# ─────────────────────────────────────────────────────────────

_ESTILOS = {
    "secao": ParagraphStyle(
        "secao", fontName="Helvetica-Bold", fontSize=11, textColor=colors.white, leading=14
    ),
    "label": ParagraphStyle("label", fontName="Helvetica-Bold", fontSize=9, textColor=colors.HexColor("#333333"), leading=12),
    "valor": ParagraphStyle("valor", fontName="Helvetica", fontSize=9, textColor=colors.HexColor("#111111"), leading=12),
    "cabecalho_tabela": ParagraphStyle("cabecalho_tabela", fontName="Helvetica-Bold", fontSize=9, textColor=colors.white, leading=12),
    "vazio": ParagraphStyle("vazio", fontName="Helvetica-Oblique", fontSize=9, textColor=colors.HexColor(f"#{COR_TEXTO_SECUNDARIO}")),
}


def _tabela_pares(linhas_labels_valores):
    dados = [[Paragraph(label, _ESTILOS["label"]), Paragraph(str(valor), _ESTILOS["valor"])] for label, valor in linhas_labels_valores]
    tabela = Table(dados, colWidths=[4.5 * cm, 12.5 * cm])
    estilo = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor(f"#{COR_BORDA}")),
    ]
    for i in range(len(dados)):
        if i % 2 == 1:
            estilo.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor(f"#{COR_CINZA_CLARO}")))
    tabela.setStyle(TableStyle(estilo))
    return tabela


def _titulo_secao(texto):
    tabela = Table([[Paragraph(texto, _ESTILOS["secao"])]], colWidths=[17 * cm])
    tabela.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(f"#{COR_NAVY}")),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return tabela


def _tabela_lista(cabecalhos, linhas_dados, larguras):
    if not linhas_dados:
        corpo = [[Paragraph("Nenhum registro encontrado.", _ESTILOS["vazio"])]]
        tabela = Table(corpo, colWidths=[sum(larguras)])
        tabela.setStyle(
            TableStyle(
                [
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor(f"#{COR_BORDA}")),
                ]
            )
        )
        return tabela

    dados = [[Paragraph(c, _ESTILOS["cabecalho_tabela"]) for c in cabecalhos]]
    for linha_dados in linhas_dados:
        dados.append([Paragraph(str(v), _ESTILOS["valor"]) for v in linha_dados])

    tabela = Table(dados, colWidths=larguras, repeatRows=1)
    estilo = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(f"#{COR_NAVY}")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor(f"#{COR_BORDA}")),
    ]
    for i in range(1, len(dados)):
        if i % 2 == 0:
            estilo.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor(f"#{COR_CINZA_CLARO}")))
    tabela.setStyle(TableStyle(estilo))
    return tabela


class _NumberedCanvas(pdf_canvas.Canvas):
    """Canvas que sabe o total de páginas - permite mostrar 'Página X de Y'
    no rodapé (renderiza em duas passadas: guarda o estado de cada página
    e só desenha o rodapé no save() final, quando já sabe o total)."""

    def __init__(self, *args, empresa_nome="", **kwargs):
        super().__init__(*args, **kwargs)
        self._paginas = []
        self._empresa_nome = empresa_nome

    def showPage(self):
        self._paginas.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._paginas)
        # Contador de anotação (usado pelo linkURL do rodapé pra nomear o
        # link) como variável LOCAL, de propósito - se fosse atributo da
        # instância (self.algumacoisa), o "self.__dict__.update(estado)"
        # logo abaixo ia sobrescrever ele de volta pro valor congelado na
        # captura de cada página (sempre 0, já que a página é capturada
        # antes do rodapé existir), fazendo cada página gerar o mesmo nome
        # de anotação ("NUMBER1") e colidir com a anterior.
        proximo_id_anotacao = 0
        for estado in self._paginas:
            self.__dict__.update(estado)
            self._annotationCount = proximo_id_anotacao
            self._desenhar_rodape(total)
            proximo_id_anotacao = self._annotationCount
            pdf_canvas.Canvas.showPage(self)
        pdf_canvas.Canvas.save(self)

    def _desenhar_rodape(self, total_paginas):
        largura, _ = A4
        self.setStrokeColor(colors.HexColor(f"#{COR_ACENTO}"))
        self.setLineWidth(1)
        self.line(2 * cm, 1.7 * cm, largura - 2 * cm, 1.7 * cm)

        # "NexBiz · " (fixo) + "Abdala Nexus" (link pro site institucional,
        # em ciano pra parecer clicável - mesmo padrão do rodapé do site).
        self.setFont("Helvetica-Bold", 8)
        y_marca = 1.15 * cm
        x = 2 * cm
        prefixo = "NexBiz · "
        self.setFillColor(colors.HexColor(f"#{COR_NAVY}"))
        self.drawString(x, y_marca, prefixo)
        x_link = x + self.stringWidth(prefixo, "Helvetica-Bold", 8)

        texto_link = "Abdala Nexus"
        largura_link = self.stringWidth(texto_link, "Helvetica-Bold", 8)
        self.setFillColor(colors.HexColor(f"#{COR_ACENTO}"))
        self.drawString(x_link, y_marca, texto_link)
        self.linkURL(
            "https://www.abdalanexus.com/",
            (x_link, y_marca - 2, x_link + largura_link, y_marca + 8),
            relative=0,
            thickness=0,
        )

        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor(f"#{COR_TEXTO_SECUNDARIO}"))
        self.drawString(2 * cm, 0.75 * cm, self._empresa_nome)

        self.drawRightString(
            largura - 2 * cm, 1.15 * cm, f"Página {self._pageNumber} de {total_paginas}"
        )
        self.drawRightString(
            largura - 2 * cm, 0.75 * cm, _agora_brasilia().strftime("Gerado em %d/%m/%Y %H:%M")
        )


def gerar_pdf(resultado, msg_tp_tributacao, msg_lei_do_bem, inpi_cnpj_status, inpi_nome_status, caminho_logo=None):
    buffer = io.BytesIO()
    largura_pagina, altura_pagina = A4
    empresa_nome = resultado.get("fantasia") or resultado.get("nome") or "-"

    margem_topo = 3.6 * cm  # espaço reservado pro cabeçalho, desenhado à parte
    margem_rodape = 2.2 * cm

    doc = BaseDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=margem_topo,
        bottomMargin=margem_rodape,
        title=f"Consulta CNPJ - {empresa_nome}",
        author="NexBiz - Abdala Nexus",
    )

    frame = Frame(
        doc.leftMargin,
        doc.bottomMargin,
        doc.width,
        doc.height,
        id="conteudo",
    )

    def desenhar_cabecalho(canvas_obj, _doc):
        canvas_obj.saveState()
        canvas_obj.setFillColor(colors.HexColor(f"#{COR_NAVY_ESCURO}"))
        canvas_obj.rect(0, altura_pagina - 2.6 * cm, largura_pagina, 2.6 * cm, stroke=0, fill=1)

        # O logo (com o fundo branco) é posicionado primeiro, com uma
        # margem fixa a partir do topo da faixa - o título é alinhado
        # a partir DELE depois (topo do texto == topo do badge branco),
        # não o contrário como na tentativa anterior (que centralizava
        # o título com o meio do logo - não era o que tinha sido pedido).
        margem_topo_badge = 0.5 * cm
        x_texto = 2 * cm  # sem logo, o texto cai na margem esquerda normal

        if caminho_logo:
            try:
                # Lockup horizontal (marca + "Abdala Nexus") - a proporção
                # real do arquivo é lida pra não distorcer se o logo mudar.
                proporcao = 455 / 66
                try:
                    from PIL import Image as _PILImage

                    with _PILImage.open(caminho_logo) as _img:
                        proporcao = _img.width / _img.height
                except Exception:
                    pass
                logo_altura = 0.95 * cm
                logo_largura = logo_altura * proporcao
                logo_x = 2 * cm

                pad_h = 0.22 * cm
                pad_v = 0.14 * cm
                badge_altura = logo_altura + 2 * pad_v
                badge_topo = altura_pagina - margem_topo_badge
                badge_base = badge_topo - badge_altura
                logo_y = badge_base + pad_v

                # Fundo branco arredondado atrás do logo, igual ao site
                # institucional (o lockup é escuro/ciano, sem isso ele some
                # em cima da faixa navy do cabeçalho).
                canvas_obj.setFillColor(colors.white)
                canvas_obj.roundRect(
                    logo_x - pad_h,
                    badge_base,
                    logo_largura + 2 * pad_h,
                    badge_altura,
                    radius=0.12 * cm,
                    stroke=0,
                    fill=1,
                )

                canvas_obj.drawImage(
                    caminho_logo,
                    logo_x,
                    logo_y,
                    width=logo_largura,
                    height=logo_altura,
                    mask="auto",
                )

                x_texto = logo_x + logo_largura + 2 * pad_h + 0.5 * cm
            except Exception:
                badge_topo = altura_pagina - margem_topo_badge
        else:
            badge_topo = altura_pagina - margem_topo_badge

        # Título: o topo do texto (baseline + cap-height do Helvetica-Bold
        # 16pt) alinhado com o topo do badge branco. 0.40cm (cap-height
        # "de livro") deixava o título um pouco acima do badge de verdade -
        # 0.50cm bateu certo depois de medir pixel a pixel.
        cap_height_titulo = 0.43 * cm
        y_titulo = badge_topo - cap_height_titulo
        y_subtitulo = y_titulo - 0.6 * cm

        canvas_obj.setFillColor(colors.white)
        canvas_obj.setFont("Helvetica-Bold", 16)
        canvas_obj.drawString(x_texto, y_titulo, "Relatório de Consulta CNPJ")
        canvas_obj.setFont("Helvetica", 10)
        canvas_obj.setFillColor(colors.HexColor(f"#{COR_ACENTO}"))
        canvas_obj.drawString(x_texto, y_subtitulo, "Gerado por NexBiz - Abdala Nexus")
        canvas_obj.restoreState()

    doc.addPageTemplates([PageTemplate(id="padrao", frames=[frame], onPage=desenhar_cabecalho)])

    elementos = []

    elementos.append(_titulo_secao("Identificação e dados societários"))
    elementos.append(
        _tabela_pares(
            _linhas_identificacao(resultado, msg_tp_tributacao, msg_lei_do_bem, inpi_cnpj_status, inpi_nome_status)
        )
    )
    elementos.append(Spacer(1, 14))

    endereco = _endereco_formatado(resultado)
    if endereco:
        elementos.append(_titulo_secao("Endereço"))
        elementos.append(_tabela_pares([("Endereço completo", endereco)]))
        elementos.append(Spacer(1, 14))

    socios = [(s.get("nome", "-"), s.get("qual", "-")) for s in (resultado.get("qsa") or [])]
    elementos.append(_titulo_secao("Sócios"))
    elementos.append(_tabela_lista(["Nome", "Qualificação"], socios, [8.5 * cm, 8.5 * cm]))
    elementos.append(Spacer(1, 14))

    principal = [(a.get("code", "-"), a.get("text", "-")) for a in (resultado.get("atividade_principal") or [])]
    elementos.append(_titulo_secao("Atividade principal"))
    elementos.append(_tabela_lista(["Código", "Descrição"], principal, [3 * cm, 14 * cm]))
    elementos.append(Spacer(1, 14))

    secundarias = [(a.get("code", "-"), a.get("text", "-")) for a in (resultado.get("atividades_secundarias") or [])]
    elementos.append(_titulo_secao("Atividades secundárias"))
    elementos.append(_tabela_lista(["Código", "Descrição"], secundarias, [3 * cm, 14 * cm]))

    def fabricar_canvas(*args, **kwargs):
        return _NumberedCanvas(*args, empresa_nome=f"{empresa_nome} · CNPJ {resultado.get('cnpj', '-')}", **kwargs)

    doc.build(elementos, canvasmaker=fabricar_canvas)
    buffer.seek(0)
    return buffer, _nome_arquivo(resultado, "pdf")
