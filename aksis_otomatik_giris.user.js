// ==UserScript==
// @name         Sinav Takip - Aksis Otomatik Giris
// @namespace    sinav-takip
// @version      1.1
// @description  sinav_takip.py oturum dusunce actigi pencerede girisi doldurur; Cloudflare tikini SEN isaretlersin, sonra cookie'leri script'e verir.
// @match        https://aksis.iuc.edu.tr/*
// @match        https://teknokta.iuc.edu.tr/Account/Login*
// @match        https://obs.iuc.edu.tr/*
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_deleteValue
// @grant        GM_xmlhttpRequest
// @grant        GM_cookie
// @grant        window.close
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==

(function () {
  "use strict";

  const SERVER = "http://127.0.0.1:47615";
  const ACTIVE_MS = 10 * 60 * 1000;   // Python'un bekleme suresiyle ayni
  const TURNSTILE_WAIT_MS = 20 * 1000; // tik gelmezse bu kadar sonra yine de Giris'e bas
  const MAX_ATTEMPTS = 3;

  const log = (...a) => console.log("[sinav-takip]", ...a);

  // Sadece sinav_takip.py'nin actigi pencerede calis (normal gezinmene karismaz)
  const hashKey = (location.hash.match(/sinavtakip=([\w-]+)/) || [])[1];
  if (hashKey) {
    GM_setValue("st_key", hashKey);
    GM_setValue("st_time", Date.now());
    GM_setValue("st_attempts", 0);
    history.replaceState(null, "", location.pathname + location.search);
  }
  const key = GM_getValue("st_key", "");
  const active = key && Date.now() - GM_getValue("st_time", 0) < ACTIVE_MS;
  if (!active) return;

  function finish() {
    GM_deleteValue("st_key");
    GM_deleteValue("st_time");
    GM_deleteValue("st_attempts");
    setTimeout(() => window.close(), 500);
  }

  function request(method, path, body) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method, url: SERVER + path,
        headers: { "X-Sinavtakip-Key": key, "Content-Type": "application/json" },
        data: body ? JSON.stringify(body) : undefined,
        onload: (r) => resolve({ status: r.status, json: (() => { try { return JSON.parse(r.responseText); } catch (e) { return {}; } })() }),
        onerror: reject,
      });
    });
  }

  function setInput(el, value) {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function listCookies(details) {
    return new Promise((resolve) => {
      if (typeof GM_cookie === "undefined" || !GM_cookie.list) {
        log("GM_cookie bu Tampermonkey surumunde yok/kapali");
        return resolve([]);
      }
      try {
        GM_cookie.list(details, (cookies, error) => {
          if (error) log("cookie okunamadi", JSON.stringify(details), "HATA:", error);
          log("okunan", JSON.stringify(details), "->", (cookies || []).map((c) => c.name + (c.httpOnly ? "(httpOnly)" : "")));
          resolve(cookies || []);
        });
      } catch (e) {
        log("GM_cookie.list istisna:", e && e.message ? e.message : e);
        resolve([]);
      }
    });
  }

  async function sendCookies() {
    log("Tampermonkey", (typeof GM_info !== "undefined" && GM_info.version) || "?",
        "| document.cookie isimleri:", document.cookie.split(";").map((c) => c.split("=")[0].trim()));
    const all = {};
    const queries = [
      { url: "https://aksis.iuc.edu.tr/" }, { url: "https://obs.iuc.edu.tr/" },
      { domain: "iuc.edu.tr" }, { domain: ".iuc.edu.tr" },
    ];
    for (const q of queries) {
      for (const c of await listCookies(q)) all[c.name] = { name: c.name, value: c.value };
    }
    const res = await request("POST", "/cookie", { cookies: Object.values(all) });
    log("cookie gonderildi:", res.status, res.json);
    if (res.status === 200) return finish();
    if (res.status === 422 && location.hostname !== "obs.iuc.edu.tr") {
      location.href = "https://obs.iuc.edu.tr/"; // OBS cookie'si OBS'ye girince olusuyor
    }
  }

  // 1) Karsilama ekrani: "Oturum ac"
  if (location.hostname === "aksis.iuc.edu.tr" && location.pathname.startsWith("/giris/main")) {
    const btn = document.querySelector('a[href="/giris/idslogin"]');
    log("Oturum ac'a basiliyor");
    if (btn) btn.click(); else location.href = "/giris/idslogin";
    return;
  }

  // 2) Kimlik sunucusu giris formu
  if (location.hostname === "teknokta.iuc.edu.tr") {
    const attempts = GM_getValue("st_attempts", 0);
    if (attempts >= MAX_ATTEMPTS) { log("Deneme hakki doldu, elle giris yap."); return; }
    GM_setValue("st_attempts", attempts + 1);

    request("GET", "/bilgiler").then(({ status, json }) => {
      if (status !== 200) { log("Bilgiler alinamadi:", status); return; }
      const user = document.querySelector('input[name="Username"]');
      const pass = document.querySelector('input[name="Password"]');
      const remember = document.querySelector('input[type="checkbox"][name="RememberLogin"]');
      const submit = document.querySelector('button[type="submit"][value="login"]') ||
                     document.querySelector('form button[type="submit"]');
      if (!user || !pass || !submit) { log("Form alanlari bulunamadi"); return; }
      setInput(user, json.username);
      setInput(pass, json.password);
      if (remember && !remember.checked) remember.click();

      log("Cloudflare dogrulamasini onaylamani bekliyorum...");
      const started = Date.now();
      const timer = setInterval(() => {
        const token = document.querySelector('input[name="cf-turnstile-response"]');
        const verified = token && token.value;
        if (verified || Date.now() - started > TURNSTILE_WAIT_MS) {
          clearInterval(timer);
          log(verified ? "Dogrulama tamam, Giris'e basiliyor" : "20 sn doldu, Giris'e basiliyor");
          submit.click();
        }
      }, 500);
    }).catch((e) => log("Yerel sunucuya ulasilamadi (sinav_takip.py calisiyor mu?)", e));
    return;
  }

  // 3) Giris sonrasi Aksis ana sayfasi veya OBS: cookie'leri al, gonder, kapat
  if ((location.hostname === "aksis.iuc.edu.tr" && !location.pathname.startsWith("/giris")) ||
      location.hostname === "obs.iuc.edu.tr") {
    sendCookies().catch((e) => log("Cookie gonderilemedi", e));
  }
})();
