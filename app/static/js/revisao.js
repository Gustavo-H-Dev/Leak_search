/* Fila de revisão: salva a decisão sem recarregar e oferece "Desfazer" por alguns segundos. */
(function () {
  "use strict";

  var barra = document.getElementById("revisao-desfazer");
  var texto = document.getElementById("revisao-desfazer-texto");
  var anteriores = document.getElementById("revisao-anteriores");
  var leaks = document.getElementById("revisao-leaks");
  var fechar = document.getElementById("revisao-desfazer-fechar");
  var ignorar = document.getElementById("revisao-ignorar");
  var ignorarTexto = document.getElementById("revisao-ignorar-texto");
  var ignorarDominio = document.getElementById("revisao-ignorar-dominio");
  var ignorarNao = document.getElementById("revisao-ignorar-nao");
  var temporizador = null;

  function esconderDesfazer() {
    if (!barra) return;
    barra.hidden = true;
    if (temporizador) {
      clearTimeout(temporizador);
      temporizador = null;
    }
  }

  function mostrarDesfazer(dados) {
    if (!barra || !anteriores || !leaks) return;
    anteriores.value = JSON.stringify(dados.anteriores || {});
    leaks.value = JSON.stringify(dados.leaks_criados || []);
    if (texto) texto.textContent = "Revisão salva. Dá para desfazer por alguns segundos.";
    barra.hidden = false;
    barra.scrollIntoView({ block: "nearest", behavior: "smooth" });
    if (temporizador) clearTimeout(temporizador);
    temporizador = setTimeout(esconderDesfazer, 8000);
  }

  function mostrarIgnorar(sugestao) {
    if (!ignorar || !sugestao) return;
    if (ignorarTexto) {
      ignorarTexto.textContent =
        'Você marcou ' + sugestao.total + ' itens de ' + sugestao.dominio +
        ' como "Não é". Ignorar esse domínio nas próximas buscas?';
    }
    if (ignorarDominio) ignorarDominio.value = sugestao.dominio;
    ignorar.hidden = false;
  }

  if (fechar) fechar.addEventListener("click", esconderDesfazer);
  if (ignorarNao) {
    ignorarNao.addEventListener("click", function () {
      if (ignorar) ignorar.hidden = true;
    });
  }

  document.querySelectorAll("form[data-revisao]").forEach(function (form) {
    // FormData nao inclui o botao que submeteu; guardamos o valor do "status".
    form.querySelectorAll("button[name]").forEach(function (botao) {
      botao.addEventListener("click", function () {
        form.dataset.statusEscolhido = botao.value;
      });
    });

    form.addEventListener("submit", async function (evento) {
      evento.preventDefault();
      var card = form.closest("[data-card-revisao]");
      var erro = card ? card.querySelector("[data-erro]") : null;
      var botoes = form.querySelectorAll("button");
      botoes.forEach(function (botao) { botao.disabled = true; });
      if (erro) erro.textContent = "";
      var dados = new FormData(form);
      var enviado = evento.submitter || null;
      if (enviado && enviado.name) {
        dados.append(enviado.name, enviado.value);
      } else if (!dados.get("status") && form.dataset.statusEscolhido) {
        dados.append("status", form.dataset.statusEscolhido);
      }
      try {
        var resposta = await fetch(form.action, {
          method: "POST",
          body: dados,
          headers: { Accept: "application/json" },
        });
        var dados = await resposta.json();
        if (!resposta.ok || !dados.ok) {
          throw new Error(dados.erro || "Não foi possível salvar a revisão.");
        }
        if (card) card.remove();
        mostrarDesfazer(dados);
        if (dados.sugerir_ignorar) mostrarIgnorar(dados.sugerir_ignorar);
        var restantes = document.querySelectorAll("[data-card-revisao]");
        if (!restantes.length) {
          window.location.href = "/revisao?mensagem=" +
            encodeURIComponent("Fila atualizada.");
        }
      } catch (falha) {
        if (erro) erro.textContent = falha.message;
        botoes.forEach(function (botao) { botao.disabled = false; });
      }
    });
  });
})();
