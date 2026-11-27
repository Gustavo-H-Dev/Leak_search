/* Linhas dinâmicas do formulário de perfis monitorados (wizard e configurações).
   Sem isso o formulário funciona: ele já começa com uma linha em branco. */
(function () {
  "use strict";

  var contador = 1;

  function nova() {
    var modelo = document.getElementById("modelo-linha-perfil");
    if (!modelo) return null;
    return modelo.content.firstElementChild.cloneNode(true);
  }

  function renomear(linha) {
    linha.querySelectorAll("select, input").forEach(function (campo) {
      contador += 1;
      var id = "perfil-campo-" + contador;
      campo.id = id;
      var rotulo = campo.closest(".ds-campo") && campo.closest(".ds-campo").querySelector("label");
      if (rotulo) rotulo.setAttribute("for", id);
    });
  }

  document.addEventListener("click", function (evento) {
    var adicionar = evento.target.closest("[data-adicionar-perfil]");
    if (adicionar) {
      var formulario = adicionar.closest("[data-perfis-form]");
      var container = formulario && formulario.querySelector("[data-perfis-linhas]");
      var linha = nova();
      if (!container || !linha) return;
      renomear(linha);
      container.appendChild(linha);
      var primeiro = linha.querySelector("input");
      if (primeiro) primeiro.focus();
      return;
    }

    var remover = evento.target.closest("[data-remover-linha]");
    if (!remover) return;
    var alvo = remover.closest("[data-perfil-linha]");
    var formulario = remover.closest("[data-perfis-form]");
    if (alvo) alvo.remove();
    if (formulario && !formulario.querySelector("[data-perfil-linha]")) {
      var adicionarDeNovo = formulario.querySelector("[data-adicionar-perfil]");
      if (adicionarDeNovo) adicionarDeNovo.click();
    }
  });
})();
