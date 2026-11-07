// Copier l'adresse de la source
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  const el = document.querySelector(btn.dataset.copy);
  const text = el.textContent.trim();
  const done = () => { const old = btn.textContent; btn.textContent = "Copié ✓"; setTimeout(() => (btn.textContent = old), 1500); };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done);
  } else {  // http://IP:8000 n'est pas un contexte sécurisé : on sélectionne le texte
    const r = document.createRange(); r.selectNodeContents(el);
    const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    try { document.execCommand("copy"); done(); } catch (_) {}
  }
});

// Recherche dans le catalogue
const q = document.getElementById("q");
if (q) q.addEventListener("input", () => {
  const term = q.value.trim().toLowerCase();
  let shown = 0;
  document.querySelectorAll("#grid .card").forEach((c) => {
    const ok = c.dataset.name.includes(term);
    c.hidden = !ok; if (ok) shown++;
  });
  document.getElementById("none").hidden = shown > 0;
});

// Confirmation avant suppression
document.addEventListener("submit", (e) => {
  const msg = e.target.dataset && e.target.dataset.confirm;
  if (msg && !confirm(msg)) e.preventDefault();
});

// Envoi d'IPA avec barre de progression
document.querySelectorAll("form[data-upload]").forEach((form) => {
  form.addEventListener("submit", (e) => {
    if (!form.querySelector("input[type=file]").files.length) return;
    e.preventDefault();
    const bar = form.querySelector("progress"), msg = form.querySelector(".upmsg");
    const btn = form.querySelector("button[type=submit]");
    const xhr = new XMLHttpRequest();
    xhr.open("POST", form.action);
    xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");
    xhr.upload.onprogress = (ev) => {
      if (!ev.lengthComputable) return;
      bar.hidden = false; bar.value = (ev.loaded / ev.total) * 100;
      msg.textContent = Math.round(bar.value) + " %";
    };
    xhr.onload = () => {
      if (xhr.status === 204) { msg.textContent = "Traitement…"; location.reload(); return; }
      msg.textContent = xhr.status === 413 ? "Fichier trop gros pour ce chemin." : "Erreur " + xhr.status + " : " + xhr.responseText.slice(0, 200);
      btn.disabled = false;
    };
    xhr.onerror = () => {
      msg.textContent = "Envoi interrompu (fichier trop gros pour ce chemin, ou connexion coupée).";
      btn.disabled = false;
    };
    btn.disabled = true;
    xhr.send(new FormData(form));
  });
});
