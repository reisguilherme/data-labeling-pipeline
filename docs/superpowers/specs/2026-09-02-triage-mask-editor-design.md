# Fluxo de triagem e editor de máscaras

## Objetivo

Fazer a triagem avançar imediatamente para o próximo vídeo, entregar com segurança o trabalho salvo ao exportador/SAM3 e tornar a correção de máscaras eficiente como o fluxo de pincel do CVAT.

## Diagnóstico

O worker CPU mantém `ObjectContext` e `AnnotationStore` em memória entre jobs. Como o processo da API grava `annotations.json` separadamente, o worker pode iniciar uma exportação com um snapshot anterior e gerar `anotacao ou intervalos ausentes`, embora a API tenha acabado de salvar o intervalo. Além disso, o frontend espera toda a exportação terminar antes de navegar.

## Fluxo aprovado

- A API salva intervalos antes de criar o job durável de exportação.
- O worker CPU abre um contexto novo a cada job e relê o JSON persistido.
- O frontend inicia a exportação, acompanha sua finalização em segundo plano e navega imediatamente ao próximo pendente.
- A finalização continua sendo autoritativa no backend e enfileira o vídeo no SAM3.
- Marcar "Sem objeto" persiste, atualiza a biblioteca e navega ao próximo; falhas continuam visíveis.

## Editor de máscaras

O editor terá barra fixa com mover, pincel e borracha; cursor com diâmetro real; tamanho ajustável; pincel circular ou quadrado; opacidade/ocultação; zoom na roda centrado no cursor; pan com Espaço; ajuste à tela; seleção/visibilidade de instâncias; desfazer/refazer e atalhos. A imagem e a bbox derivada continuam sempre visíveis. A máscara binária e as revisões imutáveis permanecem canônicas.

## Compatibilidade

O status interno `no_boom` não muda para preservar dados legados. Apenas o texto da interface e dos atalhos passa a ser "Sem objeto". Nenhum artefato antigo é apagado ou convertido.

## Aceitação

- Um vídeo recém-salvo é exportado pelo worker mesmo que ele tenha processado jobs anteriores.
- O clique de salvar/enviar navega antes do FFmpeg terminar.
- A finalização leva o vídeo ao SAM3.
- "Sem objeto" remove o vídeo da triagem pendente e abre o próximo.
- O editor oferece os controles e atalhos descritos sem alterar o PNG/revisionamento do backend.
