"""Constantes que controlam a detecção da folha e a câmera.

Ficam todas aqui para que seja fácil ajustar o comportamento do programa sem
procurar números espalhados pelo código. Cada constante explica o efeito de
aumentar ou diminuir o seu valor.
"""

# Lado maior (em pixels) usado na detecção da folha. Reduzir a imagem deixa a
# detecção rápida e ignora detalhes pequenos (textura da mesa, letras). O
# recorte final é feito na imagem original, sem perder qualidade.
LADO_DETECCAO = 500

# Índice da câmera padrão. Se houver mais de uma câmera, teste 1, 2 etc.
INDICE_CAMERA = 0

# --- Prévia da câmera ao vivo -------------------------------------------------
# A prévia só mostra a imagem (nenhum processamento roda ao vivo). O
# processamento completo acontece quando o usuário clica em Capturar quadro.

# Intervalo (ms) entre atualizações da imagem na tela. 33 ms = no máximo ~30
# quadros por segundo. Aumente (ex.: 50 ou 66) se a máquina for lenta.
INTERVALO_EXIBICAO_MS = 33

# Giro (graus, sentido horário) aplicado à imagem da câmera: 0, 90, 180 ou 270.
# 90 deixa a imagem na VERTICAL (em pé), melhor para enquadrar uma folha. Se a
# imagem aparecer de cabeça para baixo, use 270. Durante a câmera ao vivo, os
# botões de "Girar imagem" também ajustam esse giro.
GIRO_CAMERA_GRAUS = 90

# O documento precisa ocupar pelo menos 15% da foto. Isso evita escolher um
# retângulo pequeno, como a foto 3x4 dentro de uma identidade.
AREA_MINIMA_RELATIVA = 0.15

# Pelo menos metade do contorno precisa estar sobre bordas reais da imagem.
# Aumentar deixa a detecção mais exigente (menos falsos positivos, mais
# fotos em que nada é encontrado); diminuir faz o contrário.
APOIO_MINIMO = 0.5

# Cada lado do contorno (fora da margem da foto) precisa ter pelo menos esta
# fração apoiada em bordas reais. Evita aceitar um lado "inventado", por
# exemplo uma diagonal cortando a folha, mesmo que os outros três lados sejam
# fortes e puxem a média para cima.
LADO_MINIMO = 0.2
