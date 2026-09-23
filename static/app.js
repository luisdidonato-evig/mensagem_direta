const STATUSES = [
  ["AGUARDANDO", "Aguardando"],
  ["EM_ATENDIMENTO", "Em atendimento"],
  ["AGUARDANDO_CLIENTE", "Aguardando cliente"],
  ["AGUARDANDO_INTERNO", "Aguardando interno"],
  ["ENCERRADO", "Encerrados"],
];

const CONTACT_STAGES = [
  ["NAO_CLASSIFICADO", "Não classificado"],
  ["NOVO_CONTATO", "Novo contato"],
  ["CLIENTE_POTENCIAL", "Cliente em potencial"],
  ["CLIENTE_ATIVO", "Cliente ativo"],
  ["INATIVO", "Inativo"],
];

const SESSION_TOKEN_KEY = "centro_atendimento_token";
const SESSION_ACTOR_KEY = "centro_atendimento_actor";

const state = {
  view: "attendances",
  attendances: [],
  contacts: [],
  contactsLoaded: false,
  groups: [],
  agents: [],
  memberships: [],
  editingAgentId: null,
  editingGroupId: null,
  membershipGroupId: null,
  quickReplies: [],
  editingQuickReplyId: null,
  selectedId: null,
  selected: null,
  selectedContact: null,
  token: null,
  actor: null,
};
const $ = (selector) => document.querySelector(selector);

function identity() {
  return state.token ? { Authorization: `Bearer ${state.token}` } : {};
}

function loadSession() {
  try {
    const token = sessionStorage.getItem(SESSION_TOKEN_KEY);
    const actorRaw = sessionStorage.getItem(SESSION_ACTOR_KEY);
    if (!token || !actorRaw) return false;
    state.token = token;
    state.actor = JSON.parse(actorRaw);
    return true;
  } catch { return false; }
}

function saveSession(token, actor) {
  state.token = token;
  state.actor = actor;
  try {
    sessionStorage.setItem(SESSION_TOKEN_KEY, token);
    sessionStorage.setItem(SESSION_ACTOR_KEY, JSON.stringify(actor));
  } catch { /* sessão só em memória se storage indisponível */ }
}

function clearSession() {
  state.token = null;
  state.actor = null;
  try {
    sessionStorage.removeItem(SESSION_TOKEN_KEY);
    sessionStorage.removeItem(SESSION_ACTOR_KEY);
  } catch { /* nada a limpar */ }
}

async function api(path, options = {}) {
  const contentHeaders = options.body instanceof FormData ? {} : { "Content-Type": "application/json" };
  const response = await fetch(path, {
    ...options,
    headers: { ...contentHeaders, ...identity(), ...(options.headers || {}) },
  });
  if (response.status === 401 && path !== "/api/v1/auth/login") {
    clearSession();
    location.reload();
    throw new Error("Sessão expirada");
  }
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: "Falha inesperada" }));
    const failure = new Error(error.detail || `HTTP ${response.status}`);
    failure.status = response.status;
    throw failure;
  }
  if (response.status === 204) return null;
  return response.json();
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

function initials(value) {
  const parts = String(value).replace(/[+_.-]/g, " ").trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "--";
  return (parts[0][0] + (parts[1]?.[0] || parts[0][1] || "")).toUpperCase();
}

function relativeTime(value) {
  const minutes = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 60000));
  if (minutes < 1) return "agora";
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h`;
  return `${Math.floor(hours / 24)} d`;
}

function formatDate(value) {
  return new Intl.DateTimeFormat("pt-BR", { dateStyle: "short", timeStyle: "short" }).format(new Date(value));
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}min`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

function renderMetrics(metrics) {
  $("#metric-open").textContent = metrics.total_open;
  $("#metric-unassigned").textContent = metrics.unassigned;
  $("#metric-mine").textContent = metrics.mine;
  $("#metric-first-response").textContent = formatDuration(metrics.average_first_response_seconds);
  $("#metric-resolution").textContent = formatDuration(metrics.average_resolution_seconds);
  $("#metric-closed").textContent = metrics.closed_today;
  $("#metric-rating").textContent = metrics.average_rating == null ? "—" : `${metrics.average_rating.toFixed(1)}/5`;
}

function latestPreview(item) {
  return item.last_message || item.messages?.at(-1)?.content || "Atendimento sem prévia";
}

function contactLabel(item) {
  return item.contact_display_name || item.contact_id;
}

function protocolOf(item) {
  return item.id.slice(0, 8).toUpperCase();
}

function attendanceTagOptions() {
  const tags = new Set();
  for (const item of state.attendances) for (const tag of item.tags || []) tags.add(tag);
  return [...tags].sort((a, b) => a.localeCompare(b, "pt-BR"));
}

function populateAttendanceTagFilter() {
  const select = $("#attendance-tag-filter");
  const current = select.value;
  select.innerHTML = '<option value="">Todas</option>' + attendanceTagOptions()
    .map((tag) => `<option value="${escapeHtml(tag)}">${escapeHtml(tag)}</option>`).join("");
  select.value = current;
}

async function loadGroups() {
  try {
    state.groups = await api(state.actor?.role === "ATENDENTE" ? "/api/v1/me/groups" : "/api/v1/groups");
    const select = $("#group-filter");
    const current = select.value;
    select.innerHTML = '<option value="">Todos</option>' + state.groups
      .filter((group) => group.active)
      .map((group) => `<option value="${escapeHtml(group.id)}">${escapeHtml(group.name)}</option>`)
      .join("");
    select.value = current;
    const inbound = $("#inbound-group");
    const inboundCurrent = inbound.value;
    inbound.innerHTML = '<option value="">Selecione um grupo</option>' + state.groups
      .filter((group) => group.active)
      .map((group) => `<option value="${escapeHtml(group.id)}">${escapeHtml(group.name)}</option>`).join("");
    inbound.value = inboundCurrent || (state.groups.filter((group) => group.active).length === 1 ? state.groups.find((group) => group.active).id : "");
    $("#pull-button").hidden = state.actor?.role !== "ATENDENTE";
    if (state.view === "team") renderManagement();
    renderBoard();
  } catch (error) { toast(error.message); }
}

function filteredAttendances() {
  const search = $("#search").value.trim().toLocaleLowerCase("pt-BR");
  const tag = $("#attendance-tag-filter").value;
  const groupId = $("#group-filter").value;
  const owner = $("#owner-filter").value;
  const status = $("#status-filter").value;
  const sort = $("#sort-filter").value;
  const actorId = state.actor?.id;

  const items = state.attendances.filter((item) => {
    const searchable = `${item.contact_display_name || ""} ${item.contact_id} ${item.contact_phone || ""} ${item.id} ${protocolOf(item)}`.toLocaleLowerCase("pt-BR");
    if (search && !searchable.includes(search)) return false;
    if (tag && !(item.tags || []).includes(tag)) return false;
    if (groupId && item.group_id !== groupId) return false;
    if (owner === "mine" && item.assignee_id !== actorId) return false;
    if (owner === "unassigned" && item.assignee_id) return false;
    if (status === "open" && item.status === "ENCERRADO") return false;
    if (status === "closed" && item.status !== "ENCERRADO") return false;
    return true;
  });

  items.sort((a, b) => {
    if (sort === "oldest") return new Date(a.updated_at) - new Date(b.updated_at);
    if (sort === "contact") return contactLabel(a).localeCompare(contactLabel(b), "pt-BR");
    return new Date(b.updated_at) - new Date(a.updated_at);
  });
  return items;
}

function renderBoard() {
  const filtered = filteredAttendances();
  $("#board").innerHTML = STATUSES.map(([status, label]) => {
    const items = filtered.filter((item) => item.status === status);
    const cards = items.length ? items.map((item) => {
      const tags = (item.tags || []).slice(0, 3).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("");
      const group = state.groups.find((entry) => entry.id === item.group_id);
      const groupIndex = state.groups.findIndex((entry) => entry.id === item.group_id);
      const groupBadge = state.actor?.role === "ADMIN"
        ? `<span class="card-group group-color-${Math.max(0, groupIndex) % 6}">${escapeHtml(group?.name || "Sem grupo")}</span>` : "";
      return `
      <button class="card ${item.stale ? "stale" : ""}" data-attendance-id="${item.id}">
        ${groupBadge}
        <span class="card-main">
          <span class="avatar">${escapeHtml(initials(contactLabel(item)))}</span>
          <span class="card-text">
            <span class="card-contact">${escapeHtml(contactLabel(item))}${item.stale ? ' <span class="stale-pill">⏰</span>' : ""}</span>
            <span class="card-preview">${escapeHtml(latestPreview(item))}</span>
          </span>
        </span>
        ${tags ? `<span class="card-tags">${tags}</span>` : ""}
        <span class="card-foot">
          <span><i class="owner-dot ${item.assignee_id ? "assigned" : ""}"></i>${escapeHtml(item.assignee_id || "Sem responsável")}${state.actor?.role === "SUPERVISOR" ? ` · ${escapeHtml(group?.name || "Sem grupo")}` : ""}</span>
          <span>${relativeTime(item.updated_at)}</span>
        </span>
      </button>
    `;
    }).join("") : '<div class="empty">Fila vazia</div>';
    return `<section class="column"><header class="column-head"><h2>${label}</h2><span class="column-count">${items.length}</span><span class="column-menu">⋮</span></header><div class="column-body">${cards}</div></section>`;
  }).join("");

  document.querySelectorAll("[data-attendance-id]").forEach((card) => {
    card.addEventListener("click", () => openDetail(card.dataset.attendanceId));
  });
}

async function loadBoard() {
  try {
    const [attendances, metrics] = await Promise.all([
      api("/api/v1/attendances"),
      api("/api/v1/metrics/summary"),
    ]);
    state.attendances = attendances;
    renderMetrics(metrics);
    populateAttendanceTagFilter();
    renderBoard();
  } catch (error) { toast(error.message); }
}

function switchView(view) {
  state.view = view;
  $("#nav-attendances").classList.toggle("active", view === "attendances");
  $("#nav-contacts").classList.toggle("active", view === "contacts");
  $("#nav-team").classList.toggle("active", view === "team");
  $("#attendances-view").hidden = view !== "attendances";
  $("#contacts-view").hidden = view !== "contacts";
  $("#team-view").hidden = view !== "team";
  $("#new-message-button").hidden = view === "team";
  $("#pull-button").hidden = view !== "attendances" || state.actor?.role !== "ATENDENTE";
  $("#outbox-button").hidden = view === "team" || state.actor?.role === "ATENDENTE";
  if (view === "attendances") {
    $("#page-title").textContent = "Atendimentos";
    $("#page-subtitle").textContent = "Acompanhe e responda conversas em um só lugar.";
  } else if (view === "contacts") {
    $("#page-title").textContent = "Contatos";
    $("#page-subtitle").textContent = "Classifique contatos e retome conversas.";
    if (!state.contactsLoaded) loadContacts();
  } else {
    $("#page-title").textContent = "Equipe e grupos";
    $("#page-subtitle").textContent = "Gerencie usuários, vínculos e filas de atendimento.";
    loadManagement();
  }
}

const canManageAgent = (agent) => state.actor?.role === "ADMIN" || agent?.role === "ATENDENTE";
const roleLabel = (role) => ({ ATENDENTE: "Atendente", SUPERVISOR: "Supervisor", ADMIN: "Administrador" })[role] || role;

async function loadManagement() {
  try {
    const [agents, memberships, groups] = await Promise.all([
      api("/api/v1/agents"), api("/api/v1/groups/memberships"), api("/api/v1/groups"),
    ]);
    state.agents = agents;
    state.memberships = memberships;
    state.groups = groups;
    renderManagement();
  } catch (error) { toast(error.message); }
}

function renderManagement() {
  const groupName = (id) => state.groups.find((group) => group.id === id)?.name || id;
  const memberActions = (group, link) => {
    if (!canManageAgent(state.agents.find((agent) => agent.id === link.agent_id))) return "";
    return `<button type="button" class="text-button" data-edit-membership="${escapeHtml(group.id)}" data-agent-id="${escapeHtml(link.agent_id)}">Editar</button><button type="button" class="text-button danger-text" data-unlink-group="${escapeHtml(group.id)}" data-agent-id="${escapeHtml(link.agent_id)}">Desvincular</button>`;
  };
  $("#agents-list").innerHTML = state.agents.length ? state.agents.map((agent) => {
    const links = state.memberships.filter((link) => link.agent_id === agent.id);
    return `<article class="management-item">
      <div class="management-item-main"><span class="avatar">${escapeHtml(initials(agent.id))}</span><div><strong>${escapeHtml(agent.id)}</strong><small>${roleLabel(agent.role)} · ${agent.active ? "Ativo" : "Inativo"}</small></div></div>
      <p>${links.length ? links.map((link) => escapeHtml(groupName(link.group_id))).join(" · ") : "Sem grupos vinculados"}</p>
      ${canManageAgent(agent) ? `<button type="button" class="button ghost" data-edit-agent="${escapeHtml(agent.id)}">Editar usuário</button>` : ""}
    </article>`;
  }).join("") : '<div class="empty">Nenhum usuário</div>';

  $("#groups-list").innerHTML = state.groups.length ? state.groups.map((group) => {
    const links = state.memberships.filter((link) => link.group_id === group.id);
    return `<article class="management-item">
      <div class="management-item-main"><div><strong>${escapeHtml(group.name)}</strong><small>${group.active ? "Ativo" : "Inativo"} · carga ${group.max_active_attendances} · custo ${group.load_cost_per_attendance} por atendimento</small></div></div>
      <div class="member-list">${links.length ? links.map((link) => `<div class="member-row"><span>${escapeHtml(link.agent_id)}${link.max_load_override ? ` · limite ${link.max_load_override}` : ""}</span><span>${memberActions(group, link)}</span></div>`).join("") : '<small>Nenhum usuário vinculado</small>'}</div>
      <div class="management-actions"><button type="button" class="button ghost" data-link-group="${escapeHtml(group.id)}" ${group.active ? "" : "disabled"}>＋ Vincular usuário</button>${state.actor?.role === "ADMIN" ? `<button type="button" class="button ghost" data-edit-group="${escapeHtml(group.id)}">Editar grupo</button>` : ""}</div>
    </article>`;
  }).join("") : '<div class="empty">Nenhum grupo</div>';
}

function openAgentDialog(agentId = null) {
  const agent = state.agents.find((item) => item.id === agentId);
  state.editingAgentId = agent?.id || null;
  $("#agent-dialog-title").textContent = agent ? "Editar usuário" : "Novo usuário";
  $("#agent-dialog-help").textContent = agent ? "Altere papel, senha ou situação da conta." : "Cadastre uma conta para a equipe.";
  $("#agent-id").value = agent?.id || "";
  $("#agent-id").disabled = Boolean(agent);
  $("#agent-role").value = agent?.role || "ATENDENTE";
  $("#agent-role-label").hidden = state.actor?.role !== "ADMIN";
  $("#agent-active-label").hidden = !agent;
  $("#agent-active").checked = agent?.active ?? true;
  $("#agent-password").value = "";
  $("#agent-password").required = !agent;
  $("#agent-password-help").textContent = agent ? "Deixe vazio para manter a senha atual." : "Mínimo de 8 caracteres.";
  $("#agent-dialog").showModal();
}

async function saveAgent(event) {
  event.preventDefault();
  const id = state.editingAgentId;
  const password = $("#agent-password").value;
  const role = state.actor?.role === "ADMIN" ? $("#agent-role").value : "ATENDENTE";
  const payload = id ? { active: $("#agent-active").checked } : { id: $("#agent-id").value.trim(), role, password };
  if (id && state.actor?.role === "ADMIN") payload.role = role;
  if (id && password) payload.password = password;
  try {
    await api(id ? `/api/v1/agents/${encodeURIComponent(id)}` : "/api/v1/agents", {
      method: id ? "PATCH" : "POST", body: JSON.stringify(payload),
    });
    $("#agent-dialog").close();
    await loadManagement();
    toast(id ? "Usuário atualizado" : "Usuário criado");
  } catch (error) { toast(error.message); }
}

function openGroupDialog(groupId = null) {
  const group = state.groups.find((item) => item.id === groupId);
  state.editingGroupId = group?.id || null;
  $("#group-dialog-title").textContent = group ? "Editar grupo" : "Novo grupo";
  $("#group-name").value = group?.name || "";
  $("#group-capacity").value = group?.max_active_attendances || 5;
  $("#group-load-cost").value = group?.load_cost_per_attendance ?? 1;
  $("#group-wait-message").value = group?.queue_wait_message || "Você entrou na fila de espera para ser atendido.";
  $("#group-active-label").hidden = !group;
  $("#group-active").checked = group?.active ?? true;
  $("#group-dialog").showModal();
}

async function saveGroup(event) {
  event.preventDefault();
  const id = state.editingGroupId;
  const payload = { name: $("#group-name").value.trim(), max_active_attendances: Number($("#group-capacity").value), load_cost_per_attendance: Number($("#group-load-cost").value), queue_wait_message: $("#group-wait-message").value.trim() };
  if (!id) payload.max_load_per_agent = payload.max_active_attendances;
  if (id) payload.active = $("#group-active").checked;
  try {
    await api(id ? `/api/v1/groups/${encodeURIComponent(id)}` : "/api/v1/groups", {
      method: id ? "PATCH" : "POST", body: JSON.stringify(payload),
    });
    $("#group-dialog").close();
    await loadManagement();
    await loadGroups();
    await loadBoard();
    toast(id ? "Grupo atualizado" : "Grupo criado");
  } catch (error) { toast(error.message); }
}

function openMembershipDialog(groupId, agentId = null) {
  const group = state.groups.find((item) => item.id === groupId);
  if (!group) return;
  state.membershipGroupId = groupId;
  $("#membership-dialog-title").textContent = `Vínculo · ${group.name}`;
  const eligible = state.agents.filter((agent) => agent.active && canManageAgent(agent));
  if (agentId && !eligible.some((agent) => agent.id === agentId)) { toast("Usuário indisponível para edição"); return; }
  $("#membership-agent").innerHTML = eligible.map((agent) => `<option value="${escapeHtml(agent.id)}">${escapeHtml(agent.id)} · ${roleLabel(agent.role)}</option>`).join("");
  $("#membership-agent").value = agentId || eligible.find((agent) => agent.role === "ATENDENTE")?.id || eligible[0]?.id || "";
  $("#membership-agent").disabled = Boolean(agentId);
  const link = state.memberships.find((item) => item.group_id === groupId && item.agent_id === agentId);
  $("#membership-capacity").value = link?.max_load_override || "";
  if (!eligible.length) { toast("Cadastre um usuário ativo para vincular"); return; }
  $("#membership-dialog").showModal();
}

async function saveMembership(event) {
  event.preventDefault();
  const groupId = state.membershipGroupId;
  const agentId = $("#membership-agent").value;
  const capacity = $("#membership-capacity").value;
  try {
    await api(`/api/v1/groups/${encodeURIComponent(groupId)}/agents/${encodeURIComponent(agentId)}`, {
      method: "PUT", body: JSON.stringify({ max_load_override: capacity ? Number(capacity) : null }),
    });
    $("#membership-dialog").close();
    await loadManagement();
    toast("Vínculo salvo");
  } catch (error) { toast(error.message); }
}

async function unlinkMembership(groupId, agentId) {
  try {
    await api(`/api/v1/groups/${encodeURIComponent(groupId)}/agents/${encodeURIComponent(agentId)}`, { method: "DELETE" });
    await loadManagement();
    toast("Usuário desvinculado");
  } catch (error) { toast(error.message); }
}

function populateTransferAgents() {
  if (state.actor?.role === "ATENDENTE") {
    $("#transfer-agent").innerHTML = '<option value="">Fila do grupo, sem responsável</option>';
    return;
  }
  const groupId = $("#transfer-group").value;
  const candidates = state.agents.filter((agent) => agent.active && state.memberships.some((link) => link.group_id === groupId && link.agent_id === agent.id));
  $("#transfer-agent").innerHTML = '<option value="">Fila do grupo, sem responsável</option>' + candidates
    .map((agent) => `<option value="${escapeHtml(agent.id)}">${escapeHtml(agent.id)}</option>`).join("");
}

async function openTransferDialog() {
  try {
    const manager = ["SUPERVISOR", "ADMIN"].includes(state.actor?.role);
    const groups = await api("/api/v1/groups/transfer-targets");
    const targets = manager ? groups : groups.filter((group) => group.id !== state.selected.group_id);
    if (!targets.length) { toast("Nenhum outro grupo disponível"); return; }
    if (manager) {
      [state.agents, state.memberships] = await Promise.all([
        api("/api/v1/agents"), api("/api/v1/groups/memberships"),
      ]);
    }
    $("#transfer-agent-field").hidden = !manager;
    $("#transfer-group").innerHTML = targets
      .map((group) => `<option value="${escapeHtml(group.id)}">${escapeHtml(group.name)}</option>`).join("");
    $("#transfer-group").value = state.selected.group_id;
    if (!$("#transfer-group").value) $("#transfer-group").selectedIndex = 0;
    if (manager) {
      populateTransferAgents();
      $("#transfer-agent").value = state.selected.assignee_id || "";
    } else {
      $("#transfer-agent").value = "";
    }
    $("#transfer-dialog").showModal();
  } catch (error) { toast(error.message); }
}

async function transferSelected(event) {
  event.preventDefault();
  const groupId = $("#transfer-group").value;
  const agentId = $("#transfer-agent").value;
  try {
    await api(`/api/v1/attendances/${state.selectedId}/transfer`, {
      method: "POST",
      body: JSON.stringify({ target_group_id: groupId, target_actor_id: agentId || null, expected_version: state.selected.version }),
    });
    $("#transfer-dialog").close();
    if (state.actor?.role === "ATENDENTE") {
      closeDetail();
      state.selectedId = null;
      state.selected = null;
      state.selectedContact = null;
      await loadBoard();
    } else {
      await refreshSelected();
    }
    toast("Atendimento transferido");
  } catch (error) { toast(error.message); await refreshSelected(); }
}

function contactTagOptions() {
  const tags = new Set();
  for (const contact of state.contacts) for (const tag of contact.tags) tags.add(tag);
  return [...tags].sort((a, b) => a.localeCompare(b, "pt-BR"));
}

function populateContactTagFilter() {
  const select = $("#contacts-tag-filter");
  const current = select.value;
  select.innerHTML = '<option value="">Todas</option>' + contactTagOptions()
    .map((tag) => `<option value="${escapeHtml(tag)}">${escapeHtml(tag)}</option>`).join("");
  select.value = current;
}

function filteredContacts() {
  const search = $("#contacts-search").value.trim().toLocaleLowerCase("pt-BR");
  const tag = $("#contacts-tag-filter").value;
  const sort = $("#contacts-sort").value;

  const items = state.contacts.filter((contact) => {
    const label = contact.display_name || contact.id;
    const searchable = `${label} ${contact.id} ${contact.phone || ""}`.toLocaleLowerCase("pt-BR");
    if (search && !searchable.includes(search)) return false;
    if (tag && !contact.tags.includes(tag)) return false;
    return true;
  });

  items.sort((a, b) => {
    if (sort === "oldest") return new Date(a.updated_at) - new Date(b.updated_at);
    if (sort === "contact") {
      return (a.display_name || a.id).localeCompare(b.display_name || b.id, "pt-BR");
    }
    return new Date(b.updated_at) - new Date(a.updated_at);
  });
  return items;
}

function renderContactsBoard() {
  const filtered = filteredContacts();
  $("#contacts-board").innerHTML = CONTACT_STAGES.map(([stage, label]) => {
    const items = filtered.filter((contact) => contact.stage === stage);
    const cards = items.length ? items.map((contact) => {
      const displayLabel = contact.display_name || contact.id;
      const tags = contact.tags.slice(0, 3).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("");
      return `
      <button class="card" draggable="true" data-contact-id="${escapeHtml(contact.id)}">
        <span class="card-main">
          <span class="avatar">${escapeHtml(initials(displayLabel))}</span>
          <span class="card-text">
            <span class="card-contact">${escapeHtml(displayLabel)}</span>
            <span class="card-preview">${escapeHtml(contact.phone || contact.id)}</span>
          </span>
        </span>
        ${tags ? `<span class="card-tags">${tags}</span>` : ""}
        <span class="card-foot"><span>${relativeTime(contact.updated_at)}</span></span>
      </button>
    `;
    }).join("") : '<div class="empty">Nenhum contato</div>';
    return `<section class="column"><header class="column-head"><h2>${label}</h2><span class="column-count">${items.length}</span></header><div class="column-body" data-stage="${stage}">${cards}</div></section>`;
  }).join("");

  document.querySelectorAll("[data-contact-id]").forEach((card) => {
    card.addEventListener("click", () => openContactDetail(card.dataset.contactId));
    card.addEventListener("dragstart", (event) => {
      event.dataTransfer.setData("text/plain", card.dataset.contactId);
      event.dataTransfer.effectAllowed = "move";
      card.classList.add("dragging");
    });
    card.addEventListener("dragend", () => card.classList.remove("dragging"));
  });

  document.querySelectorAll(".column-body[data-stage]").forEach((columnBody) => {
    columnBody.addEventListener("dragover", (event) => {
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      columnBody.classList.add("drop-target");
    });
    columnBody.addEventListener("dragleave", () => columnBody.classList.remove("drop-target"));
    columnBody.addEventListener("drop", async (event) => {
      event.preventDefault();
      columnBody.classList.remove("drop-target");
      const contactId = event.dataTransfer.getData("text/plain");
      const targetStage = columnBody.dataset.stage;
      const contact = state.contacts.find((item) => item.id === contactId);
      if (!contact || contact.stage === targetStage) return;
      try {
        await api(`/api/v1/contacts/${contactId}/stage`, {
          method: "PATCH",
          body: JSON.stringify({ stage: targetStage }),
        });
        await loadContacts();
        if (state.selectedContact?.id === contactId) {
          state.selectedContact.stage = targetStage;
          $("#profile-stage").value = targetStage;
        }
        toast("Estágio do contato atualizado");
      } catch (error) { toast(error.message); }
    });
  });
}

async function loadContacts() {
  try {
    state.contacts = await api("/api/v1/contacts");
    state.contactsLoaded = true;
    populateContactTagFilter();
    renderContactsBoard();
  } catch (error) { toast(error.message); }
}

async function openContactDetail(contactId) {
  const contact = state.contacts.find((item) => item.id === contactId);
  if (contact?.last_attendance_id) {
    await openDetail(contact.last_attendance_id);
    return;
  }
  toast("Contato ainda sem atendimento — inicie uma conversa");
  $("#inbound-contact").value = contactId;
  $("#inbound-dialog").showModal();
}

async function updateContactStage(stage) {
  if (!state.selectedContact) return;
  try {
    state.selectedContact = await api(`/api/v1/contacts/${state.selected.contact_id}/stage`, {
      method: "PATCH",
      body: JSON.stringify({ stage }),
    });
    if (state.contactsLoaded) await loadContacts();
    toast("Estágio do contato atualizado");
  } catch (error) {
    toast(error.message);
    $("#profile-stage").value = state.selectedContact.stage;
  }
}

function actionButton(label, status, css = "ghost") {
  return `<button class="button ${css}" data-status="${status}">${label}</button>`;
}

function renderDetail() {
  const item = state.selected;
  if (!item) return;
  const contact = state.selectedContact || { id: item.contact_id, display_name: null, phone: null, tags: [] };
  const displayName = contact.display_name || item.contact_id;
  const itemInitials = initials(displayName);
  const statusLabel = STATUSES.find(([key]) => key === item.status)?.[1] || item.status;

  $("#detail-avatar").textContent = itemInitials;
  $("#profile-avatar").textContent = itemInitials;
  $("#detail-contact").textContent = displayName;
  $("#profile-name").textContent = displayName;
  $("#profile-id").textContent = item.contact_id;
  $("#profile-phone").textContent = contact.phone || "Telefone não informado";
  $("#detail-status").textContent = statusLabel;
  $("#detail-stale").hidden = !item.stale;
  $("#detail-updated").textContent = formatDate(item.updated_at);
  $("#profile-protocol").textContent = protocolOf(item);
  $("#attendance-tags").innerHTML = (item.tags || []).length
    ? item.tags.map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")
    : '<span class="empty-tag">Sem tags do atendimento</span>';
  $("#profile-owner").textContent = item.assignee_id || "Não atribuído";
  $("#profile-queue").textContent = item.queue_id;
  $("#profile-team").textContent = item.team_id || "Não definida";
  $("#profile-group").textContent = state.groups.find((group) => group.id === item.group_id)?.name || item.queue_id;
  $("#profile-rating").textContent = item.rating ? `${item.rating.score}/5 estrelas` : "Ainda não avaliado";
  $("#profile-stage").value = contact.stage || "NAO_CLASSIFICADO";
  $("#compose-contact").textContent = item.contact_id;
  $("#profile-tags").innerHTML = contact.tags.length
    ? contact.tags.map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")
    : '<span class="empty-tag">Sem tags</span>';
  $("#profile-notes").innerHTML = (item.notes || []).length
    ? item.notes.map((note) => `<article class="internal-note"><p>${escapeHtml(note.content)}</p><small>${escapeHtml(note.actor_id)} · ${formatDate(note.created_at)}</small></article>`).join("")
    : '<span class="empty-note">Nenhuma nota</span>';

  $("#timeline").innerHTML = item.messages.length ? item.messages.map((message) => {
    const out = message.direction === "SAIDA";
    return `
    <article class="message ${out ? "out" : "in"}">
      <span class="message-row">
        <div class="message-bubble">${escapeHtml(message.content)}${(message.buttons || []).length
          ? `<div class="message-options">${message.buttons.map((button) => `<span class="quick-reply-chip">${escapeHtml(button.title)}</span>`).join("")}</div>`
          : ""}${message.attachment
          ? `<button class="text-button attachment-link" type="button" data-attachment-id="${escapeHtml(message.attachment.id)}" data-filename="${escapeHtml(message.attachment.filename)}">📎 ${escapeHtml(message.attachment.filename)}</button>`
          : ""}</div>
        ${out ? '<span class="sender-dot" title="Enviado por atendente humano" aria-hidden="true">👤</span>' : ""}
      </span>
      <small class="message-meta">${formatDate(message.created_at)} · ${out ? escapeHtml(message.delivery_status) : "Recebida"}</small>
    </article>
  `;
  }).join("") : '<div class="empty">Sem mensagens</div>';
  $("#timeline").scrollTop = $("#timeline").scrollHeight;
  $("#timeline").querySelectorAll("[data-attachment-id]").forEach((button) => {
    button.addEventListener("click", () => downloadAttachment(button.dataset.attachmentId, button.dataset.filename));
  });

  const actions = [];
  const manager = ["SUPERVISOR", "ADMIN"].includes(state.actor?.role);
  const canOperate = manager || item.assignee_id === state.actor?.id;
  if (item.status === "AGUARDANDO" && !item.assignee_id) actions.push('<button class="button primary" data-action="claim">Assumir atendimento</button>');
  if (item.status === "EM_ATENDIMENTO" && canOperate) {
    actions.push(actionButton("Aguardar cliente", "AGUARDANDO_CLIENTE"));
    actions.push(actionButton("Aguardar interno", "AGUARDANDO_INTERNO"));
    actions.push('<button class="button danger" data-action="close">Encerrar</button>');
  }
  if (["AGUARDANDO_CLIENTE", "AGUARDANDO_INTERNO"].includes(item.status) && canOperate) actions.push(actionButton("Retomar atendimento", "EM_ATENDIMENTO"));
  if (item.status !== "ENCERRADO" && canOperate) actions.push('<button class="button ghost" data-action="transfer">Redirecionar grupo</button>');
  $("#detail-actions").innerHTML = actions.join("") || '<small>Sem ações disponíveis</small>';

  const canSend = item.status !== "ENCERRADO" && canOperate;
  $("#open-composer").hidden = !canSend;
  $("#edit-attendance-tags").hidden = !canOperate;
  $("#edit-contact").hidden = !canOperate;
  $("[data-action='claim']")?.addEventListener("click", claimSelected);
  $("[data-action='transfer']")?.addEventListener("click", openTransferDialog);
  $("[data-action='close']")?.addEventListener("click", () => $("#closure-dialog").showModal());
  document.querySelectorAll("[data-status]").forEach((button) => button.addEventListener("click", () => changeStatus(button.dataset.status)));
}

async function openDetail(id) {
  state.selectedId = id;
  try {
    [state.selected, state.selectedContact] = await Promise.all([
      api(`/api/v1/attendances/${id}`),
      api(`/api/v1/contacts/${state.attendances.find((item) => item.id === id)?.contact_id || id}`),
    ]);
    renderDetail();
    $("#detail-panel").classList.add("open");
    $("#detail-panel").setAttribute("aria-hidden", "false");
    $("#backdrop").classList.add("open");
  } catch (error) { toast(error.message); }
}

function closeDetail() {
  $("#detail-panel").classList.remove("open");
  $("#detail-panel").setAttribute("aria-hidden", "true");
  $("#backdrop").classList.remove("open");
}

async function refreshSelected() {
  await loadBoard();
  if (state.contactsLoaded) await loadContacts();
  if (state.selectedId) {
    try {
      state.selected = await api(`/api/v1/attendances/${state.selectedId}`);
      state.selectedContact = await api(`/api/v1/contacts/${state.selected.contact_id}`);
      renderDetail();
    } catch (error) {
      if (![403, 404].includes(error.status)) throw error;
      closeDetail();
      state.selectedId = null;
      state.selected = null;
      state.selectedContact = null;
    }
  }
}

function openContactEditor() {
  const contact = state.selectedContact;
  if (!contact) return;
  $("#contact-name").value = contact.display_name || "";
  $("#contact-phone").value = contact.phone || "";
  $("#contact-tags").value = contact.tags.join(", ");
  $("#contact-dialog").showModal();
}

async function saveContact(event) {
  event.preventDefault();
  try {
    const tags = $("#contact-tags").value.split(",").map((tag) => tag.trim()).filter(Boolean);
    state.selectedContact = await api(`/api/v1/contacts/${state.selected.contact_id}`, {
      method: "PUT",
      body: JSON.stringify({
        display_name: $("#contact-name").value.trim() || null,
        phone: $("#contact-phone").value.trim() || null,
        tags,
      }),
    });
    $("#contact-dialog").close();
    await loadBoard();
    if (state.contactsLoaded) await loadContacts();
    renderDetail();
    toast("Contato atualizado");
  } catch (error) { toast(error.message); }
}

function openAttendanceTagsEditor() {
  if (!state.selected) return;
  $("#attendance-tags-input").value = (state.selected.tags || []).join(", ");
  $("#attendance-tags-dialog").showModal();
}

async function saveAttendanceTags(event) {
  event.preventDefault();
  try {
    const tags = $("#attendance-tags-input").value.split(",").map((tag) => tag.trim()).filter(Boolean);
    await api(`/api/v1/attendances/${state.selectedId}/tags`, {
      method: "PUT",
      body: JSON.stringify({ tags }),
    });
    $("#attendance-tags-dialog").close();
    await refreshSelected();
    toast("Tags do atendimento atualizadas");
  } catch (error) { toast(error.message); }
}

async function loadQuickReplies() {
  try {
    state.quickReplies = await api("/api/v1/quick-replies");
    renderQuickReplyChips();
  } catch (error) { toast(error.message); }
}

function renderQuickReplyChips() {
  const replies = state.quickReplies.filter((reply) => !reply.group_id || reply.group_id === state.selected?.group_id);
  $("#quick-replies-list").innerHTML = replies.length
    ? replies.map((reply) => `<button type="button" class="quick-reply-chip" data-quick-reply-id="${reply.id}">${escapeHtml(reply.title)}</button>`).join("")
    : "";
  document.querySelectorAll("[data-quick-reply-id]").forEach((chip) => {
    chip.addEventListener("click", () => {
      const reply = state.quickReplies.find((item) => item.id === chip.dataset.quickReplyId);
      if (reply) $("#message-content").value = reply.content;
    });
  });
}

function renderQuickReplyManageList() {
  const manager = ["SUPERVISOR", "ADMIN"].includes(state.actor?.role);
  $("#quick-reply-group").innerHTML = '<option value="">Todos os grupos</option>' + state.groups.map((group) => `<option value="${escapeHtml(group.id)}">${escapeHtml(group.name)}</option>`).join("");
  $("#quick-reply-manage-list").innerHTML = state.quickReplies.map((reply) => `
    <article class="quick-reply-item">
      <div><strong>${escapeHtml(reply.title)}</strong><small> · ${escapeHtml(state.groups.find((group) => group.id === reply.group_id)?.name || "Todos os grupos")}</small><p>${escapeHtml(reply.content)}</p></div>
      ${manager ? `<button type="button" class="text-button" data-edit-quick-reply="${reply.id}">Editar</button><button type="button" class="icon-button" data-remove-quick-reply="${reply.id}" aria-label="Remover">×</button>` : ""}
    </article>
  `).join("");
  $("#quick-reply-form").hidden = !manager;
  document.querySelectorAll("[data-edit-quick-reply]").forEach((button) => {
    button.addEventListener("click", () => {
      const reply = state.quickReplies.find((item) => item.id === button.dataset.editQuickReply);
      if (!reply) return;
      state.editingQuickReplyId = reply.id;
      $("#quick-reply-title").value = reply.title;
      $("#quick-reply-content").value = reply.content;
      $("#quick-reply-group").value = reply.group_id || "";
      $("#save-quick-reply").textContent = "Salvar resposta rápida";
      $("#cancel-quick-reply-edit").hidden = false;
    });
  });
  document.querySelectorAll("[data-remove-quick-reply]").forEach((button) => {
    button.addEventListener("click", () => deleteQuickReply(button.dataset.removeQuickReply));
  });
}

async function createQuickReply(event) {
  event.preventDefault();
  try {
    const id = state.editingQuickReplyId;
    await api(id ? `/api/v1/quick-replies/${id}` : "/api/v1/quick-replies", {
      method: id ? "PATCH" : "POST",
      body: JSON.stringify({
        title: $("#quick-reply-title").value.trim(),
        content: $("#quick-reply-content").value.trim(),
        group_id: $("#quick-reply-group").value || null,
      }),
    });
    cancelQuickReplyEdit();
    await loadQuickReplies();
    renderQuickReplyManageList();
    toast(id ? "Resposta rápida atualizada" : "Resposta rápida adicionada");
  } catch (error) { toast(error.message); }
}

function cancelQuickReplyEdit() {
  state.editingQuickReplyId = null;
  $("#quick-reply-title").value = "";
  $("#quick-reply-content").value = "";
  $("#quick-reply-group").value = "";
  $("#save-quick-reply").textContent = "Adicionar resposta rápida";
  $("#cancel-quick-reply-edit").hidden = true;
}

async function deleteQuickReply(id) {
  try {
    await api(`/api/v1/quick-replies/${id}`, { method: "DELETE" });
    await loadQuickReplies();
    renderQuickReplyManageList();
    toast("Resposta rápida removida");
  } catch (error) { toast(error.message); }
}

function renderOutboxList(items) {
  $("#outbox-list").innerHTML = items.map((item) => `
    <article class="outbox-item ${item.attempts >= item.max_attempts ? "exhausted" : ""}">
      <div class="outbox-item-text">
        <p>${escapeHtml(item.content)}</p>
        <small>Contato ${escapeHtml(item.contact_id)} · tentativa ${item.attempts}/${item.max_attempts} · ${escapeHtml(item.delivery_status)}</small>
      </div>
      <button type="button" class="button ghost" data-retry-outbox="${item.id}">Tentar novamente</button>
    </article>
  `).join("");
  document.querySelectorAll("[data-retry-outbox]").forEach((button) => {
    button.addEventListener("click", () => retryOutboxItem(button.dataset.retryOutbox));
  });
}

async function openOutboxDialog() {
  $("#outbox-dialog").showModal();
  try {
    renderOutboxList(await api("/api/v1/integrations/outbox"));
  } catch (error) { toast(error.message); }
}

async function retryOutboxItem(id) {
  try {
    await api(`/api/v1/integrations/outbox/${id}/retry`, { method: "POST" });
    renderOutboxList(await api("/api/v1/integrations/outbox"));
    toast("Item marcado para nova tentativa");
  } catch (error) { toast(error.message); }
}

async function processOutboxNow() {
  try {
    const result = await api("/api/v1/integrations/outbox/process", { method: "POST" });
    renderOutboxList(await api("/api/v1/integrations/outbox"));
    toast(`Processado: ${result.delivered} entregue(s), ${result.failed} falha(s)`);
    await refreshSelected();
  } catch (error) { toast(error.message); }
}

function requestNotificationPermission() {
  if ("Notification" in window && Notification.permission === "default") {
    Notification.requestPermission().catch(() => {});
  }
}

function notifyUser(title, body) {
  toast(body);
  if ("Notification" in window && Notification.permission === "granted") {
    new Notification(title, { body });
  }
}

function handleRealtimeEvent(raw) {
  let data;
  try { data = JSON.parse(raw); } catch { return; }
  const attendance = data.attendance;
  if (!attendance) return;
  const actorId = state.actor?.id;
  if (data.type === "attendance.created" && !attendance.assignee_id) {
    notifyUser("Novo atendimento", `Contato ${attendance.contact_id} está aguardando`);
  } else if (data.type === "attendance.updated" && attendance.assignee_id === actorId) {
    notifyUser("Cliente respondeu", `Contato ${attendance.contact_id} enviou nova mensagem`);
  }
}

async function claimSelected() {
  try {
    await api(`/api/v1/attendances/${state.selectedId}/claim`, { method: "POST", body: JSON.stringify({ expected_version: state.selected.version }) });
    await refreshSelected();
    toast("Atendimento assumido");
  } catch (error) { toast(error.message); await refreshSelected(); }
}

async function changeStatus(status, closure = {}) {
  try {
    await api(`/api/v1/attendances/${state.selectedId}/status`, { method: "PATCH", body: JSON.stringify({ status, expected_version: state.selected.version, ...closure }) });
    await refreshSelected();
    return true;
  } catch (error) { toast(error.message); await refreshSelected(); return false; }
}

async function addInternalNote(event) {
  event.preventDefault();
  const content = $("#note-content").value.trim();
  if (!content) return;
  try {
    await api(`/api/v1/attendances/${state.selectedId}/notes`, {
      method: "POST",
      body: JSON.stringify({ content }),
    });
    $("#note-content").value = "";
    $("#note-dialog").close();
    await refreshSelected();
    toast("Nota interna adicionada");
  } catch (error) { toast(error.message); }
}

async function closeAttendance(event) {
  event.preventDefault();
  try {
    const changed = await changeStatus("ENCERRADO", {
      closure_reason: $("#closure-reason").value,
      closure_note: $("#closure-note").value.trim() || null,
    });
    if (!changed) return;
    $("#closure-note").value = "";
    $("#closure-dialog").close();
    toast("Atendimento encerrado");
  } catch (error) { toast(error.message); }
}

async function sendMessage(event) {
  event.preventDefault();
  const content = $("#message-content").value.trim();
  const file = $("#message-file").files[0];
  const titles = $("#message-buttons").value.split(",").map((title) => title.trim()).filter(Boolean);
  if (titles.length > 3 || titles.some((title) => title.length > 20)) { toast("Use até 3 botões com 20 caracteres cada"); return; }
  if (file && titles.length) { toast("Botões só podem acompanhar mensagem de texto"); return; }
  if (!content && !file) return;
  try {
    if (file) {
      const form = new FormData();
      form.append("file", file);
      form.append("client_message_id", crypto.randomUUID());
      form.append("caption", content);
      await api(`/api/v1/attendances/${state.selectedId}/attachments`, { method: "POST", body: form });
    } else {
      await api(`/api/v1/attendances/${state.selectedId}/messages`, {
        method: "POST",
        body: JSON.stringify({ content, client_message_id: crypto.randomUUID(), buttons: titles.map((title) => ({ id: crypto.randomUUID(), title })) }),
      });
    }
    $("#message-content").value = "";
    $("#message-file").value = "";
    $("#message-buttons").value = "";
    $("#compose-dialog").close();
    await refreshSelected();
    toast("Mensagem registrada para envio");
  } catch (error) { toast(error.message); }
}

async function downloadAttachment(id, filename) {
  try {
    const response = await fetch(`/api/v1/attachments/${encodeURIComponent(id)}`, { headers: identity() });
    if (!response.ok) throw new Error("Anexo indisponível");
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = filename || "anexo";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (error) { toast(error.message); }
}

async function receiveInbound(event) {
  event.preventDefault();
  try {
    const suffix = crypto.randomUUID();
    const result = await api("/api/v1/inbox/messages", {
      method: "POST",
      body: JSON.stringify({
        external_event_id: `demo-event-${suffix}`,
        external_message_id: `demo-message-${suffix}`,
        contact_id: $("#inbound-contact").value.trim(),
        content: $("#inbound-content").value.trim(),
        group_id: $("#inbound-group").value,
      }),
    });
    $("#inbound-dialog").close();
    $("#inbound-content").value = "";
    await loadBoard();
    if (state.contactsLoaded) await loadContacts();
    await openDetail(result.attendance.id);
  } catch (error) { toast(error.message); }
}

function renderSessionBadge() {
  if (!state.actor) return;
  $("#session-actor").textContent = `${state.actor.id} · ${state.actor.role}`;
}


async function loadCompany() {
  try {
    const company = await api("/api/v1/me/company");
    $("#company-logo").textContent = company.name.trim().charAt(0).toLocaleUpperCase("pt-BR") || "C";
    $("#company-logo").setAttribute("aria-label", `${company.name} · Atendimento`);
    document.title = `${company.name} · Atendimento`;
  } catch (error) { toast(error.message); }
}

function showLoginDialog() {
  $("#login-dialog").showModal();
  $("#login-id").focus();
}

async function login(event) {
  event.preventDefault();
  try {
    const result = await api("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({
        id: $("#login-id").value.trim(),
        password: $("#login-password").value,
      }),
    });
    saveSession(result.access_token, result.actor);
    $("#login-password").value = "";
    $("#login-dialog").close();
    boot();
  } catch (error) { toast(error.message); }
}

function logout() {
  clearSession();
  location.reload();
}

function boot() {
  renderSessionBadge();
  loadCompany();
  $("#nav-team").hidden = state.actor?.role === "ATENDENTE";
  $("#outbox-button").hidden = state.actor?.role === "ATENDENTE";
  $("#new-group-button").hidden = state.actor?.role !== "ADMIN";
  loadGroups();
  loadBoard();
  loadQuickReplies();
  requestNotificationPermission();
  connectRealtime();
}

async function pullNext() {
  const groupId = $("#group-filter").value || (state.groups.length === 1 ? state.groups[0].id : null);
  if (!groupId) { toast("Selecione um grupo para puxar atendimento"); return; }
  try {
    const response = await fetch(`/api/v1/groups/${encodeURIComponent(groupId)}/attendances/pull`, {
      method: "POST", headers: identity(),
    });
    if (response.status === 204) { toast("Fila vazia"); return; }
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }
    const attendance = await response.json();
    await loadBoard();
    await openDetail(attendance.id);
    toast("Atendimento atribuído");
  } catch (error) { toast(error.message); }
}

let toastTimer;
function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("show"), 2600);
}

function connectRealtime() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${location.host}/api/v1/ws`);
  socket.onopen = () => {
    socket.send(JSON.stringify({ token: state.token }));
    $("#connection").textContent = "tempo real";
    $("#connection").className = "connection online";
  };
  socket.onmessage = (event) => {
    handleRealtimeEvent(event.data);
    refreshSelected().catch(() => loadBoard());
  };
  socket.onclose = (event) => {
    if (event.code === 1008) {
      clearSession();
      location.reload();
      return;
    }
    $("#connection").textContent = "reconectando";
    $("#connection").className = "connection offline";
    if (state.token) setTimeout(connectRealtime, 1500);
  };
}

$("#close-detail").addEventListener("click", closeDetail);
$("#backdrop").addEventListener("click", closeDetail);
$("#edit-contact").addEventListener("click", openContactEditor);
$("#contact-form").addEventListener("submit", saveContact);
$("#close-contact-dialog").addEventListener("click", () => $("#contact-dialog").close());
$("#add-note").addEventListener("click", () => $("#note-dialog").showModal());
$("#note-form").addEventListener("submit", addInternalNote);
$("#close-note-dialog").addEventListener("click", () => $("#note-dialog").close());
$("#closure-form").addEventListener("submit", closeAttendance);
$("#close-closure-dialog").addEventListener("click", () => $("#closure-dialog").close());
$("#compose-form").addEventListener("submit", sendMessage);
$("#pull-button").addEventListener("click", pullNext);
$("#group-filter").addEventListener("change", renderBoard);
$("#open-composer").addEventListener("click", () => { renderQuickReplyChips(); $("#compose-dialog").showModal(); });
$("#close-compose-dialog").addEventListener("click", () => $("#compose-dialog").close());
$("#edit-attendance-tags").addEventListener("click", openAttendanceTagsEditor);
$("#attendance-tags-form").addEventListener("submit", saveAttendanceTags);
$("#close-attendance-tags-dialog").addEventListener("click", () => $("#attendance-tags-dialog").close());
$("#manage-quick-replies").addEventListener("click", () => { renderQuickReplyManageList(); $("#quick-reply-dialog").showModal(); });
$("#close-quick-reply-dialog").addEventListener("click", () => $("#quick-reply-dialog").close());
$("#quick-reply-form").addEventListener("submit", createQuickReply);
$("#cancel-quick-reply-edit").addEventListener("click", cancelQuickReplyEdit);
$("#outbox-button").addEventListener("click", openOutboxDialog);
$("#close-outbox-dialog").addEventListener("click", () => $("#outbox-dialog").close());
$("#process-outbox").addEventListener("click", processOutboxNow);
$("#new-message-button").addEventListener("click", () => {
  $("#inbound-contact").value = "contato-demo";
  $("#inbound-dialog").showModal();
});
$("#close-inbound-dialog").addEventListener("click", () => $("#inbound-dialog").close());
$("#inbound-form").addEventListener("submit", receiveInbound);
$("#refresh-button").addEventListener("click", () => (state.view === "contacts" ? loadContacts() : loadBoard()));
$("#login-form").addEventListener("submit", login);
$("#login-dialog").addEventListener("cancel", (event) => event.preventDefault());
$("#logout-button").addEventListener("click", logout);
for (const id of ["#search", "#attendance-tag-filter", "#owner-filter", "#status-filter", "#sort-filter"]) {
  $(id).addEventListener(id === "#search" ? "input" : "change", renderBoard);
}
$("#profile-stage").addEventListener("change", (event) => updateContactStage(event.target.value));
$("#nav-attendances").addEventListener("click", () => switchView("attendances"));
$("#nav-contacts").addEventListener("click", () => switchView("contacts"));
$("#nav-team").addEventListener("click", () => switchView("team"));
$("#new-agent-button").addEventListener("click", () => openAgentDialog());
$("#agent-form").addEventListener("submit", saveAgent);
$("#close-agent-dialog").addEventListener("click", () => $("#agent-dialog").close());
$("#new-group-button").addEventListener("click", () => openGroupDialog());
$("#group-form").addEventListener("submit", saveGroup);
$("#close-group-dialog").addEventListener("click", () => $("#group-dialog").close());
$("#membership-form").addEventListener("submit", saveMembership);
$("#close-membership-dialog").addEventListener("click", () => $("#membership-dialog").close());
$("#transfer-group").addEventListener("change", populateTransferAgents);
$("#transfer-form").addEventListener("submit", transferSelected);
$("#close-transfer-dialog").addEventListener("click", () => $("#transfer-dialog").close());
$("#agents-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-edit-agent]");
  if (button) openAgentDialog(button.dataset.editAgent);
});
$("#groups-list").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.dataset.editGroup) openGroupDialog(button.dataset.editGroup);
  if (button.dataset.linkGroup) openMembershipDialog(button.dataset.linkGroup);
  if (button.dataset.editMembership) openMembershipDialog(button.dataset.editMembership, button.dataset.agentId);
  if (button.dataset.unlinkGroup) unlinkMembership(button.dataset.unlinkGroup, button.dataset.agentId);
});
for (const id of ["#contacts-search", "#contacts-tag-filter", "#contacts-sort"]) {
  $(id).addEventListener(id === "#contacts-search" ? "input" : "change", renderContactsBoard);
}

if (loadSession()) {
  boot();
} else {
  showLoginDialog();
}
