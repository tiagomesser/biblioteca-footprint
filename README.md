# biblioteca-footprint

Biblioteca pessoal KiCad (formato KiCad 10) de símbolos, footprints e modelos 3D, com marcação dos componentes validados em produção.

## Estrutura

```
symbols/TM_<Categoria>.kicad_sym
footprints/TM_<Categoria>.pretty/
3dmodels/TM_<Categoria>.3dshapes/
componentes.csv        índice e fonte de verdade do status
sym-lib-table          tabela de símbolos (modelo)
fp-lib-table           tabela de footprints (modelo)
```

Categorias: Analogicos, Conectores, Digitais, Drivers, Fontes, Magneticos, Mecanicos, Passivos, Potencia.

## Instalação

1. Clonar o repositório em uma pasta fixa.
2. KiCad, *Preferências > Configurar caminhos*: criar a variável `TM_LIB` apontando para essa pasta.
3. *Preferências > Gerenciar bibliotecas de símbolos* e *de footprints*: adicionar as entradas de `sym-lib-table` e `fp-lib-table` (ou copiá-las para as tabelas globais).

## Status dos componentes

Cada símbolo possui o campo oculto `Status`, com um dos valores:

| Status | Significado |
|---|---|
| `Em avaliação` | Ainda não utilizado em placa fabricada. |
| `Validado` | Montado e testado em protótipo, sem problemas de footprint. |
| `Produção` | Utilizado em placa de produção. |

O arquivo `componentes.csv` (separador `;`) registra símbolo, footprint, fabricante, status, projetos, observações (por exemplo, problemas de footprint), descrição e origem. Ao alterar o status, atualizar o campo `Status` do símbolo e a linha correspondente do CSV.

## Inclusão de novo componente

Adicionar o símbolo na categoria adequada, o footprint em `footprints/TM_<Categoria>.pretty`, o modelo 3D em `3dmodels/TM_<Categoria>.3dshapes` com caminho `${TM_LIB}/3dmodels/...`, preencher `Status = Em avaliação` e incluir a linha no CSV.
