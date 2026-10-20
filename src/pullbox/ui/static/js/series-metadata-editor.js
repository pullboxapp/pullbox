/* Reviewed series corrections; comic metadata is deliberately outside this workflow. */
function seriesMetadataEditor(seriesId) {
  const endpoint = "/api/v1/series/" + seriesId + "/metadata-editor";
  return {
    open: false, busy: false, checkingJob: true, error: "", preview: null, drafts: {},
    mode: "library_and_sidecars", allFields: false, reviewedInputs: null,
    job: null, librarySaved: false, jobTargets: [], jobWarnings: [], partial: false, retryable: false,
    trigger: null, timer: null, generation: 0, jobGeneration: 0, disposed: false,
    init() { this.loadJob().finally(() => { this.checkingJob = false; }); },
    destroy() { this.disposed = true; this.generation += 1; this.jobGeneration += 1; clearTimeout(this.timer); },
    async request(url, body) {
      const response = await fetch(url, body === undefined ? { cache: "no-store" } : {
        method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": readCsrfTokenFromBody() },
        body: JSON.stringify(body),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(_extractApiErrorMessage(response, data, "Could not check series metadata. Try again."));
      return data;
    },
    async show() {
      this.trigger = document.activeElement;
      this.open = true;
      this.$nextTick(() => this.$refs.dialog.focus({ preventScroll: true }));
      if (!this.preview && !this.job && !this.busy && !this.checkingJob) await this.loadPreview();
    },
    close() {
      this.open = false;
      this.generation += 1;
      if (this.trigger && this.trigger.isConnected) this.trigger.focus({ preventScroll: true });
    },
    invalidate() { this.reviewedInputs = null; },
    fields() {
      return (this.preview ? this.preview.editor_fields : []).filter(field => this.allFields || field.changed ||
        (field.sources || []).some(source => source.value !== field.current) ||
        (this.drafts[field.key] && this.drafts[field.key].selected));
    },
    sourceOptions(field) {
      const options = (field.sources || []).filter(source => this.mode !== "library_only" || source.token === "library")
        .map(source => ({ value: source.token, label: source.token === "library" ? "Keep library value" : source.label }));
      options.push({ value: "custom", label: "Enter correction" });
      if (field.can_clear) options.push({ value: "clear", label: "Clear value" });
      if (field.overridden) options.push({ value: "reset", label: "Remove manual override" });
      return options;
    },
    valueOptions(field) { return [{ value: "", label: "Choose a value", disabled: true }].concat(field.options); },
    sourceChanged(field) {
      const draft = this.drafts[field.key];
      if (draft.source === "custom" && field.options.length && !field.options.some(option => option.value === draft.value)) draft.value = "";
      this.invalidate();
    },
    selectFields(selected) {
      (this.preview.editor_fields || []).filter(field => field.editable).forEach(field => { this.drafts[field.key].selected = selected; });
      this.invalidate();
    },
    inputs() {
      const edits = {};
      (this.preview ? this.preview.editor_fields : []).filter(field => field.editable).forEach(field => {
        const draft = this.drafts[field.key];
        if (!draft || !draft.selected) return;
        let edit;
        if (draft.source === "custom") {
          let value = draft.value;
          if (field.input_type === "number" && String(value).trim() && Number.isSafeInteger(Number(value))) value = Number(value);
          edit = { action: "set", value: value };
        } else if (["library", "clear", "reset"].includes(draft.source)) edit = { action: draft.source };
        else edit = { action: "sidecar", source: draft.source };
        edits[field.key.slice(7)] = edit;
      });
      return { mode: this.mode, edits: edits };
    },
    canWrite() {
      return !this.busy && !this.checkingJob && !this.job && this.preview && this.preview.ready && this.preview.editor_changed &&
        this.reviewedInputs === JSON.stringify(this.inputs());
    },
    async loadPreview(reset = false) {
      if (this.busy || this.checkingJob || this.active() || (this.job && this.job.state === "PAUSED")) return;
      const generation = ++this.generation;
      this.busy = true; this.error = ""; this.invalidate();
      const body = reset ? { mode: this.mode, edits: {} } : this.inputs();
      const signature = JSON.stringify(body);
      const missingChoice = (this.preview ? this.preview.editor_fields : []).find(field => {
        const edit = body.edits[field.key.slice(7)];
        return field.options.length && edit && edit.action === "set" && !field.options.some(option => option.value === edit.value);
      });
      if (missingChoice) {
        this.error = "Choose a value for " + missingChoice.label + ".";
        this.busy = false;
        return;
      }
      if (this.mode === "library_only" && Object.values(body.edits).some(edit => edit.action === "sidecar")) {
        this.error = "Sidecar values are not available with Library only. Choose another value source or include series.json files.";
        this.busy = false;
        return;
      }
      try {
        const data = await this.request(endpoint + "/preview", body);
        if (this.disposed || generation !== this.generation) return;
        const initialize = reset || !this.preview;
        this.preview = data;
        if (initialize) {
          this.drafts = {}; this.allFields = false;
          data.editor_fields.filter(field => field.editable).forEach(field => {
            this.drafts[field.key] = { selected: false, source: "library", value: field.current };
          });
        }
        this.job = null; this.librarySaved = false; this.jobTargets = []; this.jobWarnings = []; this.retryable = false; this.partial = false;
        if (JSON.stringify(this.inputs()) === signature) this.reviewedInputs = signature;
      } catch (error) { if (!this.disposed && generation === this.generation) this.error = error.message; }
      finally { if (!this.disposed) this.busy = false; }
    },
    active() { return !!this.job && ["QUEUED", "RUNNING", "PAUSING", "CANCELLING"].includes(this.job.state); },
    async refreshSeries() {
      const target = document.querySelector('[data-testid="series-detail-hero"]');
      try {
        if (target) await htmx.ajax("GET", "/series/" + seriesId, {
          target: target, select: '[data-testid="series-detail-hero"]', swap: "morph:outerHTML",
        });
      } catch (_) {
        if (!this.disposed) this.error = "Library changes are saved, but the page could not refresh. Reload to see the latest values.";
      }
      if (!this.disposed) window.dispatchEvent(new CustomEvent("series-metadata-editor-saved"));
    },
    async loadJob() {
      clearTimeout(this.timer);
      const generation = this.jobGeneration;
      const wasActive = this.active();
      try {
        const data = await this.request(endpoint + "/job");
        if (this.disposed || generation !== this.jobGeneration) return;
        this.job = data.job;
        this.librarySaved = !!data.library_saved;
        this.jobWarnings = data.warnings || [];
        this.jobTargets = data.targets || []; this.partial = !!data.partial; this.retryable = !!data.retryable;
        if (wasActive && this.job && !this.active()) await this.refreshSeries();
      } catch (error) { if (!this.disposed && generation === this.jobGeneration) this.error = error.message; }
      if (!this.disposed && generation === this.jobGeneration && this.active()) this.timer = setTimeout(() => this.loadJob(), 1000);
    },
    async write() {
      if (!this.canWrite()) return;
      this.busy = true; this.error = "";
      const approval = Object.assign({}, this.inputs(), { review_key: this.preview.review_key });
      this.invalidate();
      try {
        const result = await this.request(endpoint + "/write", approval);
        if (this.disposed) return;
        this.jobGeneration += 1;
        this.librarySaved = !!result.library_saved;
        this.job = { id: result.job_id, state: result.state, percent: 0, message: "Queued", error: null };
        await this.refreshSeries();
        await this.loadJob();
      } catch (error) { if (!this.disposed) this.error = error.message; }
      finally { if (!this.disposed) this.busy = false; }
    },
    async retry() {
      if (this.busy || !this.job || !this.retryable || this.active()) return;
      this.busy = true; this.error = "";
      try {
        const result = await this.request(endpoint + "/retry", { job_id: this.job.id });
        if (this.disposed) return;
        this.jobGeneration += 1;
        this.job = { id: result.job_id, state: result.state, percent: 0, message: "Queued", error: null };
        this.retryable = false;
        await this.loadJob();
      } catch (error) { if (!this.disposed) this.error = error.message; }
      finally { if (!this.disposed) this.busy = false; }
    },
    async cancel() {
      if (this.busy || !this.active() || this.job.state === "CANCELLING") return;
      this.busy = true; this.error = "";
      try { await this.request("/api/v1/utilities/jobs/" + this.job.id + "/cancel", {}); await this.loadJob(); }
      catch (error) { if (!this.disposed) this.error = error.message; }
      finally { if (!this.disposed) this.busy = false; }
    },
    targets() { return this.job ? this.jobTargets : this.preview && this.reviewedInputs && this.mode === this.preview.mode ? this.preview.targets : []; },
    targetLabel(target) {
      const state = String(target.action || target.state || "").toLowerCase();
      return { create: "Create series.json", update: "Update series.json", unchanged: "Up to date", blocked: "Left unchanged",
        written: "Written", completed: "Written", failed: "Needs attention", pending: "Pending", queued: "Queued",
        running: "Writing", cancelled: "Cancelled", cancelling: "Cancelling", skipped: "Left unchanged" }[state] || state;
    },
    escape(event) {
      if (!this.open) return;
      const dropdown = event.target.closest('[data-dropdown-select]');
      if (event.target.closest('[data-dropdown-select-panel]') || (dropdown && dropdown.querySelector('[aria-expanded="true"]'))) return;
      event.preventDefault(); event.stopPropagation(); this.close();
    },
    trapFocus(event) {
      if (!this.open || event.target.closest('[data-dropdown-select-panel]')) return;
      const elements = Array.from(this.$refs.dialog.querySelectorAll('button:not([disabled]), input:not([disabled]), textarea:not([disabled]), a[href], summary, [tabindex="0"]'))
        .filter(el => el.getClientRects().length > 0);
      const first = elements[0], last = elements[elements.length - 1];
      if (!first) { event.preventDefault(); return; }
      if (event.shiftKey && (document.activeElement === first || document.activeElement === this.$refs.dialog)) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || document.activeElement === this.$refs.dialog)) { event.preventDefault(); first.focus(); }
    },
  };
}
