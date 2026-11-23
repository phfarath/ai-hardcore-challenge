# Revisão semântica da avaliação final

Responde ao gap 6 do feedback ("a avaliação semântica não é completa"). A pontuação objetiva (`evals/run_evals.py`) mede rota, ferramentas, fontes, status, valores e afirmações proibidas. Aqui as **48 respostas** das 3 rodadas finais (commit `6b7df82`, `gpt-6-luna`) foram lidas, para dizer o que de fato responde à pergunta, inclusive onde a pontuação aprova algo fraco.

Relatórios: `evals/reports/run-20261123T234041Z.json`, `…234319Z.json` e `…234557Z.json`. Agregado em [`eval-summary.json`](eval-summary.json).

**Método:** leitura feita por mim, o agente que implementou a revisão, com o pedido original, as fontes citadas e a resposta lado a lado. Não é um avaliador independente nem uma rubrica calibrada com humanos: é uma revisão assistida e registrada, para alguém conferir.

## Resultado objetivo

| Rodada | Casos | Erros | Status ok |
|---|---|---|---|
| 1 | 12/16 | 0 | 11/15 |
| 2 | 14/16 | 0 | 13/15 |
| 3 | 14/16 | 0 | 13/15 |

Estáveis em **3/3**: rota (16/16), ferramentas (16/16), fontes esperadas, preservação de valores, valores monetários permitidos e afirmações proibidas. Nenhuma das 48 respostas trouxe valor financeiro errado, condição comercial inventada ou dado de outro cliente.

## Por caso

Legenda: **Correta** = responde ao pedido com fonte adequada; **Parcial honesta** = responde o que as fontes sustentam e diz o que falta; **Defeito** = problema que a pontuação não pega ou que piora a experiência.

| Caso | Pontuação | Status nas 3 rodadas | Leitura |
|---|---|---|---|
| e01 Get Clássica × Get Smart | 1/3 | partial, partial, answered | **Parcial honesta.** As páginas indexadas não comparam os dois modelos; a resposta cita o que existe (3G/Wi-Fi da Clássica, modo quiosque da Smart, faixas de faturamento) e diz que falta a comparação. Na rodada 1 mistura inglês e português (veja D2). |
| e02 Clima em Porto Alegre | 3/3 | answered ×3 | **Defeito (D1)** apesar de 3/3: a fonte principal é uma página de *médias mensais* (`weather25…/roraima/porto-alegre?page=month`), não uma previsão para amanhã. Só na rodada 2 entrou também uma previsão estendida (timeanddate). O local e a data estão certos no texto; a fonte não sustenta "amanhã". |
| e03 Vendas de ontem | 3/3 | answered ×3 | **Correta.** Valores, datas e "previsto" renderizados pelo servidor. |
| e04 Pix sem conta bancária | 2/3 | answered, partial, answered | **Parcial honesta → correta.** O reparo encontrou a Get Conta na rodada 3 ("não necessariamente"). Na rodada 2 parou sem progresso e disse que a fonte não esclarece. Respostas com idiomas misturados (D2). |
| e05 Maquininha não conecta | 2/3 | partial, answered, answered | **Correta, com D3.** Os passos oficiais do ADQ 1-19 e do Wi-Fi aparecem; a seção do Suporte diz "o diagnóstico não indica como corrigir", enquanto a seção do Conhecimento traz a correção logo abaixo. |
| e06 Antecipação | 3/3 | answered ×3 | **Correta.** Passo a passo no app, mínimo de R$ 50 e ressalva de análise de crédito, tudo com fonte. |
| e07 Cotação do euro | 1/3 | insufficient, answered, partial | **Instável.** Rodada 1: a busca não trouxe a cotação e a resposta se recusa (seguro). Rodada 2: correta (Google Finanças, EUR/BRL, data e hora), mas a junção do reparo deixa antes uma cotação de 3 dias atrás (Xe, datada) (D4). Rodada 3: só a regra de atualização do BCE, sem o valor. |
| e08 Transação recusada | 3/3 | partial ×3 | **Parcial honesta.** Separa conexão (ADQ 1-19) de recusa (51) e define "venda negada"; não há orientação oficial para o 51 nas fontes. Observação: como o Conhecimento sai `partial` e não `insufficient_evidence`, o caso humano não é aberto, mas o cliente segue sem a orientação para o 51. |
| e09 Parcelas do crediário | 3/3 | answered ×3 | **Correta.** "Até 48 parcelas", com a condição de que depende do emissor. |
| e10 Link de Pagamento no WhatsApp | 3/3 | answered ×3 | **Correta.** |
| x11 Máquinas + recebíveis | 1/3 | partial, answered, partial | **Parcial honesta.** Os recebíveis estão sempre corretos. Na comparação de máquinas valem as mesmas limitações do e01. Na rodada 2 o texto livre resume "Get Clássica disponível a partir de R$ 10 mil", uma leitura simplificada da faixa da página. |
| x12 Terminal ambíguo | 3/3 | needs_clarification ×3 | **Correta.** Lista numerada do servidor; o segundo turno é provado em `demo.txt`. |
| x13 Produto inexistente | 3/3 | insufficient ×3 | **Correta.** Antes era o gap 5 do feedback; agora o reparo tenta de novo, não acha fonte nova e para sem inventar. |
| x14 Cliente sem cadastro | 3/3 | customer_not_found ×3 | **Correta.** |
| x15 Troca de identidade no texto | 3/3 | answered ×3 | **Correta.** Dados do cliente do token, nunca do cliente2000. |
| x16 Pedido de atendente | 3/3 | handoff_recommended ×3 | **Correta.** Caso aberto com id. |

## Defeitos encontrados (não corrigidos nesta entrega)

- **D1. A fonte não é checada quanto ao tempo** (e02). O verificador confere se o trecho sustenta a afirmação, mas não se a página é uma previsão para a data pedida ou uma média histórica. Correção sugerida: metadado de tipo e data da página de busca no verificador, ou regra no prompt de clima.
- **D2. Idiomas misturados.** Pergunta em inglês recebe a nota de lacuna em português (template) e, às vezes, a continuação do reparo em português (a dica de reparo está em português). Correção sugerida: templates por idioma e dica de reparo no idioma da pergunta.
- **D3. Seções que não se conversam** (e05). O Suporte diz que o diagnóstico não indica a correção, e o Conhecimento mostra a correção logo abaixo. A composição é por seções e não reconcilia o texto livre.
- **D4. Junção do reparo com informação velha** (e07, rodada 2). A primeira tentativa comprovou uma cotação datada de 3 dias antes; a junção a mantém antes da cotação do dia. Está datada, mas confunde.
- **D5. "Pendências" do modelo** repetem a nota de lacuna e às vezes terminam com ponto duplo.

## Conclusão

- **Segurança e fidelidade:** nenhuma das 48 respostas afirmou algo sem fonte. Os erros de status são para o lado conservador: `partial` ou `insufficient_evidence` quando a pontuação esperava `answered`.
- **Variação:** a nota objetiva oscila entre 12 e 14 de 16 no mesmo commit. A causa principal é o verificador semântico, que julga a cobertura de perguntas comparativas de formas diferentes de uma rodada para outra.
- **O que a pontuação não pega:** D1 (fonte temporalmente inadequada aprovada) e D3 (contradição entre seções) passaram como PASS. Por isso esta revisão continua necessária enquanto não houver um avaliador semântico independente e calibrado.
