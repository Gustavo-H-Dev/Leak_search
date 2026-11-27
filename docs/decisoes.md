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
