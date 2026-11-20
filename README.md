## Busca de imagens na web

A seção Google Cloud Vision da página `/busca-web` usa o recurso Web Detection do Google Cloud Vision para
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
3. Acesse `/busca-web` e confirme o envio da imagem em cada busca.

Os resultados são páginas e imagens públicas que o serviço conseguiu indexar;
não garantem cobertura total nem identificam com certeza quem publicou.

As páginas internas exigem login. Mesmo assim, configure limites de uso antes de
disponibilizar o site publicamente para controlar custos de APIs externas.

## Provedores de busca

As telas de busca mostram um catálogo de provedores separado por tipo. Google
Cloud Vision e SerpApi continuam sendo as integrações disponíveis nos fluxos
atuais; Brave Search, TinEye, DataForSEO e SearXNG aparecem como opções futuras,
mas ficam desabilitados e não recebem consultas. A seleção de um provedor ainda
não integrado retorna uma mensagem explícita, sem simular uma busca.

As variáveis `BRAVE_SEARCH_API_KEY`, `TINEYE_API_KEY`, `DATAFORSEO_LOGIN`,
`DATAFORSEO_PASSWORD` e `SEARXNG_BASE_URL` podem ser preparadas no `.env`, mas
configurá-las não ativa essas integrações. Cada provedor precisará de um
adaptador próprio e de normalização para o formato de resultados já usado pela
aplicação antes de ser habilitado.

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
- O cadastro público cria uma conta de criadora e solicita nome público e
  arroba. Com `EMAIL_VERIFICATION_REQUIRED=false`, a conta é ativada na hora e
  associada a um perfil existente com o mesmo e-mail, ou cria um perfil novo.
  Administradores também podem cadastrar perfis sem conta. Nome e arroba são
  autodeclarados.
- A página `/usuarios` e o catálogo de criadoras são administrados por
  administradores. Catálogo, comparação e buscas também são restritos a
  administradores; contas de criadoras não têm acesso aos arquivos, catálogo ou
  ferramentas de busca.
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

O aplicativo continua usando SQLite local quando `DATABASE_URL` está vazia.
Para o Render, crie um PostgreSQL e configure `DATABASE_URL` nas variáveis do
serviço Web com a URL interna fornecida pelo Render. O app cria/atualiza as
tabelas ao iniciar. Não use a URL externa do banco entre serviços hospedados no
Render.

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

A página separa quatro mecanismos e faz somente a chamada selecionada em cada
envio:

- **Google Images** e **Google Images Light**: buscas por texto, não por imagem.
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

Na página SerpApi, o botão **Consultar saldo** busca o uso do mês, a cota e as
consultas restantes pela
[Account API](https://serpapi.com/account-api). Essa é uma chamada HTTP separada
da busca, mas a SerpApi informa que a Account API é gratuita e não consome a
cota mensal. O botão só aparece quando `SERPAPI_API_KEY` está configurada; sem
uma conta e chave SerpApi, não há saldo que o site possa consultar.

No catálogo, a criadora é o cadastro principal. Um perfil pode existir sem
conta; no cadastro público, a conta é associada a um perfil existente com o
mesmo e-mail. Foto e site oficial são opcionais; sites e fotos já cadastrados
ficam agrupados dentro do registro da criadora e também podem ser adicionados
depois. As buscas do Google Vision,
SerpApi e Instagram só marcam a URL exata da página cadastrada (sem considerar
outras páginas do mesmo domínio). O resultado continua visível para revisão e
a marcação identifica para qual criadora a página foi registrada. Endereços
globais cadastrados em versões anteriores são preservados como pendentes, sem
serem considerados oficiais, até que sejam associados a uma criadora e
revisados.

## Testes com APIs do Instagram

A seção Instagram da página `/busca-web` usa a mesma `SERPAPI_API_KEY` e mantém as duas
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