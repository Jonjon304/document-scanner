"""Janela principal: liga as peças da interface e conduz o fluxo do programa.

Quem faz o quê:
    Estado               guarda os dados do documento atual;
    PainelLateral        barra da esquerda (entrada, girar, status);
    AreaPrincipal        abas, imagens, botões de ação e texto;
    ControladorCamera    câmera ao vivo;
    ProcessadorDocumento processamento em segundo plano.

Esta classe NÃO desenha nada nem calcula nada de imagem: ela recebe os avisos
das peças acima (cliques, quadros da câmera, etapas concluídas) e decide o que
atualizar. É o único lugar que conhece todas as peças.

Fluxo típico:
    carregar() ou capturar_quadro()  ->  processar()  ->  (etapas chegam pelos _ao_*)
    ->  salvar() / salvar_texto() / salvar_pdf()
"""

import os
import time
from queue import Empty, Queue
from threading import Thread
import tkinter as tk
from tkinter import filedialog, messagebox

import cv2
from PIL import Image

from interface.area_principal import AreaPrincipal
from interface.camera import ControladorCamera
from interface.conversao import para_rgb
from interface.dialogo_cantos import abrir_dialogo_cantos
from interface.estado import Estado
from interface.painel_lateral import PainelLateral
from interface.processamento import ProcessadorDocumento
from interface.tema import CARTOES_PROCESSO, COR_FUNDO, aplicar_tema_ttk
from nucleo.imagem import desenhar_contorno
from nucleo.ocr import CONFIANCA_MINIMA
from nucleo.pdf import gerar_pdf_pesquisavel


class AplicativoScanner:
    """O aplicativo: cria a janela e coordena todas as peças."""

    def __init__(self, raiz):
        """Configura a janela `raiz` e cria todas as peças da interface."""
        self.raiz = raiz
        self.raiz.title("Scanner de Documentos")
        self.raiz.geometry("1280x820")
        self.raiz.minsize(1000, 700)
        self.raiz.configure(bg=COR_FUNDO)
        self.raiz.protocol("WM_DELETE_WINDOW", self.fechar)
        aplicar_tema_ttk(self.raiz)

        self.estado = Estado()

        # Interface: barra lateral à esquerda, área principal ocupando o resto.
        self.painel = PainelLateral(
            self.raiz,
            ao_carregar=self.carregar,
            ao_alternar_camera=self.alternar_camera,
            ao_capturar=self.capturar_quadro,
            ao_ajustar_cantos=self.ajustar_cantos,
            ao_girar=self.girar,
        )
        self.painel.pack(side="left", fill="y")

        self.area = AreaPrincipal(
            self.raiz,
            ao_processar=self.processar,
            ao_salvar=self.salvar,
            ao_salvar_texto=self.salvar_texto,
            ao_salvar_pdf=self.salvar_pdf,
            ao_texto_editado=self._atualizar_botoes,
        )
        self.area.pack(side="left", fill="both", expand=True)

        # Peças que trabalham "por trás" e avisam por funções.
        self.camera = ControladorCamera(
            self.raiz,
            ao_quadro=self._ao_quadro_camera,
            ao_falha=self._ao_falha_camera,
        )
        self.processador = ProcessadorDocumento(
            self.raiz,
            ao_etapa=self.painel.definir_status,
            ao_preprocessamento=self._ao_preprocessamento,
            ao_deteccao=self._ao_deteccao,
            ao_digitalizada=self._ao_digitalizada,
            ao_concluir=self._ao_concluir,
            ao_erro=self._ao_erro,
        )

        self._gerando_pdf = False   # True enquanto o PDF é gerado em segundo plano
        self._dica_camera = None    # última dica mostrada no status durante a câmera
        self._atualizar_botoes()

    # ------------------------------------------------------------ estado dos botões

    def _atualizar_botoes(self):
        """Ativa/desativa todos os botões conforme o momento do programa.

        Três fatos decidem tudo: se há processamento em andamento, se a câmera
        está ligada e se há imagem carregada (a câmera ligada não conta, porque
        a imagem é só a prévia).
        """
        ocupado = self.processador.ocupado or self._gerando_pdf
        camera_ativa = self.camera.ativa
        tem_imagem = self.estado.imagem is not None and not camera_ativa

        self.painel.atualizar_botoes(ocupado, camera_ativa, tem_imagem)
        self.area.atualizar_botoes(
            ocupado, tem_imagem,
            tem_digitalizada=self.estado.digitalizada is not None,
            tem_pb=self.estado.digitalizada_pb is not None,
        )

    # --------------------------------------------------------------- câmera ao vivo

    def alternar_camera(self):
        """Botão "Câmera ao vivo" / "Fechar câmera"."""
        if self.camera.ativa:
            self.parar_camera()
        else:
            self.iniciar_camera()

    def iniciar_camera(self):
        """Liga a câmera e começa a prévia (descartando o resultado anterior)."""
        if self.processador.ocupado:
            return
        erro = self.camera.abrir()
        if erro is not None:
            messagebox.showerror("Câmera indisponível", erro)
            return

        # Ao entrar no modo câmera, descarta o resultado anterior.
        self.estado.imagem = None
        self.estado.nome_arquivo = ""
        self._limpar_resultados()
        self.area.modo_camera(True)   # tela única: só a imagem da câmera
        self._dica_camera = None
        self.area.limpar_imagens(("original",), "Inicializando câmera…")

        self.painel.definir_status("Câmera iniciada. Enquadre a folha com os quatro cantos "
                                   "visíveis e clique em Capturar quadro.")
        self._atualizar_botoes()
        self.camera.iniciar_previa()

    def parar_camera(self, atualizar_status=True):
        """Desliga a câmera e volta ao layout normal das abas."""
        estava_ativa = self.camera.ativa
        self.camera.parar()
        if estava_ativa:
            self.area.modo_camera(False)
        if atualizar_status:
            self.painel.definir_status("Câmera encerrada. Escolha uma imagem ou inicie a câmera.")
        self._atualizar_botoes()

    def capturar_quadro(self):
        """Congela o quadro atual da câmera e só então o processa.

        Ao vivo nada é processado: o processamento completo (detecção,
        perspectiva e OCR) roda uma única vez, sobre o quadro capturado.
        """
        quadro = self.camera.quadro_atual  # já é uma cópia
        if not self.camera.ativa or quadro is None:
            return

        self.parar_camera(atualizar_status=False)

        self.estado.imagem = quadro
        self.estado.nome_arquivo = time.strftime("captura_%Y%m%d_%H%M%S.jpg")
        # Se a detecção falhar, dá para marcar a folha com Ajustar cantos.
        self._iniciar_processamento(None)

    def _ao_quadro_camera(self, quadro, cantos):
        """Chegou um quadro novo: mostra a imagem com o contorno, se a folha foi achada."""
        if cantos is not None:
            self.area.mostrar("original", desenhar_contorno(quadro, cantos))
            dica = "Documento detectado. Clique em Capturar quadro para digitalizar."
        else:
            self.area.mostrar("original", quadro)
            dica = "Aponte para uma folha com os quatro cantos visíveis."
        # Só troca o texto quando a dica muda (não sobrescreve a cada quadro).
        if dica != self._dica_camera:
            self._dica_camera = dica
            self.painel.definir_status(dica)

    def _ao_falha_camera(self, titulo, mensagem):
        """A câmera não abriu ou falhou durante o uso (já está desligada)."""
        self.parar_camera()  # garante status e botões coerentes
        messagebox.showerror(titulo, mensagem)

    def fechar(self):
        """Fecha a janela, desligando antes a câmera."""
        self.parar_camera(atualizar_status=False)
        self.raiz.destroy()

    # ------------------------------------------------------------ entrada da imagem

    def carregar(self):
        """Botão "Escolher arquivo": abre uma imagem do disco."""
        if self.processador.ocupado:
            return
        if self.camera.ativa:
            self.parar_camera(atualizar_status=False)

        caminho = filedialog.askopenfilename(
            filetypes=[("Imagens", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff"),
                       ("Todos os arquivos", "*.*")]
        )
        if not caminho:
            return

        imagem = cv2.imread(caminho)
        if imagem is None:
            messagebox.showerror("Erro", "Não foi possível abrir essa imagem.")
            return

        self.estado.imagem = imagem
        self.estado.nome_arquivo = os.path.basename(caminho)
        self._limpar_resultados()
        self.area.mostrar("original", imagem)
        self.painel.definir_status("Imagem carregada. Clique em Processar.")
        self._atualizar_botoes()

    def girar(self, codigo, descricao):
        """Gira a imagem de entrada (ex.: foto de cabeça para baixo).

        O giro é feito na imagem original, antes da detecção, e é sem perdas
        (múltiplos de 90°). Os resultados anteriores deixam de valer, então são
        limpos; clique em Processar para analisar a imagem na nova orientação.

        Com a câmera ligada, quem gira é a imagem da câmera (vale para a prévia
        e para o quadro capturado).
        """
        if self.processador.ocupado:
            return
        if self.camera.ativa:
            graus = {cv2.ROTATE_90_CLOCKWISE: 90, cv2.ROTATE_180: 180,
                     cv2.ROTATE_90_COUNTERCLOCKWISE: 270}[codigo]
            self.camera.girar(graus)
            self.painel.definir_status(f"Câmera girada {descricao}. Enquadre a folha e "
                                       "clique em Capturar quadro.")
            return
        if self.estado.imagem is None:
            return
        self.estado.imagem = cv2.rotate(self.estado.imagem, codigo)
        self._limpar_resultados()
        self.area.mostrar("original", self.estado.imagem)
        self.painel.definir_status(f"Imagem girada {descricao}. Clique em Processar.")
        self._atualizar_botoes()

    def ajustar_cantos(self):
        """Botão "Ajustar cantos": abre a janela para arrastar os 4 cantos da folha."""
        if (self.estado.imagem is None or self.processador.ocupado
                or self.camera.ativa):
            return
        abrir_dialogo_cantos(self.raiz, self.estado.imagem, self.estado.cantos,
                             ao_aplicar=self._iniciar_processamento)

    # ----------------------------------------------------------------- processamento

    def processar(self):
        """Botão "Processar": detecta o documento e extrai o texto."""
        if self.estado.imagem is None or self.processador.ocupado:
            return
        self._iniciar_processamento(None)

    def _limpar_resultados(self):
        """Esquece tudo que foi calculado a partir da imagem (a imagem fica)."""
        self.estado.limpar_resultados()
        self.painel.definir_resumo("Processe a imagem para ver a avaliação.")
        self.area.mostrar_texto("")
        self.area.limpar_imagens(CARTOES_PROCESSO, "Aguardando")
        self._atualizar_botoes()

    def _iniciar_processamento(self, cantos_manuais=None):
        """Prepara a tela e dispara o processamento em segundo plano.

        Com `cantos_manuais` (vindos de "Ajustar cantos") a detecção automática
        é pulada. Os resultados chegam depois pelos métodos `_ao_*` abaixo.
        """
        if self.estado.imagem is None or self.processador.ocupado:
            return

        self._limpar_resultados()
        self.area.mostrar("original", self.estado.imagem)
        self.painel.definir_status("Pré-processando a imagem…")
        self.painel.iniciar_progresso()
        self.processador.iniciar(self.estado.imagem.copy(), cantos_manuais)
        self._atualizar_botoes()  # já com o processador ocupado

    def _ao_preprocessamento(self, cinza, desfocada, binaria):
        """Chegaram as imagens do pré-processamento."""
        self.area.mostrar("cinza", cinza)
        self.area.mostrar("desfocada", desfocada)
        self.area.mostrar("binaria", binaria)

    def _ao_deteccao(self, cantos, encontrou, contorno):
        """Chegou o resultado da detecção do documento."""
        self.estado.cantos = cantos
        self.estado.documento_encontrado = encontrou
        self.area.mostrar("contorno", contorno)
        if not encontrou:
            self.painel.definir_status("Documento não detectado: a imagem inteira foi "
                                       "usada. Use Ajustar cantos.")

    def _ao_digitalizada(self, cor, pb):
        """Chegou o documento corrigido (colorido e com efeito de scanner)."""
        self.estado.digitalizada, self.estado.digitalizada_pb = cor, pb
        self.area.mostrar("digitalizada", cor)
        self.area.mostrar("pb", pb)

    def _ao_concluir(self, resultado):
        """O OCR terminou: mostra o texto, a avaliação e a mensagem final."""
        self.painel.encerrar_progresso()

        self.estado.texto_ocr = resultado["texto"]
        self._mostrar_avaliacao(resultado)
        self.area.mostrar_texto(self.estado.texto_ocr)
        self._atualizar_botoes()

        if not self.estado.texto_ocr.strip():
            self.painel.definir_status("Nenhum texto encontrado.")
            messagebox.showinfo("OCR", "Nenhum texto foi encontrado.")
        elif self.estado.documento_encontrado:
            self.painel.definir_status("Pronto. Você pode corrigir o texto e salvar.")
        else:
            self.painel.definir_status("Pronto, mas o documento não foi detectado com "
                                       "segurança. Confira com Ajustar cantos.")

    def _ao_erro(self, mensagem):
        """O processamento falhou em alguma etapa."""
        self.painel.encerrar_progresso()
        self.painel.definir_status("Falha ao processar. Confira a mensagem.")
        self.painel.definir_resumo("Avaliação indisponível.")
        self._atualizar_botoes()
        messagebox.showerror("Erro", mensagem)

    def _mostrar_avaliacao(self, resultado):
        """Escreve o cartão "Avaliação do OCR" com os números do resultado."""
        confianca = f"{resultado['confianca_media']:.1f}".replace(".", ",")
        self.painel.definir_resumo(
            f"Letras e números extraídos: {resultado['caracteres_total']}\n\n"
            f"Em palavras com confiança ≥ {CONFIANCA_MINIMA}: "
            f"{resultado['caracteres_confiaveis']}\n\n"
            f"Confiança média das palavras: {confianca} / 100"
        )

    # ------------------------------------------------------------------------ salvar

    def salvar_texto(self):
        """Salva o texto da caixa (com as correções do usuário) em um .txt."""
        texto = self.area.texto_atual()
        if not texto.strip():
            messagebox.showwarning("Aviso", "Não há texto para salvar.")
            return

        caminho = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Arquivo de texto", "*.txt")],
            initialfile="texto_extraido.txt",
        )
        if not caminho:
            return

        try:
            with open(caminho, "w", encoding="utf-8") as arquivo:
                arquivo.write(texto)
        except OSError as erro:
            messagebox.showerror("Erro", f"Não foi possível salvar o texto:\n{erro}")
            return

        messagebox.showinfo("Pronto", f"Texto salvo em:\n{caminho}")

    def salvar(self, versao="cor"):
        """Salva o documento digitalizado em PNG ("cor" ou "pb")."""
        imagem = self.estado.digitalizada if versao == "cor" else self.estado.digitalizada_pb
        if imagem is None:
            messagebox.showwarning("Aviso", "Processe a imagem antes de salvar.")
            return

        caminho = filedialog.asksaveasfilename(
            defaultextension=".png",
            filetypes=[("PNG", "*.png")],
            initialfile=f"scan_{'colorido' if versao == 'cor' else 'pb'}.png",
        )
        if not caminho:
            return

        Image.fromarray(para_rgb(imagem)).save(caminho, "PNG")
        messagebox.showinfo("Pronto", f"Salvo em:\n{caminho}")

    def salvar_pdf(self):
        """Salva o documento digitalizado como PDF pesquisável.

        O PDF tem a imagem colorida do documento e, por cima, o texto do OCR
        invisível (dá para buscar e copiar). Gerar o PDF roda o Tesseract de
        novo (alguns segundos), então o trabalho vai para uma thread separada
        e a janela não trava.
        """
        imagem = self.estado.digitalizada
        if imagem is None:
            messagebox.showwarning("Aviso", "Processe a imagem antes de salvar.")
            return
        if self._gerando_pdf:
            return

        nome_base = os.path.splitext(self.estado.nome_arquivo or "documento")[0]
        caminho = filedialog.asksaveasfilename(
            defaultextension=".pdf",
            filetypes=[("PDF", "*.pdf")],
            initialfile=f"scan_{nome_base}.pdf",
        )
        if not caminho:
            return

        fila = Queue()

        def trabalho():
            """Executado na thread separada: gera e grava o PDF."""
            try:
                dados = gerar_pdf_pesquisavel(imagem.copy())
                with open(caminho, "wb") as arquivo:
                    arquivo.write(dados)
                fila.put(("ok", caminho))
            except Exception as erro:
                fila.put(("erro", str(erro)))

        def acompanhar():
            """Na thread da janela: espera a thread terminar e avisa o usuário."""
            try:
                tipo, dados = fila.get_nowait()
            except Empty:
                self.raiz.after(100, acompanhar)
                return
            self._gerando_pdf = False
            self.painel.encerrar_progresso()
            self._atualizar_botoes()
            if tipo == "ok":
                self.painel.definir_status("PDF pesquisável salvo.")
                messagebox.showinfo("Pronto", f"PDF pesquisável salvo em:\n{dados}")
            else:
                self.painel.definir_status("Falha ao gerar o PDF.")
                messagebox.showerror("Erro", f"Não foi possível gerar o PDF:\n{dados}")

        self._gerando_pdf = True
        self.painel.definir_status("Gerando PDF pesquisável…")
        self.painel.iniciar_progresso()
        self._atualizar_botoes()
        Thread(target=trabalho, daemon=True).start()
        self.raiz.after(100, acompanhar)
