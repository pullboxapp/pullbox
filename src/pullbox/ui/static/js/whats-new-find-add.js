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

function seriesWatchActions() {
  return {
    watchBusy: false, watchFeedback: '', watchRows: 0, watchRootOpen: false,
    watchRootId: '', watchRootOptions: [], pendingWatch: null, watchTrigger: null, watchFocus: null, watchSurface: null,

    async watchRequest(url, body) {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 30000);
      try {
        const response = await fetch(url, {
          method: body === undefined ? 'GET' : 'POST', signal: controller.signal,
          headers: body === undefined ? {} : { 'Content-Type': 'application/json', 'X-CSRF-Token': readCsrfTokenFromBody() },
          ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || data.error?.message || 'Watch could not finish. Reload and retry.');
        return data;
      } finally { clearTimeout(timeout); }
    },

    async startWatch(cacheId, releaseId, identity, trigger) {
      if (this.watchBusy || this.adding || this.findOpen) return;
      this.watchSurface ||= this.$root;
      this.watchBusy = true;
      this.setWatchButtonsDisabled(true);
      this.watchTrigger = trigger;
      this.watchFeedback = 'Saving Watch...';
      try {
        const data = await this.watchRequest(`/api/v1/whats-new/resolve/${cacheId}/${releaseId}`);
        if (this.disposed) return;
        if (!data.can_watch || data.locg_series_id !== identity) throw new Error('This release changed. Reload before watching it.');
        this.pendingWatch = { selection: data.selection, identity };
        if (data.watch_default_id) {
          await this.savePendingWatch(data.watch_default_id);
        } else {
          this.watchRootId = '';
          this.watchRootOptions = [['', 'Choose a library'], ...(data.watch_roots || []).map(root => [String(root.id), root.path + ' - ' + root.name])];
          this.watchRootOpen = true;
          this.watchFeedback = '';
          this.$nextTick(() => requestAnimationFrame(() => {
            if (this.watchRootOpen) this.$refs.watchRootDialog.focus({ preventScroll: true });
          }));
        }
      } catch (error) { this.watchFeedback = this.watchError(error); }
      finally { this.finishWatch(); }
    },

    async savePendingWatch(rootId) {
      const pending = this.pendingWatch;
      const data = await this.watchRequest('/api/v1/whats-new/watch', {
        selection: pending.selection, library_root_id: Number(rootId),
      });
      if (this.disposed) return;
      if (data.state !== 'watching' || data.locg_series_id !== pending.identity || !Number.isSafeInteger(data.id)) {
        throw new Error('Watch response could not be verified. Reload the list.');
      }
      this.watchRootOpen = false;
      this.patchWatchCells(data, true);
      this.pendingWatch = null;
      this.watchFeedback = 'Watching. Use Find & Add to confirm the series; automatic adding is not active yet.';
    },

    async confirmWatchRoot() {
      if (this.watchBusy || !this.watchRootId || !this.pendingWatch) return;
      this.watchBusy = true;
      try { await this.savePendingWatch(this.watchRootId); }
      catch (error) { this.watchFeedback = this.watchError(error); }
      finally { this.finishWatch(); }
    },

    closeWatchRoot() {
      if (this.watchBusy || !this.watchRootOpen) return;
      this.watchRootOpen = false;
      this.pendingWatch = null;
      this.watchTrigger?.isConnected && this.watchTrigger.focus({ preventScroll: true });
    },

    trapWatchRoot(event) {
      const controls = [...this.$refs.watchRootDialog.querySelectorAll('button:not([disabled]), a[href]')]
        .filter(node => node.getClientRects().length > 0);
      const first = controls[0], last = controls[controls.length - 1];
      const focused = document.activeElement;
      if (event.shiftKey && (focused === first || focused === this.$refs.watchRootDialog)) {
        event.preventDefault(); last?.focus();
      } else if (!event.shiftKey && (focused === last || focused === this.$refs.watchRootDialog)) {
        event.preventDefault(); first?.focus();
      }
    },

    async cancelWatch(id, identity, trigger) {
      if (this.watchBusy || this.adding) return;
      this.watchSurface ||= this.$root;
      this.watchBusy = true;
      this.setWatchButtonsDisabled(true);
      this.watchTrigger = trigger;
      try {
        const data = await this.watchRequest(`/api/v1/whats-new/watch/${id}/cancel`, {});
        if (this.disposed) return;
        if (data.id !== id || data.locg_series_id !== identity || data.state !== 'cancelled') {
          throw new Error('Cancel response could not be verified. Reload the list.');
        }
        const heading = this.$refs.watchHeading;
        this.patchWatchCells(data, false);
        for (const row of this.watchSurface.querySelectorAll('[data-watch-id]')) {
          if (row.dataset.watchId === String(id)) row.remove();
        }
        this.watchRows = this.watchSurface.querySelectorAll('[data-watch-id]').length;
        if (heading) {
          const next = this.watchSurface.querySelector('[data-watch-id] button') || heading;
          this.watchFocus = next;
        }
        this.watchFeedback = 'Watch cancelled. No series or files were deleted.';
      } catch (error) { this.watchFeedback = this.watchError(error); }
      finally { this.finishWatch(); }
    },

    patchWatchCells(data, active) {
      let focus = null;
      for (const cell of this.watchSurface.querySelectorAll('[data-discovery-watch]')) {
        if (cell.dataset.discoveryWatch !== data.locg_series_id) continue;
        const nodes = [];
        if (active) {
          const badge = document.createElement('span');
          badge.className = 'badge badge-muted';
          badge.textContent = 'Watching';
          nodes.push(badge, document.createTextNode(' '));
        }
        if (active || cell.dataset.watchable === 'true') {
          const button = document.createElement('button');
          button.type = 'button'; button.className = 'btn-ghost btn-sm whitespace-nowrap';
          button.disabled = this.watchBusy;
          button.textContent = active ? 'Cancel Watch' : 'Watch';
          button.addEventListener('click', () => active
            ? this.cancelWatch(data.id, data.locg_series_id, button)
            : this.startWatch(Number(cell.dataset.watchCache), Number(cell.dataset.watchRelease), data.locg_series_id, button));
          nodes.push(button);
          if (cell.contains(this.watchTrigger)) focus = button;
        }
        cell.replaceChildren(...nodes);
      }
      this.watchFocus = focus;
    },

    finishWatch() {
      this.watchBusy = false;
      this.setWatchButtonsDisabled(false);
      const focus = this.watchFocus;
      this.watchFocus = null;
      this.$nextTick(() => focus?.isConnected && focus.focus({ preventScroll: true }));
    },

    setWatchButtonsDisabled(disabled) {
      // The clicked Alpine element may be removed when its action cell changes.
      for (const button of this.watchSurface.querySelectorAll('[data-discovery-watch] button')) button.disabled = disabled;
    },

    watchError(error) {
      return error.name === 'AbortError' ? 'Watch timed out. Reload to check its saved state before retrying.' : error.message;
    },
  };
}

function whatsNewFindAdd() {
  const shared = addSeries();
  return {
    ...shared,
    ...seriesWatchActions(),
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
