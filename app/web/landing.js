"use strict";

/* Sample data mirroring data/synthetic/manifest.json. The rule below is the same one the
   server enforces in SQL; this page only illustrates it and decides nothing. */
const PEOPLE = [
  { id: "bob", name: "Bob", detail: "Acme, employee", tenant: "acme", roles: ["employee"] },
  { id: "carol", name: "Carol", detail: "Acme, finance", tenant: "acme", roles: ["finance", "employee"] },
  { id: "dave", name: "Dave", detail: "Globex, employee", tenant: "globex", roles: ["employee"] },
  { id: "erin", name: "Erin", detail: "Globex, admin", tenant: "globex", roles: ["admin", "employee"] },
];

const GROUPS = [
  { name: "Shared with everyone", docs: [
    { title: "Employee Handbook", scope: "default", roles: ["employee", "admin", "finance", "engineering"] },
    { title: "Security Policy", scope: "default", roles: ["employee", "admin", "finance", "engineering"] },
    { title: "Platform Admin Runbook", scope: "default", roles: ["admin"] },
  ] },
  { name: "Acme", docs: [
    { title: "Engineering Overview", scope: "tenant", tenant: "acme", roles: ["employee", "admin"] },
    { title: "Finance Report Q4", scope: "tenant", tenant: "acme", roles: ["finance", "admin"] },
    { title: "Executive Compensation", scope: "tenant", tenant: "acme", roles: ["admin"] },
  ] },
  { name: "Globex", docs: [
    { title: "Engineering Overview", scope: "tenant", tenant: "globex", roles: ["employee", "admin"] },
    { title: "Finance Report Q4", scope: "tenant", tenant: "globex", roles: ["admin"] },
    { title: "Product Roadmap", scope: "tenant", tenant: "globex", roles: ["admin"] },
  ] },
];

function verdict(person, doc) {
  if (doc.scope === "tenant" && doc.tenant !== person.tenant) return [false, "Another organization"];
  if (!doc.roles.some((role) => person.roles.includes(role))) return [false, "Role not allowed"];
  return [true, doc.scope === "default" ? "Shared" : "Your organization"];
}

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

function render(person) {
  let visible = 0;
  let total = 0;
  const list = document.getElementById("doclist");
  list.replaceChildren();
  for (const group of GROUPS) {
    const section = el("div", "group");
    section.append(el("h3", "", group.name));
    const ul = el("ul");
    for (const doc of group.docs) {
      const [ok, why] = verdict(person, doc);
      total += 1;
      if (ok) visible += 1;
      const row = el("li", `row ${ok ? "yes" : "no"}`);
      const reason = el("span", "why", why);
      if (!ok) reason.prepend(el("span", "sr-only", "Hidden: "));
      row.append(el("span", "title", doc.title), reason);
      ul.append(row);
    }
    section.append(ul);
    list.append(section);
  }
  document.getElementById("tally").textContent =
    `${person.name} can retrieve ${visible} of ${total} documents.`;
}

const fieldset = document.querySelector(".people");
PEOPLE.forEach((person, index) => {
  const label = el("label", "person");
  const input = el("input");
  input.type = "radio";
  input.name = "person";
  input.value = person.id;
  input.checked = index === 0;
  input.addEventListener("change", () => render(person));
  const text = el("span", "", person.name);
  text.append(el("small", "", person.detail));
  label.append(input, text);
  fieldset.append(label);
});
render(PEOPLE[0]);
