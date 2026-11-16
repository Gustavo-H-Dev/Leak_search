## Busca de imagens na web

A página `/buscar-google` usa o recurso Web Detection do Google Cloud Vision para
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
3. Acesse `/buscar-google` e confirme o envio da imagem em cada busca.

Os resultados são páginas e imagens públicas que o serviço conseguiu indexar;
não garantem cobertura total nem identificam com certeza quem publicou.

**Antes de disponibilizar o site publicamente**, implemente autenticação e limite
de uso para impedir que visitantes não autorizados consumam a cota e gerem
cobranças no projeto Google Cloud.

## Testes com SerpApi

A página `/buscar-serpapi` separa cinco mecanismos e faz somente a chamada
selecionada em cada envio:

- **Google Images** e **Google Images Light**: buscas por texto, não por imagem.
- **Google Lens**: envia a imagem selecionada do catálogo à SerpApi. O aplicativo
  reduz imagens grandes para respeitar o limite documentado de 500 KB.
- **Google Reverse Image**: recebe uma URL de imagem pública que a SerpApi possa
  acessar; arquivos locais do catálogo não são publicados automaticamente.
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

O site ainda não possui login nem limite mensal local. Não configure a chave
enquanto o site estiver acessível publicamente sem proteção: visitantes podem
usar a cota da sua conta.

## Testes com APIs do Instagram

A página `/buscar-instagram` usa a mesma `SERPAPI_API_KEY` e mantém as duas
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