"""Runner headless do SAM3 sobre o que a movies-screening-tool exporta.

O notebook continua existindo para os casos difíceis (reanotar um frame em que o
tracking se perdeu, inspecionar máscara a máscara). Este pacote é o caminho
automático: lê o `prompt.json` que a triagem já grava, propaga, e converte as
máscaras em rótulos YOLO — sem widget, sem CVAT, sem intervenção.
"""

from .config import VERSION

__version__ = VERSION
