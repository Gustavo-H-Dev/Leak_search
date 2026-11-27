/* Comportamento dos componentes do design system: modal de confirmacao e toast.
   Sem dependencia externa; a pagina declara os elementos e isso cuida do resto. */
(function () {
  "use strict";

  var ultimoFoco = null;

  function focaveis(raiz) {
    var seletor =
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
    return Array.prototype.slice.call(raiz.querySelectorAll(seletor)).filter(function (el) {
      return el.offsetWidth > 0 || el.offsetHeight > 0 || el === document.activeElement;
    });
  }

  function abrirModal(modal, gatilho) {
    if (!modal || !modal.hidden) return;
    ultimoFoco = gatilho || document.activeElement;
    modal.hidden = false;
    document.body.style.overflow = "hidden";
    var caixa = modal.querySelector(".ds-modal__caixa") || modal;
    var primeiro = focaveis(caixa)[0];
    if (primeiro) primeiro.focus();
  }

  function fecharModal(modal) {
    if (!modal || modal.hidden) return;
    modal.hidden = true;
    document.body.style.overflow = "";
    if (ultimoFoco && typeof ultimoFoco.focus === "function") ultimoFoco.focus();
    ultimoFoco = null;
  }

  document.addEventListener("click", function (evento) {
    var abrir = evento.target.closest("[data-abrir-modal]");
    if (abrir) {
      evento.preventDefault();
      abrirModal(document.getElementById(abrir.getAttribute("data-abrir-modal")), abrir);
      return;
    }
    var fechar = evento.target.closest("[data-fechar-modal]");
    if (fechar) {
      evento.preventDefault();
      fecharModal(fechar.closest("[data-modal]"));
      return;
    }
    if (evento.target.classList && evento.target.classList.contains("ds-modal")) {
      fecharModal(evento.target);
    }
  });

  document.addEventListener("keydown", function (evento) {
    var aberto = document.querySelector("[data-modal]:not([hidden])");
    if (!aberto) return;
    if (evento.key === "Escape") {
      evento.preventDefault();
      fecharModal(aberto);
      return;
    }
    if (evento.key !== "Tab") return;
    var lista = focaveis(aberto);
    if (!lista.length) return;
    var primeiro = lista[0];
    var ultimo = lista[lista.length - 1];
    if (evento.shiftKey && document.activeElement === primeiro) {
      evento.preventDefault();
      ultimo.focus();
    } else if (!evento.shiftKey && document.activeElement === ultimo) {
      evento.preventDefault();
      primeiro.focus();
    }
  });

  function mostrarToast(texto, tom, duracaoMs) {
    var area = document.getElementById("ds-toast-area");
    if (!area || !texto) return;
    var toast = document.createElement("div");
    toast.className = "ds-toast" + (tom ? " ds-toast--" + tom : "");
    toast.setAttribute("role", tom === "erro" ? "alert" : "status");
    toast.textContent = texto;
    area.appendChild(toast);
    window.setTimeout(function () {
      toast.remove();
    }, duracaoMs || 4200);
  }

  document.addEventListener("click", function (evento) {
    var gatilho = evento.target.closest("[data-toast]");
    if (!gatilho) return;
    evento.preventDefault();
    mostrarToast(
      gatilho.getAttribute("data-toast"),
      gatilho.getAttribute("data-toast-tom") || "",
      Number(gatilho.getAttribute("data-toast-duracao")) || undefined
    );
  });

  window.dsUI = { mostrarToast: mostrarToast };
})();
