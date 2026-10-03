function safeSeriesCoverUrl(value) {
  if (typeof value !== 'string' || value.length > 4096 || /[\s\x00-\x1f]/.test(value)) return '';
  try {
    const url = new URL(value);
    const path = decodeURIComponent(url.pathname);
    if (url.protocol !== 'https:' || (url.port && url.port !== '443') ||
        url.username || url.password || url.search || url.hash ||
        path.split('/').includes('..') || /[\\\x00-\x1f]/.test(path)) return '';
    const metron = ['metron.cloud', 'static.metron.cloud'].includes(url.hostname);
    return (metron ? path.startsWith('/media/') :
      ['comicvine.gamespot.com', 'comicvine.com'].includes(url.hostname) ||
      url.hostname.endsWith('.cbsistatic.com')) ? value : '';
  } catch (_) { return ''; }
}

function metronSeriesCover(config = {}) {
  return {
    coverUrl: '', coverLoaded: false, coverState: 'pending',
    coverDisposed: false, coverController: null, coverObserver: null,
    init() {
      this.coverObserver = new IntersectionObserver(entries => {
        if (entries.some(entry => entry.isIntersecting)) {
          this.coverObserver.disconnect();
          this.queueSeriesCover(this);
        }
      });
      this.coverObserver.observe(this.$el);
    },
    destroy() {
      this.coverDisposed = true;
      this.coverObserver?.disconnect();
      this.coverController?.abort();
    },
    async loadCover() {
      if (this.coverDisposed || !this.$el.isConnected) return;
      const controller = new AbortController();
      this.coverController = controller;
      const timeout = setTimeout(() => controller.abort(), 15000);
      this.coverState = 'loading';
      try {
        const response = await fetch('/api/v1/metadata/series/issues', {
          method: 'POST', signal: controller.signal,
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': readCsrfTokenFromBody() },
          body: JSON.stringify({
            source: 'metron_api', external_id: config.externalId || this.$el.dataset.coverSeriesId,
            source_revision: config.sourceRevision ?? Number(this.$el.dataset.coverSourceRevision), page: 1,
          }),
        });
        if (!response.ok || this.coverDisposed) return;
        const data = await response.json();
        if (this.coverDisposed || !this.$el.isConnected) return;
        if (data.status === 'ok') this.coverUrl = safeSeriesCoverUrl(data.series_cover_url);
      } catch (_) {
        // Artwork is optional; a failed lookup must never interrupt search or Add.
      } finally {
        clearTimeout(timeout);
        if (!this.coverDisposed && !this.coverUrl) this.coverState = 'unavailable';
      }
    },
  };
}

function addSeries(config = {}) {
  return {
    showModal: false,
    adding: false,
    previewLoading: false,
    previewReady: false,
    previewFailed: false,
    previewSequence: 0,
    previewController: null,
    disposed: false,
    trigger: null,
    addError: '',
    selectedSource: '',
    selectedExternalId: '',
    sourceRevision: null,
    catalogReview: null,
    reviewPage: 0,
    selectedTitle: '',
    selectedPublisher: '',
    selectedYear: null,
    selectedIssueCount: null,
    selectedCoverUrl: '',
    selectedDescription: '',
    selectedFolderPreview: '',
    libraryRootId: config.libraryRootId || null,
    folderPreview: '',
    coverQueue: [],
    coverWorker: false,
    rootPaths: config.rootPaths || {},
    rootLabel: config.rootLabel || '',
    afterSeriesAdded: null,

    sourceLabel() {
      return {
        comicvine_local: 'ComicVine Local Catalog', comicvine_api: 'ComicVine API',
        metron_api: 'Metron', gcd_local: 'GCD Local Database', gcd_api_v2: 'GCD API v2',
      }[this.selectedSource] || 'Metadata source';
    },

    queueSeriesCover(cover) {
      this.coverQueue = this.coverQueue.filter(item => !item.coverDisposed && item.$el.isConnected);
      if (this.coverQueue.length < 20) this.coverQueue.push(cover);
      this.processSeriesCovers();
    },

    async processSeriesCovers() {
      if (this.coverWorker || this.disposed || this.showModal) return;
      this.coverWorker = true;
      try {
        while (this.coverQueue.length && !this.disposed && !this.showModal) {
          await this.coverQueue.shift().loadCover();
        }
      } finally { this.coverWorker = false; }
    },

    destroy() {
      this.disposed = true;
      this.coverQueue = [];
      this.previewSequence++;
      this.previewController?.abort();
    },

    closeModal() {
      if (!this.showModal || this.adding) return;
      this.showModal = false;
      this.previewSequence++;
      this.previewController?.abort();
      this.previewLoading = false;
      this.previewReady = false;
      const trigger = this.trigger?.isConnected && this.trigger !== document.body
        ? this.trigger : document.querySelector('[data-testid="add-series-search-input"]');
      trigger?.focus({ preventScroll: true });
      this.processSeriesCovers();
    },

    trapFocus(event) {
      const dialog = this.$refs.dialog;
      const controls = [...dialog.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), a[href], [tabindex="0"]')]
        .filter(node => node.getClientRects().length > 0);
      const first = controls[0], last = controls[controls.length - 1];
      if (!first) { event.preventDefault(); return; }
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog)) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog)) {
        event.preventDefault(); first.focus();
      }
    },

    previewFailure(status, retryAfter) {
      const reasons = {
        disabled: 'This source is disabled. Enable it in Metadata settings, then retry.',
        feature_disabled: 'This source is not available in this build.',
        unconfigured: 'Configure this source in Metadata settings, then retry.',
        authentication_failed: 'Check this source\'s credentials in Metadata settings, then retry.',
        not_found: 'This series is no longer available from the selected source.',
        rate_limited: 'This source is rate limited.' + (Number.isSafeInteger(retryAfter) && retryAfter > 0 ? ' Retry in ' + retryAfter + ' seconds.' : ' Try again later.'),
        timeout: 'This source took too long to respond. Retry the preview.',
      };
      return reasons[status] || 'The preview could not be verified. Retry or check Metadata settings.';
    },

    async loadPreview() {
      if (this.adding || !this.showModal) return;
      this.previewController?.abort();
      const controller = new AbortController();
      this.previewController = controller;
      const sequence = ++this.previewSequence;
      const source = this.selectedSource, externalId = this.selectedExternalId;
      this.previewLoading = true;
      this.previewReady = false;
      this.previewFailed = false;
      this.sourceRevision = null;
      this.catalogReview = null;
      this.reviewPage = 0;
      this.addError = '';
      try {
        const rootId = Number(this.libraryRootId);
        if (!Number.isSafeInteger(rootId) || rootId < 1) {
          throw new Error('Choose an enabled managed library root in Media Management, then retry.');
        }
        const response = await fetch('/api/v1/metadata/series/preview', {
          method: 'POST', signal: controller.signal,
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': readCsrfTokenFromBody() },
          body: JSON.stringify({ source, external_id: externalId, library_root_id: rootId }),
        });
        const data = await response.json();
        if (sequence !== this.previewSequence || this.disposed || !this.showModal) return;
        if (!response.ok) throw new Error(data.error?.message || (typeof data.detail === 'string' ? data.detail : 'Preview failed. Retry the preview.'));
        const profile = data.series?.data;
        if (data.series?.status !== 'ok') throw new Error(this.previewFailure(data.series?.status, data.series?.retry_after_seconds));
        if (data.source !== source || data.external_id !== externalId ||
            profile?.source !== source || profile?.external_id !== externalId ||
            !profile.title || !Number.isSafeInteger(data.source_revision) || data.source_revision < 0) {
          throw new Error('The preview identity changed. Search again before adding this series.');
        }
        this.selectedTitle = profile.title;
        this.selectedPublisher = profile.publisher || '';
        this.selectedYear = profile.year_start || null;
        this.selectedIssueCount = profile.issue_count ?? null;
        this.selectedCoverUrl = safeSeriesCoverUrl(profile.image_url);
        if (data.issues?.status !== 'ok' || !data.issues.data ||
            !Number.isSafeInteger(data.issues.data.total) || data.issues.data.total < 0) {
          throw new Error(this.previewFailure(data.issues?.status, data.issues?.retry_after_seconds));
        }
        if (typeof data.folder_preview !== 'string' || !data.folder_preview.trim()) {
          throw new Error('The folder preview could not be verified. Retry the preview.');
        }
        this.selectedFolderPreview = data.folder_preview;
        this.updateFolderPreview();
        this.selectedIssueCount = data.issues.data.total;
        this.sourceRevision = data.source_revision;
        this.catalogReview = data.catalog_review || null;
        this.previewReady = true;
      } catch (error) {
        if (sequence !== this.previewSequence || this.disposed || !this.showModal) return;
        this.addError = error.name === 'AbortError' ? 'Preview cancelled. Retry the preview.' : error.message;
        this.previewFailed = true;
      } finally {
        if (sequence === this.previewSequence && !this.disposed) this.previewLoading = false;
      }
    },

    updateFolderPreview() {
      if (!this.selectedTitle) {
        this.folderPreview = '';
        return;
      }
      if (this.selectedFolderPreview) {
        this.folderPreview = this.selectedFolderPreview;
        return;
      }
      const year = this.selectedYear || 'Unknown';
      this.folderPreview = this.selectedTitle + ' (' + year + ')';
    },

    addSeriesToLibrary() {
      if (this.adding || !this.previewReady || !this.libraryRootId || (this.catalogReview && !this.catalogReview.supported_count)) return;
      this.adding = true;
      this.addError = '';

      const body = {
        source: this.selectedSource,
        external_id: this.selectedExternalId,
        source_revision: this.sourceRevision,
        library_root_id: this.libraryRootId ? parseInt(this.libraryRootId, 10) : null,
      };
      if (this.catalogReview) body.catalog_review_token = this.catalogReview.token;
      if (this.releaseContext?.selection) body.whats_new_selection = this.releaseContext.selection;

      fetch('/api/v1/series', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': readCsrfTokenFromBody(),
        },
        body: JSON.stringify(body),
      })
        .then(res => {
          if (res.ok) return res.json();
          return res.json().then(data => {
            throw new Error((data.error && data.error.message) || (typeof data.detail === 'string' ? data.detail : 'Failed to add series.'));
          });
        })
        .then(data => {
          this.adding = false;
          if (this.disposed) return;
          if (typeof this.afterSeriesAdded === 'function') this.afterSeriesAdded(data);
          this.closeModal();
          if (typeof showToast === 'function') {
            showToast({ message: '"' + (data.title || this.selectedTitle) + '" is in your library.', level: 'success' });
          }
          const form = document.getElementById('add-series-search-form');
          if (!this.afterSeriesAdded && form && window.htmx) {
            htmx.trigger(form, 'submit');
          }
        })
        .catch(err => {
          if (this.disposed) return;
          this.addError = err.message;
          this.adding = false;
          this.previewReady = false;
          this.previewFailed = true;
        });
    },
  };
}

function selectResult(payload, sourceEl = null) {
  const resolvedPayload = (typeof payload === 'number' || typeof payload === 'string')
    ? {
        externalId: sourceEl?.dataset.seriesExternalId || String(payload),
        source: sourceEl?.dataset.seriesSource || 'comicvine_api',
        title: sourceEl?.dataset.seriesTitle || '',
        publisher: sourceEl?.dataset.seriesPublisher || '',
        year: sourceEl?.dataset.seriesYear ? Number(sourceEl.dataset.seriesYear) : null,
        issueCount: sourceEl?.dataset.seriesIssueCount ? Number(sourceEl.dataset.seriesIssueCount) : null,
        coverUrl: sourceEl?.closest('[data-testid="add-series-result-card"]')
          ?.querySelector('[data-metadata-series-cover]')?.getAttribute('src') || sourceEl?.dataset.seriesCoverUrl || '',
        description: sourceEl?.dataset.seriesDescription || '',
        folderPreview: sourceEl?.dataset.seriesFolderPreview || '',
      }
    : payload;
  const el = document.getElementById('add-series-app');
  const component = Alpine.$data(el);
  if (component.adding) return;
  component.trigger = sourceEl || document.activeElement;
  component.selectedSource = resolvedPayload.source || 'comicvine_api';
  component.selectedExternalId = String(resolvedPayload.externalId || resolvedPayload.id || '');
  component.selectedTitle = resolvedPayload.title || '';
  component.selectedPublisher = resolvedPayload.publisher || '';
  component.selectedYear = resolvedPayload.year || null;
  component.selectedIssueCount = resolvedPayload.issueCount || null;
  component.selectedCoverUrl = safeSeriesCoverUrl(resolvedPayload.coverUrl);
  component.selectedDescription = resolvedPayload.description || '';
  component.selectedFolderPreview = resolvedPayload.folderPreview || '';
  component.addError = '';
  component.showModal = true;
  component.updateFolderPreview();
  component.$nextTick(() => requestAnimationFrame(() => {
    if (component.showModal && !component.disposed) {
      component.$refs.dialog.focus({ preventScroll: true });
    }
  }));
  component.loadPreview();
}
