/* Explicit series.json output. All values and target locations come from the server. */
function seriesSidecar(seriesId) {
  const endpoint = "/api/v1/series/" + seriesId + "/sidecar";
  return {
    open: false, busy: false, preview: null, result: null, error: "", trigger: null,
    generation: 0, disposed: false,
    destroy() { this.disposed = true; this.generation += 1; },
    async request(action, body) {
      const response = await fetch(endpoint + "/" + action, {
        method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": readCsrfTokenFromBody() },
        body: JSON.stringify(body),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(_extractApiErrorMessage(response, data, "Could not prepare series metadata. Try again."));
      return data;
    },
    async show() {
      this.trigger = document.activeElement;
      this.open = true;
      this.$nextTick(() => this.$refs.dialog.focus({ preventScroll: true }));
      if (!this.busy) await this.loadPreview();
    },
    close() {
      this.open = false;
      const target = this.trigger?.isConnected && this.trigger.getClientRects().length
        ? this.trigger : this.$refs.writeTrigger;
      target?.focus({ preventScroll: true });
    },
    async loadPreview() {
      if (this.busy) return;
      const generation = ++this.generation;
      this.busy = true; this.error = "";
      try {
        const data = await this.request("preview", {});
        if (this.disposed || generation !== this.generation) return;
        this.preview = data; this.result = null;
      } catch (error) {
        if (!this.disposed && generation === this.generation) this.error = error.message;
      } finally { if (!this.disposed) this.busy = false; }
    },
    async write() {
      if (this.busy || !this.preview || !this.preview.ready || this.result) return;
      this.busy = true; this.error = "";
      try {
        const data = await this.request("write", { review_key: this.preview.review_key });
        if (!this.disposed) {
          this.result = data;
          this.$dispatch("series-sidecar-written", { seriesId, counts: {
            updated: data.written, current: data.unchanged, blocked: this.blockedCount(),
          } });
        }
      } catch (error) {
        if (!this.disposed) { this.error = error.message; this.preview.ready = false; }
      } finally { if (!this.disposed) this.busy = false; }
    },
    targets() { return this.result ? this.result.targets : this.preview ? this.preview.targets : []; },
    actionLabel(action) {
      return { create: "Create series.json", update: "Update series.json", unchanged: "Up to date", blocked: "Left unchanged" }[action] || action;
    },
    blockedCount() { return this.result.targets.filter(target => target.action === "blocked").length; },
    summary() {
      if (!this.result) return "";
      const written = this.result.written;
      const blocked = this.blockedCount();
      return "Wrote " + written + " series.json file" + (written === 1 ? "." : "s.") +
        (blocked ? " " + blocked + " folder" + (blocked === 1 ? " was" : "s were") + " left unchanged; review the reasons below." : "");
    },
    trapFocus(event) {
      if (!this.open) return;
      const elements = Array.from(this.$refs.dialog.querySelectorAll('button:not([disabled]), a[href], summary, [tabindex="0"]')).filter(el => el.getClientRects().length > 0);
      const first = elements[0], last = elements[elements.length - 1];
      if (!first) { event.preventDefault(); return; }
      if (event.shiftKey && (document.activeElement === first || document.activeElement === this.$refs.dialog)) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || document.activeElement === this.$refs.dialog)) { event.preventDefault(); first.focus(); }
    },
  };
}
