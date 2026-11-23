"""Câmera ao vivo: só mostra a imagem para o usuário enquadrar e capturar.

A prévia NÃO processa nada. Antes, cada quadro passava pela detecção e por
todas as etapas (~8 vezes por segundo), o que gastava processador à toa: o
resultado só interessa no quadro que o usuário escolhe. Agora o fluxo é:

    câmera ligada   uma tela única mostra a imagem ao vivo;
    Capturar quadro congela a imagem atual, desliga a câmera e só então roda
                    o processamento completo (detecção, perspectiva, OCR).

Por que ainda há uma thread
---------------------------
A leitura da câmera (`camera.read()`) pode demorar (pouca luz, câmera que
demora a "esquentar"). Se rodasse na thread da janela, a interface travaria, e
o OpenCV entregaria quadros velhos do buffer. Então:

    thread de LEITURA  só lê a câmera, sem parar, guardando o quadro MAIS RECENTE;
    thread da JANELA   a cada ~33 ms mostra esse quadro. Só ela mexe em widgets
                       (regra do Tkinter).
"""

import tkinter as tk
from threading import Event, Lock, Thread

import cv2

from nucleo.configuracao import INDICE_CAMERA, INTERVALO_EXIBICAO_MS


class ControladorCamera:
    """Abre a câmera e entrega os quadros à interface.

    O controlador não mexe em widgets. Funções recebidas na criação (sempre
    chamadas na thread da janela):
        ao_quadro(quadro)
            quando há um quadro novo para mostrar.
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
        self._parar = Event()        # pedido de encerramento da thread de leitura
        self._thread = None

        # Dados compartilhados entre as threads (sempre acessados com _trava).
        self._trava = Lock()
        self._quadro = None          # quadro mais recente lido da câmera
        self._numero_quadro = 0      # conta quadros lidos (para saber se há novidade)
        self._falha = None           # (titulo, mensagem) se a leitura falhou

        self._ultimo_quadro_exibido = 0

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
        """Inicia a thread de leitura e o ciclo de atualização da tela."""
        with self._trava:
            self._quadro = None
            self._numero_quadro = 0
            self._falha = None
        self._parar.clear()
        self._ultimo_quadro_exibido = 0

        self._thread = Thread(target=self._ler_continuamente, args=(self._camera,),
                              daemon=True)
        self._thread.start()
        self._atualizar_tela()

    def parar(self):
        """Encerra a leitura e libera a câmera. Pode ser chamado várias vezes."""
        if self._agendamento is not None:
            try:
                self._raiz.after_cancel(self._agendamento)
            except tk.TclError:  # id já expirado ou janela fechando
                pass
            self._agendamento = None

        self._parar.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

        self._camera = None  # a thread de leitura já liberou o dispositivo ao terminar
        with self._trava:
            self._quadro = None

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
                    with self._trava:
                        self._falha = ("Falha na câmera",
                                       "A câmera foi aberta, mas não foi possível "
                                       "receber a imagem.")
                    return
                with self._trava:
                    self._quadro = quadro
                    self._numero_quadro += 1
        finally:
            camera.release()

    # ------------------------------------------------- ciclo da thread da janela (tela)

    def _atualizar_tela(self):
        """Mostra o quadro mais recente (roda na thread da janela)."""
        self._agendamento = None
        if self._camera is None:
            return

        try:
            with self._trava:
                falha = self._falha
                quadro, numero = self._quadro, self._numero_quadro

            if falha is not None:
                self.parar()
                self._ao_falha(*falha)
                return

            # Quadro novo: troca a imagem da tela. Sem novidade, não gasta nada.
            if quadro is not None and numero != self._ultimo_quadro_exibido:
                self._ultimo_quadro_exibido = numero
                self._ao_quadro(quadro)
        except Exception as erro:
            # Qualquer erro na prévia encerra a câmera COM aviso (em vez de a
            # imagem congelar em silêncio).
            self.parar()
            self._ao_falha("Erro na prévia da câmera",
                           f"Não foi possível atualizar a imagem da câmera:\n{erro}")
            return

        # O intervalo é contado após o trabalho, então atualizações não se acumulam.
        self._agendamento = self._raiz.after(INTERVALO_EXIBICAO_MS, self._atualizar_tela)
