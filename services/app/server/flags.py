"""Definição das flags de categorização — dirigida por dados.

Editar este arquivo muda a UI e a validação juntas; nenhum componente React
conhece nomes de flags. Adicionar opção é seguro a qualquer momento. REMOVER uma
opção não corrompe anotações antigas (a leitura preserva ids desconhecidos e só
avisa), mas invalida novas escritas com aquele id — então prefira adicionar.

Suba FLAG_GROUPS_VERSION quando mudar a semântica de um id existente, para que dê
para distinguir anotações feitas sob definições diferentes.
"""

from __future__ import annotations

FLAG_GROUPS_VERSION = 1

FLAG_GROUPS: list[dict] = [
    {
        "id": "dificuldade",
        "label": "Dificuldade",
        "multi": False,
        "required": True,
        "help": "Quão difícil é enxergar/detectar o boom neste intervalo.",
        "options": [
            {"id": "facil", "label": "Fácil"},
            {"id": "medio", "label": "Médio"},
            {"id": "dificil", "label": "Difícil"},
        ],
    },
    {
        "id": "qualidade_visual",
        "label": "Qualidade visual",
        "multi": True,
        "required": False,
        "help": "Condição da imagem no trecho.",
        "options": [
            {"id": "nitido", "label": "Nítido"},
            {"id": "motion_blur", "label": "Motion blur"},
            {"id": "desfocado", "label": "Desfocado"},
            {"id": "baixa_luz", "label": "Baixa luz"},
            {"id": "superexposto", "label": "Superexposto"},
        ],
    },
    {
        "id": "tipo_aparicao",
        "label": "Tipo de aparição",
        "multi": True,
        "required": False,
        "help": "Como o objeto se apresenta no quadro.",
        "options": [
            {"id": "direto", "label": "Direto"},
            {"id": "reflexo", "label": "Reflexo"},
            {"id": "parcialmente_ocluso", "label": "Parcialmente ocluso"},
            {"id": "cortado_pela_borda", "label": "Cortado pela borda"},
            {"id": "muito_pequeno_distante", "label": "Muito pequeno / distante"},
        ],
    },
    {
        "id": "camera",
        "label": "Câmera",
        "multi": True,
        "required": False,
        "help": "Comportamento da câmera durante o intervalo.",
        "options": [
            {"id": "estatica", "label": "Estática"},
            {"id": "em_movimento", "label": "Em movimento"},
        ],
    },
]

_BY_ID = {group["id"]: group for group in FLAG_GROUPS}


# --------------------------------------------------------------------------
# sugestão a partir do nome do arquivo
# --------------------------------------------------------------------------

# Os nomes de entrega já descrevem a categoria ("APAGAR REFLEXO DE BOOM NO
# ESPELHO"), então dá para pré-marcar e deixar só a confirmação.
#
# Superfícies refletoras contam mesmo sem a palavra "reflexo": "BOOM NA
# CRISTALEIRA" é um reflexo.
#
# Deliberadamente FORA da lista, por ambiguidade em jargão de cinema:
#   QUADRO — tanto quadro de parede quanto ENQUADRAMENTO ("boom no quadro
#            esquerdo", "direita de quadro" são posição na imagem). Dos 29
#            arquivos com a palavra, ~20 já trazem "REFLEXO" explícito, então
#            excluí-la quase não perde acerto e evita marcar errado o resto.
#   TELA   — "parte inferior da tela" é a imagem, não um monitor.
#   PORTA  — porta é porta; o que reflete é o VIDRO da porta.
#   ARMARIO— nem todo armário tem espelho.
_REFLECTIVE = (
    "REFLEXO", "ESPELHO", "VIDRO", "CRISTALEIRA", "PORTA-RETRATO", "PORTA RETRATO",
    "OCULOS", "ÓCULOS", "JANELA", "MICROONDAS", "VITRINE", "MONITOR",
    "GELADEIRA", "FORNO", "RETROVISOR",
)

# Sinais de aparição direta do equipamento no quadro.
_DIRECT = ("VAZA", "VAZAMENTO", "TETO", "CHAO", "CHÃO", "ENTRA BOOM", "BOOM ENTRANDO")

# "REFLETOR" é um refletor de luz, não um reflexo — e contém "REFLE". Sem esta
# exclusão, todo arquivo de refletor viraria falso positivo de reflexo.
_FALSE_FRIENDS = ("REFLETOR", "REFLETORES")


# As heurísticas acima são do acervo de BOOM: nasceram dos 416 nomes reais
# ("APAGAR REFLEXO DE BOOM NO ESPELHO") e das armadilhas daquele vocabulário.
# Aplicá-las a outro objeto pré-marcaria `tipo_aparicao` com base em evidência
# que não existe — e flag errada pré-marcada é pior que campo em branco. Por isso
# o conjunto de regras é escolhido POR OBJETO e o padrão de um objeto novo é
# nenhuma sugestão.
SUGGEST_RULES = ("boom",)


def suggest_from_name(name: str, rules: str | None = None) -> dict:
    """Flags sugeridas a partir do nome do arquivo.

    Conservador de propósito: só sugere quando há evidência no nome. Uma flag
    errada e pré-marcada que passa despercebida contamina o dataset — bem pior
    que um campo em branco que a pessoa preenche.

    `rules=None` (objeto sem heurística cadastrada) devolve {}.
    """
    if rules != "boom":
        return {}

    upper = name.upper()
    for term in _FALSE_FRIENDS:
        upper = upper.replace(term, "")

    if any(term in upper for term in _REFLECTIVE):
        return {"tipo_aparicao": ["reflexo"]}
    if any(term in upper for term in _DIRECT):
        return {"tipo_aparicao": ["direto"]}
    return {}


def group_ids() -> list[str]:
    return [group["id"] for group in FLAG_GROUPS]


def required_group_ids() -> list[str]:
    return [group["id"] for group in FLAG_GROUPS if group["required"]]


def validate(flags: dict) -> list[str]:
    """Valida um dict de flags de intervalo. Retorna lista de erros legíveis."""
    errors: list[str] = []

    for key, value in flags.items():
        group = _BY_ID.get(key)
        if group is None:
            errors.append(f"grupo de flag desconhecido: {key!r}")
            continue

        valid = {option["id"] for option in group["options"]}

        if group["multi"]:
            if not isinstance(value, list):
                errors.append(f"{key}: esperava lista, veio {type(value).__name__}")
                continue
            unknown = [v for v in value if v not in valid]
            if unknown:
                errors.append(f"{key}: opções desconhecidas {unknown}")
        else:
            if value is None or value == "":
                continue
            if not isinstance(value, str):
                errors.append(f"{key}: esperava string, veio {type(value).__name__}")
            elif value not in valid:
                errors.append(f"{key}: opção desconhecida {value!r}")

    for group in FLAG_GROUPS:
        if not group["required"]:
            continue
        value = flags.get(group["id"])
        if not value:
            errors.append(f"{group['label']} é obrigatório")

    return errors


def normalize(flags: dict | None) -> dict:
    """Preenche grupos ausentes com o vazio do tipo certo e descarta ids inválidos
    dentro de grupos conhecidos. Ids de GRUPO desconhecidos são preservados, para
    que editar este arquivo nunca apague histórico."""
    flags = dict(flags or {})
    result: dict = {}

    for group in FLAG_GROUPS:
        gid = group["id"]
        valid = {option["id"] for option in group["options"]}
        value = flags.pop(gid, None)
        if group["multi"]:
            items = value if isinstance(value, list) else []
            result[gid] = [v for v in items if v in valid]
        else:
            result[gid] = value if isinstance(value, str) and value in valid else None

    result.update(flags)  # ids desconhecidos sobrevivem
    return result
