"""Câmera ao vivo: prévia com a detecção da folha em tempo real.

Ao vivo roda SÓ a detecção das bordas da folha (as passadas rápidas, ~20 ms
por quadro), para desenhar o contorno e ajudar a enquadrar. Nenhuma outra
etapa (cinza, threshold, perspectiva, efeito de scanner, OCR) roda ao vivo:

    câmera ligada   uma tela única mostra a imagem ao vivo, girada para ficar
                    na vertical (ver GIRO_CAMERA_GRAUS em nucleo/configuracao.py),
                    com o contorno da folha quando ela é encontrada;
    Capturar quadro congela a imagem atual, desliga a câmera e só então roda
                    o processamento completo (detecção, perspectiva, OCR).

Por que há threads
------------------
A leitura da câmera (`camera.read()`) pode demorar (pouca luz, câmera que
demora a "esquentar") e a detecção leva alguns milissegundos. Se rodassem na
thread da janela, a interface travaria, e o OpenCV entregaria quadros velhos do
buffer. Então:

    thread de LEITURA   só lê a câmera, sem parar, guardando o quadro MAIS RECENTE;
    thread de DETECÇÃO  pega o quadro mais recente e procura os 4 cantos da
                        folha, no máximo ~10 vezes por segundo;
    thread da JANELA    a cada ~33 ms mostra o quadro mais recente com o
                        contorno da última detecção. Só ela mexe em widgets
                        (regra do Tkinter).
"""

import tkinter as tk
from threading import Event, Lock, Thread

import cv2

from nucleo.configuracao import (DETECCOES_PERDIDAS_TOLERADAS, GIRO_CAMERA_GRAUS,
                                 INDICE_CAMERA, INTERVALO_DETECCAO_S, INTERVALO_EXIBICAO_MS)
from nucleo.deteccao import detectar_documento

# Giro em graus (sentido horário) -> código do cv2.rotate (0 = sem giro).
CODIGOS_GIRO = {
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}


class ControladorCamera:
    """Abre a câmera e entrega os quadros (com os cantos da folha) à interface.

    O controlador não mexe em widgets. Funções recebidas na criação (sempre
    chamadas na thread da janela):
        ao_quadro(quadro, cantos)
            quando há um quadro novo para mostrar. `cantos` são os 4 cantos da
            folha na última detecção, ou None se nenhuma folha foi encontrada.
        ao_falha(titulo, mensagem)
            a câmera parou de funcionar DURANTE o uso. Já foi encerrada quando
            esta função é chamada. Falhas ao ABRIR a câmera são devolvidas por
            `abrir`.
    """

    def __init__(self, raiz, ao_quadro, ao_falha):
        """`raiz` é a janela do Tkinter (usada para agendar a atualização da tela)."""
        self._raiz = raiz
        self._ao_quadro = ao_quadro
        self._ao_falha = ao_falha

        self._camera = None          # cv2.VideoCapture (None = desligada)
        self._agendamento = None     # id da próxima atualização de tela em raiz.after
        self._parar = Event()        # pedido de encerramento das threads
        self._threads = []

        # Dados compartilhados entre as threads (sempre acessados com _trava).
        self._trava = Lock()
        self._quadro = None          # quadro mais recente lido da câmera
        self._numero_quadro = 0      # conta quadros lidos (para saber se há novidade)
        self._novo_quadro = Event()  # avisa a thread de detecção que há quadro novo
        self._deteccao = None        # (cantos, forma do quadro) ou None
        self._falha = None           # (titulo, mensagem) se algo falhou numa thread

        self._ultimo_quadro_exibido = 0

        # Giro aplicado a cada quadro (graus, sentido horário). Fica guardado
        # entre uma abertura e outra da câmera.
        self.giro_graus = GIRO_CAMERA_GRAUS % 360

    def girar(self, graus):
        """Soma `graus` (90, 180 ou 270, sentido horário) ao giro da imagem da câmera."""
        self.giro_graus = (self.giro_graus + graus) % 360
        with self._trava:
            self._deteccao = None  # o contorno antigo não vale na nova orientação

    @property
    def ativa(self):
        """True se a câmera está ligada."""
        return self._camera is not None

    @property
    def quadro_atual(self):
        """Cópia do quadro mais recente (para capturar), ou None se ainda não chegou."""
        with self._trava:
            return None if self._quadro is None else self._quadro.copy()

    # --------------------------------------------------------------- ligar/desligar

    def abrir(self):
        """Tenta abrir a câmera (sem iniciar a prévia).

        Retorna None se conseguiu, ou o texto do erro (para a interface exibir)
        se a câmera não pôde ser acessada.
        """
        camera = cv2.VideoCapture(INDICE_CAMERA)
        if not camera.isOpened():
            camera.release()
            return ("Não foi possível acessar a câmera. Verifique se ela está conectada, "
                    "se as permissões estão habilitadas e se outro programa não está "
                    "usando-a.")

        camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        self._camera = camera
        return None

    def iniciar_previa(self):
        """Inicia as threads de leitura e de detecção e o ciclo de atualização da tela."""
        with self._trava:
            self._quadro = None
            self._numero_quadro = 0
            self._deteccao = None
            self._falha = None
        self._novo_quadro.clear()
        self._parar.clear()
        self._ultimo_quadro_exibido = 0

        self._threads = [
            Thread(target=self._ler_continuamente, args=(self._camera,), daemon=True),
            Thread(target=self._detectar_continuamente, daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        self._atualizar_tela()

    def parar(self):
        """Encerra as threads e libera a câmera. Pode ser chamado várias vezes."""
        if self._agendamento is not None:
            try:
                self._raiz.after_cancel(self._agendamento)
            except tk.TclError:  # id já expirado ou janela fechando
                pass
            self._agendamento = None

        self._parar.set()
        self._novo_quadro.set()  # acorda a thread de detecção para ela perceber o pedido
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads = []

        self._camera = None  # a thread de leitura já liberou o dispositivo ao terminar
        with self._trava:
            self._quadro = None
            self._deteccao = None

    # ----------------------------------------------------- thread de leitura (câmera)

    def _ler_continuamente(self, camera):
        """Lê a câmera sem parar e guarda só o quadro mais recente.

        Ler o tempo todo esvazia o buffer do driver, então o quadro guardado é
        sempre "de agora" (sem atraso). Esta thread é a dona da câmera e a
        libera ao terminar.
        """
        try:
            while not self._parar.is_set():
                sucesso, quadro = camera.read()
                if not sucesso or quadro is None:
                    self._registrar_falha("Falha na câmera",
                                          "A câmera foi aberta, mas não foi possível "
                                          "receber a imagem.")
                    return
                # Gira já na leitura: a prévia e o quadro capturado saem iguais.
                codigo = CODIGOS_GIRO.get(self.giro_graus)
                if codigo is not None:
                    quadro = cv2.rotate(quadro, codigo)
                with self._trava:
                    self._quadro = quadro
                    self._numero_quadro += 1
                self._novo_quadro.set()
        finally:
            camera.release()

    # ------------------------------------------------- thread de detecção (bordas)

    def _detectar_continuamente(self):
        """Procura a folha no quadro mais recente, no máximo ~10 vezes por segundo.

        Só as passadas rápidas da detecção rodam (baixo_contraste=False). Se a
        folha "some" por poucas detecções seguidas (reflexo, mão passando), o
        último contorno é mantido, para ele não ficar piscando na tela.
        """
        ultimo_analisado = 0
        perdidas = 0
        while not self._parar.is_set():
            # Dorme até haver um quadro novo (acorda sozinha de vez em quando).
            self._novo_quadro.wait(timeout=0.5)
            self._novo_quadro.clear()
            if self._parar.is_set():
                return
            with self._trava:
                quadro, numero = self._quadro, self._numero_quadro
            if quadro is None or numero == ultimo_analisado:
                continue
            ultimo_analisado = numero

            try:
                cantos, encontrou = detectar_documento(quadro, baixo_contraste=False)
            except Exception as erro:
                self._registrar_falha("Erro na detecção ao vivo",
                                      f"Não foi possível detectar a folha:\n{erro}")
                return

            with self._trava:
                if encontrou:
                    perdidas = 0
                    self._deteccao = (cantos, quadro.shape)
                else:
                    perdidas += 1
                    if perdidas > DETECCOES_PERDIDAS_TOLERADAS:
                        self._deteccao = None

            # Limita a taxa de detecções: poupa processador sem prejudicar a tela.
            self._parar.wait(INTERVALO_DETECCAO_S)

    def _registrar_falha(self, titulo, mensagem):
        """Uma thread de trabalho avisa a thread da janela de que algo falhou."""
        with self._trava:
            if self._falha is None:
                self._falha = (titulo, mensagem)

    # ------------------------------------------------- ciclo da thread da janela (tela)

    def _atualizar_tela(self):
        """Mostra o quadro mais recente com o contorno (roda na thread da janela)."""
        self._agendamento = None
        if self._camera is None:
            return

        try:
            with self._trava:
                falha = self._falha
                quadro, numero = self._quadro, self._numero_quadro
                deteccao = self._deteccao

            if falha is not None:
                self.parar()
                self._ao_falha(*falha)
                return

            # Quadro novo: troca a imagem da tela. Sem novidade, não gasta nada.
            if quadro is not None and numero != self._ultimo_quadro_exibido:
                self._ultimo_quadro_exibido = numero
                # Só usa os cantos se vieram de um quadro do mesmo tamanho
                # (depois de girar a câmera, a forma muda).
                cantos = None
                if deteccao is not None and deteccao[1] == quadro.shape:
                    cantos = deteccao[0]
                self._ao_quadro(quadro, cantos)
        except Exception as erro:
            # Qualquer erro na prévia encerra a câmera COM aviso (em vez de a
            # imagem congelar em silêncio).
            self.parar()
            self._ao_falha("Erro na prévia da câmera",
                           f"Não foi possível atualizar a imagem da câmera:\n{erro}")
            return

        # O intervalo é contado após o trabalho, então atualizações não se acumulam.
        self._agendamento = self._raiz.after(INTERVALO_EXIBICAO_MS, self._atualizar_tela)
