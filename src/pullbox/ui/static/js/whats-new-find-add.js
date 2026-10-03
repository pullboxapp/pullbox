function discoverySeriesCover(result, revision) {
  const cover = metronSeriesCover({ externalId: result.external_id, sourceRevision: revision });
  return {
    ...cover,
    init() {
      this.coverUrl = safeSeriesCoverUrl(result.cover_url);
      if (!this.coverUrl && result.source === 'metron_api' && Number.isSafeInteger(revision)) {
        cover.init.call(this);
      }
    },
  };
}

function whatsNewFindAdd() {
  const shared = addSeries();
  return {
    ...shared,
    findOpen: false, findBusy: false, findError: '', findQuery: '', findSource: 'all',
    findResults: [], findMessages: [], findPage: 1, findPages: 1, findTotal: 0,
    findSources: [['all', 'All enabled sources']], findRevisions: {}, findSequence: 0, findController: null,
    releaseContext: null, releaseTrigger: null, releaseCompleted: false,

    async findRelease(cacheId, releaseId, trigger) {
      if (this.adding) return;
      this.findController?.abort();
      const sequence = ++this.findSequence;
      const controller = new AbortController();
      this.findController = controller;
      this.releaseContext = null;
      this.releaseCompleted = false;
      this.releaseTrigger = trigger;
      this.findOpen = true;
      this.findBusy = true;
      this.findError = '';
      this.findResults = [];
      this.findMessages = [];
      this.findSource = 'all';
      this.findQuery = '';
      this.$nextTick(() => requestAnimationFrame(() => {
        if (this.findOpen && !this.disposed) this.$refs.findQuery?.focus({ preventScroll: true });
      }));
      const timeout = setTimeout(() => controller.abort(), 30000);
      try {
        const response = await fetch(`/api/v1/whats-new/resolve/${cacheId}/${releaseId}`, { signal: controller.signal });
        const data = await response.json();
        if (sequence !== this.findSequence || this.disposed || !this.findOpen) return;
        if (!response.ok) throw new Error(data.detail || data.error?.message || 'Reload the release list and retry.');
        this.releaseContext = data;
        this.findQuery = data.title;
        const roots = data.roots || [];
        this.libraryRootId = roots[0]?.id || null;
        this.rootLabel = roots[0] ? roots[0].path + ' - ' + roots[0].name : '';
        this.rootPaths = Object.fromEntries(roots.map(root => [root.id, root.path]));
        this.findBusy = false;
        await this.searchReleaseSeries();
      } catch (error) {
        if (sequence === this.findSequence && !this.disposed && this.findOpen) {
          this.findError = error.name === 'AbortError' ? 'The lookup timed out. Close and retry Find & Add.' : error.message;
          this.findBusy = false;
        }
      } finally { clearTimeout(timeout); }
    },

    async searchReleaseSeries(page = 1) {
      if (!this.releaseContext || !this.findOpen || this.findQuery.trim().length < 2) return;
      this.findController?.abort();
      const controller = new AbortController();
      this.findController = controller;
      const sequence = ++this.findSequence;
      this.findBusy = true;
      this.findError = '';
      const timeout = setTimeout(() => controller.abort(), 90000);
      try {
        const selected = this.releaseContext.selection;
        const params = new URLSearchParams({ q: this.findQuery, source: this.findSource, page });
        const response = await fetch(`/whats-new/find-series/${selected.cache_id}/${selected.release_id}?${params}`, { signal: controller.signal });
        const data = await response.json();
        if (sequence !== this.findSequence || this.disposed || !this.findOpen) return;
        if (!response.ok) throw new Error(data.detail || data.error?.message || 'Search could not finish. Retry.');
        this.findResults = data.search_results || [];
        this.findMessages = data.search_source_messages || [];
        this.findSources = data.search_source_options || [['all', 'All enabled sources']];
        this.findRevisions = data.search_source_revisions || {};
        this.findPage = data.search_page;
        this.findPages = data.search_total_pages;
        this.findTotal = data.search_total_results;
        this.findError = data.search_error || '';
      } catch (error) {
        if (sequence === this.findSequence && !this.disposed && this.findOpen) {
          this.findError = error.name === 'AbortError' ? 'Search timed out. Retry or choose another source.' : error.message;
        }
      } finally {
        clearTimeout(timeout);
        if (sequence === this.findSequence && !this.disposed) this.findBusy = false;
      }
    },

    selectDiscoveryResult(result, trigger) {
      if (this.findBusy || this.adding || !this.releaseContext || result.identity_needs_review) return;
      this.findOpen = false;
      this.selectedSource = result.source;
      this.selectedExternalId = String(result.external_id);
      this.selectedTitle = result.title;
      this.selectedPublisher = result.publisher_name || '';
      this.selectedYear = result.year_start || null;
      this.selectedIssueCount = result.issue_count;
      this.selectedCoverUrl = safeSeriesCoverUrl(result.cover_url);
      this.selectedFolderPreview = '';
      this.trigger = trigger;
      this.showModal = true;
      this.$nextTick(() => this.$refs.dialog.focus({ preventScroll: true }));
      this.loadPreview();
    },

    closeFind() {
      if (this.adding || !this.findOpen) return;
      this.findOpen = false;
      this.findSequence++;
      this.findController?.abort();
      this.findBusy = false;
      this.releaseTrigger?.isConnected && this.releaseTrigger.focus({ preventScroll: true });
    },

    closeModal() {
      if (!this.showModal || this.adding) return;
      shared.closeModal.call(this);
      if (!this.releaseCompleted) {
        this.findOpen = true;
        this.$nextTick(() => this.$refs.findQuery.focus({ preventScroll: true }));
      } else {
        const focus = this.$root.querySelector('[data-testid="whats-new-local-series"]') || this.releaseTrigger;
        focus?.isConnected && focus.focus({ preventScroll: true });
      }
    },

    trapFindFocus(event) {
      const dialog = this.$refs.findDialog;
      const controls = [...dialog.querySelectorAll('button:not([disabled]), input:not([disabled]), a[href], [tabindex="0"]')]
        .filter(node => node.getClientRects().length > 0);
      const first = controls[0], last = controls[controls.length - 1];
      if (!first) { event.preventDefault(); return; }
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog)) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog)) {
        event.preventDefault(); first.focus();
      }
    },

    afterSeriesAdded(data) {
      if (!Number.isSafeInteger(data.id) || data.id < 1 || typeof data.monitored !== 'boolean') {
        throw new Error('The Add response could not be verified. Reload the release list.');
      }
      this.releaseCompleted = true;
      const identity = this.releaseContext.locg_series_id;
      if (!identity) return;
      // Patch only confirmed rows, keeping the release table, scroll and filters intact.
      for (const cell of this.$root.querySelectorAll('[data-discovery-series]')) {
        if (cell.dataset.discoverySeries !== String(identity)) continue;
        const link = document.createElement('a');
        link.className = 'btn-ghost btn-sm';
        link.dataset.testid = 'whats-new-local-series';
        link.href = '/series/' + data.id;
        link.textContent = 'Added';
        const status = document.createElement('span');
        status.className = 'badge badge-muted';
        status.textContent = data.monitored ? 'Tracked' : 'Paused';
        cell.replaceChildren(link, document.createTextNode(' '), status);
      }
    },

    destroy() {
      shared.destroy.call(this);
      this.findSequence++;
      this.findController?.abort();
    },
  };
}
