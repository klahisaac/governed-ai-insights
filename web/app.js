// Interface layer. Everything rendered goes through textContent: narrator output and
// documents are untrusted text (in production one comes from an LLM, the other from
// people), so neither is ever parsed as HTML.

const byId = (id) => document.getElementById(id);
const state = { metricTitles: {}, regionNames: {}, request: 0, token: null, metric: "", scope: "", shownScope: null, narrator: "", me: null, policy: null, view: null };

function element(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name === "class") node.className = value;
    else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else node.setAttribute(name, value);
  }
  for (const child of children.flat()) node.append(child instanceof Node ? child : String(child));
  return node;
}

function svgElement(tag, attributes = {}, ...children) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
  for (const child of children) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return node;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { Authorization: `Bearer ${state.token}`, ...(options.body ? { "Content-Type": "application/json" } : {}) },
  });
  const body = await response.json();
  if (!response.ok) throw Object.assign(new Error(body.error || response.statusText), { status: response.status });
  return body;
}

// Stored times are UTC; readers (Celldex is in New Jersey) see US Eastern Time.
const EASTERN_TIME = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZoneName: "short",
});

function easternTime(iso, dateOnly = false) {
  const part = Object.fromEntries(EASTERN_TIME.formatToParts(new Date(iso)).map((item) => [item.type, item.value]));
  const date = `${part.year}-${part.month}-${part.day}`;
  return dateOnly ? date : `${date} ${part.hour}:${part.minute} ${part.timeZoneName}`;
}

function fillSelect(select, options, value) {
  select.replaceChildren(...options.map(([optionValue, label]) => element("option", { value: optionValue }, label)));
  select.value = value;
}

// ---- policy wording ------------------------------------------------------------

// The threshold lives in the policy, so the page reads it from there instead of hard-coding it.
function smallCellRule() {
  const { minimum_cell_size: minimum, version } = state.policy;
  return `Small cell policy ${version}: groups of 1 to ${minimum - 1} patients are never shown, ` +
    "and neither is any number that would reveal one by subtraction.";
}

function infoIcon(text) {
  return element("span", { class: "info", tabindex: "0", role: "img", "aria-label": text, "data-tip": text }, "i");
}

function withheld(label) {
  return element("span", { class: "withheld" }, label, infoIcon(smallCellRule()));
}

// One floating tooltip for every info icon, positioned in the viewport so that
// scrolling tables cannot clip it. Shown on hover and on keyboard focus.
const floatingTip = element("div", { class: "floating-tip", role: "tooltip", hidden: "" });
document.body.append(floatingTip);

function showTip(icon) {
  floatingTip.textContent = icon.dataset.tip;
  floatingTip.hidden = false;
  const anchor = icon.getBoundingClientRect(), box = floatingTip.getBoundingClientRect();
  const left = Math.min(Math.max(8, anchor.left + anchor.width / 2 - box.width / 2), innerWidth - box.width - 8);
  const above = anchor.top - box.height - 6;
  floatingTip.style.left = `${left}px`;
  floatingTip.style.top = `${above >= 8 ? above : anchor.bottom + 6}px`;
}

document.addEventListener("mouseover", (event) => {
  const icon = event.target.closest?.(".info");
  if (icon) showTip(icon); else floatingTip.hidden = true;
});
document.addEventListener("focusin", (event) => {
  if (event.target.matches?.(".info")) showTip(event.target);
});
document.addEventListener("focusout", () => { floatingTip.hidden = true; });

// ---- chart -------------------------------------------------------------------

// Stack order is fixed (bottom to top) so a segment's color never depends on its rank.
const PARTS = [
  { key: "switch", label: "Switch", className: "segment-switch" },
  { key: "naive", label: "Naive", className: "segment-naive" },
];

// Bar with a 4px rounded data end on top and a square base.
function barPath(x, y, width, height, radius) {
  radius = Math.max(0, Math.min(radius, height, width / 2));
  return `M${x},${y + height}V${y + radius}Q${x},${y} ${x + radius},${y}H${x + width - radius}` +
    `Q${x + width},${y} ${x + width},${y + radius}V${y + height}Z`;
}

function hatch(id, className) {
  return svgElement("pattern", { id, width: 6, height: 6, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" },
    svgElement("rect", { width: 6, height: 6, class: "hatch-background" }),
    svgElement("line", { x1: 1, y1: 0, x2: 1, y2: 6, class: `hatch-line ${className}` }));
}

function tooltip(point) {
  if (point.suppressed) return `Week of ${point.week}: withheld by policy. ${smallCellRule()}`;
  const split = point.parts_hidden
    ? "switch/naive breakdown withheld by policy"
    : PARTS.map((part) => `${part.label.toLowerCase()} ${point.parts[part.key]}`).join(", ");
  const note = point.parts_hidden ? ` ${smallCellRule()}` : "";
  return `Week of ${point.week}: ${point.value} new patients (${split})${point.provisional ? " · provisional" : ""}${note}`;
}

function rateTooltip(point, definition) {
  const label = `${definition.week_label} of ${point.week}`;
  if (point.provisional) return `${label}: not measured yet (${definition.provisional_reason})`;
  if (point.suppressed) return `${label}: withheld by policy. ${smallCellRule()}`;
  if (point.value === null) return `${label}: no new patients`;
  return `${label}: ${point.value}% still on therapy (${point.numerator} of ${point.denominator})`;
}

function renderLegend(definition) {
  const item = (swatch, label) => element("span", {}, element("i", { class: `swatch ${swatch}` }), label);
  const items = definition.measure === "rate"
    ? [item("rate", "Share still on therapy"),
       item("band-unmeasured", `Not measured yet (${definition.provisional_reason})`),
       item("band-hidden", withheld("Rate withheld by policy"))]
    : [item("switch", "Switch from another therapy"),
       item("naive", "Naive (new to advanced therapy)"),
       item("total", withheld("Total only: switch/naive breakdown withheld by policy")),
       item("hatched", `Provisional (${definition.provisional_reason})`),
       item("band-hidden", withheld("Week withheld by policy"))];
  byId("legend").replaceChildren(...items);
}

function chartFrame(series, maximum) {
  const WIDTH = 760, HEIGHT = 240, LEFT = 34, BOTTOM = 22, TOP = 8;
  // About four gridlines at a round step (…, 10, 20, 25, 30, 40, 50, …).
  const magnitude = Math.pow(10, Math.floor(Math.log10(maximum / 4)));
  const tick = [1, 2, 2.5, 3, 4, 5, 10].map((multiple) => multiple * magnitude).find((step) => step * 4 >= maximum);
  const tickCount = Math.ceil(maximum / tick);
  const yMaximum = tick * tickCount;
  const slot = (WIDTH - LEFT) / series.length;
  const frame = {
    WIDTH, HEIGHT, LEFT, BOTTOM, TOP, slot,
    barWidth: Math.min(24, slot * 0.7),
    y: (value) => TOP + (HEIGHT - TOP - BOTTOM) * (1 - value / yMaximum),
    root: svgElement("svg", { viewBox: `0 0 ${WIDTH} ${HEIGHT}` }),
  };
  frame.root.append(svgElement("defs", {},
    ...PARTS.map((part) => hatch(`hatch-${part.key}`, part.className)),
    hatch("hatch-total", "segment-total")));
  for (let index = 0; index <= tickCount; index++) {
    const value = tick * index;
    frame.root.append(svgElement("line", { x1: LEFT, x2: WIDTH, y1: frame.y(value), y2: frame.y(value), class: "grid-line" }));
    frame.root.append(svgElement("text", { x: LEFT - 6, y: frame.y(value) + 3, "text-anchor": "end", class: "axis" },
      Math.round(value)));
  }
  return frame;
}

function addColumn(frame, point, index, group, label) {
  const { LEFT, TOP, HEIGHT, BOTTOM, slot, barWidth } = frame;
  // Hit target: the whole column, not just the painted bar.
  const hit = svgElement("rect", { x: LEFT + index * slot, y: TOP, width: slot, height: HEIGHT - TOP - BOTTOM, class: "hit" });
  hit.append(svgElement("title", {}, label));
  group.append(hit);
  frame.root.append(group);
  if (index % 4 === 0) {
    const x = LEFT + index * slot + (slot - barWidth) / 2;
    frame.root.append(svgElement("text", { x: x + barWidth / 2, y: HEIGHT - 6, "text-anchor": "middle", class: "axis" },
      point.week.slice(5)));
  }
}

// A week with no value to show gets a full-height band, not a bar: any height would read as a value.
function statusBand(frame, index, className) {
  const { LEFT, TOP, HEIGHT, BOTTOM, slot } = frame;
  return svgElement("rect", { x: LEFT + index * slot + 1, y: TOP, width: Math.max(0, slot - 2),
                              height: HEIGHT - TOP - BOTTOM, rx: 2, class: className });
}

function renderRateChart(series, definition) {
  const RADIUS = 4;
  const frame = chartFrame(series, 100);
  const { y, slot, barWidth, LEFT } = frame;
  series.forEach((point, index) => {
    const x = LEFT + index * slot + (slot - barWidth) / 2;
    const group = svgElement("g", { class: "bar" });
    if (point.provisional) {
      group.append(statusBand(frame, index, "band-unmeasured"));
    } else if (point.suppressed) {
      group.append(statusBand(frame, index, "band-hidden"));
    } else if (point.value !== null) {
      group.append(svgElement("path", { d: barPath(x, y(point.value), barWidth, y(0) - y(point.value), RADIUS), class: "rate" }));
    }
    addColumn(frame, point, index, group, rateTooltip(point, definition));
  });
  byId("chart").replaceChildren(frame.root);
}

function renderChart(series, definition) {
  byId("chart").setAttribute("aria-label", definition.title);
  renderLegend(definition);
  if (definition.measure === "rate") return renderRateChart(series, definition);
  const GAP = 2, RADIUS = 4;
  const frame = chartFrame(series, Math.max(10, ...series.map((point) => point.value ?? 0)));
  const { y, slot, barWidth, LEFT } = frame;

  series.forEach((point, index) => {
    const x = LEFT + index * slot + (slot - barWidth) / 2;
    const group = svgElement("g", { class: "bar" });

    if (point.suppressed) {
      group.append(statusBand(frame, index, "band-hidden"));
    } else if (point.parts_hidden) {
      group.append(svgElement("path", {
        d: barPath(x, y(point.value), barWidth, y(0) - y(point.value), RADIUS),
        class: `segment-total${point.provisional ? " provisional-total" : ""}`,
      }));
    } else {
      let base = 0;
      const visible = PARTS.filter((part) => point.parts[part.key] > 0);
      visible.forEach((part, partIndex) => {
        const value = point.parts[part.key];
        const yTop = y(base + value);
        // A 2px surface gap separates each segment from the one below it.
        const yBottom = y(base) - (partIndex > 0 ? GAP : 0);
        group.append(svgElement("path", {
          d: barPath(x, yTop, barWidth, Math.max(0, yBottom - yTop), partIndex === visible.length - 1 ? RADIUS : 0),
          class: `${part.className}${point.provisional ? ` provisional-${part.key}` : ""}`,
        }));
        base += value;
      });
    }
    addColumn(frame, point, index, group, tooltip(point));
  });
  byId("chart").replaceChildren(frame.root);
}

// ---- panels ------------------------------------------------------------------

function flash(...selectors) {
  const targets = selectors.map((selector) => document.querySelector(selector)).filter(Boolean);
  if (!targets.length) return;
  targets[0].scrollIntoView({ behavior: "smooth", block: "center" });
  for (const target of targets) {
    if (target.tagName === "DETAILS") target.open = true;
    target.classList.add("flash");
    setTimeout(() => target.classList.remove("flash"), 1400);
  }
}

// ---- views ---------------------------------------------------------------------

// The cards a role sees come from the policy (roles.<role>.views).
function canView(view) {
  return state.me.views.includes(view);
}

function applyViews() {
  for (const card of document.querySelectorAll("[data-view]")) {
    // Audit and evaluation cards also need their API permission; their render functions decide.
    if (!["audit", "evaluation"].includes(card.dataset.view)) card.hidden = !canView(card.dataset.view);
  }
}

// ---- violations ----------------------------------------------------------------

// What kind of rule each violation code breaks, and where that rule lives on the page.
// Policy rules point at the Policy card; evidence checks point at the facts they concern,
// which only exist on the page for the summary being viewed.
const VIOLATION_KINDS = {
  causal_language: { kind: "policy", label: "Policy violation", view: "policy",
    targets: (violation) => ['[data-policy="forbidden_phrases"]',
      `[data-word="${(violation.detail.match(/"([^"]+)"/) ?? [])[1]}"]`] },
  overstated: { kind: "policy", label: "Policy violation", view: "policy", targets: () => ['[data-policy="strong_words"]'] },
  suppressed_fact: { kind: "policy", label: "Policy violation", view: "policy", targets: () => ['[data-policy="small_cell"]'] },
  missing_required: { kind: "required", label: "Required caveat", view: "facts",
    targets: (violation) => [`[data-fact="${(violation.detail.match(/required fact (\S+)/) ?? [])[1]}"]`], current: true },
  ungrounded_number: { kind: "evidence", label: "Evidence check", view: "facts", targets: () => ["#facts"], current: true },
  ungrounded_date: { kind: "evidence", label: "Evidence check", view: "facts", targets: () => ["#facts"], current: true },
  direction_mismatch: { kind: "evidence", label: "Evidence check", view: "facts", targets: () => ["#facts"], current: true },
  unknown_fact: { kind: "evidence", label: "Evidence check", view: "facts", targets: () => ["#facts"], current: true },
  unknown_document: { kind: "evidence", label: "Evidence check", view: "documents", targets: () => ["#documents"], current: true },
  no_evidence: { kind: "evidence", label: "Evidence check", targets: () => [], current: true },
  narrator_error: { kind: "error", label: "Writer error", targets: () => [] },
};

// `onCurrentView` is false for evaluation cases, whose facts are not the ones on the page.
function violationLine(violation, onCurrentView) {
  const known = VIOLATION_KINDS[violation.code] ?? { kind: "error", label: "Check", targets: () => [] };
  // No link when the rule's card is not part of this role's views, or not about this view's facts.
  const targets = (known.current && !onCurrentView) || !canView(known.view) ? [] : known.targets(violation);
  const code = targets.length
    ? element("button", { type: "button", class: "rule-link", title: "Show the rule",
                          onclick: () => flash(...targets) }, violation.code)
    : element("code", {}, violation.code);
  return [element("span", { class: `violation-kind kind-${known.kind}` }, known.label), " ", code, ` ${violation.detail}`];
}

function claimList(claims, facts, documents) {
  const factsById = Object.fromEntries(facts.map((fact) => [fact.id, fact]));
  const documentsById = Object.fromEntries(documents.map((item) => [item.id, item]));
  return element("ol", { class: "claims" },
    claims.map((claim) => element("li", {}, claim.text,
      claim.author === "platform"
        ? element("span", { class: "level-tag author-platform",
                            title: "Approved caveat, added to every summary by the platform rather than the summary writer" },
                  "Platform")
        : "",
      claim.distribution !== state.policy.data_distribution
        ? element("span", { class: `level-tag level-${claim.distribution}` }, levelRule(claim.distribution).label.split(" · ")[0])
        : "",
      // A chip jumps to its fact or document when that card is shown; otherwise it only tells its values.
      claim.fact_ids.map((id) => element("span", {
        class: canView("facts") ? "chip" : "chip chip-static",
        title: JSON.stringify(factsById[id]?.values ?? {}),
        ...(canView("facts") ? { onclick: () => flash(`[data-fact="${id}"]`) } : {}),
      }, id)),
      claim.document_ids.map((id) => element("span", {
        class: canView("documents") ? "chip chip-document" : "chip chip-document chip-static",
        title: documentsById[id]?.title ?? "not provided to this summary",
        ...(canView("documents") ? { onclick: () => flash(`[data-document="${id}"]`) } : {}),
      }, id)))));
}

// ---- distribution ------------------------------------------------------------

function levelRule(level) {
  return state.policy.distribution_levels[level] ?? { label: level, copy: "blocked", share: false };
}

function distributionBar(insight) {
  const rule = levelRule(insight.distribution);
  const share = element("button", { type: "button", class: "secondary" }, "Share externally");
  if (rule.share) share.addEventListener("click", () => alert("External sharing is not connected in this prototype."));
  else share.disabled = true;
  return element("div", { class: `distribution level-${insight.distribution}` },
    element("span", { class: "distribution-label" }, rule.label),
    infoIcon("A summary takes the strictest level of the facts and documents it cites. " +
      (rule.copy === "blocked" ? "Copying is blocked and attempts are logged." :
        "Copied text carries this label and the insight id, and each copy is logged.") +
      " Company DLP enforces outside this app."),
    rule.share ? share : element("span", { class: "share-off" }, share,
      infoIcon("Only summaries approved for external use (MLR approved) can be shared.")));
}

function onSummaryCopy(event) {
  const view = state.view;
  if (!view) return;
  const insight = view.insight, rule = levelRule(insight.distribution);
  // A withheld summary broke the rules, so neither it nor its draft may leave the page.
  const withheld = insight.status !== "accepted";
  const selected = String(document.getSelection() ?? "");
  const report = (outcome) => api("/api/audit/copy", {
    method: "POST",
    body: JSON.stringify({ outcome, insight_id: insight.id, metric: state.metric, scope: view.scope,
                           distribution: insight.distribution, characters: selected.length }),
  }).then(renderAudit).catch(() => {});
  event.preventDefault();
  if (withheld || rule.copy === "blocked") {
    event.clipboardData.setData("text/plain", "");
    byId("copy-note").textContent = withheld
      ? "Copy blocked: this summary was withheld. The attempt was logged."
      : `Copy blocked: ${rule.label}. The attempt was logged.`;
    byId("copy-note").hidden = false;
    report("blocked");
    return;
  }
  const notice = `\n\n— ${rule.label}. Insight ${insight.id}, copied by ${state.me.name} ` +
    `on ${easternTime(new Date().toISOString(), true)} (Eastern Time).`;
  event.clipboardData.setData("text/plain", selected + notice);
  byId("copy-note").textContent = "Copied with its distribution label. The copy was logged.";
  byId("copy-note").hidden = false;
  report("copied");
}

// Where this summary came from: the cache, or written for this request; and a way to write it again.
function summaryOrigin(insight) {
  const trace = insight.trace;
  const writer = byId("narrator").selectedOptions[0]?.textContent ?? insight.provenance.narrator;
  const written = trace.generated_at ? easternTime(trace.generated_at) : null;
  let text;
  if (trace.cache === "hit") {
    text = `Cached summary${written ? `, written ${written}` : ""}. No new model call.`;
  } else if (trace.model) {
    text = `Written just now by ${writer}: ${(trace.duration_milliseconds / 1000).toFixed(1)}s, ` +
      `$${trace.cost_usd.toFixed(4)}.`;
  } else {
    text = `Written just now by ${writer}.`;
  }
  const regenerate = element("button", { type: "button", class: "secondary", title:
    "Write this summary again, skipping the cache. An LLM writer is called again and the call is logged." },
    "Regenerate");
  regenerate.addEventListener("click", () => { regenerate.disabled = true; refresh(true); });
  return element("div", { class: "summary-origin" },
    element("span", { class: `origin-tag ${trace.cache === "hit" ? "cached" : "fresh"}` },
      trace.cache === "hit" ? "Cached" : "New"),
    element("span", { class: "muted small-inline" }, text), regenerate);
}

function renderInsight(insight, facts, documents) {
  const box = byId("insight");
  const accepted = insight.status === "accepted";
  const parts = [element("span", { class: `status ${insight.status}` }, accepted ? "Validated" : "Summary withheld"),
                 summaryOrigin(insight)];
  if (accepted) {
    parts.push(element("p", { class: "muted small" },
      "Every sentence below passed the validator when this summary was written." +
      (canView("evaluation")
        ? " The Summary writer evaluation card further down counts separate test runs and other readers' summaries; it is not a check of this one." : "")));
    parts.push(distributionBar(insight));
    const blocked = levelRule(insight.distribution).copy === "blocked";
    parts.push(insight.claims.length
      ? element("div", { class: `claims-area${blocked ? " no-select" : ""}` },
          claimList(insight.claims, facts, documents))
      : element("p", { class: "muted" }, "Nothing to report that passes the rules for this scope."),
      element("p", { id: "copy-note", class: "copy-note", hidden: "" }));
  } else {
    const failed = insight.violations.some((violation) => violation.code === "narrator_error");
    parts.push(
      element("p", {}, failed
        ? "The summary writer could not produce a summary, so none is shown. The chart and facts are unaffected."
        : "The summary writer's draft broke the rules below, so no summary is shown. The chart and facts are unaffected."),
      element("ul", { class: "violations" }, insight.violations.map((violation) =>
        element("li", {}, ...violationLine(violation, true),
          violation.claim === null ? "" : element("span", { class: "muted" }, ` (claim ${violation.claim + 1})`)))));
    if (insight.blocked_draft.length) {
      parts.push(element("details", {}, element("summary", {}, "What the summary writer tried to say (withheld draft)"),
        element("div", { class: "draft no-select" }, claimList(insight.blocked_draft, facts, documents))));
    }
    parts.push(element("p", { id: "copy-note", class: "copy-note", hidden: "" }));
  }
  box.replaceChildren(...parts);
}

function renderDefinition(definition) {
  byId("metric-title").textContent = definition.title;
  byId("definition").replaceChildren(
    element("p", {}, definition.description),
    element("dl", { class: "key-value" },
      ["version", "owner", "approved_on"].flatMap((key) => [element("dt", {}, key), element("dd", {}, definition[key])]),
      Object.entries(definition.parameters).flatMap(([key, value]) => [element("dt", {}, key), element("dd", {}, value)])));
}

function renderFacts(facts) {
  byId("facts").replaceChildren(
    element("tr", {}, ["id", "kind", "values", "attributes"].map((heading) => element("th", {}, heading))),
    ...facts.map((fact) => element("tr", { "data-fact": fact.id },
      element("td", {}, element("code", {}, fact.id), fact.required ? " · required" : ""),
      element("td", {}, fact.kind),
      element("td", {}, fact.suppressed ? withheld("Withheld by policy") : element("code", {}, JSON.stringify(fact.values))),
      element("td", {}, element("code", {}, JSON.stringify(fact.attributes))))));
}

function renderPolicy(policy) {
  const chips = (words) => element("span", { class: "word-list" },
    words.map((word) => element("code", { class: "word", "data-word": word }, word)));
  const roles = element("table", {},
    element("tr", {}, ["role", "sees", "audience", "permissions", "cards"].map((heading) => element("th", {}, heading))),
    ...Object.entries(policy.roles).map(([name, role]) => element("tr", {},
      element("td", {}, element("code", {}, name)),
      element("td", {}, role.scope === "all" ? `every ${policy.scope_dimension}` : `own ${policy.scope_dimension} only`),
      element("td", {}, role.audience),
      element("td", {}, role.permissions.length ? role.permissions.join(", ") : "none"),
      element("td", {}, (role.views ?? []).join(", ")))));
  byId("policy").replaceChildren(
    element("dl", { class: "key-value" },
      element("dt", {}, "version"), element("dd", {}, policy.version),
      element("dt", {}, "small cell rule"),
      element("dd", { "data-policy": "small_cell" }, smallCellControl(policy), infoIcon(smallCellRule())),
      element("dt", {}, "scope by"), element("dd", {}, policy.scope_dimension),
      element("dt", {}, "never allowed"),
      element("dd", { "data-policy": "forbidden_phrases" }, chips(policy.forbidden_phrases), infoIcon("Summaries may say what moved, never why. Any of these phrases withholds the summary.")),
      element("dt", {}, "only for large changes"),
      element("dd", { "data-policy": "strong_words" }, chips(policy.strong_words), infoIcon("Allowed only when a cited fact is marked as a large change."))),
    element("div", { class: "tablewrap" }, roles),
    element("h3", { class: "subheading" }, "Distribution"),
    element("div", { class: "tablewrap" }, element("table", {},
      element("tr", {}, ["level", "label", "copy", "external share"].map((heading) => element("th", {}, heading))),
      ...Object.entries(policy.distribution_levels).map(([level, rule]) => element("tr", {},
        element("td", {}, element("code", {}, level), level === policy.data_distribution ? " · computed data" : ""),
        element("td", {}, rule.label),
        element("td", {}, rule.copy === "blocked" ? "blocked, logged" : "with label, logged"),
        element("td", {}, rule.share ? "allowed" : "not allowed"))))),
    ...policyChangeNotes(policy));
}

function smallCellControl(policy) {
  if (!state.me.can_edit_policy) return `minimum cell size ${policy.minimum_cell_size}`;
  const [low, high] = policy.minimum_cell_size_range;
  const input = element("input", {
    type: "number", min: low, max: high, step: 1, value: policy.minimum_cell_size,
    class: "number-input", "aria-label": "Minimum cell size",
  });
  const apply = (value) => changeMinimumCellSize(Number(value));
  return element("span", { class: "policy-control" },
    "minimum cell size", input,
    element("button", { type: "button", onclick: () => apply(input.value) }, "Apply"),
    policy.minimum_cell_size !== policy.approved_minimum_cell_size
      ? element("button", { type: "button", class: "secondary", onclick: () => apply(policy.approved_minimum_cell_size) },
          `Reset to approved (${policy.approved_minimum_cell_size})`)
      : "");
}

function policyChangeNotes(policy) {
  const notes = [];
  if (policy.minimum_cell_size !== policy.approved_minimum_cell_size) {
    notes.push(element("p", { class: "policy-note" },
      `Runtime change, not saved to policy.toml (approved: ${policy.approved_minimum_cell_size}, ` +
      `version ${policy.approved_version}). Restarting the server returns to the approved rule. The change is in the audit log.`));
    if (policy.minimum_cell_size < policy.approved_minimum_cell_size) {
      notes.push(element("p", { class: "policy-note warning" },
        "This is looser than the approved rule: smaller groups of patients become visible to every reader."));
    }
  }
  if (!state.me.can_edit_policy) {
    notes.push(element("p", { class: "muted small" }, "Only administrators can change the policy."));
  }
  return notes;
}

async function changeMinimumCellSize(value) {
  try {
    state.policy = await api("/api/policy/minimum-cell-size", { method: "POST", body: JSON.stringify({ value }) });
  } catch (error) {
    byId("error").textContent = `Policy not changed: ${error.message}`;
    byId("error").hidden = false;
    return;
  }
  // Every number, chart, fact and summary depends on the policy, so redraw the whole page.
  renderPolicy(state.policy);
  await refresh();
}

function renderDocuments(documents) {
  byId("documents").replaceChildren(...(documents.length
    ? documents.map((item) => element("details", { class: "document", "data-document": item.id },
        element("summary", {},
          element("span", { class: "document-title" }, item.title),
          element("span", { class: `grade-badge level-${item.distribution}`, title: levelRule(item.distribution).label },
            `MLR grade: ${item.distribution}`),
          element("span", { class: "muted small-inline" },
            `${item.kind.replaceAll("_", " ")} · ${item.owner} · ${item.published_on} · ` +
            `${item.scope ?? "all regions"} · v${item.version}`)),
        item.prototype_note ? element("p", { class: "prototype-note" }, `Planted on purpose: ${item.prototype_note}`) : "",
        element("pre", { class: "document-body" }, item.body),
        element("p", { class: "muted small-inline" }, `id ${item.id} · content hash ${item.content_hash}`)))
    : [element("p", { class: "muted" }, "No documents for this view.")]));
}

function renderProvenance(insight) {
  const provenance = { insight_id: insight.id, ...insight.provenance };
  provenance.snapshot = `${provenance.snapshot.as_of} · ${provenance.snapshot.source_hash}`;
  provenance.documents = provenance.documents.length
    ? provenance.documents.map((item) => `${item.id}@${item.version} (${item.content_hash})`).join(", ")
    : "none";
  const trace = insight.trace;
  if (trace.model) {
    provenance.llm = `${trace.model} · ${trace.input_tokens} in / ${trace.output_tokens} out · ` +
      `$${trace.cost_usd.toFixed(4)} (API-rate estimate) · ${(trace.duration_milliseconds / 1000).toFixed(1)}s`;
    for (const [model, usage] of Object.entries(trace.models ?? {})) {
      if (model !== trace.model) {
        provenance["helper model"] = `${model} · ${usage.input_tokens} in / ${usage.output_tokens} out · $${usage.cost_usd.toFixed(4)}`;
      }
    }
    provenance.prompt_hash = trace.prompt_hash;
    provenance.cache = trace.cache ?? "miss (called the model)";
  }
  byId("provenance").replaceChildren(...Object.entries(provenance).flatMap(([key, value]) =>
    [element("dt", {}, key), element("dd", {}, value)]));
}

// One sentence can break several rules; show it once with every rule it broke.
function groupBySentence(failures) {
  const groups = new Map();
  for (const failure of failures) {
    const key = failure.sentence ?? "";
    groups.set(key, [...(groups.get(key) ?? []), failure]);
  }
  return [...groups];
}

function regionLabel(scope) {
  return scope === "ALL" ? "All regions (national)" : `${scope} · ${state.regionNames[scope] ?? scope}`;
}

function passBar(passed, total, unit) {
  const share = total ? passed / total : 0;
  return element("span", { class: "pass-cell" },
    element("span", { class: "pass-rate" },
      element("span", { class: `pass-bar ${share === 1 ? "all" : share === 0 ? "none" : "some"}`,
                        style: `width: ${Math.round(share * 100)}%` })),
    ` ${passed} of ${total} ${unit}`);
}

async function runGate(writer, button) {
  button.disabled = true;
  button.textContent = "Evaluating…";
  try {
    await api("/api/evaluations/run", { method: "POST", body: JSON.stringify({ narrator: writer.narrator }) });
  } catch (error) {
    byId("error").textContent = `Evaluation not started: ${error.message}`;
    byId("error").hidden = false;
  }
  await Promise.all([renderEvaluations(), renderAudit()]);
}

async function renderEvaluations() {
  const card = byId("evaluation-card");
  card.hidden = !(state.me.can_read_evaluations && canView("evaluation"));
  if (card.hidden) return;
  const writers = await api("/api/evaluations");
  const money = (value) => value ? `$${value.toFixed(4)}` : "none";
  const table = element("table", {},
    element("tr", {},
      element("th", {}, "summary writer"), element("th", {}, "model"),
      element("th", {}, "passed"), element("th", {}, "cost"), element("th", {}, "time"),
      element("th", {}, "last run"),
      state.me.can_run_evaluations ? element("th", {}, "") : ""),
    ...writers.map((writer) => {
      const gate = writer.gate;
      if (gate) {  // the legend states the real number of cases and their shape
        const metrics = new Set(gate.cases.map((item) => item.metric)).size;
        byId("case-count").textContent = gate.total;
        byId("case-shape").textContent = `${metrics} metrics × ${gate.total / metrics} views: national and ${gate.total / metrics - 1} regions`;
      }
      const run = element("button", { type: "button", class: "secondary",
        title: "Run every case for this writer now. LLM writers are called for each case." },
        writer.running ? "Evaluating…" : gate ? "Re-evaluate" : "Evaluate");
      if (writer.running) run.disabled = true;
      run.addEventListener("click", () => runGate(writer, run));
      return element("tr", {},
        element("td", {}, writer.title, element("span", { class: "muted small-inline" }, ` v${writer.version}`)),
        element("td", {}, element("code", {}, gate?.model ?? (gate ? "none (deterministic)" : "—"))),
        element("td", {}, gate ? passBar(gate.passed, gate.total, "cases") : element("span", { class: "muted" }, "not run")),
        element("td", {}, gate ? money(gate.cost_usd) : ""),
        element("td", {}, gate ? `${gate.duration_seconds}s` : ""),
        element("td", { class: "muted small-inline" }, gate ? easternTime(gate.started_at) : ""),
        state.me.can_run_evaluations ? element("td", {}, run) : "");
    }));
  const failures = writers.filter((writer) => writer.gate && writer.gate.passed < writer.gate.total).map((writer) => {
    const failed = writer.gate.cases.filter((item) => item.status !== "accepted");
    // Name each failed case so it can be found on the page: the metric and the region.
    const where = (item) => [state.metricTitles[item.metric] ?? item.metric, regionLabel(item.scope)];
    return element("details", { class: "evaluation-failures" },
      element("summary", {}, `${writer.title}: ${failed.length} of ${writer.gate.total} test cases failed`,
        failed.length <= 3
          ? element("span", { class: "muted failed-cases" }, ` (${failed.map((item) => where(item).join(", ")).join("; ")})`)
          : ""),
      element("ul", {}, failed.map((item) =>
        element("li", {},
          element("div", { class: "failed-case" },
            element("span", {}, element("span", { class: "muted" }, "Metric "), element("strong", {}, where(item)[0])),
            element("span", {}, element("span", { class: "muted" }, "Region "), element("strong", {}, where(item)[1]))),
          element("ul", {}, groupBySentence(item.failures).map(([sentence, failures]) => element("li", {},
            sentence ? element("blockquote", {}, sentence) : "",
            element("ul", { class: "rule-list" }, failures.map((failure) =>
              element("li", {}, ...violationLine(failure, false)))))))))));
  });
  byId("evaluations").replaceChildren(element("div", { class: "tablewrap" }, table), ...failures);
}

async function renderAudit() {
  const card = byId("audit-card");
  card.hidden = !(state.me.can_read_audit && canView("audit"));
  if (card.hidden) return;
  const rows = await api("/api/audit");
  const columns = ["timestamp", "user", "action", "scope", "metric_version", "insight_status", "narrator", "documents", "detail"];
  const cell = (row, column) => {
    if (column === "detail" && row.action?.startsWith("summary_copy")) {
      return `${row.distribution}, ${row.characters} characters`;
    }
    if (column === "detail" && row.setting) {
      return row.action === "policy_changed"
        ? `${row.setting}: ${row.previous} → ${row.value} (policy ${row.policy_version})`
        : `${row.setting}: requested ${row.requested}`;
    }
    if (column === "timestamp") return easternTime(row.timestamp);
    if (column === "scope") return row.scope ?? row.requested_scope ?? "";
    if (column === "documents") return row.documents ? String(row.documents.length) : "";
    return row[column] ?? "";
  };
  byId("audit").replaceChildren(
    element("tr", {}, columns.map((column) => element("th", {}, column === "narrator" ? "summary writer" : column))),
    ...rows.slice(0, 15).map((row) => element("tr", {}, columns.map((column) => element("td", {}, cell(row, column))))));
}

// ---- flow ----------------------------------------------------------------------

async function refresh(regenerate = false) {
  // Summaries can take seconds; when the controls change again meanwhile, only the newest request may draw.
  const request = ++state.request;
  byId("error").hidden = true;
  byId("insight").replaceChildren(element("p", { class: "muted" },
    regenerate ? "Writing a new summary…" : "Generating summary…"));
  try {
    const query = new URLSearchParams({ scope: state.scope, narrator: state.narrator });
    if (regenerate) query.set("regenerate", "1");
    const view = await api(`/api/metrics/${state.metric}?${query}`);
    if (request !== state.request) return;
    renderChart(view.series, view.definition);
    state.view = view;
    renderInsight(view.insight, view.facts, view.documents);
    renderDefinition(view.definition);
    renderFacts(view.facts);
    renderDocuments(view.documents);
    renderProvenance(view.insight);
    state.shownScope = state.scope;
  } catch (error) {
    if (request !== state.request) return;
    // Keep the controls consistent with the data still on screen.
    state.scope = state.shownScope ?? "";
    byId("scope").value = state.scope;
    byId("error").textContent = error.status === 403
      ? `Access denied: ${error.message}. The attempt was written to the audit log.`
      : `Error: ${error.message}`;
    byId("error").hidden = false;
  }
  await renderAudit();
}

async function signIn(token) {
  state.token = token;
  state.me = await api("/api/me");
  applyViews();
  const catalog = await api("/api/catalog");
  state.policy = catalog.policy;
  if (catalog.public) {
    const badge = byId("prototype-badge");
    badge.textContent = "Public prototype · synthetic data · fictional brand";
    badge.title = "Anyone can sign in as any role here, so model-backed summary writers, evaluation runs and policy changes are off.";
  }
  state.metricTitles = Object.fromEntries(catalog.metrics.map((metric) => [metric.name, metric.title]));
  state.regionNames = Object.fromEntries(catalog.scope_members.map((member) => [member.code, member.name]));
  renderPolicy(catalog.policy);
  state.scope = state.me.scope_value ?? "";
  state.shownScope = null;
  state.narrator = state.narrator || catalog.narrators[0].name;
  state.metric = state.metric || catalog.metrics[0].name;
  fillSelect(byId("metric"), catalog.metrics.map((metric) => [metric.name, metric.title]), state.metric);
  fillSelect(byId("scope"), [["", "All regions"],
    ...catalog.scope_members.map((member) => [member.code, `${member.code} · ${member.name}`])], state.scope);
  fillSelect(byId("narrator"), catalog.narrators.map((narrator) => [narrator.name, `${narrator.title} (v${narrator.version})`]),
    state.narrator);
  byId("me").textContent = `role: ${state.me.role} · audience: ${state.me.audience}`;
  await Promise.all([refresh(), renderEvaluations()]);
}

async function main() {
  const logins = await (await fetch("/api/prototype-logins")).json();
  fillSelect(byId("login"), logins.map((login) => [login.token, login.name]), logins[0].token);
  byId("login").addEventListener("change", (event) => signIn(event.target.value));
  byId("insight").addEventListener("copy", onSummaryCopy);
  byId("metric").addEventListener("change", (event) => { state.metric = event.target.value; refresh(); });
  byId("scope").addEventListener("change", (event) => { state.scope = event.target.value; refresh(); });
  byId("narrator").addEventListener("change", (event) => { state.narrator = event.target.value; refresh(); });
  await signIn(logins[0].token);
}

main().then(
  () => byId("loading").remove(),
  (error) => {
    byId("loading").classList.add("failed");
    byId("loading-message").textContent = `Could not load the page: ${error.message}. Reload to try again.`;
  });
