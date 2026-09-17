/* Durable, non-destructive series folder reconciliation. */
function seriesRescan(seriesId) {
  return {
    open: false, busy: false, loading: false, reviewing: false, error: "",
    report: { job: null, counts: {}, items: [], page: 1, pages: 0 },
    timer: null, disposed: false, trigger: null, pendingPage: null,
    requestedJob: new URLSearchParams(window.location.search).get("rescan"),
    init: function () {
      this.loadReport(1);
      if (this.requestedJob) this.showReport();
    },
    destroy: function () {
      this.disposed = true;
      clearTimeout(this.timer);
    },
    request: async function (url, options) {
      var response = await fetch(url, options || { cache: "no-store" });
      var data = await response.json();
      if (!response.ok) throw new Error((data.error && data.error.message) || data.detail || "Rescan request failed.");
      return data;
    },
    showReport: function (trigger) {
      this.trigger = trigger || document.activeElement;
      this.open = true;
      this.$nextTick(() => this.$refs.dialog.focus({ preventScroll: true }));
    },
    closeReport: function () {
      this.open = false;
      if (this.trigger && this.trigger.isConnected) this.trigger.focus({ preventScroll: true });
    },
    trapFocus: function (event) {
      if (!this.open) return;
      var elements = Array.from(this.$refs.dialog.querySelectorAll('button:not([disabled]), a[href], [tabindex="0"]'))
        .filter(function (el) { return el.getClientRects().length > 0; });
      var first = elements[0], last = elements[elements.length - 1];
      if (!first) { event.preventDefault(); return; }
      if (event.shiftKey && (document.activeElement === first || document.activeElement === this.$refs.dialog)) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === this.$refs.dialog)) {
        event.preventDefault(); first.focus();
      }
    },
    start: async function () {
      if (this.busy || (this.report.job && this.report.job.active)) return;
      var trigger = document.activeElement;
      this.busy = true;
      this.error = "";
      try {
        var data = await this.request("/api/v1/series/" + seriesId + "/rescan", {
          method: "POST", headers: { "X-CSRF-Token": readCsrfTokenFromBody() },
        });
        this.requestedJob = data.job_id;
        this.showReport(trigger);
        await this.loadReport(1);
      } catch (err) {
        this.error = err.message;
        this.showReport(trigger);
      } finally { this.busy = false; }
    },
    loadReport: async function (page) {
      if (this.disposed) return;
      if (this.loading) { this.pendingPage = page || 1; return; }
      this.loading = true;
      clearTimeout(this.timer);
      try {
        var query = new URLSearchParams({ page: page || 1 });
        if (this.requestedJob) query.set("job_id", this.requestedJob);
        var data = await this.request("/api/v1/series/" + seriesId + "/rescan?" + query);
        if (this.disposed) return;
        var finishedNewJob = data.job && !data.job.active &&
          (!this.report.job || this.report.job.active || this.report.job.id !== data.job.id);
        this.report = data;
        this.error = "";
        if (finishedNewJob) {
          this.$dispatch("series-files-rescanned");
        }
      } catch (err) { this.error = err.message; }
      finally {
        this.loading = false;
        if (!this.disposed && this.pendingPage !== null) {
          var pending = this.pendingPage;
          this.pendingPage = null;
          this.timer = setTimeout(() => this.loadReport(pending), 0);
        } else if (!this.disposed && ((this.report.job && this.report.job.active) || this.error)) {
          this.timer = setTimeout(() => this.loadReport(this.report.page), 3000);
        }
      }
    },
    reviewFile: async function (path) {
      if (this.reviewing) return;
      this.reviewing = true;
      try {
        var data = await this.request("/api/v1/import", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": readCsrfTokenFromBody() },
          body: JSON.stringify({ source_path: path, file_paths: [path], source_type: "filesystem", file_handling_mode: "in_place" }),
        });
        window.location.assign("/import?tab=collection&resume_job_id=" + encodeURIComponent(data.id));
      } catch (err) { this.error = err.message; }
      finally { this.reviewing = false; }
    },
  };
}
