/* Preview and approve one managed comic; native conversion is explicit. */
function issueFileMetadata(issueId) {
  var endpoint = "/api/v1/issues/" + issueId + "/file-metadata";
  return {
    open: false, busy: false, error: "", preview: null, job: null, choices: {}, choicesDirty: false,
    timer: null, disposed: false, trigger: null, generation: 0, jobGeneration: 0,
    init: function () { this.loadJob(); },
    destroy: function () { this.disposed = true; this.generation += 1; clearTimeout(this.timer); },
    request: async function (url, body) {
      var response = await fetch(url, body === undefined ? { cache: "no-store" } : {
        method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": readCsrfTokenFromBody() }, body: JSON.stringify(body),
      });
      var data = await response.json();
      if (!response.ok) throw new Error(_extractApiErrorMessage(response, data, "Could not check file metadata. Try again."));
      return data;
    },
    active: function () { return !!this.job && ["QUEUED", "RUNNING", "PAUSING", "CANCELLING"].includes(this.job.state); },
    loadJob: async function () {
      clearTimeout(this.timer);
      var generation = this.jobGeneration;
      var wasActive = this.active();
      try {
        var data = await this.request(endpoint + "/job");
        if (this.disposed || generation !== this.jobGeneration) return;
        this.job = data.job;
        if (wasActive && this.job && this.job.state === "COMPLETED") {
          window.dispatchEvent(new CustomEvent("issue-file-metadata-complete"));
          var target = document.getElementById("issue-metadata-content");
          if (target) await htmx.ajax("GET", "/htmx/issues/" + issueId + "/metadata", { target: target, swap: "morph:outerHTML" });
        }
      } catch (error) { if (!this.disposed) this.error = error.message; }
      if (!this.disposed && this.active()) this.timer = setTimeout(() => this.loadJob(), 1000);
    },
    show: async function () {
      this.trigger = document.activeElement;
      this.open = true;
      this.$nextTick(() => this.$refs.dialog.focus({ preventScroll: true }));
      if (!this.job || ["FAILED", "CANCELLED"].includes(this.job.state)) await this.loadPreview();
    },
    close: function () {
      this.open = false;
      this.generation += 1;
      if (this.trigger && this.trigger.isConnected) this.trigger.focus({ preventScroll: true });
    },
    choose: function (key, source) {
      this.choices[key] = source;
      this.choicesDirty = true;
    },
    canWrite: function () {
      return !!this.preview && this.preview.ready !== false && !this.choicesDirty;
    },
    loadPreview: async function (reset = true) {
      if (this.busy) return;
      clearTimeout(this.timer);
      this.jobGeneration += 1;
      var generation = ++this.generation;
      this.busy = true; this.error = "";
      this.choicesDirty = true;
      var choices = reset ? {} : Object.assign({}, this.choices);
      try {
        var data = await this.request(endpoint + "/preview", Object.keys(choices).length ? { choices: choices } : {});
        if (this.disposed || generation !== this.generation) return;
        this.preview = data; this.job = null;
        this.choices = {};
        (data.conflicts || []).forEach(conflict => { if (conflict.selected) this.choices[conflict.key] = conflict.selected; });
        this.choicesDirty = false;
      } catch (error) { if (generation === this.generation) this.error = error.message; }
      finally { if (!this.disposed) this.busy = false; }
    },
    write: async function () {
      if (this.busy || !this.canWrite() || this.active()) return;
      this.busy = true; this.error = "";
      try {
        var approval = { review_key: this.preview.review_key };
        if (Object.keys(this.choices).length) approval.choices = Object.assign({}, this.choices);
        var result = await this.request(endpoint + "/write", approval);
        if (this.disposed) return;
        this.jobGeneration += 1;
        this.job = { id: result.job_id, state: result.state, percent: 0, message: "Queued", error: null };
        await this.loadJob();
      } catch (error) { if (!this.disposed) this.error = error.message; }
      finally { if (!this.disposed) this.busy = false; }
    },
    cancel: async function () {
      if (this.busy || !this.active()) return;
      this.busy = true;
      try { await this.request("/api/v1/utilities/jobs/" + this.job.id + "/cancel", {}); await this.loadJob(); }
      catch (error) { if (!this.disposed) this.error = error.message; }
      finally { if (!this.disposed) this.busy = false; }
    },
    trapFocus: function (event) {
      if (!this.open) return;
      var elements = Array.from(this.$refs.dialog.querySelectorAll('button:not([disabled]), input:not([disabled]), a[href], [tabindex="0"]')).filter(el => el.getClientRects().length > 0);
      var first = elements[0], last = elements[elements.length - 1];
      if (!first) { event.preventDefault(); return; }
      if (event.shiftKey && (document.activeElement === first || document.activeElement === this.$refs.dialog)) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || document.activeElement === this.$refs.dialog)) { event.preventDefault(); first.focus(); }
    },
  };
}
