
/* Shared by index.html, about.html and item.html:
   helpers, header, theme and M-Pesa payment dialog.
*/
(function () {
  "use strict";

  var $ = function (selector) {
    return document.querySelector(selector);
  };

  var IH = window.IH = {
    cfg: { whatsapp: "", delivery_fee: 0 },
    me: null
  };

  IH.esc = function (value) {
    return String(value).replace(/[&<>"']/g, function (character) {
      return {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[character];
    });
  };

  IH.ksh = function (amount) {
    return "KSh " +
      Math.round(Number(amount) || 0).toLocaleString("en-KE");
  };

  IH.api = function (path, options) {
    options = options || {};

    var headers = Object.assign(
      { "Content-Type": "application/json" },
      options.headers || {}
    );

    options.headers = headers;

    if (options.body && typeof options.body !== "string") {
      options.body = JSON.stringify(options.body);
    }

    options.credentials = "same-origin";

    return fetch(path, options).then(function (response) {
      return response.json().catch(function () {
        return {};
      }).then(function (data) {
        if (!response.ok) {
          throw new Error(data.error || "Something went wrong.");
        }
        return data;
      });
    });
  };

  var colors = {
    Sneakers: "#10232E",
    Boots: "#3D4D55",
    Sandals: "#B58863",
    Formal: "#181818",
    Sports: "#A79E9C",
    Heels: "#D3C3B9"
  };

  IH.shoeSvg = function (type) {
    return '<svg viewBox="0 0 120 70" role="img" aria-label="' +
      IH.esc(type || "Shoes") +
      '"><path d="M8 50 L8 30 Q8 24 14 24 L30 24 Q38 36 52 36 L60 28 Q64 24 70 28 L104 42 Q114 46 112 54 L112 58 L8 58 Z" fill="' +
      (colors[type] || "#10232E") +
      '"/><rect x="8" y="54" width="104" height="8" rx="4" fill="#181818" opacity=".85"/></svg>';
  };

  IH.wa = function (item) {
    if (!IH.cfg.whatsapp) return "";

    var message =
      "Hi Ianoh_hubs, is the " + item.name +
      " (size " + item.size + ") still available?";

    return '<a class="btn alt" style="color:var(--ink)" target="_blank" rel="noopener" href="https://wa.me/' +
      encodeURIComponent(IH.cfg.whatsapp) +
      '?text=' + encodeURIComponent(message) +
      '">Ask on WhatsApp</a>';
  };

  /* Theme management */

  function applySiteTheme(theme) {
    var next = theme === "dark" ? "dark" : "light";

    document.documentElement.setAttribute("data-theme", next);
    document.documentElement.style.colorScheme = next;

    var button = $("#theme");

    if (button) {
      button.textContent = next === "dark" ? "☼" : "◐";
      button.setAttribute(
        "aria-label",
        next === "dark" ? "Switch to light theme" : "Switch to dark theme"
      );
      button.setAttribute(
        "title",
        next === "dark" ? "Switch to light theme" : "Switch to dark theme"
      );
    }
  }

  try {
    applySiteTheme(
      localStorage.getItem("ianoh-theme") ||
      document.documentElement.getAttribute("data-theme")
    );
  } catch (error) {
    applySiteTheme(
      document.documentElement.getAttribute("data-theme")
    );
  }

  var themeButton = $("#theme");

  if (themeButton) {
    themeButton.addEventListener("click", function () {
      var next =
        document.documentElement.getAttribute("data-theme") === "dark"
          ? "light"
          : "dark";

      applySiteTheme(next);

      try {
        localStorage.setItem("ianoh-theme", next);
      } catch (error) {}
    });
  }

  window.addEventListener("storage", function (event) {
    if (
      event.key === "ianoh-theme" &&
      (event.newValue === "dark" || event.newValue === "light")
    ) {
      applySiteTheme(event.newValue);
    }
  });

  /* Shared session and navigation */

  IH.ready = Promise.all([
    IH.api("/api/config")
      .then(function (config) {
        IH.cfg = Object.assign(IH.cfg, config);
      })
      .catch(function () {}),

    IH.api("/api/me")
      .then(function (data) {
        IH.me = data.user || null;
      })
      .catch(function () {
        IH.me = null;
      })
  ]).then(function () {
    var me = IH.me;
    var who = $("#who");
    var orders = $("#navOrders");
    var admin = $("#navAdmin");
    var auth = $("#navAuth");

    if (who) {
      who.textContent = me
        ? "Hi, " + me.name + (me.role === "admin" ? " (admin)" : "")
        : "";
    }

    if (orders) orders.hidden = !me;

    if (admin) {
      admin.hidden = !(me && me.role === "admin");
    }

    if (auth) {
      auth.textContent = me ? "Log out" : "Log in";

      auth.onclick = function () {
        if (!IH.me) {
          location.href = "/";
          return;
        }

        auth.disabled = true;

        IH.api("/api/logout", { method: "POST" })
          .then(function () {
            location.href = "/";
          })
          .catch(function () {
            auth.disabled = false;
            auth.textContent = "Log out";
          });
      };
    }
  });

  /* M-Pesa payment dialog */

  var dlg = null;
  var shoe = null;
  var poll = null;
  var after = null;

  function msg(message, className) {
    var element = $("#payMsg");

    if (!element) return;

    element.className = className;
    element.textContent = message;
  }

  function fee() {
    return $("#payMethod").value === "delivery"
      ? (Number(IH.cfg.delivery_fee) || 0)
      : 0;
  }

  function total() {
    $("#payAddrF").hidden =
      $("#payMethod").value !== "delivery";

    $("#payInfo").textContent =
      shoe.name + " · " + IH.ksh(shoe.final) +
      (fee()
        ? " + delivery " + IH.ksh(fee()) +
          " = " + IH.ksh(Number(shoe.final) + fee())
        : "");
  }

  function track(orderId, attempts) {
    clearTimeout(poll);

    if (attempts > 30) {
      msg(
        "Still waiting. If M-Pesa charged you, the order will update shortly; contact us with your receipt.",
        "hint"
      );

      $("#payGo").disabled = false;
      return;
    }

    poll = setTimeout(function () {
      IH.api("/api/orders/" + encodeURIComponent(orderId))
        .then(function (order) {
          if (order.status === "paid") {
            msg(
              "Paid. M-Pesa receipt " +
              (order.receipt || "") +
              ". Thank you!",
              "ok"
            );

            if (after) after();

          } else if (order.status === "pending") {
            track(orderId, attempts + 1);

          } else {
            msg(
              order.note || "Payment was not completed.",
              "err"
            );

            $("#payGo").disabled = false;
          }
        })
        .catch(function () {
          track(orderId, attempts + 1);
        });

    }, 3000);
  }

  function build() {
    dlg = document.createElement("dialog");
dlg.id = "payDialog";

    dlg.innerHTML =
      '<h2>Pay with M-Pesa</h2>' +
      '<p id="payInfo" class="meta"></p>' +

      '<div class="field">' +
        '<label for="payMethod">Get your shoes</label>' +
        '<select id="payMethod">' +
          '<option value="pickup">Pickup (free)</option>' +
          '<option value="delivery">Delivery</option>' +
        '</select>' +
      '</div>' +

      '<div class="field" id="payAddrF" hidden>' +
        '<label for="payAddr">Delivery address</label>' +
        '<input id="payAddr" autocomplete="street-address" placeholder="Estate, street, town">' +
      '</div>' +

      '<div class="field">' +
        '<label for="payPhone">Safaricom number</label>' +
        '<input id="payPhone" type="tel" inputmode="tel" placeholder="0712 345 678" autocomplete="tel">' +
      '</div>' +

      '<div id="payMsg" role="status" class="err"></div>' +

      '<button class="btn tag" id="payGo" type="button">' +
        'Send M-Pesa prompt' +
      '</button> ' +

      '<button class="btn alt" id="payClose" type="button" style="color:var(--ink)">' +
        'Close' +
      '</button>';

    document.body.appendChild(dlg);

    $("#payMethod").onchange = total;

    $("#payClose").onclick = function () {
      clearTimeout(poll);
      dlg.close();
    };

    $("#payGo").onclick = function () {
      var button = $("#payGo");

      button.disabled = true;
      msg("", "err");

      IH.api("/api/pay", {
        method: "POST",
        body: {
          shoe_id: shoe.id,
          phone: $("#payPhone").value,
          method: $("#payMethod").value,
          address: $("#payAddr").value
        }
      }).then(function (result) {
        msg(
          "Check your phone and enter your M-Pesa PIN.",
          "hint"
        );

        track(result.order_id, 0);

      }).catch(function (error) {
        msg(error.message, "err");
        button.disabled = false;
      });
    };
  }

  /* Public checkout function used by item.html and index.html */

  IH.openPay = function (item, onPaid) {
    if (!dlg) build();

    shoe = item;
    after = onPaid;

    $("#payMethod").value = "pickup";
    $("#payPhone").value = "";
    $("#payAddr").value = "";

    total();
    msg("", "err");

    $("#payGo").disabled = false;

    dlg.showModal();
  };

  /* Mobile navigation */

  var toggle = document.getElementById("menuToggle");
  var links = document.getElementById("navLinks");

  if (toggle && links) {
    toggle.addEventListener("click", function () {
      var open = links.classList.toggle("open");

      toggle.setAttribute("aria-expanded", String(open));
    });
  }

})();
