const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const dashboardPath = "dashboard/index.html";
const dataPath = "dashboard/data/samr-viz-data.js";
const html = fs.readFileSync(dashboardPath, "utf8");
const inlineScript = html.split("<script>")[1].split("</script>")[0];

function makeElement(id) {
  return {
    id,
    value: "",
    textContent: "",
    innerHTML: "",
    hidden: false,
    disabled: false,
    children: [],
    listeners: {},
    classList: { add() {}, remove() {} },
    appendChild(child) { this.children.push(child); },
    addEventListener(type, handler) { this.listeners[type] = handler; },
    scrollIntoView() {},
  };
}

const ids = [
  "samrDashboard", "dataMeta", "datasetFilter", "yearFilter", "sourceFilter", "categoryFilter", "typeFilter",
  "searchFilter", "clearFilters", "metricCases", "metricCasesContext", "metricFiles", "metricEntities", "metricEntitiesContext",
  "metricUnknown", "timelineNote", "timelineChart", "timelineLegend", "simpleTypeNote", "simpleTypeChart", "simpleTypeLegend",
  "enforcementNote", "enforcementChart", "enforcementLegend", "qualityList", "fileQualityList", "coverageList", "entityNote",
  "selectedEntityFilter", "entityGraph", "entityList", "casesPanel", "caseListNote", "caseRows", "pageInfo", "prevPage",
  "nextPage", "caseDetail", "closeDetail", "detailTitle", "detailMeta", "detailParticipants", "detailFiles", "footerNote",
];
const elements = new Map(ids.map((id) => [id, makeElement(id)]));
const document = {
  body: makeElement("body"),
  getElementById(id) { return elements.get(id) || makeElement(id); },
  createElement() { return makeElement("option"); },
};

const context = { console, document, window: {}, Intl, Set, Map, Math, JSON };
context.globalThis = context;
vm.runInNewContext(fs.readFileSync(dataPath, "utf8"), context, { filename: dataPath });
vm.runInNewContext(inlineScript, context, { filename: dashboardPath });

assert.strictEqual(context.window.SAMR_VIZ_DATA.meta.caseCount, 5501);
assert.strictEqual(elements.get("metricCases").textContent, "5,501");
assert.ok(elements.get("timelineChart").innerHTML.includes("<svg"));
assert.ok(elements.get("entityGraph").innerHTML.includes("<svg"));
assert.ok(elements.get("caseRows").innerHTML.includes("<tr"));
assert.ok(elements.get("datasetFilter").children.length >= 4);
console.log("dashboard smoke test OK");
