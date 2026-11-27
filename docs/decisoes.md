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

### Pendências conhecidas

- Telas ainda no CSS antigo: home, busca na web, Valida, análise, domínios,
  execuções, perfil de busca, usuários, catálogo e formulários de criadora.
  Migrar uma por fase e só então remover `legado.css`.
- "Remoções" precisa de página e fluxo próprios.
- Cliente não tem histórico por criadora; por isso o item é oculto para ele.
