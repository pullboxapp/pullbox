/* Explicit series-field propagation; selecting fields never starts a write. */
function seriesMetadataPropagation(seriesId) {
  const endpoint = "/api/v1/series/" + seriesId + "/metadata-propagation";
  return {
    open: false, busy: false, error: "", fields: [], selected: [], report: null,
    page: 1, timer: null, disposed: false, trigger: null, approved: false, generation: 0,
    options: [{key:"title",label:"Series name"}, {key:"publisher",label:"Publisher"},
      {key:"language",label:"Language"}, {key:"format",label:"Publication format"},
      {key:"sort_title",label:"Series sort name"}, {key:"year_start",label:"Series start year"},
      {key:"volume",label:"Series volume"}],
    destroy() { this.disposed = true; this.generation++; clearTimeout(this.timer); },
    async request(url, body) {
      const response = await fetch(url, body === undefined ? {cache:"no-store"} : {
        method:"POST", headers:{"Content-Type":"application/json", "X-CSRF-Token":readCsrfTokenFromBody()}, body:JSON.stringify(body)});
      const data = await response.json();
      if (!response.ok) throw new Error(_extractApiErrorMessage(response, data, "Could not check comic metadata. Try again."));
      return data;
    },
    async show() {
      this.trigger = document.activeElement; this.open = true;
      this.$nextTick(() => this.$refs.dialog.focus({preventScroll:true}));
      if (!this.report && !this.busy) {
        this.busy = true;
        try { await this.load(); } finally { this.busy = false; }
      }
    },
    close() { this.open = false; if (this.trigger && this.trigger.isConnected) this.trigger.focus({preventScroll:true}); },
    active() { return !!this.report && this.report.job && !["COMPLETED","FAILED","CANCELLED"].includes(this.report.job.state); },
    ready() { return this.report && this.report.mode === "preview" && !this.active() && this.approved; },
    invalidate() { this.approved = false; },
    count(key) { return this.report && this.report.counts ? this.report.counts[key] || 0 : 0; },
    summary() {
      const cancelled = this.count("cancelled") ? "; " + this.count("cancelled") + " cancelled" : "";
      return (this.report && this.report.mode === "write" ? this.count("written") + " updated; " + this.count("current") + " already current; " + this.count("attention") + " need attention" :
        this.count("ready") + " ready; " + this.count("current") + " already current; " + this.count("attention") + " need attention") + cancelled;
    },
    toggle(id) { this.selected = this.selected.includes(id) ? this.selected.filter(value => value !== id) : this.selected.concat([id]); },
    async load() {
      clearTimeout(this.timer); const generation = ++this.generation;
      try {
        const data = await this.request(endpoint + "/job?page=" + this.page);
        if (this.disposed || generation !== this.generation) return;
        const previous = this.report && this.report.job;
        this.report = data.job ? data : null;
        if (data.job && (!previous || previous.id !== data.job.id)) {
          this.fields = data.fields || []; this.selected = data.ready_ids || []; this.approved = data.mode === "preview";
        } else if (data.job && data.mode === "preview" && previous && ["QUEUED","RUNNING","CANCELLING"].includes(previous.state)) {
          this.selected = data.ready_ids || [];
        }
      } catch (error) { if (!this.disposed && generation === this.generation) this.error = error.message; }
      if (!this.disposed && this.active()) this.timer = setTimeout(() => this.load(), 1200);
    },
    async start(action, body) {
      if (this.busy || this.active()) return;
      this.busy = true; this.error = "";
      try {
        await this.request(endpoint + "/" + action, body);
        if (this.disposed) return;
        this.report = null; this.page = 1; this.selected = []; this.approved = false;
        await this.load();
      } catch (error) { if (!this.disposed) this.error = error.message; }
      finally { if (!this.disposed) this.busy = false; }
    },
    preview() { if (this.fields.length) return this.start("preview", {fields:this.fields}); },
    write() { if (this.ready() && this.selected.length) return this.start("write", {job_id:this.report.job.id, issue_ids:this.selected}); },
    retry() { if (this.report && this.report.retryable) return this.start("retry", {job_id:this.report.job.id}); },
    async cancel() {
      if (this.busy || !this.active()) return;
      this.busy = true; this.error = "";
      try { await this.request("/api/v1/utilities/jobs/" + this.report.job.id + "/cancel", {}); await this.load(); }
      catch (error) { this.error = error.message; }
      finally { this.busy = false; }
    },
    async turnPage(delta) { this.page += delta; await this.load(); },
    label(outcome) { return {ready:"Ready to update", current:"Already current", written:"Updated", attention:"Needs attention", cancelled:"Cancelled", pending:"Pending", running:"Working"}[outcome] || outcome; },
    escape(event) { if (this.open) { event.preventDefault(); event.stopPropagation(); this.close(); } },
    trapFocus(event) {
      if (!this.open) return;
      const elements = Array.from(this.$refs.dialog.querySelectorAll('button:not([disabled]), input:not([disabled]), a[href], summary, [tabindex="0"]')).filter(el => el.getClientRects().length > 0);
      const first = elements[0], last = elements[elements.length - 1];
      if (!first) { event.preventDefault(); return; }
      if (event.shiftKey && (document.activeElement === first || document.activeElement === this.$refs.dialog)) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || document.activeElement === this.$refs.dialog)) { event.preventDefault(); first.focus(); }
    },
  };
}
