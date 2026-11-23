/* Preview and approve one managed comic; native conversion is explicit. */
function issueEditorDraftState(fieldIdPrefix) {
  return {
    fieldIdPrefix: fieldIdPrefix, allFields: false, editorDrafts: {}, arcRowId: 0, choicesDirty: false,
    lastEditorDocument: "",
    initializeFields: function (data) {
      this.allFields = false; this.editorDrafts = {}; this.lastEditorDocument = "";
      (data.editor_fields || []).filter(field => field.editable).forEach(field => {
        var hasLibrary = this.editorOptions(field).some(option => option.value === "library");
        this.editorDrafts[field.key] = { selected: false, source: hasLibrary ? "library" : "unchanged", value: this.initialEditorValue(field) };
      });
    },
    editorFields: function () {
      return (this.preview ? this.preview.editor_fields || [] : []).filter(field =>
        this.allFields || field.differs || (this.editorDrafts[field.key] && this.editorDrafts[field.key].selected));
    },
    editorValueLabel: function (field, value) {
      return field.key === "issue.manga" && value === "YesAndRightToLeft" ? "Yes (right to left)" : value;
    },
    editorOptions: function (field) {
      var options = [{ value: "unchanged", label: "Leave unchanged" }];
      for (const option of field.options || []) {
        if (option.selectable !== false && option.value != null && String(option.value).trim() !== "")
          options.push({ value: option.source, label: option.source === "library" ? "Keep library value" : option.source });
      }
      options.push({ value: "custom", label: "Enter correction" }, { value: "clear", label: "Clear value" });
      if (field.overridden) options.push({ value: "reset", label: "Remove manual override" });
      return options;
    },
    initialEditorValue: function (field) {
      if (field.input_type === "arcs") {
        return (field.arc_draft && field.arc_draft.length ? field.arc_draft : [{ name: "", position: null }])
          .map(entry => this.arcRow(entry));
      }
      if (!field.xml_conflict_key || (field.allowed_values && field.allowed_values.length) || field.current || field.overridden) return field.current || "";
      var files = field.options.filter(option => option.source !== "library");
      return files.length && files.every(option => option.selectable !== false && option.value === files[0].value)
        ? files[0].value : "";
    },
    arcRow: function (entry = {}) {
      return { key: ++this.arcRowId, name: entry.name || "", position: entry.position == null ? "" : String(entry.position) };
    },
    arcValues: function (rows) {
      if (!rows.length) throw new Error("Choose Clear value to remove all embedded arcs.");
      return rows.map(row => {
        if (!row.name.trim() || row.name.includes(",")) throw new Error("Each arc needs a name without commas.");
        if (row.position !== "" && (!/^\d{1,10}$/.test(row.position) || Number(row.position) < 1))
          throw new Error("Enter a positive whole arc position or leave it blank.");
        return { name: row.name, position: row.position === "" ? null : Number(row.position) };
      });
    },
    selectEditorFields: function (selected) {
      (this.preview.editor_fields || []).filter(field => field.editable).forEach(field => { this.editorDrafts[field.key].selected = selected; });
      this.choicesDirty = true;
    },
    checkedEditorFields: function () {
      return (this.preview?.editor_fields || []).filter(field => field.editable && this.editorDrafts[field.key]?.selected);
    },
    pendingDocumentFields: function (source) {
      return this.checkedEditorFields().filter(field => this.editorDrafts[field.key].source !== source &&
        this.editorOptions(field).some(option => option.value === source));
    },
    useEditorDocument: function (source) {
      if (this.busy || !["ComicInfo.xml", "MetronInfo.xml"].includes(source)) return;
      const fields = this.pendingDocumentFields(source);
      if (!fields.length) return;
      for (const field of fields) this.editorDrafts[field.key].source = source;
      this.lastEditorDocument = source;
      this.choicesDirty = true;
    },
    editorDocumentSummary: function () {
      if (!this.lastEditorDocument) return "";
      const fields = this.checkedEditorFields();
      if (!fields.length) return "";
      const count = fields.filter(field => this.editorDrafts[field.key].source === this.lastEditorDocument).length;
      return count + " of " + fields.length + " checked " + (fields.length === 1 ? "field uses " : "fields use ") + this.lastEditorDocument + ".";
    },
    editorEdits: function () {
      var edits = {};
      Object.keys(this.editorDrafts).forEach(key => {
        var draft = this.editorDrafts[key];
        if (!draft.selected || draft.source === "unchanged") return;
        edits[key.slice(6)] = draft.source === "custom"
          ? { source: draft.source, value: key === "issue.story_arcs" ? this.arcValues(draft.value) : draft.value }
          : { source: draft.source };
      });
      return edits;
    },
  };
}

function issueFileMetadata(issueId) {
  var endpoint = issueId ? "/api/v1/issues/" + issueId + "/file-metadata" : "";
  return {
    ...issueEditorDraftState("editor-"),
    recovery: !issueId, resolved: false, editor: false,
    open: false, busy: false, error: "", preview: null, job: null, choices: {}, choicesDirty: false,
    timer: null, disposed: false, trigger: null, generation: 0, jobGeneration: 0,
    init: function () { if (endpoint) this.loadJob(); },
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
        if (this.recovery) this.resolved = !!data.resolved;
        if (wasActive && this.job && this.job.state === "COMPLETED") {
          if (this.recovery) return;
          window.dispatchEvent(new CustomEvent("issue-file-metadata-complete"));
          var target = document.getElementById("issue-metadata-content");
          if (target) await htmx.ajax("GET", "/htmx/issues/" + issueId + "/metadata", { target: target, swap: "morph:outerHTML" });
        }
      } catch (error) { if (!this.disposed) this.error = error.message; }
      if (!this.disposed && this.active()) this.timer = setTimeout(() => this.loadJob(), 1000);
    },
    showRecovery: async function (pendingId) {
      if (!this.recovery || this.busy || !Number.isSafeInteger(pendingId) || pendingId <= 0) return;
      clearTimeout(this.timer);
      this.jobGeneration += 1;
      this.generation += 1;
      endpoint = "/api/v1/intervention/" + pendingId + "/file-metadata";
      this.editor = false; this.editorDrafts = {}; this.allFields = false; this.choicesDirty = false;
      this.preview = null; this.job = null; this.choices = {}; this.error = ""; this.resolved = false;
      this.trigger = document.activeElement;
      this.open = true; this.busy = true;
      this.$nextTick(() => this.$refs.dialog.focus({ preventScroll: true }));
      await this.loadJob();
      this.busy = false;
      if (this.open && !this.active() && !this.resolved && !(this.job && this.job.state === "PAUSED")) await this.loadPreview();
    },
    show: async function (editor = false) {
      if (this.busy) return;
      this.editor = editor;
      this.trigger = document.activeElement;
      this.open = true;
      this.$nextTick(() => this.$refs.dialog.focus({ preventScroll: true }));
      if (!this.job || ["COMPLETED", "FAILED", "CANCELLED", "ROLLED_BACK"].includes(this.job.state)) await this.loadPreview();
    },
    close: function () {
      this.open = false;
      this.generation += 1;
      if (this.trigger && this.trigger.isConnected) this.trigger.focus({ preventScroll: true });
      if (this.recovery && this.resolved) htmx.trigger(document.body, "intervention:refresh");
    },
    choose: function (key, source) {
      this.choices[key] = source;
      this.choicesDirty = true;
    },
    canWrite: function () {
      return !!this.preview && this.preview.ready !== false && !this.choicesDirty && (this.recovery || !this.editor || this.preview.editor_changed);
    },
    conflictChoices: function () {
      var conflicts = this.preview ? this.preview.conflicts || [] : [];
      return !this.editor ? conflicts : this.recovery ? conflicts.filter(row => row.key.startsWith("series.")) : [];
    },
    reviewEndpoint: function () {
      return this.editor && !this.recovery ? endpoint.replace("/file-metadata", "/metadata-editor") : endpoint;
    },
    replacementUrl: function (action) {
      return this.preview && Number.isSafeInteger(this.preview.issue_id)
        ? "/issues/" + this.preview.issue_id + "?replacement=" + action : "#";
    },
    dismissReplacement: async function () {
      if (this.busy || !this.recovery || !this.preview || !this.preview.replacement_required) return;
      this.open = false;
      await this.$nextTick();
      var confirmed = await pbConfirm({ title: "Dismiss recovery?", message: "This removes the recovery notification. It does not fix or delete the incomplete file, change ownership, or blocklist the release.", confirmText: "Dismiss recovery" });
      if (!confirmed) {
        this.open = true;
        this.$nextTick(() => this.$refs.dialog.focus({ preventScroll: true }));
        return;
      }
      this.busy = true;
      try {
        await this.request(endpoint + "/dismiss", {});
        this.resolved = true;
        this.close();
      } catch (error) {
        this.error = error.message;
        this.open = true;
        this.$nextTick(() => this.$refs.dialog.focus({ preventScroll: true }));
      }
      finally { this.busy = false; }
    },
    loadPreview: async function (reset = true, editRecovery = this.editor) {
      if (this.busy) return;
      clearTimeout(this.timer);
      this.jobGeneration += 1;
      var generation = ++this.generation;
      this.busy = true; this.error = "";
      this.choicesDirty = true;
      var choices = reset ? {} : Object.assign({}, this.choices);
      if (reset && this.recovery && editRecovery && !this.editor) {
        Object.keys(this.choices).filter(key => key.startsWith("series.")).forEach(key => { choices[key] = this.choices[key]; });
      }
      if (reset && !this.recovery) { this.job = null; this.preview = null; }
      try {
        var edits = reset ? {} : this.editorEdits();
        var body = Object.keys(choices).length ? { choices: choices } : {};
        if (this.recovery && editRecovery) body.editor = { edits: edits };
        else if (this.recovery && reset) body.offer_field_repair = true;
        else if (this.editor) body = { edits: edits };
        var data = await this.request(this.reviewEndpoint() + "/preview", body);
        if (this.disposed || generation !== this.generation) return;
        this.preview = data; this.job = null;
        if (this.recovery) this.editor = !!(data.editor_fields && data.editor_fields.length);
        if (this.editor && reset) {
          this.initializeFields(data);
        }
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
        if (this.editor && this.recovery) approval.editor = { edits: this.editorEdits() };
        else if (this.editor) approval.edits = this.editorEdits();
        if ((!this.editor || this.recovery) && Object.keys(this.choices).length) approval.choices = Object.assign({}, this.choices);
        var result = await this.request(this.reviewEndpoint() + "/write", approval);
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
      var elements = Array.from(this.$refs.dialog.querySelectorAll('button:not([disabled]), input:not([disabled]), textarea:not([disabled]), a[href], [tabindex="0"]')).filter(el => el.getClientRects().length > 0);
      var first = elements[0], last = elements[elements.length - 1];
      if (!first) { event.preventDefault(); return; }
      if (event.shiftKey && (document.activeElement === first || document.activeElement === this.$refs.dialog)) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || document.activeElement === this.$refs.dialog)) { event.preventDefault(); first.focus(); }
    },
  };
}
