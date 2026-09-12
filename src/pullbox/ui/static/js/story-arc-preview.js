/* The complete bounded preview stays local: paging never refetches Comic Vine
 * or drops order/skip choices for members on another page. */
function storyArcPreview() {
  return {
    members: [], page: 1, perPage: '25', ready: false,
    libraryRootId: '', monitored: false, rootOptions: [],
    title: '', description: '', coverUrl: '', coverFailed: false,
    fingerprint: '', fileDefaultsFingerprint: '', fileSummary: '', fileExample: '',
    error: '', submitError: '', notice: '', busy: '', controller: null, endpoint: '', reorderAnnouncement: '',

    init() {
      this.endpoint = this.$el.dataset.previewUrl;
      this.applySnapshot(JSON.parse(this.$el.querySelector('[data-preview-data]').textContent), false);
      this.$watch('page', () => this.publish());
      this.$watch('perPage', () => { this.page = 1; this.publish(); });
      this.$watch('skippedCount', () => this.publish());
      this.$watch('libraryRootId', () => { this.submitError = ''; });
      this.$nextTick(() => this.publish());
    },
    destroy() { if (this.controller) this.controller.abort(); },
    get totalPages() { return Math.max(1, Math.ceil(this.members.length / Number(this.perPage))); },
    get visibleMembers() {
      const start = (this.page - 1) * Number(this.perPage);
      return this.members.slice(start, start + Number(this.perPage));
    },
    get skippedCount() { return this.members.filter(member => member.skipped).length; },
    publish() {
      this.$dispatch('story-arc-preview-status', {
        page: this.page, totalPages: this.totalPages, total: this.members.length,
        skipped: this.skippedCount, ready: this.ready,
      });
    },
    setPage(page) {
      this.page = Math.max(1, Math.min(this.totalPages, Number(page)));
    },
    renumber(members) {
      this.members = members.map((member, index) => ({ ...member, order: index + 1 }));
    },
    moveMember(providerId, direction) {
      if (this.busy || ![-1, 1].includes(direction)) return;
      const index = this.members.findIndex(member => member.provider_id === providerId);
      const target = index + direction;
      if (index < 0 || target < 0 || target >= this.members.length) return;
      // Replace the array in one pass so keyed rows never see duplicate IDs.
      const reordered = [...this.members];
      [reordered[index], reordered[target]] = [reordered[target], reordered[index]];
      this.renumber(reordered);
      const previousPage = this.page;
      this.setPage(Math.floor(target / Number(this.perPage)) + 1);
      const moved = this.members[target];
      this.reorderAnnouncement = `${moved.series_name} #${moved.issue_number} moved to position ${target + 1}.`;
      this.$nextTick(() => {
        const row = this.$root.querySelector(`[data-provider-issue-id="${providerId}"]`);
        const button = row?.querySelector(`[data-order-direction="${direction < 0 ? 'up' : 'down'}"]`);
        const focusTarget = button?.disabled ? row.querySelector('[data-order-direction]:not(:disabled)') : button;
        focusTarget?.focus({ preventScroll: true });
        if (previousPage !== this.page) row?.scrollIntoView({ block: 'nearest' });
      });
    },
    applySnapshot(data, preserve = true) {
      this.error = data.error;
      this.ready = data.ready;
      // A failed/incomplete response is not a replacement for a complete draft.
      if (!data.ready && preserve && this.members.length) { this.publish(); return; }
      const previous = new Map(this.members.map(member => [member.provider_id, member]));
      let added = 0;
      const incoming = new Set(data.members.map(member => member.provider_id));
      const removed = this.members.filter(member => !incoming.has(member.provider_id)).length;
      const merged = data.members.map(member => {
        const old = preserve ? previous.get(member.provider_id) : null;
        if (preserve && !old) added += 1;
        return { ...member, skipped: old ? old.skipped : false };
      });
      // Retain the user's workspace row positions for surviving members, too.
      if (preserve) {
        const positions = new Map(this.members.map((member, index) => [member.provider_id, index]));
        merged.sort((a, b) => (positions.get(a.provider_id) ?? Infinity) - (positions.get(b.provider_id) ?? Infinity));
      }
      this.renumber(merged);
      this.title = data.title;
      this.description = data.description;
      if (this.coverUrl !== data.coverUrl) this.coverFailed = false;
      this.coverUrl = data.coverUrl;
      this.fingerprint = data.fingerprint;
      const defaultsChanged = preserve && this.fileDefaultsFingerprint !== data.fileDefaultsFingerprint;
      this.fileDefaultsFingerprint = data.fileDefaultsFingerprint;
      this.fileSummary = data.fileSummary;
      this.fileExample = data.fileExample;
      this.rootOptions = data.roots;
      const rootRemoved = this.libraryRootId && !data.roots.some(root => root[0] === this.libraryRootId);
      if (rootRemoved) this.libraryRootId = '';
      this.setPage(this.page);
      this.notice = preserve ? (added || removed
        ? `Preview updated: ${added} added, ${removed} no longer listed. Choices for remaining issues were kept. Review the updated list before adding.`
        : 'Preview updated. Your reading order, skips, and settings were kept.') : '';
      if (defaultsChanged) this.notice += ' Story Arc file defaults changed; review the settings summary below.';
      if (rootRemoved) this.notice += ' The selected library root is no longer available. Choose another root.';
      this.publish();
    },
    async retry() { await this.requestPreview(); },
    explainSubmitError(message) {
      this.submitError = message;
      this.$nextTick(() => this.$root.querySelector('[data-testid="story-arc-preview-submit-error"]').focus());
    },
    async submit(form) {
      if (this.busy) return;
      if (!this.ready) {
        this.explainSubmitError('The preview is incomplete. Select Retry preview to load all issues before adding this Story Arc.');
        return;
      }
      if (!this.libraryRootId) {
        this.explainSubmitError(this.rootOptions.length < 2
          ? 'No managed library root is available. Configure one in Settings > Media Management > Library roots, then select Retry preview.'
          : 'Choose a library root for new series before adding this Story Arc.');
        return;
      }
      const body = new FormData(form);
      // Always submit every page. Positions come from list order, never user input.
      for (const field of ['issue_provider_ids', 'reading_orders', 'skipped_issue_provider_ids']) body.delete(field);
      for (const [index, member] of this.members.entries()) {
        body.append('issue_provider_ids', member.provider_id);
        body.append('reading_orders', index + 1);
        if (member.skipped) body.append('skipped_issue_provider_ids', member.provider_id);
      }
      await this.requestPreview(body);
    },
    async requestPreview(body = null) {
      if (this.busy) return;
      this.busy = body ? 'adding' : 'retrying';
      this.error = '';
      this.submitError = '';
      this.notice = '';
      const controller = new AbortController();
      this.controller = controller;
      const timeout = setTimeout(() => controller.abort(), 120000);
      try {
        const response = await fetch(this.endpoint, {
          method: body ? 'POST' : 'GET', body, credentials: 'same-origin', cache: 'no-store',
          headers: body ? { 'X-CSRF-Token': readCsrfTokenFromBody() } : {}, signal: controller.signal,
        });
        if (!this.$el.isConnected) return;
        const target = new URL(response.url);
        if (response.ok && target.origin === location.origin && /^\/story-arcs\/\d+$/.test(target.pathname)) {
          location.assign(target.href);
          return;
        }
        if (target.pathname === '/login' || response.status === 401 || response.status === 403) {
          throw new Error('Your session expired. Sign in again before adding this Story Arc.');
        }
        if (!response.ok) throw new Error('The preview could not be saved or refreshed. Your edits are still here. Retry preview.');
        const document = new DOMParser().parseFromString(await response.text(), 'text/html');
        const seed = document.querySelector('[data-preview-data]');
        if (!seed) throw new Error('The preview response was unavailable. Your edits are still here. Retry preview.');
        this.applySnapshot(JSON.parse(seed.textContent));
      } catch (error) {
        if (!this.$el.isConnected) return;
        this.ready = false;
        this.error = error.name === 'AbortError'
          ? 'Comic Vine took too long to respond. Your edits are still here. Retry preview.'
          : error instanceof TypeError || error instanceof SyntaxError
            ? 'The connection failed. Your edits are still here. Retry preview.' : error.message;
      } finally {
        clearTimeout(timeout);
        this.controller = null;
        this.busy = '';
        if (this.$el.isConnected) this.publish();
      }
    },
  };
}

function storyArcPreviewFooter() {
  return {
    page: 1, totalPages: 1, total: 0, skipped: 0, ready: false,
    init() { this.$nextTick(() => this.$dispatch('story-arc-preview-request-status')); },
    get pageTokens() {
      const total = this.totalPages, page = this.page;
      if (total <= 5) return Array.from({ length: total }, (_, index) => index + 1);
      if (page <= 2 || page >= total - 1) return [1, 2, null, total - 1, total];
      return page <= Math.floor((total + 1) / 2)
        ? [page - 1, page, null, total - 1, total] : [1, 2, null, page, page + 1];
    },
    goToPage(page) { this.$dispatch('story-arc-preview-page', { page }); },
  };
}
