/*
 * STAR Rennes – carte « Mes trajets » pour Home Assistant.
 * Chargée automatiquement par l'intégration star_rennes_live.
 *
 *   type: custom:star-trajets-card
 *   title: Mes trajets                         # facultatif
 *   perturbations: sensor.star_perturbations   # facultatif
 *   show_distributeurs: false                  # facultatif
 *   entities:
 *     - sensor.becquerel_za_saint_sulpice      # bus
 *     - sensor.station_italie                  # station de métro (+ prochains passages)
 *     - entity: sensor.atalante_grand_quartier
 *       name: Atalante                         # nom affiché (facultatif)
 */

(() => {
const CARD_VERSION = "1.10.1";

// Affluence publiée par STAR pour chaque rame (couleur de remplissage)
const CROWD_LEVELS = {
  "vert clair": [1, "ok", "Peu de monde"],
  "vert fonce": [2, "ok", "Affluence moyenne"],
  "orange": [3, "warn", "Forte affluence"],
  "rouge": [3, "bad", "Très forte affluence"],
};

// Couleur de jauge : vélos disponibles, puis taux d'occupation d'un parc relais
function bikeLevel(bikes, total) {
  if (!total) return "neutral";
  if (bikes === 0) return "bad";
  return bikes / total < 0.2 ? "warn" : "ok";
}

function parkingLevel(occupation) {
  if (occupation >= 0.9) return "bad";
  return occupation >= 0.7 ? "warn" : "ok";
}

function crowdHtml(fill) {
  if (!fill) return "";
  const key = String(fill).normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().trim();
  const [level, tone, label] = CROWD_LEVELS[key] || [0, "neutral", "Affluence"];
  const bars = [1, 2, 3].map((n) => `<i class="${n <= level ? "on" : ""}"></i>`).join("");
  return `<span class="crowd ${tone}" title="${esc(label)} (STAR : ${esc(fill)})">${bars}</span>`;
}

const LINE_FALLBACK_COLORS = { A: "#e2001a", B: "#00a651" };

const CATEGORY_LABELS = {
  ascenseurs: ["Ascenseur", "Ascenseurs"],
  escaliers: ["Escalator", "Escalators"],
  distributeurs: ["Distributeur", "Distributeurs"],
};

// « 3 rames », « 1 vélo » : pluriel sans gabarit imbriqué
const plural = (n, word) => (n > 1 ? word + "s" : word);

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);

class StarTrajetsCard extends HTMLElement {
  setConfig(config) {
    if (!config || !Array.isArray(config.entities)) {
      throw new Error("star-trajets-card : la liste « entities » est obligatoire");
    }
    this._config = {
      title: "Mes trajets",
      show_distributeurs: false,
      ...config,
      entities: config.entities.map((e) => (typeof e === "string" ? { entity: e } : e)),
    };
    this._signature = null;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  connectedCallback() {
    // Les minutes défilent même sans nouvelle donnée de Home Assistant
    this._timer = setInterval(() => this._render(), 20000);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
  }

  getCardSize() {
    return 2 + (this._config?.entities.length || 0) * 2;
  }

  static getStubConfig(hass) {
    const ids = Object.keys(hass?.states || {});
    const pick = (test) => ids.filter((id) => test(hass.states[id].attributes || {}));
    return {
      perturbations: pick((a) => Array.isArray(a.perturbations))[0],
      entities: [
        ...pick((a) => Array.isArray(a.departs)).slice(0, 2),
        ...pick((a) => Array.isArray(a.directions)).slice(0, 1),
      ],
    };
  }

  // ---------------------------------------------------------------- données
  _state(entityId) {
    return entityId ? this._hass?.states?.[entityId] : undefined;
  }

  _perturbations() {
    const st = this._state(this._config.perturbations);
    const metro = Array.isArray(st?.attributes?.perturbations) ? st.attributes.perturbations : [];
    return [...this._busSuspensions(), ...metro];
  }

  // Lignes de bus suspendues (alertes des bus affichés), regroupées par alerte
  _busSuspensions() {
    const groups = new Map();
    for (const conf of this._config.entities) {
      const a = this._state(conf.entity)?.attributes;
      for (const alert of a?.alertes || []) {
        if (alert.type !== "suspension") continue;
        const key = `${alert.titre}|${alert.description}`;
        const group = groups.get(key) || { ...alert, mine: new Set() };
        group.mine.add(a.ligne);
        groups.set(key, group);
      }
    }
    return [...groups.values()].map((g) => ({
      type: "bus", niveau: "critique", busLines: [...g.mine],
      titre: `${g.titre} · bus à l'arrêt`,
      detail: g.description,
    }));
  }

  _busSuspended(a) {
    return (a.alertes || []).some((alert) => alert.type === "suspension");
  }

  _lineIcon(code) {
    // Icône a / b fournie par les capteurs de ligne (ou de station) de l'intégration
    const states = Object.values(this._hass?.states || {});
    const line = states.find(
      (s) => s.attributes?.line_code === code && "perturbation" in s.attributes && s.attributes.entity_picture
    );
    if (line) return line.attributes.entity_picture;
    const station = states.find(
      (s) => s.attributes?.line_code === code && Array.isArray(s.attributes.directions) && s.attributes.entity_picture
    );
    return station?.attributes.entity_picture;
  }

  _lineBadge(code, size = 22) {
    const icon = this._lineIcon(code);
    if (icon) {
      return `<img class="line-icon" style="width:${size}px;height:${size}px" src="${esc(icon)}" alt="Ligne ${esc(code)}">`;
    }
    const color = LINE_FALLBACK_COLORS[code] || "var(--primary-color)";
    return `<span class="line-letter" style="width:${size}px;height:${size}px;background:${color}">${esc(String(code).toLowerCase())}</span>`;
  }

  // ------------------------------------------------------------- formatage
  _upcoming(attrs) {
    const now = Date.now();
    const raw = Array.isArray(attrs.departs) && attrs.departs.length
      ? attrs.departs.map((d) => ({ t: Date.parse(d.depart), rt: d.temps_reel }))
      : (attrs.departures || []).map((d) => ({ t: Date.parse(d), rt: null }));
    return raw.filter((d) => !Number.isNaN(d.t) && d.t > now - 30000).sort((a, b) => a.t - b.t);
  }

  _fmtWait(t) {
    const minutes = Math.floor((t - Date.now()) / 60000);
    if (minutes < 1) return "< 1 min";
    if (minutes >= 60) {
      return new Date(t).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
    }
    return `${minutes} min`;
  }

  // ------------------------------------------------------------- rendu
  _render() {
    if (!this.shadowRoot || !this._config || !this._hass) return;

    // Évite de reconstruire le DOM si rien n'a changé
    const ids = [this._config.perturbations, ...this._config.entities.map((e) => e.entity)];
    const signature = ids.map((id) => this._state(id)?.last_updated || "-").join("|")
      + `|${Math.floor(Date.now() / 20000)}`;
    if (signature === this._signature) return;
    this._signature = signature;

    const perturbations = this._perturbations();
    const rows = this._config.entities.map((conf) => this._renderEntity(conf)).join("");
    const realtime = this._config.entities.some((conf) => this._state(conf.entity)?.attributes?.temps_reel === true);

    let status = "";
    if (perturbations.length) {
      const n = perturbations.length;
      status = `<span class="pill warn">${n} ${plural(n, "perturbation")}</span>`;
    } else if (realtime) {
      status = `<span class="muted small">temps réel</span>`;
    }

    this.shadowRoot.innerHTML = `
      <style>${StarTrajetsCard.styles}</style>
      <ha-card>
        <div class="header">
          <span class="header-icon"><ha-icon icon="mdi:bus"></ha-icon></span>
          <span class="title">${esc(this._config.title)}</span>
          <span class="spacer"></span>
          ${status}
        </div>
        <div class="content">
          ${perturbations.map((p) => this._renderPerturbation(p)).join("")}
          ${rows}
        </div>
      </ha-card>`;
  }

  _renderPerturbation(p) {
    const critical = p.niveau === "critique";
    const badge = this._perturbationBadge(p);
    return `
      <div class="perturbation ${critical ? "critical" : "warning"}">
        ${badge}
        <div class="text">
          <div class="strong">${esc(p.titre)}</div>
          ${p.detail ? `<div class="small soft">${esc(p.detail)}</div>` : ""}
        </div>
      </div>`;
  }

  _perturbationBadge(p) {
    if (p.type === "ligne" && p.ligne) return this._lineBadge(p.ligne);
    if (p.type === "bus") {
      const badges = p.busLines.map((l) => '<span class="bus-badge alert">' + esc(l) + "</span>").join("");
      return `<span class="bus-badges">${badges}</span>`;
    }
    return `<span class="alert-icon"><ha-icon icon="mdi:alert-outline"></ha-icon></span>`;
  }

  _renderEntity(conf) {
    const st = this._state(conf.entity);
    if (!st) {
      return `<div class="tile muted small">Entité introuvable : ${esc(conf.entity)}</div>`;
    }
    const a = st.attributes || {};
    if (Array.isArray(a.directions)) return this._renderStation(conf, st);
    if (Array.isArray(a.departs) || Array.isArray(a.departures)) {
      // Ligne à l'arrêt : alerte en tête, encart réduit et grisé pour garder le repère
      return this._busSuspended(a) ? this._renderBusSuspended(conf, st) : this._renderBus(conf, st);
    }
    if ("perturbation" in a && a.line_code) return this._renderLine(conf, st);
    if ("velos_disponibles" in a) return this._renderBike(conf, st);
    if ("occupation_pct" in a || ("capacite" in a && "places_libres" in a)) return this._renderParking(conf, st);
    return `<div class="tile small">${esc(conf.name || a.friendly_name || conf.entity)} : ${esc(st.state)}</div>`;
  }

  _renderBus(conf, st) {
    const a = st.attributes;
    const unavailable = st.state === "unavailable";
    const next = unavailable ? [] : this._upcoming(a);
    const line = a.ligne || "";
    const stop = conf.name || a.stop_name || a.friendly_name;

    let right;
    if (unavailable) {
      right = `<div class="small muted">indisponible</div>`;
    } else if (!next.length) {
      right = `<div class="wait muted">—</div><div class="small muted">aucun passage</div>`;
    } else {
      const theoretical = next[0].rt === false;
      const then = next[1] ? '<div class="small soft">puis ' + esc(this._fmtWait(next[1].t)) + "</div>" : "";
      right = `
        <div class="wait ${theoretical ? "soft" : ""}" title="${theoretical ? "Horaire théorique" : "Temps réel"}">${esc(this._fmtWait(next[0].t))}</div>
        ${then}`;
    }

    // Report d'arrêt, déviation… : dans l'encart, seulement pour le sens concerné
    const infos = (a.alertes || []).filter((x) => x.type !== "suspension" && x.ce_sens !== false);
    const infoHtml = infos.map((x) => `
        <div class="bus-info">
          <ha-icon icon="mdi:sign-direction"></ha-icon>
          <div><span class="strong">${esc(x.titre)}</span> · ${esc(x.description)}</div>
        </div>`).join("");
    return `
      <div class="tile bus-tile">
        <div class="bus">
          <span class="bus-badge">${esc(line)}</span>
          <div class="text">
            <div class="strong">${esc(stop)}</div>
            <div class="small soft">vers ${esc(a.destination)}</div>
          </div>
          <div class="right">${right}</div>
        </div>
        ${infoHtml}
      </div>`;
  }

  _renderBusSuspended(conf, st) {
    const a = st.attributes;
    const stop = conf.name || a.stop_name || a.friendly_name;
    return `
      <div class="tile bus suspended" title="vers ${esc(a.destination)}">
        <span class="bus-badge">${esc(a.ligne || "")}</span>
        <div class="text"><div class="strong">${esc(stop)} <span class="small soft">vers ${esc(a.destination)}</span></div></div>
        <div class="right small nowrap">ligne à l'arrêt</div>
      </div>`;
  }

  _gauge(ratio, level) {
    const pct = Math.max(0, Math.min(100, Math.round(ratio * 100)));
    return `<div class="gauge"><span class="gauge-fill ${level}" style="width:${pct}%"></span></div>`;
  }

  // Logo fourni par l'intégration (entity_picture), sinon pastille de secours
  _kindIcon(st, fallback) {
    const pic = st.attributes?.entity_picture;
    return pic ? `<img class="line-icon" style="width:22px;height:22px" src="${esc(pic)}" alt="">` : fallback;
  }

  _renderBike(conf, st) {
    const a = st.attributes;
    const name = conf.name || a.friendly_name || conf.entity;
    const bikes = Number(a.velos_disponibles);
    const docks = Number(a.places_libres);
    const known = Number.isFinite(bikes) && Number.isFinite(docks);
    const total = known ? bikes + docks : 0;
    const fallback = `<span class="kind-badge"><ha-icon icon="mdi:bicycle"></ha-icon></span>`;
    const right = known
      ? `<div class="wait">${bikes}<span class="unit"> ${plural(bikes, "vélo")}</span></div>
               <div class="small soft">${docks} ${plural(docks, "place")} ${plural(docks, "libre")}</div>`
      : `<div class="small muted">—</div>`;

    return `
      <div class="tile bike">
        <div class="text">
          <div class="station-head">${this._kindIcon(st, fallback)}<span class="strong">${esc(name)}</span></div>
          ${this._bikeStatus(st)}
          ${known && total ? this._gauge(bikes / total, bikeLevel(bikes, total)) : ""}
        </div>
        <div class="right">
          ${right}
        </div>
      </div>`;
  }

  _bikeStatus(st) {
    if (st.state === "Ouvert") return `<div class="small soft">Le vélo STAR</div>`;
    const tone = st.state === "Maintenance" || st.state === "En panne" ? "warn" : "bad";
    const status = st.state === "unavailable" ? "Indisponible" : st.state;
    return `<div class="small status-${tone}">${esc(status)}</div>`;
  }

  _renderParking(conf, st) {
    const a = st.attributes;
    let name = conf.name || a.friendly_name || conf.entity;
    if (!conf.name) name = String(name).replace(/^Parc relais\s+/i, "");
    const free = Number(a.places_libres);
    const capacity = Number(a.capacite);
    const known = Number.isFinite(free) && Number.isFinite(capacity) && capacity > 0;
    const occupation = known ? (capacity - free) / capacity : null;
    const fallback = `<span class="kind-badge parking-badge">P</span>`;
    const right = known
      ? `<div class="wait">${free}<span class="unit"> ${plural(free, "place")}</span></div>
               <div class="small soft">sur ${capacity}</div>`
      : `<div class="small muted">—</div>`;

    return `
      <div class="tile parking">
        <div class="text">
          <div class="station-head">${this._kindIcon(st, fallback)}<span class="strong">${esc(name)}</span></div>
          ${this._parkingStatus(st, occupation)}
          ${known ? this._gauge(occupation, parkingLevel(occupation)) : ""}
        </div>
        <div class="right">
          ${right}
        </div>
      </div>`;
  }

  _parkingStatus(st, occupation) {
    if (st.state !== "Ouvert") {
      return `<div class="small status-bad">${esc(st.state === "unavailable" ? "Indisponible" : "Fermé")}</div>`;
    }
    const rate = occupation === null ? "" : " · occupé à " + Math.round(occupation * 100) + " %";
    return `<div class="small soft">Parc relais${rate}</div>`;
  }

  _renderLine(conf, st) {
    const a = st.attributes;
    const ok = !a.perturbation;
    return `
      <div class="tile line">
        ${this._lineBadge(a.line_code)}
        <div class="text strong">${esc(conf.name || `Ligne ${String(a.line_code).toLowerCase()}`)}</div>
        <span class="pill ${ok ? "ok" : "bad"}">${esc(ok ? "Normal" : st.state)}</span>
      </div>`;
  }

  _renderStation(conf, st) {
    const a = st.attributes;
    const name = conf.name || a.station_name || a.friendly_name;
    const codes = String(a.line_code || "").split("/").filter(Boolean);
    const badges = codes.map((c) => this._lineBadge(c)).join("");

    let pill;
    if (st.state === "unavailable") pill = `<span class="pill neutral">Indisponible</span>`;
    else if (a.fermee) pill = `<span class="pill bad">Fermée</span>`;
    else if ((a.lignes_fermees || []).length) {
      pill = `<span class="pill warn">Fermée ligne ${esc(a.lignes_fermees.map((c) => c.toLowerCase()).join(", "))}</span>`;
    } else if (!st.state || st.state === "N/A" || st.state === "unknown") {
      pill = `<span class="pill neutral">État inconnu</span>`;
    } else pill = `<span class="pill ok">${esc(st.state)}</span>`;

    const categories = ["ascenseurs", "escaliers"];
    if (this._config.show_distributeurs) categories.push("distributeurs");

    // Prochains passages par sens (horaires théoriques GTFS)
    const passages = Array.isArray(a.prochains_passages) ? a.prochains_passages : [];
    const byDirection = {};
    for (const p of passages) byDirection[p.direction] = p;
    let directions = a.directions || [];
    if (!directions.length && passages.length) directions = passages.map((p) => ({ direction: p.direction }));
    const waitHtml = (p) => {
      if (!p) return "";
      const fills = Array.isArray(p.affluence) ? p.affluence : [];
      const next = (p.departs || [])
        .map((d, i) => ({ t: Date.parse(d), fill: fills[i] }))
        .filter((x) => x.t > Date.now() - 30000);
      if (!next.length) return `<div class="dir-wait small muted">aucun passage</div>`;
      return `
        <div class="dir-wait" title="${p.temps_reel ? "Temps réel" : "Horaire théorique (GTFS STAR)"}">
          <span class="wait-sm">${esc(this._fmtWait(next[0].t))}</span>${crowdHtml(next[0].fill)}
          ${next[1] ? `<span class="small soft">puis ${esc(this._fmtWait(next[1].t))}</span>${crowdHtml(next[1].fill)}` : ""}
        </div>`;
    };

    const columns = directions.map((d) => {
      const items = categories
        .filter((cat) => d[cat] && d[cat].total > 0)
        .map((cat) => {
          const { total, hs, hs_noms: names = [] } = d[cat];
          const [one, many] = CATEGORY_LABELS[cat];
          if (!hs) {
            return `<div class="eq ok"><span class="mark">✓</span>${esc(total > 1 ? many : one)}</div>`;
          }
          const label = hs === 1 ? `${one} HS` : `${hs} ${many.toLowerCase()} HS`;
          return `<div class="eq hs" title="${esc(names.join("\n"))}"><span class="mark">✕</span>${esc(label)}</div>`;
        });
      const hasIssue = categories.some((cat) => d[cat] && d[cat].hs > 0);
      return `
        <div class="direction ${hasIssue ? "issue" : ""}">
          <div class="small soft dir-title">Vers ${esc(d.direction)}</div>
          ${waitHtml(byDirection[d.direction])}
          ${items.join("") || ((a.directions || []).length ? `<div class="small muted">Aucun équipement</div>` : "")}
        </div>`;
    });

    return `
      <div class="tile station">
        <div class="station-head">
          <span class="badges">${badges}</span>
          <span class="strong">Station ${esc(name)}</span>
          <span class="spacer"></span>
          ${pill}
        </div>
        ${columns.length ? `<div class="directions">${columns.join("")}</div>` : ""}
      </div>`;
  }
}

StarTrajetsCard.styles = `
  :host {
    --star-tile: rgba(127, 127, 127, 0.10);
    --star-border: rgba(127, 127, 127, 0.22);
    --star-ok: #43a047;
    --star-warn: #f5a623;
    --star-bad: #e53935;
    /* Texte coloré lisible en clair comme en sombre */
    --star-ok-text: color-mix(in srgb, var(--star-ok) 80%, var(--primary-text-color));
    --star-warn-text: color-mix(in srgb, var(--star-warn) 72%, var(--primary-text-color));
    --star-bad-text: color-mix(in srgb, var(--star-bad) 80%, var(--primary-text-color));
  }
  ha-card { padding: 16px; }
  .header { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
  .header-icon {
    display: inline-flex; align-items: center; justify-content: center;
    width: 28px; height: 28px; border-radius: 8px;
    background: rgba(66, 133, 244, 0.18); color: #6ea8ff;
    --mdc-icon-size: 14px;
  }
  .title { font-size: 16px; font-weight: 600; color: var(--primary-text-color); }
  .spacer { flex: 1; }
  .content { display: flex; flex-direction: column; gap: 8px; }
  .text { flex: 1; min-width: 0; }
  .strong { font-weight: 600; font-size: 14px; color: var(--primary-text-color); }
  .small { font-size: 12px; }
  .soft { color: var(--secondary-text-color); }
  .muted { color: var(--disabled-text-color, var(--secondary-text-color)); }
  .text .strong, .text .small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .pill {
    font-size: 11px; font-weight: 600; padding: 3px 9px; border-radius: 999px;
    white-space: nowrap;
  }
  .pill.ok { background: rgba(67, 160, 71, 0.18); color: var(--star-ok-text); }
  .pill.warn { background: rgba(245, 166, 35, 0.18); color: var(--star-warn-text); }
  .pill.bad { background: rgba(229, 57, 53, 0.18); color: var(--star-bad-text); }
  .pill.neutral { background: var(--star-tile); color: var(--secondary-text-color); }

  .perturbation {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 12px; border-radius: 12px; border: 1px solid;
  }
  .perturbation.critical { background: rgba(229, 57, 53, 0.14); border-color: rgba(229, 57, 53, 0.45); }
  .perturbation.warning { background: rgba(245, 166, 35, 0.12); border-color: rgba(245, 166, 35, 0.45); }
  .perturbation.warning .strong { color: var(--star-warn-text); }
  .alert-icon { color: var(--star-warn-text); --mdc-icon-size: 20px; display: inline-flex; }

  .tile { background: var(--star-tile); border-radius: 12px; padding: 10px 12px; }
  .bus, .line { display: flex; align-items: center; gap: 12px; }
  .bus-badge {
    min-width: 30px; text-align: center; font-size: 12px; font-weight: 700;
    padding: 4px 6px; border-radius: 6px;
    background: rgba(127, 127, 127, 0.25); color: var(--primary-text-color);
  }
  .bus-badges { display: inline-flex; flex-wrap: wrap; gap: 4px; max-width: 110px; flex: none; }
  .perturbation .small { white-space: normal; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .bus-badge.alert { background: rgba(229, 57, 53, 0.25); }
  .tile.suspended { opacity: 0.55; padding-top: 7px; padding-bottom: 7px; }
  .tile.suspended .strong { font-weight: 500; }
  .bus-info {
    display: flex; gap: 8px; align-items: flex-start; margin-top: 8px; padding: 7px 9px;
    border-radius: 8px; font-size: 12.5px; line-height: 1.35;
    background: rgba(245, 166, 35, 0.12); color: var(--primary-text-color);
    --mdc-icon-size: 16px;
  }
  .bus-info ha-icon { color: var(--star-warn-text); flex: none; margin-top: 1px; }
  .bus-info .strong { color: var(--star-warn-text); }
  .right { text-align: right; }
  .wait { font-size: 20px; font-weight: 700; color: var(--primary-text-color); line-height: 1.1; white-space: nowrap; }
  .wait.soft { color: var(--secondary-text-color); }

  .bike, .parking { display: flex; align-items: center; gap: 12px; }
  .kind-badge {
    width: 22px; height: 22px; border-radius: 50%; flex: none;
    display: inline-flex; align-items: center; justify-content: center;
    background: rgba(127, 127, 127, 0.25); color: var(--primary-text-color);
    --mdc-icon-size: 14px;
  }
  .parking-badge { background: #2b6cd8; color: #fff; font-weight: 800; font-size: 13px; }
  .unit { font-size: 13px; font-weight: 600; color: var(--secondary-text-color); }
  .gauge { height: 4px; border-radius: 2px; background: rgba(127, 127, 127, 0.25); margin-top: 6px; overflow: hidden; max-width: 160px; }
  .gauge-fill { display: block; height: 100%; border-radius: 2px; }
  .gauge-fill.ok { background: var(--star-ok); }
  .gauge-fill.warn { background: var(--star-warn); }
  .gauge-fill.bad { background: var(--star-bad); }
  .gauge-fill.neutral { background: var(--secondary-text-color); }
  .status-warn { color: var(--star-warn-text); }
  .status-bad { color: var(--star-bad-text); }
  .line-icon { border-radius: 50%; object-fit: cover; flex: none; }
  .line-letter {
    display: inline-flex; align-items: center; justify-content: center; flex: none;
    border-radius: 50%; color: #fff; font-weight: 700; font-size: 12px;
  }
  .badges { display: inline-flex; gap: 4px; }

  .station-head { display: flex; align-items: center; gap: 8px; }
  .directions {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
    gap: 8px; margin-top: 10px;
  }
  .direction { border: 1px solid var(--star-border); border-radius: 10px; padding: 8px 10px; }
  .direction.issue { border-color: rgba(245, 166, 35, 0.6); background: rgba(245, 166, 35, 0.10); }
  .dir-title { margin-bottom: 4px; }
  .dir-wait { display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap; margin-bottom: 6px; }
  .crowd { display: inline-flex; align-items: flex-end; gap: 1.5px; height: 11px; margin: 0 6px 0 4px; vertical-align: baseline; }
  .crowd i { display: block; width: 3px; border-radius: 1px; background: rgba(127, 127, 127, 0.35); }
  .crowd i:nth-child(1) { height: 5px; }
  .crowd i:nth-child(2) { height: 8px; }
  .crowd i:nth-child(3) { height: 11px; }
  .crowd.ok i.on { background: var(--star-ok); }
  .crowd.warn i.on { background: var(--star-warn); }
  .crowd.bad i.on { background: var(--star-bad); }
  .wait-sm { font-size: 18px; font-weight: 700; color: var(--primary-text-color); white-space: nowrap; }
  .eq { font-size: 13px; display: flex; gap: 6px; align-items: baseline; color: var(--primary-text-color); padding: 1px 0; }
  .eq .mark { width: 12px; flex: none; }
  .eq.ok .mark { color: var(--star-ok); }
  .eq.hs { color: var(--star-warn-text); }
`;

// Le fichier peut être chargé deux fois (par l'intégration et comme ressource
// de tableau de bord) : on n'enregistre la carte qu'une seule fois.
if (!customElements.get("star-trajets-card")) {
  customElements.define("star-trajets-card", StarTrajetsCard);

  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "star-trajets-card",
    name: "STAR Rennes – Mes trajets",
    description: "Prochains bus, perturbations du métro et équipements de station par direction.",
  });

  console.info(`%c STAR-TRAJETS-CARD %c ${CARD_VERSION} `, "background:#e2001a;color:#fff", "background:#333;color:#fff");
}

/*
 * ===========================================================================
 * Carte « Métro » : schéma horizontal des lignes a et b, rames animées.
 *
 *   type: custom:star-metro-card
 *   entity: sensor.star_metro_rames      # capteur « STAR Métro – Rames »
 *   title: Métro                         # facultatif
 *   lignes: [A, B]                       # facultatif (ordre / filtre)
 *   quais_uniques: [JFK]                 # terminus à quai unique ([] après les travaux)
 * ===========================================================================
 */
// Géométrie du schéma horizontal (px)
const PAD_SIDE = 12; // marge gauche / droite du trait
const TRACK_GAP = 13; // écart entre les deux voies : en haut → , en bas ←
const STAGGER_BELOW = 26; // en dessous de cet écart entre stations, acronymes sur 2 rangs

function findLineSensor(hass, code) {
  return Object.values(hass?.states || {}).find(
    (s) => s.attributes?.line_code === code && "perturbation" in s.attributes
  );
}

function trainPosition(stops, now) {
  // stops : [{ i, arr, dep }] en ms ; retourne l'index (fractionnaire) ou null
  if (!stops.length || now < stops[0].arr || now > stops[stops.length - 1].dep) return null;
  for (let k = 0; k < stops.length; k++) {
    const s = stops[k];
    if (now >= s.arr && now <= s.dep) return s.i;
    const n = stops[k + 1];
    if (n && now > s.dep && now < n.arr) return s.i + ((now - s.dep) / (n.arr - s.dep)) * (n.i - s.i);
  }
  return null;
}

class StarMetroCard extends HTMLElement {
  setConfig(config) {
    if (!config?.entity) {
      throw new Error("star-metro-card : « entity » est obligatoire (capteur « STAR Métro – Rames »)");
    }
    // Terminus à quai unique : les deux voies se rejoignent avant la station
    // (J.F. Kennedy tant que le second quai est en travaux)
    this._config = { title: "Métro", quais_uniques: ["JFK"], ...config };
    this._signature = null;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    if (this._hass) this._build();
  }

  set hass(hass) {
    this._hass = hass;
    const st = hass.states[this._config?.entity];
    const codes = Object.keys(st?.attributes?.lignes || {});
    const signature = [st?.last_updated || "-", ...codes.map((c) => findLineSensor(hass, c)?.last_updated || "-")].join("|");
    if (signature !== this._signature) {
      this._signature = signature;
      this._build();
    }
  }

  connectedCallback() {
    clearInterval(this._timer);
    this._timer = setInterval(() => this._tick(), 1000);
    // Le schéma est recalculé quand la largeur de la carte change
    if (!this._resize && "ResizeObserver" in window) {
      this._resize = new ResizeObserver((entries) => {
        const width = entries[0].contentRect.width;
        if (width && Math.abs(width - (this._width || 0)) > 8) {
          this._width = width;
          this._build();
        }
      });
    }
    this._resize?.observe(this);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    this._resize?.disconnect();
  }

  getCardSize() {
    return 2 + 3 * (this._lines?.length || 2);
  }

  static getStubConfig(hass) {
    const id = Object.keys(hass?.states || {}).find((i) => Array.isArray(hass.states[i].attributes?.rames));
    return { entity: id || "sensor.star_metro_rames" };
  }

  _lineIcon(code) {
    const s = findLineSensor(this._hass, code);
    return s?.attributes?.entity_picture;
  }

  _build() {
    if (!this.shadowRoot || !this._config || !this._hass) return;
    const st = this._hass.states[this._config.entity];
    let body;
    this._geometry = {};

    if (!st) {
      body = `<div class="msg">Entité introuvable : ${esc(this._config.entity)}</div>`;
      this._lines = [];
    } else if (st.state === "unavailable" || st.state === "unknown") {
      body = `<div class="msg">Chargement des horaires du métro…<br><span class="small soft">Le fichier GTFS est téléchargé au démarrage, puis chaque nuit.</span></div>`;
      this._lines = [];
    } else {
      const a = st.attributes || {};
      const t0 = (a.t0 || 0) * 1000;
      const wanted = Array.isArray(this._config.lignes) ? this._config.lignes.map((c) => String(c).toUpperCase()) : null;
      const codes = (wanted || Object.keys(a.lignes || {}).sort((x, y) => x.localeCompare(y))).filter((c) => a.lignes?.[c]);

      this._trains = (a.rames || []).map((r) => ({
        line: r.l,
        headsign: r.h || "",
        forward: r.d !== -1, // vers la dernière station (à droite)
        stops: (r.s || []).map(([i, arr, dep]) => ({ i, arr: t0 + arr * 1000, dep: t0 + dep * 1000 })),
      }));

      this._lines = codes.map((code) => {
        const line = a.lignes[code];
        const sensor = findLineSensor(this._hass, code);
        return {
          code,
          name: line.nom || code.toLowerCase(),
          color: line.couleur || LINE_FALLBACK_COLORS[code] || "var(--primary-color)",
          stations: line.stations || [],
          codes: line.codes,
          disrupted: !!sensor?.attributes?.perturbation,
          state: sensor?.state,
        };
      });

      // Largeur utile : carte - marges de la carte (2 × 16) et du bloc (2 × 12)
      const width = Math.max(260, (this._width || this.getBoundingClientRect().width || 380) - 56);
      body = `<div class="lines">${this._lines.map((l) => this._renderLine(l, width)).join("")}</div>`;
    }

    this.shadowRoot.innerHTML = `
      <style>${StarTrajetsCard.styles}${StarMetroCard.styles}</style>
      <ha-card>
        <div class="header">
          <span class="header-icon"><ha-icon icon="mdi:subway-variant"></ha-icon></span>
          <span class="title">${esc(this._config.title)}</span>
          <span class="spacer"></span>
          ${this._sourceLabel(st)}
        </div>
        ${body}
      </ha-card>`;
    this._tick();
  }

  _sourceLabel(st) {
    if (!st || st.state === "unavailable" || st.state === "unknown") return "";
    const live = st.attributes?.source === "temps_reel";
    return live
      ? `<span class="muted small" title="Positions reconstituées à partir des prochains passages en temps réel">temps réel</span>`
      : `<span class="muted small" title="Positions estimées d'après les horaires théoriques (GTFS STAR)">horaires théoriques</span>`;
  }

  _renderLine(line, width) {
    const n = line.stations.length;
    const step = n > 1 ? (width - 2 * PAD_SIDE) / (n - 1) : 0;
    const x = (i) => PAD_SIDE + i * step;
    const stagger = step < STAGGER_BELOW; // acronymes alternés haut / bas si serré
    const yTop = stagger ? 38 : 26; // voie vers la dernière station (→)
    const yBottom = yTop + TRACK_GAP; // voie vers la première station (←)
    const yMid = (yTop + yBottom) / 2;
    const height = yBottom + 14;

    const icon = this._lineIcon(line.code);
    const badge = icon
      ? `<img class="line-icon" style="width:22px;height:22px" src="${esc(icon)}" alt="">`
      : `<span class="line-letter" style="width:22px;height:22px;background:${esc(line.color)}">${esc(line.name)}</span>`;
    const color = esc(line.color);
    const codes = line.codes || line.stations.map((s) => s.slice(0, 3).toUpperCase());

    // Terminus à quai unique (réglage « quais_uniques » : acronymes ou noms)
    const single = (this._config.quais_uniques || []).map((v) => String(v).toLowerCase());
    const isSingle = (i) => n > 2 && single.some((v) => v === String(codes[i] || "").toLowerCase() || v === line.stations[i].toLowerCase());
    const singleStart = isSingle(0);
    const singleEnd = isSingle(n - 1);
    this._geometry[line.code] = { step, yTop, yBottom, yMid, singleStart, singleEnd, n };

    // Voies : deux traits parallèles, qui convergent vers un terminus à quai unique
    const x0 = singleStart ? x(1) : x(0);
    const x1 = singleEnd ? x(n - 2) : x(n - 1);
    let tracks = `
      <line x1="${x0}" y1="${yTop}" x2="${x1}" y2="${yTop}" class="track" style="stroke:${color}"></line>
      <line x1="${x0}" y1="${yBottom}" x2="${x1}" y2="${yBottom}" class="track" style="stroke:${color}"></line>`;
    if (singleStart) {
      tracks += `<polyline points="${x(1)},${yTop} ${x(0)},${yMid} ${x(1)},${yBottom}" class="track" style="stroke:${color}"></polyline>`;
    }
    if (singleEnd) {
      tracks += `<polyline points="${x(n - 2)},${yTop} ${x(n - 1)},${yMid} ${x(n - 2)},${yBottom}" class="track" style="stroke:${color}"></polyline>`;
    }

    const stations = line.stations.map((name, i) => {
      const terminus = i === 0 || i === n - 1;
      const r = terminus ? 4.5 : 3.5;
      const labelY = stagger && i % 2 ? yTop - 20 : yTop - 9;
      const oneDock = (i === 0 && singleStart) || (i === n - 1 && singleEnd);
      const dots = oneDock
        ? `<circle cx="${x(i).toFixed(1)}" cy="${yMid}" r="${r}" class="stop" style="fill:${color}"></circle>`
        : `<circle cx="${x(i).toFixed(1)}" cy="${yTop}" r="${r}" class="stop" style="fill:${color}"></circle>
           <circle cx="${x(i).toFixed(1)}" cy="${yBottom}" r="${r}" class="stop" style="fill:${color}"></circle>`;
      return `
        <g class="station ${terminus ? "terminus" : ""}">
          <title>${esc(name)}${oneDock ? " (quai unique)" : ""}</title>
          <text x="${x(i).toFixed(1)}" y="${labelY}" class="code">${esc(codes[i] || "")}</text>
          ${dots}
        </g>`;
    }).join("");

    return `
      <div class="metro-line ${line.disrupted ? "disrupted" : ""}" data-line="${esc(line.code)}">
        <div class="line-head">
          ${badge}
          <span class="strong nowrap">Ligne ${esc(line.name)}</span>
          <span class="spacer"></span>
          ${line.disrupted
            ? `<span class="pill warn small-pill" title="${esc(line.state || "Trafic perturbé")} · positions théoriques">⚠ ${esc(line.state || "Perturbée")}</span>`
            : `<span class="pill neutral small-pill count">–</span>`}
        </div>
        <svg class="diagram" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">
          ${tracks}
          ${stations}
          <g class="trains"></g>
        </svg>
        <div class="legend small soft">
          <span title="Voie du bas">← ${esc(line.stations[0] || "")}</span>
          <span title="Voie du haut">${esc(line.stations[n - 1] || "")} →</span>
        </div>
      </div>`;
  }

  _tick() {
    if (!this.shadowRoot || !this._lines?.length) return;
    const now = Date.now();
    for (const line of this._lines) {
      const root = this.shadowRoot.querySelector(`.metro-line[data-line="${line.code}"]`);
      const geo = this._geometry?.[line.code];
      if (!root || !geo) continue;
      const dots = this._trainDots(line, geo, now);
      root.querySelector("g.trains").innerHTML = dots.join("");
      const counter = root.querySelector(".count");
      if (counter) counter.textContent = `${dots.length} ${plural(dots.length, "rame")}`;
    }
  }

  // Une pastille par rame de la ligne en circulation à l'instant now
  _trainDots(line, geo, now) {
    const dots = [];
    for (const train of this._trains) {
      const pos = train.line === line.code ? trainPosition(train.stops, now) : null;
      if (pos === null) continue;
      const cx = PAD_SIDE + pos * geo.step;
      const cy = trainY(train, pos, geo);
      dots.push(
        `<circle cx="${cx.toFixed(1)}" cy="${cy}" r="4" class="train" style="stroke:${esc(line.color)}"><title>vers ${esc(train.headsign)}</title></circle>`
      );
    }
    return dots;
  }
}

// Ordonnée d'une rame : sa voie, ou la voie commune près d'un terminus à quai unique
function trainY(train, pos, geo) {
  const cy = train.forward ? geo.yTop : geo.yBottom;
  if (geo.singleStart && pos < 1) return geo.yMid + pos * (cy - geo.yMid);
  if (geo.singleEnd && pos > geo.n - 2) return geo.yMid + (geo.n - 1 - pos) * (cy - geo.yMid);
  return cy;
}

StarMetroCard.styles = `
  :host { display: block; }
  .lines { display: flex; flex-direction: column; gap: 12px; }
  .metro-line { background: var(--star-tile); border-radius: 12px; padding: 10px 12px 8px; min-width: 0; }
  .line-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
  .nowrap { white-space: nowrap; }
  .small-pill { font-size: 11px; padding: 2px 8px; flex: none; }
  .metro-line.disrupted .line-head .strong { color: var(--star-warn-text); }
  .diagram { display: block; overflow: visible; }
  .track { stroke-width: 3.5; stroke-linecap: round; stroke-linejoin: round; fill: none; }
  .code {
    font-size: 10px; font-weight: 600; letter-spacing: 0.02em; text-anchor: middle;
    fill: var(--secondary-text-color);
  }
  .station.terminus .code { fill: var(--primary-text-color); font-weight: 700; }
  .stop { stroke: var(--card-background-color, var(--ha-card-background, #fff)); stroke-width: 2; }
  .train { fill: #fff; stroke-width: 1.5; filter: drop-shadow(0 0 1.5px rgba(0, 0, 0, 0.45)); }
  .metro-line.disrupted .train { opacity: 0.4; }
  .legend { display: flex; justify-content: space-between; gap: 12px; margin-top: 2px; }
  .legend span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .msg { padding: 8px 4px; color: var(--secondary-text-color); line-height: 1.5; }
`;

if (!customElements.get("star-metro-card")) {
  customElements.define("star-metro-card", StarMetroCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "star-metro-card",
    name: "STAR Rennes – Métro (schéma)",
    description: "Schéma des lignes a et b avec la position estimée des rames.",
  });
}
})();
