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
  quickReplies: [],
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
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...identity(), ...(options.headers || {}) },
  });
  if (response.status === 401 && path !== "/api/v1/auth/login") {
    clearSession();
    location.reload();
    throw new Error("Sessão expirada");
  }
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: "Falha inesperada" }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }
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

function filteredAttendances() {
  const search = $("#search").value.trim().toLocaleLowerCase("pt-BR");
  const tag = $("#attendance-tag-filter").value;
  const owner = $("#owner-filter").value;
  const status = $("#status-filter").value;
  const sort = $("#sort-filter").value;
  const actorId = state.actor?.id;

  const items = state.attendances.filter((item) => {
    const searchable = `${item.contact_display_name || ""} ${item.contact_id} ${item.contact_phone || ""} ${item.id} ${protocolOf(item)}`.toLocaleLowerCase("pt-BR");
    if (search && !searchable.includes(search)) return false;
    if (tag && !(item.tags || []).includes(tag)) return false;
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
      return `
      <button class="card ${item.stale ? "stale" : ""}" data-attendance-id="${item.id}">
        <span class="card-main">
          <span class="avatar">${escapeHtml(initials(contactLabel(item)))}</span>
          <span class="card-text">
            <span class="card-contact">${escapeHtml(contactLabel(item))}${item.stale ? ' <span class="stale-pill">⏰</span>' : ""}</span>
            <span class="card-preview">${escapeHtml(latestPreview(item))}</span>
          </span>
        </span>
        ${tags ? `<span class="card-tags">${tags}</span>` : ""}
        <span class="card-foot">
          <span><i class="owner-dot ${item.assignee_id ? "assigned" : ""}"></i>${escapeHtml(item.assignee_id || "Sem responsável")}</span>
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
  $("#attendances-view").hidden = view !== "attendances";
  $("#contacts-view").hidden = view !== "contacts";
  if (view === "attendances") {
    $("#page-title").textContent = "Atendimentos";
    $("#page-subtitle").textContent = "Acompanhe e responda conversas em um só lugar.";
  } else {
    $("#page-title").textContent = "Contatos";
    $("#page-subtitle").textContent = "Classifique contatos e retome conversas.";
    if (!state.contactsLoaded) loadContacts();
  }
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
  $("#profile-group").textContent = `Fila ${item.queue_id}`;
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
        <div class="message-bubble">${escapeHtml(message.content)}</div>
        ${out ? '<span class="sender-dot" title="Enviado por atendente humano" aria-hidden="true">👤</span>' : ""}
      </span>
      <small class="message-meta">${formatDate(message.created_at)} · ${out ? escapeHtml(message.delivery_status) : "Recebida"}</small>
    </article>
  `;
  }).join("") : '<div class="empty">Sem mensagens</div>';
  $("#timeline").scrollTop = $("#timeline").scrollHeight;

  const actions = [];
  if (item.status === "AGUARDANDO" && !item.assignee_id) actions.push('<button class="button primary" data-action="claim">Assumir atendimento</button>');
  if (item.status === "EM_ATENDIMENTO") {
    actions.push(actionButton("Aguardar cliente", "AGUARDANDO_CLIENTE"));
    actions.push(actionButton("Aguardar interno", "AGUARDANDO_INTERNO"));
    actions.push('<button class="button danger" data-action="close">Encerrar</button>');
  }
  if (["AGUARDANDO_CLIENTE", "AGUARDANDO_INTERNO"].includes(item.status)) actions.push(actionButton("Retomar atendimento", "EM_ATENDIMENTO"));
  $("#detail-actions").innerHTML = actions.join("") || '<small>Sem ações disponíveis</small>';

  const canSend = item.status !== "ENCERRADO" && Boolean(item.assignee_id);
  $("#open-composer").hidden = !canSend;
  $("[data-action='claim']")?.addEventListener("click", claimSelected);
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
    state.selected = await api(`/api/v1/attendances/${state.selectedId}`);
    state.selectedContact = await api(`/api/v1/contacts/${state.selected.contact_id}`);
    renderDetail();
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
  $("#quick-replies-list").innerHTML = state.quickReplies.length
    ? state.quickReplies.map((reply) => `<button type="button" class="quick-reply-chip" data-quick-reply-id="${reply.id}">${escapeHtml(reply.title)}</button>`).join("")
    : "";
  document.querySelectorAll("[data-quick-reply-id]").forEach((chip) => {
    chip.addEventListener("click", () => {
      const reply = state.quickReplies.find((item) => item.id === chip.dataset.quickReplyId);
      if (reply) $("#message-content").value = reply.content;
    });
  });
}

function renderQuickReplyManageList() {
  $("#quick-reply-manage-list").innerHTML = state.quickReplies.map((reply) => `
    <article class="quick-reply-item">
      <div><strong>${escapeHtml(reply.title)}</strong><p>${escapeHtml(reply.content)}</p></div>
      <button type="button" class="icon-button" data-remove-quick-reply="${reply.id}" aria-label="Remover">×</button>
    </article>
  `).join("");
  document.querySelectorAll("[data-remove-quick-reply]").forEach((button) => {
    button.addEventListener("click", () => deleteQuickReply(button.dataset.removeQuickReply));
  });
}

async function createQuickReply(event) {
  event.preventDefault();
  try {
    await api("/api/v1/quick-replies", {
      method: "POST",
      body: JSON.stringify({
        title: $("#quick-reply-title").value.trim(),
        content: $("#quick-reply-content").value.trim(),
      }),
    });
    $("#quick-reply-title").value = "";
    $("#quick-reply-content").value = "";
    await loadQuickReplies();
    renderQuickReplyManageList();
    toast("Resposta rápida adicionada");
  } catch (error) { toast(error.message); }
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
  if (!content) return;
  try {
    await api(`/api/v1/attendances/${state.selectedId}/messages`, {
      method: "POST",
      body: JSON.stringify({ content, client_message_id: crypto.randomUUID() }),
    });
    $("#message-content").value = "";
    $("#compose-dialog").close();
    await refreshSelected();
    toast("Mensagem registrada para envio");
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
  loadBoard();
  loadQuickReplies();
  requestNotificationPermission();
  connectRealtime();
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
  socket.onopen = () => { $("#connection").textContent = "tempo real"; $("#connection").className = "connection online"; };
  socket.onmessage = (event) => {
    handleRealtimeEvent(event.data);
    refreshSelected().catch(() => loadBoard());
  };
  socket.onclose = () => {
    $("#connection").textContent = "reconectando";
    $("#connection").className = "connection offline";
    setTimeout(connectRealtime, 1500);
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
$("#open-composer").addEventListener("click", () => { renderQuickReplyChips(); $("#compose-dialog").showModal(); });
$("#close-compose-dialog").addEventListener("click", () => $("#compose-dialog").close());
$("#edit-attendance-tags").addEventListener("click", openAttendanceTagsEditor);
$("#attendance-tags-form").addEventListener("submit", saveAttendanceTags);
$("#close-attendance-tags-dialog").addEventListener("click", () => $("#attendance-tags-dialog").close());
$("#manage-quick-replies").addEventListener("click", () => { renderQuickReplyManageList(); $("#quick-reply-dialog").showModal(); });
$("#close-quick-reply-dialog").addEventListener("click", () => $("#quick-reply-dialog").close());
$("#quick-reply-form").addEventListener("submit", createQuickReply);
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
for (const id of ["#contacts-search", "#contacts-tag-filter", "#contacts-sort"]) {
  $(id).addEventListener(id === "#contacts-search" ? "input" : "change", renderContactsBoard);
}

if (loadSession()) {
  boot();
} else {
  showLoginDialog();
}
