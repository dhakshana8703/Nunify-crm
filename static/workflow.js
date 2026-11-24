"use strict";

for (const form of document.querySelectorAll("[data-activity-form]")) {
  const channel = form.querySelector("[data-activity-channel]");
  const outcome = form.querySelector("[data-activity-outcome]");

  function showChannelOutcomes() {
    for (const group of outcome.querySelectorAll("[data-channel]")) {
      const matchesChannel = group.dataset.channel === channel.value;
      group.disabled = !matchesChannel;
      group.hidden = !matchesChannel;
    }

    const current = outcome.options[outcome.selectedIndex];
    if (!current || current.parentElement.disabled) {
      const enabled = Array.from(outcome.options).find((option) => !option.parentElement.disabled);
      if (enabled) {
        enabled.selected = true;
      }
    }
  }

  channel.addEventListener("change", showChannelOutcomes);
  showChannelOutcomes();

  const tomorrow = form.elements.namedItem("due_tomorrow");
  if (tomorrow) {
    const dueDate = form.elements.namedItem("due_on");
    function updateDueDate() {
      dueDate.disabled = tomorrow.checked;
    }
    tomorrow.addEventListener("change", updateDueDate);
    updateDueDate();
  }
}

const assignmentFilters = document.querySelector("[data-assignment-filters]");
if (assignmentFilters) {
  const company = assignmentFilters.elements.namedItem("company");
  const results = document.querySelector("[data-assignment-results]");
  const count = document.querySelector("[data-assignment-count]");
  let pending;

  assignmentFilters.addEventListener("change", refreshResults);

  async function refreshResults() {
    pending?.abort();
    count.textContent = "Updating results…";
    results.inert = true;
    results.setAttribute("aria-busy", "true");
    const url = new URL(assignmentFilters.action);
    const filters = new FormData(assignmentFilters);
    for (const [name, value] of filters) {
      if (value !== "" && name !== "page") url.searchParams.append(name, value);
    }
    const request = new AbortController();
    pending = request;
    try {
      const response = await fetch(url, { signal: request.signal, redirect: "error" });
      if (!response.ok) throw new Error("Could not load assignments");
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      const updatedFilters = page.querySelector("[data-assignment-filters]");
      const updatedResults = page.querySelector("[data-assignment-results]");
      const updatedCount = page.querySelector("[data-assignment-count]");
      if (!updatedFilters || !updatedResults || !updatedCount) throw new Error("Missing assignment results");
      if (request.signal.aborted) return;

      for (const select of assignmentFilters.querySelectorAll("select")) {
        const updatedSelect = updatedFilters.elements.namedItem(select.name);
        if (!updatedSelect) continue;
        const value = updatedSelect.value;
        if (select.innerHTML !== updatedSelect.innerHTML) {
          select.replaceChildren(...updatedSelect.childNodes);
        }
        select.value = value;
      }
      if (company.value) url.searchParams.set("company", company.value);
      else url.searchParams.delete("company");
      results.replaceChildren(...updatedResults.childNodes);
      results.hidden = false;
      count.textContent = updatedCount.textContent;
      window.history.replaceState(window.history.state, "", url);
    } catch {
      if (!request.signal.aborted) {
        results.hidden = true;
        count.textContent = "Could not update results. Use Apply to retry.";
      }
    } finally {
      if (!request.signal.aborted) {
        results.inert = false;
        results.removeAttribute("aria-busy");
      }
    }
  }
}

const sourceRun = document.querySelector("[data-source-run-status]");
if (sourceRun) {
  const notice = sourceRun.querySelector("[role=status]");
  const originalNotice = notice.innerHTML;

  async function checkSourceRun() {
    if (!document.hidden) {
      try {
        const response = await fetch(sourceRun.dataset.sourceRunStatus, {
          redirect: "error",
          signal: AbortSignal.timeout(8000),
        });
        if (!response.ok) throw new Error("Could not check the source run");
        const { state, attempts } = await response.json();
        const changed = state !== sourceRun.dataset.state || String(attempts) !== sourceRun.dataset.attempts;
        if (changed) {
          const readingRecords = document.querySelector("[data-source-run-records] details[open]");
          const editingFilters = document.querySelector("[data-source-run-filters]:focus-within");
          if (!readingRecords && !editingFilters) {
            window.location.reload();
            return;
          }
          notice.textContent = "Run status changed. Refresh status to see the update.";
        } else if (notice.innerHTML !== originalNotice) {
          notice.innerHTML = originalNotice;
        }
      } catch {
        notice.textContent = "Could not check the run automatically. Use Refresh status to try again.";
      }
    }
    setTimeout(checkSourceRun, 3000);
  }

  setTimeout(checkSourceRun, 3000);
}
