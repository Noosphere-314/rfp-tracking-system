/* RFP Tracker — дашборд v2. Прогресивне покращення і нічого більше:
   кожна сторінка повністю робоча без цього файлу.
   ЗАБОРОНА (розділ 3.11): жодного автополінгу, meta refresh чи віджета
   здоров'я з інтервалом — будь-який автооновлювач тихо вбиває 30-хвилинний
   ідл-таймаут, бо продовжує сесію без участі людини. Таймер нижче лише
   читає cookie локально і не робить мережевих запитів без кліку. */
(function () {
  "use strict";
  document.documentElement.classList.add("js");

  function cookie(name) {
    var m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : null;
  }

  /* ── 1. Таймер сесії (розділ 1.4) ─────────────────────────────────────
     Джерело правди — companion-cookie rfp_exp (не HttpOnly), яку сервер
     перевидає на КОЖНІЙ відповіді. Тому таймер не може розійтися з сервером,
     а мультитабність працює сама собою: cookie спільна для всіх вкладок. */
  (function sessionTimer() {
    var box = document.getElementById("session-timer");
    if (!box || !cookie("rfp_exp")) return;
    var val = box.querySelector(".session__val");
    var banner = null, bannerVal = null, pinging = false;

    function fmt(sec) {
      var m = Math.floor(sec / 60), s = sec % 60;
      return m + ":" + (s < 10 ? "0" : "") + s;
    }

    function ping(then) {
      if (pinging) return;
      pinging = true;
      fetch("/session/ping", { credentials: "same-origin", cache: "no-store" })
        .then(function (r) { pinging = false; then(r.ok); })
        .catch(function () { pinging = false; then(true); });  // офлайн ≠ розлогінення
    }

    function ensureBanner() {
      if (banner) return banner;
      banner = document.createElement("div");
      banner.className = "session-banner";
      banner.setAttribute("role", "status");
      banner.innerHTML =
        '<span>Сесія завершиться за <span class="session-banner__val">2:00</span></span>';
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn--sm";
      btn.textContent = "Залишитись";
      btn.addEventListener("click", function () {
        btn.disabled = true;
        ping(function (ok) {
          btn.disabled = false;
          if (!ok) { location.href = "/login?reason=expired"; return; }
          tick();                       // перечитуємо cookie після продовження
        });
      });
      banner.appendChild(btn);
      document.body.appendChild(banner);
      bannerVal = banner.querySelector(".session-banner__val");
      return banner;
    }

    function tick() {
      var exp = parseInt(cookie("rfp_exp") || "0", 10);
      var left = exp - Math.floor(Date.now() / 1000);

      if (left <= 0) {
        val.textContent = "0:00";
        box.classList.add("is-warn");
        if (banner) banner.hidden = true;
        /* По нулю НЕ редіректимо наосліп: годинник клієнта міг збрехати. */
        ping(function (ok) { if (!ok) location.href = "/login?reason=expired"; });
        return;
      }

      val.textContent = fmt(left);
      box.classList.toggle("is-warn", left <= 120);
      if (left <= 120) {
        ensureBanner().hidden = false;
        bannerVal.textContent = fmt(left);
      } else if (banner) {
        banner.hidden = true;
      }
    }

    tick();
    setInterval(tick, 1000);
  })();

  /* ── 2. Чернетка /settings ────────────────────────────────────────────
     Форма параметрів довга; помилка валідації повертає сторінку заново. */
  (function settingsDraft() {
    var form = document.getElementById("settings-form");
    if (!form || !window.sessionStorage) return;
    var KEY = "settings-draft";

    function snapshot() {
      var out = {};
      form.querySelectorAll("input[name], select[name], textarea[name]").forEach(function (el) {
        if (el.type === "hidden" || el.name === "csrf") return;
        out[el.name] = el.value;
      });
      try { sessionStorage.setItem(KEY, JSON.stringify(out)); } catch (e) { /* quota */ }
    }

    var params = new URLSearchParams(location.search);
    if (params.has("reason") || params.has("error")) {
      var raw = sessionStorage.getItem(KEY);
      if (raw) {
        var data = {};
        try { data = JSON.parse(raw); } catch (e) { data = {}; }
        var restored = 0;
        Object.keys(data).forEach(function (name) {
          var el = form.elements[name];
          if (el && el.value !== data[name]) { el.value = data[name]; restored++; }
        });
        if (restored) {
          var note = document.createElement("div");
          note.className = "alert alert--info";
          note.setAttribute("role", "status");
          note.textContent = "Відновлено незбережені зміни (" + restored + ").";
          form.parentNode.insertBefore(note, form);
        }
      }
    }

    setInterval(snapshot, 20000);
    window.addEventListener("beforeunload", snapshot);
    form.addEventListener("submit", function () {
      try { sessionStorage.removeItem(KEY); } catch (e) { /* ignore */ }
    });
  })();

  /* ── 3. Захист від подвійного сабміту в довгий запит ──────────────────
     Кнопку вимикаємо через setTimeout(0): на момент події submit браузер уже
     зібрав дані форми разом зі значенням submitter'а, тож name/value кнопки
     не губиться. */
  document.querySelectorAll("form[data-busy]").forEach(function (form) {
    form.addEventListener("submit", function (ev) {
      if (form.dataset.busyOn === "1") { ev.preventDefault(); return; }
      form.dataset.busyOn = "1";
      var btn = ev.submitter || form.querySelector('button:not([type=button]), [type=submit]');
      setTimeout(function () {
        if (btn) {
          btn.dataset.busyLabel = btn.textContent;
          btn.textContent = form.dataset.busy;
          btn.disabled = true;
        }
      }, 0);
    });
  });

  /* ── 4. Підтвердження небезпечних дій ─────────────────────────────────── */
  /* Кастомний <dialog class="confirm"> (задача 4 аудиту 2026-08-12) замість
     window.confirm(): один діалог на сторінку, створюється лінькво (лише
     якщо на сторінці є бодай один [data-confirm]) і переюзається для
     кожного. Без JS — форма шле POST одразу, як і раніше (той самий
     інваріант файлу): це прогресивне посилення, а не заміна поведінки.

     DOM — виключно createElement/textContent, як і в розділі 7 (чат): текст
     підтвердження приходить із data-confirm у розмітці (уже екранований
     Jinja'ю при рендері), тут лише переносимо його в textContent, без
     жодного innerHTML. */
  (function confirmDialog() {
    var targets = document.querySelectorAll("[data-confirm]");
    if (!targets.length) return;

    var dialog = null, textEl = null, pending = null;

    function ensureDialog() {
      if (dialog) return dialog;
      dialog = document.createElement("dialog");
      dialog.className = "confirm";

      var body = document.createElement("div");
      body.className = "confirm__body";
      textEl = document.createElement("p");
      textEl.className = "confirm__text";
      body.appendChild(textEl);
      dialog.appendChild(body);

      var actions = document.createElement("div");
      actions.className = "confirm__actions";

      var cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.className = "btn btn--ghost";
      cancelBtn.textContent = document.body.dataset.confirmCancel || "Cancel";
      cancelBtn.addEventListener("click", function () {
        pending = null;
        dialog.close();
      });

      var okBtn = document.createElement("button");
      okBtn.type = "button";
      okBtn.className = "btn btn--danger";
      okBtn.textContent = document.body.dataset.confirmOk || "Confirm";
      okBtn.addEventListener("click", function () {
        var el = pending;
        pending = null;
        dialog.close();
        if (!el) return;
        /* dataset.confirmed="1" — прапорець-перехоплення: далі (нижче)
           слухач того самого data-confirm бачить його, пропускає показ
           діалогу вдруге і скидає прапорець назад, інакше дія зациклилась
           би сама на собі. */
        el.dataset.confirmed = "1";
        if (el.tagName === "FORM") {
          if (typeof el.requestSubmit === "function") el.requestSubmit();
          else el.submit();
        } else {
          el.click();
        }
      });

      actions.appendChild(cancelBtn);
      actions.appendChild(okBtn);
      dialog.appendChild(actions);

      /* Клік по ::backdrop = cancel: <dialog> не дає власної події для
         цього, тож порівнюємо ev.target === dialog — клік ПОЗА
         .confirm__body/.confirm__actions потрапляє саме на сам <dialog>
         (backdrop — псевдоелемент довкола нього, не окремий вузол DOM). */
      dialog.addEventListener("click", function (ev) {
        if (ev.target === dialog) { pending = null; dialog.close(); }
      });
      /* Escape — вбудована поведінка <dialog> (подія "cancel", потім
         "close"); тут лише скидаємо pending, щоб застаріла дія не
         виконалась, якщо діалог відкриють знову для іншого елемента. */
      dialog.addEventListener("cancel", function () { pending = null; });

      document.body.appendChild(dialog);
      return dialog;
    }

    targets.forEach(function (el) {
      var evt = el.tagName === "FORM" ? "submit" : "click";
      el.addEventListener(evt, function (ev) {
        if (el.dataset.confirmed === "1") { el.dataset.confirmed = ""; return; }
        ev.preventDefault();
        ev.stopPropagation();
        pending = el;
        ensureDialog();
        textEl.textContent = el.dataset.confirm;
        dialog.showModal();
      });
    });
  })();

  /* ── 5a. Друк у PDF (сторінка бріфа) ──────────────────────────────────
     Без цього блоку кнопка «Зберегти як PDF» — прогресивне покращення, якого
     нема: без JS сторінку однаково можна роздрукувати через Ctrl/Cmd+P
     браузера, друковані стилі (@media print, app.css) працюють незалежно. */
  document.querySelectorAll("[data-print]").forEach(function (btn) {
    btn.addEventListener("click", function () { window.print(); });
  });

  /* ── 5. Копіювання (сторінка бріфа) ───────────────────────────────────── */
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var src = document.querySelector(btn.dataset.copy);
      if (!src || !navigator.clipboard) return;
      navigator.clipboard.writeText(src.innerText).then(function () {
        var was = btn.textContent;
        btn.textContent = "Скопійовано";
        setTimeout(function () { btn.textContent = was; }, 1600);
      });
    });
  });

  /* ── 6. Клієнтські фільтри рядків (без змін у бекенді) ────────────────── */
  document.querySelectorAll("[data-filter-target]").forEach(function (ctl) {
    var rows = function () {
      return document.querySelectorAll(ctl.dataset.filterTarget + " tbody tr");
    };
    function apply() {
      var only = ctl.dataset.filterOnly;
      var needle = (ctl.type === "checkbox" ? "" : ctl.value).trim().toLowerCase();
      var shown = 0;
      rows().forEach(function (tr) {
        var ok = !needle || tr.textContent.toLowerCase().indexOf(needle) !== -1;
        if (ok && only && ctl.type === "checkbox" && ctl.checked) ok = !!tr.querySelector(only);
        tr.hidden = !ok;
        if (ok) shown++;
      });
      var counter = ctl.dataset.filterCount
        ? document.querySelector(ctl.dataset.filterCount) : null;
      if (counter) counter.textContent = String(shown);
    }
    ctl.addEventListener(ctl.type === "checkbox" ? "change" : "input", apply);
    if (ctl.value || ctl.checked) apply();
  });

  /* ── 7. Чат: прогресивне посилення (розділ 4.9) ────────────────────────
     ІНВАРІАНТ файлу зверху лишається: без цього блоку форма — звичайний
     <form method="post" action="/chat/send"> і сама доїжджає по PRG (303 на
     /chat). Тут лише підміна на fetch(), щоб бульбашка з'являлась одразу, а
     не після повного перезавантаження сторінки.

     Кнопку/напис вимикає й повертає розділ 3 (data-busy) — тут лише
     ДОДАЄМО те, чого той розділ не знає: textarea, і скидання стану після
     fetch (розділ 3 розрахований на навігацію геть зі сторінки, тут її нема).

     DOM — виключно createElement/textContent: reply — вивід LLM, innerHTML
     тут був би XSS-поверхнею. */
  (function chatEnhance() {
    var list = document.getElementById("chat-list");
    var empty = document.getElementById("chat-empty");
    var form = document.querySelector(".chat__composer");
    if (!list || !empty || !form) return;

    // .chat-col__scroll — внутрішня скрол-зона колонки чату (розділ 1
    // редизайну: .data більше сама не скролиться, композер і панель історії
    // тепер сидять поза нею у фіксованих частинах .chat-col/.chat-side).
    var dataBox = document.querySelector(".chat-col__scroll");
    var textarea = form.querySelector("textarea[name=message]");
    var button = form.querySelector("button[type=submit]");
    var URL_RE = /https?:\/\/[^\s<>"']+/g;

    /* D1 — приклади питань на порожньому стані: без JS це звичайні
       <a href="/chat?ask=…">, з JS клік лише заповнює композер і фокусить
       його, замість переходу — саме "заповнює", а не "надсилає" (розділ D1:
       автосабміту нема). textarea.value — властивість, не innerHTML, тож
       XSS-поверхні тут немає навіть попри те, що текст іде з data-атрибута. */
    document.querySelectorAll("#chat-empty [data-fill]").forEach(function (link) {
      link.addEventListener("click", function (ev) {
        ev.preventDefault();
        textarea.value = link.dataset.fill;
        textarea.focus();
        autoGrow();
      });
    });

    /* Автозростання textarea, 1..8 рядків (розділ 4 редизайну композера).
       CSP `style-src 'self'` (Caddyfile) не дає гарантії на `.style.*` з JS
       так само, як на style="" — тому висота керується атрибутом `rows`,
       звичайним HTML-атрибутом textarea, а не інлайновим стилем. Оцінка
       наближена (scrollHeight мінус вертикальний padding, поділене на
       line-height), але для «росте разом із текстом» цього достатньо. */
    function autoGrow() {
      var cs = getComputedStyle(textarea);
      var lineHeight = parseFloat(cs.lineHeight) || 18;
      var padY = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
      textarea.rows = 1;
      var lines = Math.round((textarea.scrollHeight - padY) / lineHeight) || 1;
      textarea.rows = Math.min(8, Math.max(1, lines));
    }
    textarea.addEventListener("input", autoGrow);
    autoGrow();

    function scrollDown() {
      if (dataBox) dataBox.scrollTop = dataBox.scrollHeight;
    }

    function linkifyInto(el, text) {
      var last = 0, m;
      URL_RE.lastIndex = 0;
      while ((m = URL_RE.exec(text))) {
        if (m.index > last) el.appendChild(document.createTextNode(text.slice(last, m.index)));
        var a = document.createElement("a");
        a.href = m[0];
        a.target = "_blank";
        a.rel = "noopener";
        a.textContent = m[0];
        el.appendChild(a);
        last = m.index + m[0].length;
      }
      if (last < text.length) el.appendChild(document.createTextNode(text.slice(last)));
    }

    function timeNow() {
      return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }

    function bubble(role, text, metaParts) {
      var row = document.createElement("div");
      row.className = "chat__msg chat__msg--" + role;
      var body = document.createElement("div");
      body.className = "chat__bubble";
      var p = document.createElement("p");
      p.className = "chat__text";
      if (role === "assistant") linkifyInto(p, text);
      else p.textContent = text;
      body.appendChild(p);
      row.appendChild(body);

      var meta = document.createElement("div");
      meta.className = "chat__meta";
      metaParts.forEach(function (part) {
        var span = document.createElement("span");
        if (part.badge) span.className = "badge " + part.badge;
        span.textContent = part.text;
        meta.appendChild(span);
      });
      row.appendChild(meta);
      return row;
    }

    function errorRow(text) {
      var row = document.createElement("div");
      row.className = "chat__msg chat__msg--error";
      row.setAttribute("role", "alert");
      var body = document.createElement("div");
      body.className = "chat__bubble chat__bubble--error";
      body.textContent = text;
      row.appendChild(body);
      return row;
    }

    function thinkingRow() {
      var row = document.createElement("div");
      row.className = "chat__msg chat__msg--assistant chat__msg--thinking";
      row.setAttribute("aria-live", "polite");
      var body = document.createElement("div");
      body.className = "chat__bubble chat__bubble--thinking";
      var dots = document.createElement("span");
      dots.className = "chat__dots";
      dots.appendChild(document.createElement("span"));
      dots.appendChild(document.createElement("span"));
      dots.appendChild(document.createElement("span"));
      body.appendChild(dots);
      row.appendChild(body);
      return row;
    }

    function resetBusy() {
      form.dataset.busyOn = "";
      textarea.disabled = false;
      if (button) {
        button.disabled = false;
        if (button.dataset.busyLabel) button.textContent = button.dataset.busyLabel;
      }
    }

    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (form.dataset.chatBusy === "1") return;
      var text = textarea.value.trim();
      if (!text) return;
      // FormData — СТРОГО до disabled: вимкнені поля не серіалізуються
      // (стандарт HTML), і POST полетить без message → 422. Знайдено лише
      // в живому браузері 2026-08-07 — curl і TestClient ліплять тіло самі
      // й повз розмітку, тому жоден бекенд-тест цього не бачить.
      var body = new FormData(form);
      form.dataset.chatBusy = "1";
      textarea.disabled = true;

      empty.hidden = true;
      list.hidden = false;
      list.appendChild(bubble("user", text, [
        { text: form.dataset.who || "" },
        { text: timeNow() },
      ]));
      var pending = thinkingRow();
      list.appendChild(pending);
      scrollDown();

      fetch("/chat/send", {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-Requested-With": "fetch" },
        body: body,
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          pending.remove();
          if (data && data.ok) {
            var meta = [];
            if (data.tier === "stub") meta.push({ text: form.dataset.stubChip || "", badge: "b-neutral" });
            if (data.model) meta.push({ text: data.model, badge: "b-info" });
            meta.push({ text: timeNow() });
            list.appendChild(bubble("assistant", data.reply || "", meta));
          } else {
            list.appendChild(errorRow((data && data.error) || form.dataset.error));
          }
        })
        .catch(function () {
          pending.remove();
          list.appendChild(errorRow(form.dataset.error));
        })
        .then(function () {
          textarea.value = "";
          resetBusy();
          autoGrow();
          form.dataset.chatBusy = "";
          scrollDown();
        });
    });

    textarea.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && !ev.shiftKey) {
        ev.preventDefault();
        if (typeof form.requestSubmit === "function") form.requestSubmit();
        else form.dispatchEvent(new Event("submit", { cancelable: true }));
      }
    });

    /* Розділ 3 редизайну — підпис випадаючого списку форумів: без JS
       summary лишається статичним "All forums" (чекбокси все одно рахуються
       на бекенді), з JS показує "N selected" після вибору — той самий
       принцип data-* перекладу, що й data-busy/data-error вище: англійський
       чи український рядок приходить із розмітки, JS лише підставляє {n}. */
    var scope = form.querySelector(".chat__scope");
    if (scope) {
      var scopeSummary = scope.querySelector("summary");
      var scopeAllLabel = scopeSummary.textContent;
      var scopeTpl = scope.dataset.selectedTpl || "{n}";
      var scopeBoxes = scope.querySelectorAll('input[name="forums"]');
      var updateScopeSummary = function () {
        var n = 0;
        scopeBoxes.forEach(function (b) { if (b.checked) n++; });
        scopeSummary.textContent = n ? scopeTpl.replace("{n}", n) : scopeAllLabel;
      };
      scopeBoxes.forEach(function (b) { b.addEventListener("change", updateScopeSummary); });
    }

    scrollDown();
  })();

  /* ── 8. Спливаючі ⋯-меню (.menu) ────────────────────────────────────────
     Генеричний компонент (app.css, розділ 9) — перший споживач AI-чат, далі
     піде по всій платформі. Без JS <details class="menu"> просто закривається
     повторним кліком по summary — прийнятний degrade. Тут лише зручність:
     клік поза відкритим меню чи Escape закриває його, а відкриття одного
     закриває решту (типова поведінка попапів, а не купа одночасно відкритих
     меню). */
  (function popupMenus() {
    function openMenus() {
      return document.querySelectorAll("details.menu[open]");
    }
    document.addEventListener("click", function (ev) {
      openMenus().forEach(function (m) {
        if (!m.contains(ev.target)) m.removeAttribute("open");
      });
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") openMenus().forEach(function (m) { m.removeAttribute("open"); });
    });
    document.querySelectorAll("details.menu").forEach(function (menu) {
      menu.addEventListener("toggle", function () {
        if (!menu.open) return;
        openMenus().forEach(function (other) {
          if (other !== menu) other.removeAttribute("open");
        });
      });
    });
  })();
})();
