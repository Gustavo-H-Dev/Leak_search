# Decisões do projeto

Registro das decisões que mudam a forma do sistema, com a alternativa que foi
descartada e o motivo. Ordem cronológica, mais recente no fim de cada fase.

## Fase 0 — Base visual e design system (27/11/2026)

Objetivo: trocar o frontend por um design system mobile-first, sem tocar em
regra de negócio e sem reescrever o que funciona.

### D1 — Jinja2 + Tailwind, CSS compilado versionado; HTMX adiado

- **Escolhido:** continuar em Jinja2 (o app já renderiza 16 telas assim) e
  adotar Tailwind como sistema de estilos.
- **Alternativas:** (A) SPA React/Next consumindo a API FastAPI; (B) manter o
  CSS atual e só reestilizar.
- **Motivo:** a SPA exigiria reescrever todas as telas e refazer o auth, que é
  sessão em cookie + CSRF por token no HTML — em SPA isso vira CORS, cookie
  cross-site e reimplementação de CSRF. Reestilizar sem utilitários manteria o
  CSS monolítico e a divergência de classes.
- **Compilação:** o Tailwind compila para `app/static/css/app.css`, que é
  **commitado**. A imagem `python:3.12-slim` não tem Node, então o deploy não
  depende de build de frontend. Depois de mudar template ou classe, rodar
  `npm run css` (ou `npm run css:watch` durante o desenvolvimento).
- **HTMX:** não entrou nesta fase porque nenhuma tela precisou de atualização
  parcial; o app já usa `fetch` pontual onde precisa. Entra na primeira fase
  que tiver interação parcial (por exemplo, revisão em lote).

### D2 — CSS sai de `base.html` e vira dois arquivos estáticos

Antes: 764 linhas de CSS dentro de `<style>` no `base.html`, nenhum
`StaticFiles`, nenhuma folha extra possível. Agora:

- `app/static/css/legado.css`: o CSS antigo, extraído **sem alteração**.
- `app/static/css/app.css`: saída compilada do design system.

As telas ainda não migradas continuam com a aparência atual; a migração é
página a página, sem big bang. `legado.css` desaparece quando a última tela
estiver no design system.

### D3 — Preflight do Tailwind desligado

`entrada.css` importa apenas `theme.css` e `utilities.css`. O preflight
(reset global) zeraria tipografia de listas e títulos das telas antigas no meio
da migração. A base do design system é aplicada pelos próprios componentes.

### D4 — Tokens

- **Cores:** mantida a paleta escura do produto (`#111014` de fundo,
  `#f078ad` de destaque). O texto secundário passou de `#aaa1ad` para
  `#b3aab6`, que passa em AA sobre a superfície `#19171d`.
- **Tipografia:** pilha do sistema. O CSS antigo declarava `Inter` mas nunca
  baixava a fonte — todo mundo já via a fonte do sistema. Carregar webfont
  externa adicionaria requisição de terceiro sem ganho garantido.
- **Alvo de toque:** botões e inputs com 44px de altura mínima; input com
  `font-size: 1rem` para o iOS não dar zoom ao focar.

### D5 — Navegação: cinco itens, barra inferior no celular

Mapeamento dos itens pedidos para as rotas que existem:

| Item | Rota | Observação |
| --- | --- | --- |
| Painel | `/` | |
| Revisão | `/valida` | |
| Remoções | `/valida?status=leaked` | **Não existe página própria de remoções.** Aponta para a fila de vazados até a fase de takedown. |
| Histórico | `/execucoes` | Só para admin: a rota é restrita. Cliente vê quatro itens. |
| Configurações | `/perfil-busca` | Perfil e conta. |

Busca na web, Usuários, Domínios e Análise ficam na seção secundária
"Operação" da lateral (desktop) e no menu "Mais" (celular). No celular a
navegação principal é uma barra fixa inferior com `aria-current="page"`; no
desktop é uma lateral fixa. O hambúrguer antigo foi removido.

### D6 — `/dev/componentes` desligada por padrão

A vitrine só responde quando `APP_ENV` é `dev`, `development` ou `local`, ou
quando `DEV_COMPONENTES=1`. Em produção nenhuma dessas variáveis existe, então
a rota devolve 404 — não depende de ninguém lembrar de desligar. Além disso
exige admin. O `compose.yaml` define `APP_ENV=development` para o ambiente
local.

### D7 — Middleware libera `/static/`

O `AutenticacaoMiddleware` redirecionava qualquer caminho não público para
`/login`, o que impediria o CSS de carregar na própria tela de login. Caminhos
sob `/static/` passam direto. Nenhuma outra regra de autenticação mudou.

### D8 — Componentes em macros Jinja + um JS pequeno

Os componentes são macros em `app/templates/componentes/` (botão, badge, cartão,
campo, tabela responsiva, lista, estado vazio, skeleton, modal, toast), porque
antes não havia nenhum `{% include %}` e o mesmo botão era copiado em cada tela.
O comportamento (modal com foco preso e Esc, toast) fica em
`app/static/js/ui.js`, sem dependência externa.

### D9 — Sem migração de banco

Esta fase não cria nem altera tabela. Nada de `ALTER TABLE`, nenhum dado
existente é tocado.

## Fase 1 — Onboarding e perfis monitorados

### D10 — Perfis, variações e páginas oficiais são a fonte única

Três tabelas novas, sempre lidas por `criadora_id` (que referencia
`users(id)`, o mesmo dono usado por `busca_execucao`/`arroba_descoberto`):

- `perfil_monitorado (criadora_id, plataforma, arroba, status)` — `status` é
  `ativo` ou `pausado`. O par (plataforma, arroba) é único por criadora.
- `nome_variante (criadora_id, texto, ativo, origem)` — `origem` guarda
  `gerada` (sugestão de `gerar_variantes_nome`) ou `manual`.
- `pagina_oficial (criadora_id, url, dominio, origem)` — `origem` `manual`,
  `bio` (links da bio do Instagram, materializados ao registrar o perfil) ou
  `valida` (quando a criadora marca um resultado como oficial).

`user_official_sites` foi descartada (`DROP TABLE IF EXISTS` no DDL): manter
duas listas do mesmo dado já tinha gerado divergência. Como o usuário autorizou
descartar os dados antigos, os links da bio e as marcações da Valida se
reconstroem sozinhos pelos fluxos existentes.

### D11 — Wizard em `/onboarding`, retomável por `?passo=`

O cadastro (quando a conta já fica ativa) redireciona para `/onboarding`. O
passo atual é derivado dos dados salvos, não de um estado no servidor:

1. concluído quando existe ao menos um `perfil_monitorado`;
2. concluído quando existe `nome_variante` ou quando a criadora já passou pela
   etapa (`onboarding_variantes_vistas`, em `app_settings`) — variações são
   opcionais, então passar por ela basta;
3. concluído com `pagina_oficial` ou com "Pular esta etapa"
   (`onboarding_oficiais_puladas`);
4. concluído no botão "Só concluir"/"Fazer primeira busca"
   (`onboarding_concluido`).

O wizard não é obrigatório: "/" continua acessível e a configuração pode ser
retomada em `/configuracoes/perfis`.

### D12 — Pontuação usa só variantes ativas

`fluxo_busca.dados_do_perfil` lê `perfil_monitorado` (só ativos) e
`nome_variante` (só ativas). Contas que ainda não têm nenhuma linha de variante
caem no comportamento antigo (`gerar_variantes_nome` a partir dos arrobas), para
que a busca delas não quebre durante a transição. Arroba pausado não entra na
busca, mas continua listado em Configurações.

### D13 — Limites em configuração

`PERFIS_MONITORADOS_LIMITE` (ambiente) → `app_settings.perfis_monitorados_limite`
→ padrão 5. Páginas oficiais: 20 por conta (`LIMITE_PAGINAS`, constante). Ao
atingir o limite, o POST volta ao passo 1 com a mensagem, sem 500. As páginas
oficiais são comparadas por URL normalizada (prefixo de caminho), então só a
própria página e o que está dentro dela é ignorado.

### D14 — Normalização da página oficial

`normalizar_pagina` usa `pontuacao.normalizar_url` (https, host minúsculo, sem
`www.`, sem parâmetros de rastreio) e ainda remove consulta e fragmento, porque
a mesma página oficial não deixa de ser oficial por causa de `?hl=pt`. O
domínio é guardado à parte para exibição e para a checagem de "mesmo site".

### D15 — Script de migração acompanha as tabelas

`scripts/migrar_sqlite_para_postgres.py` passou a copiar `perfil_monitorado`,
`nome_variante` e `pagina_oficial` e deixou de copiar `user_official_sites`.
O `excluir_usuario` também deixou de atualizar essa tabela removida.

### D16 — Primeira busca é manual e sem agendamento

O botão "Fazer primeira busca" chama `fluxo_busca.executar` uma vez, com o
provedor configurado, e leva de volta ao passo 4 com o resumo/mensagem. Sem
`SERPAPI_API_KEY`/`TINYFISH_API_KEY` ele explica o que falta em vez de quebrar.
Nada é agendado e nenhuma mídia é baixada: só metadados.

### Pendências conhecidas

- Telas ainda no CSS antigo: home, busca na web, Valida, análise, domínios,
  execuções, perfil de busca, usuários, catálogo e formulários de criadora.
  Migrar uma por fase e só então remover `legado.css`.
- "Remoções" precisa de página e fluxo próprios.
- Cliente não tem histórico por criadora; por isso o item é oculto para ele.

## Fase 2 — Verificação de titularidade (27/11/2026)

### D17 — Sem verificação de titularidade nesta fase

Decisão: não implementar verificação, nem por código na bio nem por documento
manual. O cadastro de perfil segue livre: qualquer conta pode salvar qualquer
arroba em `perfil_monitorado`, e o perfil entra na pontuação e nas buscas na
hora, sem estado intermediário de "pendente".

Descartadas: código temporário na bio (`verif-XXXXX`, 48 h, conferência pela
bio pública com `instagram.buscar_perfil`, status verificado, auditoria e
bloqueio de escaneamento/remoção enquanto pendente) e documento verificado
manualmente.

Consequências aceitas:

- Sem status pendente não há o que bloquear, então o critério de aceite da fase
  (teste provando o bloqueio em todas as rotas de escaneamento e remoção) não
  se aplica: a fase não altera rotas.
- Continua possível cadastrar o perfil de outra pessoa. Se isso virar problema,
  a fase volta com o desenho do código na bio — a leitura da bio já existe em
  `app/core/instagram.py` e o caminho de gravação em `fluxo_busca`.
- Os perfis já cadastrados não foram apagados; nada muda para quem já configurou
  e não há migração de banco nesta fase.
- As plataformas fora do Instagram nunca tiveram consulta de bio. Como não há
  verificação, todas continuam iguais, sem tratamento especial.

## Fase 3 — Escaneamento agendado (27/11/2026)

### D18 — Fila em worker separado com RQ + Redis

A fase pedia para escolher entre tarefa no processo, worker separado e cron no
container. Ficou o worker separado: `web` só enfileira, `worker` executa e
`scheduler` dispara os horários vencidos. O limite global de 30/min precisa ser
compartilhado entre processos — um contador em memória não serviria — e o Redis
que a fila exige é o mesmo lugar onde a janela deslizante vive. RQ em vez de
Celery: o job é uma função Python simples, sem broker extra além do Redis, sem
beat e sem configuração de tarefas.

### D19 — Reserva no banco em vez de trava distribuída

Dois cuidados substituem um lock de fila: o scheduler avança `proxima_em` com
`UPDATE ... WHERE proxima_em = <valor lido>` (só um chamador ganha a linha) e o
worker troca `na_fila` por `rodando` com outro `UPDATE` condicional. Reiniciar
o container, reentregar um job ou subir dois workers não duplica execução; o
pior caso é uma execução pulada, que aparece no histórico.

### D20 — Limite global no Redis, com janela deslizante

`TINYFISH_REQUISICOES_POR_MINUTO` (padrão 30) vale para todas as criadoras
juntas. A janela é deslizante (30 em qualquer intervalo de 60 s, não 30 por
minuto de relógio) e o script Lua no Redis faz remoção, contagem e inserção
atomicamente. Sem Redis, o limitador cai para uma janela em memória — correta
em um processo só — e a fila também não funciona, então não há execução
silenciosa fora do limite.

### D21 — Limite diário por criadora

`BUSCAS_POR_DIA_LIMITE` (ambiente) → `app_settings.buscas_por_dia_limite` →
padrão 4. Conta qualquer tentativa do dia local (Brasília), inclusive falha:
sem isso um erro repetido a cada clique consumiria créditos sem limite. O botão
Escanear agora e o scheduler passam pela mesma checagem.

### D22 — Estados e motivos de falha

`busca_execucao` ganhou `origem`, `agendamento_id`, `iniciada_em`, `duracao_ms`
e `motivo_falha`. O status interno `erro` é exibido como "Falhou" (o valor
antigo foi mantido para não reescrever histórico), e `avisos` cobre execução que
terminou com erro parcial ou Instagram indisponível. Motivos: `sem_creditos`
(402), `limite_provedor` (429), `instagram_bloqueado`, `configuracao`,
`interrompida` e `fila`. Nada de mensagem crua do provedor como único registro.

### D23 — Fuso e padrão

O horário escolhido é o de `America/Sao_Paulo`; o banco guarda UTC. O padrão é
semanal (segunda-feira, 09:00), como pedido. A próxima execução é recalculada a
partir do horário reservado, não do momento do enqueue, para não acumular atraso
quando o scheduler fica parado.

### D24 — Perfis que entram na busca

A fase pedia "só perfis verificados", mas a Fase 2 foi fechada sem verificação
(D17). Entram os perfis com status **ativo**; pausado não entra. Não existe
coluna de verificação e nada foi inventado para simular uma.

### D25 — Histórico e contagem para o painel

`/configuracoes/historico` lista só as execuções da própria criadora (data,
status, provedor, nº de buscas, novos, duração e erro). A contagem de novos da
última execução aparece no card de perfis e no perfil de busca. A tela admin
`/execucoes` continua sendo o histórico por consulta, sem mudança.

## Fase 4 — Painel e fila de revisão (27/11/2026)

### D26 — Mapeamento das ações para os status existentes

As três ações da fila usam (quase) os status que já existiam em
`search_candidates`:

| Ação na tela | Status | Efeito |
| --- | --- | --- |
| **É meu** | `leaked` | entra em `leak_reports` como "no ar"; soma `n_confirmados` do domínio |
| **Não é** | `not_me` | sai da fila; soma `n_falsos` do domínio |
| **Não sei** | `unsure` (novo) | sai da fila de novos sem confirmar; não mexe nos contadores |

`pending` continua sendo "novos", e `official` continua válido no banco (revisão
antiga, conta como falso positivo); ele não aparece mais como botão na fila.

O desfazer volta cada id ao status anterior, subtrai o feedback somado
(`ajustar_feedback_dominio`, sem deixar contador negativo) e apaga o relato de
vazamento que a própria ação criou — relatos que já existiam ficam.

### D27 — Um card por `chave_grupo`, paginação por grupo

A fila agrupa em Python (o mesmo álbum em sites diferentes vira um card com os
outros links dentro) e pagina **grupos**, não linhas: 10 cards por página. Por
isso a consulta traz até 2000 linhas filtradas e o corte acontece depois do
agrupamento; é o suficiente para o volume atual e evita página com card
quebrado no meio. A ordenação padrão é por nota; "mais recentes" usa
`ultima_vez_em`.

### D28 — Filtros e "abaixo do corte"

Filtros: provedor (`engine`), site (`dominio`), nota mínima, data (7/30/90 dias)
e ordem. A aba "Novos" aplica o limiar de revisão; `?abaixo=1` mostra só o que
ficou abaixo dele, preservando o comportamento antigo da Valida. O link
"abaixo do corte" aparece sempre que existir item escondido.

### D29 — Ações sem recarregar, com desfazer de 8 segundos

O formulário funciona sem JavaScript (POST + redirect). Com JavaScript, o POST
vai por `fetch` e o card sai da lista sem recarregar; uma barra de "Desfazer"
aparece por 8 segundos e, se usada, reverte status, contadores e relato. O botão
que submeteu vai no `FormData` explicitamente — `new FormData(form)` sozinho não
inclui o `status` do botão.

### D30 — Ignorar domínio depois de três "Não é"

Ao marcar "Não é", o servidor conta os `not_me` daquele domínio para a criadora.
A partir de 3, a resposta sugere ignorar o domínio; a tela mostra o aviso com
"Ignorar domínio" (grava em `dominio_ignorado`) e "Agora não". A decisão é
sempre da criadora; nada é ignorado sozinho.

### D31 — Link externo e conteúdo adulto

Nenhum título é link direto. Abrir o site exige abrir o `<details>` "Abrir site
(pode conter conteúdo adulto)", que explica que o destino é externo e não
verificado, e só então mostra o link com `target="_blank"` e
`rel="noopener noreferrer nofollow"`. Os links do mesmo álbum seguem a mesma
regra.

### D32 — Painel, remoções e privacidade

`/` para cliente virou painel (contadores, última/próxima varredura e botão
"Revisar N novos"); admin continua na home operacional. "No ar", "em remoção" e
"removidos" vêm de uma coluna nova `leak_reports.situacao` (padrão `no_ar`); o
card de "No ar" permite avançar a situação, sem página própria de remoções
ainda. Toda consulta da fila usa o id da sessão (admin pode escolher a
criadora); POST com id de outra conta responde 404, e há teste para os dois
caminhos.

### D33 — O que saiu da Valida

`/valida` virou redirect 307 para `/revisao` e o template antigo foi removido.
A ordenação por semelhança de imagem que existia só nessa tela não foi levada
para a fila nova: a comparação por pHash continua no cálculo de pontuação e na
tela de análise; a fila prioriza nota, data e agrupamento. O histórico por
consulta em `/execucoes` não mudou.

## Fase 5 — Kit de remoção e acompanhamento (27/11/2026)

### D34 — Contato é dado do admin, nunca inventado

`contato_abuso_dominio` (dominio, url_formulario, email, observacao) é
preenchida à mão em **Operação > Contatos**. O código não preenche nem sugere
e-mail ou URL de formulário. Sem contato cadastrado, a criadora vê "cadastre o
contato deste site" e o texto continua copiável; quando o admin cadastra, o
contato aparece na hora seguinte, sem migração.

### D35 — Modelos em arquivos editáveis, sem base legal nossa

Os textos ficam em `app/modelos_remocao/pt.txt` e `en.txt`, com marcadores
`{{nome}}`, `{{arroba}}`, `{{urls}}`, `{{descricao}}` e `{{data}}`. Dá para
editar o arquivo sem mexer em código. A interface marca os dois como
"Modelo a ser revisado por advogado" e o texto é genérico: pede a remoção,
oferece confirmar a titularidade e pede resposta por e-mail. Nenhum artigo,
lei ou prazo jurídico é citado pelo app — esses trechos são preenchidos pela
criadora ou pelo advogado dela.

### D36 — Status do pedido e reflexo no painel

`pedido_remocao.status` usa `a_enviar`, `enviado`, `aguardando`, `removido` e
`recusado`. Cada mudança grava uma linha em `pedido_remocao_evento` (status,
nota, data), que é o histórico mostrado na tela. O mesmo evento atualiza
`leak_reports.situacao` dos links do pedido: `a_enviar`/`enviado`/`aguardando`
→ `em_remocao`, `removido` → `removido`, `recusado` → `no_ar`. O texto gerado é
salvo como snapshot no pedido e pode ser editado antes ou depois de enviar.

### D37 — Um pedido, vários links do mesmo site

O pedido tem domínio único e uma lista de itens (`pedido_remocao_item`). Na
criação, a tela lista todos os links confirmados daquele domínio e deixa
marcar mais de um; só os marcados entram no texto e no pedido. Links de outra
criadora nem aparecem: a consulta é sempre pelo id da sessão (admin escolhe a
criadora) e um id de outra conta responde 404, com teste.

### D38 — Checagem depois de N dias, sem abrir a página

Enviar um pedido agenda `proxima_checagem_em` para hoje + N dias. O N vem de
`REMOCAO_DIAS_CHECAGEM`, depois de `app_settings.remocao_dias_checagem`
(editável em **Contatos**) e, por fim, 7 dias. "Checar agora" roda uma busca
pelo link no provedor do vazamento (uma consulta por link, no máximo 5),
compara a URL normalizada e grava `checagem_resultado`/`checagem_json` mais um
evento. Se a URL não aparecer, o pedido ganha `sugestao_removido = 1` e a tela
pergunta se quer marcar como removido — nada muda sozinho. A checagem nunca
abre a página do site, só consulta o buscador; abrir link externo continua
exigindo o `<details>` com o aviso de conteúdo adulto.

### D39 — Atalho do Google (URL conferida em 27/11/2026)

O atalho aponta para `https://support.google.com/websearch/contact/content_removal_form?hl=pt-BR`,
o formulário oficial de remoção de conteúdo pessoal da Pesquisa Google
(confirmado na Central de Ajuda do Google em 27/11/2026). Ele é apresentado
como caminho separado do pedido ao site e não substitui um ao outro; o link só
abre dentro do aviso de site externo.
