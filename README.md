## Busca de imagens na web

A rota legada de Google Cloud Vision usa o recurso Web Detection para
procurar páginas públicas com imagens correspondentes. A imagem só é enviada
depois que a pessoa marca a confirmação no formulário; o aplicativo não a grava
no catálogo nem no disco durante essa busca. Consulte as
[informações de uso de dados do Vision](https://cloud.google.com/vision/docs/data-usage).

Para habilitar a busca:

1. Crie um projeto no Google Cloud, habilite a Vision API e configure faturamento.
   Consulte o [guia oficial de configuração](https://cloud.google.com/vision/docs/setup)
   e os [preços da API](https://cloud.google.com/vision/pricing).
2. Configure Application Default Credentials no ambiente em que o servidor roda.
   Para desenvolvimento local, use `gcloud auth application-default login` e
   `gcloud auth application-default set-quota-project ID_DO_PROJETO`. Em contêiner
   ou produção, forneça credenciais ao processo de forma segura e defina
   `GOOGLE_APPLICATION_CREDENTIALS` para o caminho dessas credenciais dentro do
   contêiner. Não versione chaves ou arquivos de credenciais.
3. A rota legada exige administrador e confirmação para cada busca; ela não é
   apresentada na página `/busca-web`.

Os resultados são páginas e imagens públicas que o serviço conseguiu indexar;
não garantem cobertura total nem identificam com certeza quem publicou.

As páginas internas exigem login. Mesmo assim, configure limites de uso antes de
disponibilizar o site publicamente para controlar custos de APIs externas.

## Provedores de busca

A página de busca usa dois provedores de texto, ambos com a mesma interface
(`search(query) -> list[Candidato]`):

- **SerpAPI (Google Images/Light)** — continua com a chave
  `SERPAPI_API_KEY` e as consultas montadas a partir de arrobas, termos e sites.
- **TinyFish (web)** — `GET https://api.search.tinyfish.ai` com `X-API-Key`,
  `location=BR` e `language=pt`, chave em `TINYFISH_API_KEY`. Respeita 30
  requisições/minuto, repete em 429/5xx e avisa quando a cota diária acaba
  (HTTP 402). Consulte a
  [referência da API](https://docs.tinyfish.ai/search-api/reference.md).

As variáveis `BRAVE_SEARCH_API_KEY`, `TINEYE_API_KEY`, `DATAFORSEO_LOGIN`,
`DATAFORSEO_PASSWORD` e `SEARXNG_BASE_URL` podem ser preparadas no `.env`, mas
configurá-las não ativa essas integrações. Google Cloud Vision continua
somente pela rota legada de administrador.

## Contas, login e e-mail

Copie `.env.example` para `.env` na raiz do projeto e preencha as configurações
localmente. Não compartilhe nem versione o arquivo `.env`.

```bash
cp .env.example .env
```

- `ADMIN_USERNAME` define o login inicial do administrador (padrão
  `Gustavo.souza`).
- Defina `ADMIN_EMAIL` e `ADMIN_INITIAL_PASSWORD` para criar a conta de admin no
  primeiro início do aplicativo. Use a senha desejada somente no `.env`; senhas
  são armazenadas no banco como hashes Argon2. A conta existente não é
  sobrescrita em inicializações posteriores.
- O cadastro público cria uma conta e solicita nome público e arroba. Com
  `EMAIL_VERIFICATION_REQUIRED=false`, ela é ativada na hora. Nome e arroba são
  autodeclarados.
- Administradores podem criar contas em `/usuarios`, definindo uma senha
  inicial a ser comunicada ao usuário por um meio seguro. A mesma configuração
  de confirmação de e-mail vale para os dois tipos de cadastro.
- Apenas administradores acessam a lista de usuários e podem conceder ou retirar
  a permissão de administrador de outras contas. Todos podem pesquisar e
  validar seus próprios resultados; somente administradores podem selecionar
  outra conta. A configuração global de termos e sites é restrita a admins.
- Na lista, administradores podem editar ou excluir qualquer conta, inclusive a
  própria, desde que permaneça ao menos um administrador. Alterar e-mail ou
  senha encerra as sessões; um novo e-mail exige confirmação quando configurada.
  A exclusão remove fotos, candidatos e demais dados da conta. Registros de
  outras contas criados pelo usuário excluído são preservados e passam a
  apontar para seus respectivos donos. Falhas ao apagar arquivos são informadas.
- A conta inicial definida no ambiente só é criada quando não há administrador;
  excluir ou renomeá-la não a recria enquanto houver outro administrador.
- Ao iniciar esta versão, perfis antigos de criadoras sem conta vinculada são
  removidos junto com seus sites e conteúdos. Arquivos compartilhados com
  perfis vinculados são preservados; falhas na remoção de arquivos são
  registradas no log. Contas e seus dados vinculados não são apagados.
- Fotos de busca ficam em armazenamento privado, separadas dos arquivos
  públicos do catálogo. `USER_SEARCH_MEDIA_DIR` pode apontar para uma pasta
  persistente; por padrão, o app usa `fotos_busca` ao lado de `MEDIA_DIR`.
- Configure `SMTP_USER` e `SMTP_PASSWORD` com uma senha de app do Gmail, além
  de `SMTP_FROM` se o remetente precisar ser diferente. Nunca coloque a senha
  SMTP no código ou no chat.
- `APP_BASE_URL` determina o domínio usado nos links de confirmação e
  recuperação. Para uso somente no computador local, mantenha
  `http://localhost:8000`. Em rede local ou produção, configure o endereço que
  os usuários realmente conseguem acessar.
- Em produção, use HTTPS e defina `COOKIE_SECURE=true`.

Por enquanto, `EMAIL_VERIFICATION_REQUIRED=false` deixa o cadastro e o login
sem confirmação de e-mail, sem remover os fluxos de confirmação. Quando o SMTP
estiver pronto, altere para `EMAIL_VERIFICATION_REQUIRED=true` no `.env` e
reinicie o serviço para exigir confirmação e envio de e-mail no cadastro. A tela
de login também permite recuperar a senha por e-mail quando o SMTP estiver
configurado. A página inicial permanece pública; catálogo, comparações, buscas,
arquivos e demais rotas exigem sessão autenticada. Cada formulário de alteração
envia proteção CSRF.

Para iniciar ou atualizar o serviço:

```bash
docker compose up --build -d
```

## Render e PostgreSQL

O aplicativo **exige PostgreSQL**: sem `DATABASE_URL` ele não inicia (a flag
`APP_TESTES_SQLITE` existe apenas para a suíte de testes). Para o Render, crie
um PostgreSQL e configure `DATABASE_URL` nas variáveis do serviço Web com a URL
interna fornecida pelo Render. O app cria/atualiza as tabelas ao iniciar. Não
use a URL externa do banco entre serviços hospedados no Render.

Para levar os dados locais ao banco PostgreSQL:

1. Faça backup de `data/catalogo.db` e de `media/oficiais`.
2. Crie o PostgreSQL no Render e use a URL do banco em `DATABASE_URL` no
   terminal local, sem colocá-la no repositório ou enviá-la pelo chat.
3. Na raiz do projeto, execute
   `python -m scripts.migrar_sqlite_para_postgres`. Por padrão, o script lê
   `data/catalogo.db`; use `SQLITE_PATH` para outro caminho. O destino deve
   estar    vazio: a migração se recusa a misturar dados e copia criadoras,
   conteúdos, hashes, páginas oficiais, perfis, fotos de busca, sites oficiais
   pessoais, relatos, usuários, tokens, sessões e configurações.
4. Configure `MEDIA_DIR` para um disco persistente e copie `media/oficiais`
   para a pasta correspondente. Configure `USER_SEARCH_MEDIA_DIR` em uma pasta
   privada no mesmo disco; para migrar fotos existentes, defina
   `USER_SEARCH_SOURCE_DIR` (padrão `media/fotos_busca`) e
   `USER_SEARCH_TARGET_DIR` ao executar o script de migração. O banco guarda
   referências aos nomes; o PostgreSQL não armazena as imagens.
5. Configure as demais variáveis do serviço, incluindo `APP_BASE_URL` com a
   URL pública HTTPS e `COOKIE_SECURE=true`, e valide o site antes de alterar
   seu endereço de uso.

O armazenamento local efêmero do serviço Web não é adequado para imagens que
precisam sobreviver a uma implantação ou reinicialização. O disco persistente
do Render pode exigir um plano pago e deve ser configurado separadamente do
PostgreSQL. Consulte a [documentação de discos persistentes do Render](https://render.com/docs/disks)
e a [documentação de PostgreSQL do Render](https://render.com/docs/postgresql).

## Testes com SerpApi

A página `/busca-web` (menu "Busca na web") pesquisa o perfil do próprio usuário ou, para
administradores, o usuário selecionado. Os resultados podem ser marcados como
site oficial, ocultados localmente com “Não sou eu” ou registrados como
relatos de possível vazamento. Os relatos armazenam URL, data do registro,
título, origem, resumo e outros dados retornados pela API; não são uma
verificação automática do conteúdo.

A página reúne quatro mecanismos no seletor de endpoint do card de busca:

- **Google Images** e **Google Images Light**: buscas por texto, não por imagem.
  A busca combina nome e arroba com os termos e sites configurados e pode
  executar mais de uma consulta.
- **Google Lens**: envia à SerpApi uma foto privada salva no perfil ou uma nova
  foto do próprio usuário, que fica salva para buscas futuras. Administradores
  podem pesquisar outros usuários por texto, mas não enviar as fotos deles.
  O aplicativo reduz imagens grandes para respeitar o limite documentado de
  500 KB.
- **Google Images Related Content**: depende de um `related_content_id` retornado
  por uma busca Google Images anterior na mesma página.

Para configurar a chave, crie um arquivo `.env` na raiz do projeto (ele está no
`.gitignore`) e adicione:

```env
SERPAPI_API_KEY=sua_chave_serpapi
TINYFISH_API_KEY=sua_chave_tinyfish
```

Recrie o contêiner para carregar a variável:

```bash
docker compose up --build -d
```

A chave é usada somente no servidor. Nunca a cole no HTML, em formulários ou no
repositório. Cada mecanismo é uma consulta separada e pode consumir créditos;
confira o [plano e os limites atuais da SerpApi](https://serpapi.com/pricing).
O upload de imagem para o Google Lens usa o endpoint de imagem da SerpApi antes
da consulta Lens; o identificador enviado expira conforme a
[documentação do upload](https://serpapi.com/image-api).

Não há limite mensal de consultas por usuário. A autenticação impede acesso
anônimo, mas qualquer usuário cadastrado pode consumir as APIs; proteja também
a chave e monitore os custos.

## Fluxo de busca e validação (Valida)

1. Em `/busca-web`, o administrador escolhe a criadora, o provedor (SerpAPI ou
   TinyFish), os termos, os sites e o limite de consultas por execução
   (padrão 12, salvo em `app_settings`).
2. Ao iniciar, o servidor consulta **primeiro** o perfil público do Instagram
   (respeitando o cache de 24 h), salva as postagens e extrai da bio os outros
   arrobas citados, gravando-os em `arroba_descoberto`. Se o Instagram falhar
   ou estiver bloqueado, a execução registra o motivo em `busca_execucao` e
   continua apenas com o arroba informado.
3. As consultas combinam cada arroba com os termos e com `site:` dos domínios
   configurados. A execução roda em segundo plano (`BackgroundTasks`), com
   status consultável na própria página, e é registrada em `busca_execucao`
   (provedor, termos, status, erro do Instagram, resumo).
4. Resultados são normalizados (URL sem parâmetros de rastreio, domínio em
   minúsculas), deduplicados e pontuados por regras: arroba no título/URL,
   domínio de vazamento, palavras suspeitas; perfis oficiais da criadora,
   páginas de busca e espelhos de perfil são descartados. A nota, os motivos e
   a classificação (`provavel_vazamento`, `oficial`, `falso_positivo`,
   `indefinido`) ficam em `search_candidates`. Só entram na fila candidatos com
   nota a partir de `PONTUACAO_LIMIAR_REVISAO` (padrão 30).
5. `/valida` deixou de consultar o Instagram: ela lista os candidatos por nota,
   com os motivos, e mantém a revisão manual de sempre (**vazado**, **não sou
   eu**, **oficial**). Rodar a busca de novo atualiza `ultima_vez_em` e não
   apaga o status de revisão já dado.
6. A comparação de imagens por pHash continua no código, mas desligada por
   `COMPARACAO_IMAGEM_ATIVA=false`: nesse modo nenhuma miniatura é baixada,
   processada ou comparada e a SerpAPI também usa a pontuação por regras.

Para administradores, ao carregar a página, o uso do mês, a cota e as consultas restantes são
consultados automaticamente; o botão **Atualizar saldo** repete a consulta pela
[Account API](https://serpapi.com/account-api). Essa é uma chamada HTTP separada
da busca, mas a SerpApi informa que a Account API é gratuita e não consome a
cota mensal. O botão só aparece quando `SERPAPI_API_KEY` está configurada; sem
uma conta e chave SerpApi, não há saldo que o site possa consultar.

Fotos e sites oficiais podem ser adicionados em `/perfil-busca`. Referências
antigas já vinculadas a contas continuam disponíveis para comparação na Valida.

## Testes com APIs do Instagram

As rotas legadas de Instagram usam a mesma `SERPAPI_API_KEY`, são restritas a
administradores e não aparecem na página `/busca-web`. Elas mantêm duas
consultas independentes:

- **Instagram Profile API** recebe o nome de usuário (sem precisar do `@`) e
  mostra dados de perfil e publicações retornados pela SerpApi.
- **Instagram Popular API** recebe uma palavra-chave e mostra posts populares
  relacionados.

Cada envio exige confirmação para encaminhar o usuário ou palavra-chave à
SerpApi. A página exibe somente resultados retornados pela API; não acessa
perfis privados nem garante cobertura completa. A paginação por `next_page_token`
ainda não está implementada.

Consulte a documentação da
[Instagram Profile API](https://serpapi.com/instagram-profile-api) e da
[Instagram Popular API](https://serpapi.com/instagram-popular-api). As consultas
podem consumir créditos da mesma conta SerpApi configurada para a página anterior.
### Busca de vazamentos

Em `/busca-web` (SerpApi), o card "Busca de vazamentos" combina nome e arroba da criadora com termos (ex.: vazado, leaked, naked) e sites (ex.: erome.com) editáveis, agrupados com `OR` em 1 a 3 consultas (1 crédito cada, só a primeira página). O custo é exibido antes de rodar. O período (dia, semana, mês, ano) é opcional; o padrão é "Qualquer data". Os resultados seguem para a Valida.

### Posts oficiais do Instagram

A rota legada de importação de posts oficiais do Instagram percorre páginas do
perfil e guarda os hashes das miniaturas como referência.

### Análise dos dados

`/analise` (só admin) resume, por criadora, candidatos por status e nível de semelhança, falta de hash/referências, domínios mais frequentes e alertas de calibração (ex.: vazados confirmados caindo em "Diferentes"). Serve para acompanhar a qualidade da busca sem acessar o banco.

A busca de vazamentos roda em segundo plano (o plano gratuito do Render é lento e a requisição poderia expirar): o card mostra "em andamento", atualiza sozinho e exibe o resumo final ou o erro.

## Banco, migração e testes

O schema é criado/atualizado no início do app (`db.iniciar_banco`), sem
Alembic: `CREATE TABLE IF NOT EXISTS` e `ALTER TABLE ... ADD COLUMN`
idempotentes, tanto para as tabelas novas (`busca_execucao`,
`arroba_descoberto`) quanto para as colunas novas de `search_candidates`
(`nota`, `motivos_json`, `classificacao`, `execucao_id`, `url_normalizada`,
`dominio`, `primeira_vez_em`, `ultima_vez_em`, `resposta_bruta_json`,
`novo_para_criadora`). Nenhum dado é apagado.

Para levar um banco SQLite antigo ao PostgreSQL:

```bash
DATABASE_URL='postgresql://...' python -m scripts.migrar_sqlite_para_postgres
```

Para rodar os testes (o `conftest.py` liga um SQLite temporário só na suíte):

```bash
pip install -r requirements.txt pytest
python -m pytest tests/test_pontuacao.py tests/test_valida.py tests/test_busca_web.py
python -m pytest tests
```
