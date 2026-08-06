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
  document.querySelectorAll("[data-confirm]").forEach(function (el) {
    var evt = el.tagName === "FORM" ? "submit" : "click";
    el.addEventListener(evt, function (ev) {
      if (!window.confirm(el.dataset.confirm)) { ev.preventDefault(); ev.stopPropagation(); }
    });
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
})();
