// Copia o texto de um campo (textarea/input) ou de um elemento.
// Sem biblioteca: usa a Clipboard API e cai para selection + execCommand.
document.addEventListener("click", async function (evento) {
  var botao = evento.target.closest("[data-copiar]");
  if (!botao) return;
  evento.preventDefault();
  var seletor = botao.getAttribute("data-copiar");
  var campo = document.querySelector(seletor);
  if (!campo) return;
  var texto = "value" in campo ? campo.value : campo.textContent;

  var rotulo = botao.getAttribute("data-rotulo") || botao.textContent;
  botao.setAttribute("data-rotulo", rotulo);

  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(texto);
    } else {
      campo.focus();
      if (campo.select) campo.select();
      document.execCommand("copy");
    }
    botao.textContent = "Copiado";
  } catch (erro) {
    botao.textContent = "Selecione o texto e copie";
  }
  window.setTimeout(function () {
    botao.textContent = rotulo;
  }, 4000);
});
