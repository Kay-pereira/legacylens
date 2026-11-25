/* LegacyLens Q&A — local interface controller.
   No dependencies, no CDN, no innerHTML for dynamic text. */

(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const elements = {
    form: $("#ask-form"),
    question: $("#question"),
    mode: $("#mode"),
    modeLlm: $("#mode-llm"),
    limit: $("#limit"),
    page: $("#page"),
    askButton: $("#ask-button"),
    searchButton: $("#search-button"),
    kindFilters: $("#kind-filters"),
    healthPassages: $("#health-passages"),
    healthProvider: $("#health-provider"),
    healthVersion: $("#health-version"),
    error: $("#error-banner"),
    workspace: $("#workspace"),
    empty: $("#empty-state"),
    answerRegion: $("#answer-region"),
    answerStatus: $("#answer-status"),
    answerMode: $("#answer-mode"),
    answerText: $("#answer-text"),
    insufficient: $("#insufficient-note"),
    citations: $("#answer-citations"),
    unresolved: $("#unresolved-note"),
    copyAnswer: $("#copy-answer"),
    evidenceRegion: $("#evidence-region"),
    evidenceCount: $("#evidence-count"),
    evidenceMode: $("#evidence-mode"),
    evidenceList: $("#evidence-list"),
  };

  const state = {
    passages: [],
    answer: "",
    controller: null,
    targetTimer: 0,
  };

  const statusLabels = {
    verified: "Verified",
    inferred: "Inferred",
    unsupported: "Unsupported",
  };

  const statusClasses = ["badge-neutral", "badge-verified", "badge-inferred", "badge-unsupported"];

  /* ---------------------------------------------------------------- */
  /* Helpers                                                          */
  /* ---------------------------------------------------------------- */

  function showError(message) {
    elements.error.textContent = message;
    elements.error.hidden = false;
  }

  function clearError() {
    elements.error.textContent = "";
    elements.error.hidden = true;
  }

  function setBusy(busy, label) {
    elements.askButton.disabled = busy;
    elements.searchButton.disabled = busy;
    const text = $("span", elements.askButton);
    if (text) {
      text.textContent = busy ? label : "Ask";
    }
  }

  function selectedKinds() {
    return $$(".chip[aria-pressed='true']", elements.kindFilters)
      .map((chip) => chip.dataset.kind)
      .filter(Boolean);
  }

  function detailMessage(payload, fallback) {
    if (!payload) {
      return fallback;
    }
    const detail = payload.detail;
    if (typeof detail === "string") {
      return detail;
    }
    if (Array.isArray(detail) && detail.length) {
      return detail
        .map((item) => (item && typeof item.msg === "string" ? item.msg : null))
        .filter(Boolean)
        .join(" ");
    }
    return fallback;
  }

  async function requestJson(url, options = {}) {
    const response = await fetch(url, {
      headers: { Accept: "application/json" },
      ...options,
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    if (!response.ok) {
      throw new Error(detailMessage(payload, `Request failed (${response.status}).`));
    }
    return payload;
  }

  /* ---------------------------------------------------------------- */
  /* Health and filters                                               */
  /* ---------------------------------------------------------------- */

  async function loadHealth() {
    try {
      const health = await requestJson("/health");
      const index = health.index || {};
      const provider = health.provider || {};
      const modes = Array.isArray(health.modes) ? health.modes : ["extractive"];

      elements.healthPassages.textContent = `${index.passages ?? 0} passages indexed`;
      elements.healthProvider.textContent = `Provider: ${provider.name || "none"}`;
      elements.healthVersion.textContent = `v${health.version || "0.1.0"}`;

      if (modes.includes("llm") && elements.modeLlm) {
        elements.modeLlm.disabled = false;
        elements.modeLlm.textContent = `LLM · ${provider.model || provider.name || "configured"}`;
      }

      const pages = Array.isArray(index.docs_pages) ? index.docs_pages : [];
      for (const page of pages) {
        const option = document.createElement("option");
        option.value = page;
        option.textContent = page;
        elements.page.appendChild(option);
      }
    } catch (error) {
      elements.healthPassages.textContent = "Index unavailable";
      showError(`Could not reach the local service: ${error.message}`);
    }
  }

  elements.kindFilters.addEventListener("click", (event) => {
    const chip = event.target.closest(".chip");
    if (!chip) {
      return;
    }
    const isAll = chip.dataset.kind === "";
    if (isAll) {
      for (const item of $$(".chip", elements.kindFilters)) {
        item.setAttribute("aria-pressed", String(item === chip));
      }
      return;
    }
    chip.setAttribute("aria-pressed", chip.getAttribute("aria-pressed") === "true" ? "false" : "true");
    const all = $(".chip[data-kind='']", elements.kindFilters);
    if (all) {
      all.setAttribute("aria-pressed", "false");
    }
    if (!selectedKinds().length && all) {
      all.setAttribute("aria-pressed", "true");
    }
  });

  /* ---------------------------------------------------------------- */
  /* Asking and searching                                             */
  /* ---------------------------------------------------------------- */

  elements.form.addEventListener("submit", (event) => {
    event.preventDefault();
    ask();
  });

  elements.question.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      ask();
    }
  });

  elements.searchButton.addEventListener("click", () => {
    search();
  });

  for (const card of $$(".example-card")) {
    card.addEventListener("click", () => {
      elements.question.value = card.dataset.question || card.textContent.trim();
      elements.question.focus();
      ask();
    });
  }

  async function ask() {
    const question = elements.question.value.trim();
    if (!question) {
      showError("Enter a question first.");
      elements.question.focus();
      return;
    }

    clearError();
    setBusy(true, "Asking…");
    state.controller?.abort();
    state.controller = new AbortController();

    try {
      const payload = await requestJson("/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question,
          mode: elements.mode.value,
          limit: Number(elements.limit.value) || 8,
          kinds: selectedKinds(),
          page: elements.page.value || null,
        }),
        signal: state.controller.signal,
      });
      renderAnswer(payload);
    } catch (error) {
      if (error.name !== "AbortError") {
        showError(error.message);
      }
    } finally {
      setBusy(false);
    }
  }

  async function search() {
    const question = elements.question.value.trim();
    if (!question) {
      showError("Enter a search query first.");
      elements.question.focus();
      return;
    }

    clearError();
    setBusy(true, "Searching…");
    state.controller?.abort();
    state.controller = new AbortController();

    const params = new URLSearchParams({
      q: question,
      limit: String(Math.max(10, Number(elements.limit.value) || 8)),
    });
    for (const kind of selectedKinds()) {
      params.append("kind", kind);
    }
    if (elements.page.value) {
      params.set("page", elements.page.value);
    }

    try {
      const payload = await requestJson(`/search?${params.toString()}`, {
        signal: state.controller.signal,
      });
      elements.empty.hidden = true;
      elements.workspace.hidden = false;
      renderEvidence(payload.passages || [], `Search results · ${payload.count ?? 0} passages`);
      elements.evidenceRegion.hidden = false;
      elements.evidenceRegion.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      if (error.name !== "AbortError") {
        showError(error.message);
      }
    } finally {
      setBusy(false);
    }
  }

  /* ---------------------------------------------------------------- */
  /* Answer rendering                                                 */
  /* ---------------------------------------------------------------- */

  function renderAnswer(payload) {
    state.answer = typeof payload.answer === "string" ? payload.answer : "";

    setStatus(payload.status);
    elements.answerMode.textContent = `${payload.mode} · ${payload.cached ? "cached" : "fresh"}`;
    elements.answerText.textContent = state.answer;
    elements.copyAnswer.disabled = !state.answer;

    elements.insufficient.hidden = !payload.insufficient_evidence;

    elements.citations.replaceChildren();
    const citations = Array.isArray(payload.citations) ? payload.citations : [];
    for (const item of citations) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "citation";
      button.textContent = item.citation;
      button.dataset.citation = item.citation;
      if (item.passage_id) {
        button.dataset.passageId = item.passage_id;
      }
      button.title = item.title ? `Show evidence: ${item.title}` : "Show evidence";
      button.addEventListener("click", () => focusEvidence(item));
      elements.citations.appendChild(button);
    }

    if (citations.length) {
      elements.citations.hidden = false;
    } else {
      const note = document.createElement("p");
      note.className = "muted";
      note.textContent = "No citations were returned for this answer.";
      elements.citations.appendChild(note);
    }

    const unresolved = Array.isArray(payload.unresolved_citations) ? payload.unresolved_citations : [];
    if (unresolved.length) {
      elements.unresolved.hidden = false;
      elements.unresolved.textContent =
        "Citations that were not part of the retrieved evidence: " + unresolved.join(", ");
    } else {
      elements.unresolved.hidden = true;
      elements.unresolved.textContent = "";
    }

    const passages = Array.isArray(payload.passages) ? payload.passages : [];
    renderEvidence(passages, `Retrieved evidence · ${passages.length} passages`);

    elements.empty.hidden = true;
    elements.workspace.hidden = false;
    elements.answerRegion.hidden = false;
    elements.evidenceRegion.hidden = false;
  }

  function setStatus(status) {
    const value = typeof status === "string" ? status : "unsupported";
    elements.answerStatus.textContent = statusLabels[value] || value;
    elements.answerStatus.classList.remove(...statusClasses);
    elements.answerStatus.classList.add(`badge-${value}`);
  }

  /* ---------------------------------------------------------------- */
  /* Evidence rendering                                               */
  /* ---------------------------------------------------------------- */

  function renderEvidence(passages, label) {
    state.passages = passages;
    elements.evidenceList.replaceChildren();
    elements.evidenceCount.textContent = label || `${passages.length} passages`;
    elements.evidenceMode.textContent = passages.length ? "BM25 · lexical" : "";

    if (!passages.length) {
      const empty = document.createElement("p");
      empty.className = "empty-note";
      empty.textContent =
        "No indexed evidence matched. Try a file name, function, route, table or claim.";
      elements.evidenceList.appendChild(empty);
      return;
    }

    const fragment = document.createDocumentFragment();
    passages.forEach((passage, index) => {
      fragment.appendChild(evidenceCard(passage, index));
    });
    elements.evidenceList.appendChild(fragment);
  }

  function evidenceCard(passage, index) {
    const card = document.createElement("article");
    card.className = "evidence-card";
    card.dataset.passageId = passage.id || "";
    card.dataset.citation = passage.citation || "";
    card.dataset.index = String(index);

    const head = document.createElement("div");
    head.className = "evidence-head";

    const kind = document.createElement("span");
    kind.className = "evidence-kind";
    kind.textContent = passage.kind || "evidence";

    const title = document.createElement("h3");
    title.className = "evidence-title";
    title.textContent = passage.title || passage.citation || "Untitled evidence";

    const score = document.createElement("span");
    score.className = "evidence-score";
    score.textContent = typeof passage.score === "number" ? passage.score.toFixed(2) : "";

    head.append(kind, title, score);

    const citation = document.createElement("button");
    citation.type = "button";
    citation.className = "evidence-citation";
    citation.textContent = passage.citation || "";
    citation.title = "Copy citation";
    citation.addEventListener("click", () => copyText(citation, passage.citation || ""));

    const text = document.createElement("div");
    text.className = "evidence-text";
    text.textContent = passage.text || "";

    const foot = document.createElement("div");
    foot.className = "evidence-foot";

    const terms = document.createElement("div");
    terms.className = "term-list";
    for (const term of Array.isArray(passage.matched_terms) ? passage.matched_terms.slice(0, 6) : []) {
      const chip = document.createElement("span");
      chip.className = "term";
      chip.textContent = term;
      terms.appendChild(chip);
    }

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "link-button";
    toggle.textContent = "Expand";
    toggle.addEventListener("click", () => {
      const expanded = card.classList.toggle("is-expanded");
      toggle.textContent = expanded ? "Collapse" : "Expand";
    });

    foot.append(terms, toggle);
    card.append(head, citation, text, foot);
    return card;
  }

  function focusEvidence(item) {
    const target = findEvidence(item);
    if (!target) {
      showError("That citation is not part of the current evidence list.");
      return;
    }
    target.classList.add("is-expanded", "is-target");
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    window.clearTimeout(state.targetTimer);
    state.targetTimer = window.setTimeout(() => {
      target.classList.remove("is-target");
    }, 2200);
  }

  function findEvidence(item) {
    const cards = $$(".evidence-card", elements.evidenceList);
    if (item.passage_id) {
      const byId = cards.find((card) => card.dataset.passageId === item.passage_id);
      if (byId) {
        return byId;
      }
    }
    return cards.find((card) => card.dataset.citation === item.citation) || null;
  }

  /* ---------------------------------------------------------------- */
  /* Clipboard                                                        */
  /* ---------------------------------------------------------------- */

  elements.copyAnswer.addEventListener("click", async () => {
    if (!state.answer) {
      return;
    }
    await copyText(elements.copyAnswer, state.answer, "Copy answer", "Copied");
  });

  async function copyText(button, value, idle, done) {
    if (!value) {
      return;
    }
    const original = idle || button.textContent;
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      showError("Clipboard access was blocked by the browser.");
      return;
    }
    button.textContent = done || "Copied";
    window.setTimeout(() => {
      button.textContent = original;
    }, 1400);
  }

  /* ---------------------------------------------------------------- */
  /* Start                                                            */
  /* ---------------------------------------------------------------- */

  loadHealth();
})();
