function gcdLocalSettings(seed, csrf) {
  return {
    gcdBase: seed, gcdPath: seed?.settings?.database_path || '', gcdBusy: false,
    gcdError: '', gcdMessage: '', gcdController: null, gcdAlive: true,
    destroy() { this.gcdAlive = false; this.gcdController?.abort(); },
    cancelGcd() { this.gcdController?.abort(); },
    prioritiesUpdated(detail) {
      const policy = detail?.policies?.find(item => item.source === 'gcd_local');
      if (policy && this.gcdBase?.revision === detail.previousRevisions?.gcd_local) {
        this.gcdBase = JSON.parse(JSON.stringify(policy));
      }
    },
    async reloadGcd() {
      if (this.gcdBusy) return;
      this.gcdBusy = true;
      try {
        const response = await fetch('/api/v1/metadata/sources');
        if (!response.ok) throw new Error('load');
        const data = await response.json();
        if (!this.gcdAlive) return;
        this.gcdBase = data.find(item => item.source === 'gcd_local');
        this.gcdPath = this.gcdBase?.settings?.database_path || '';
        this.gcdError = ''; this.gcdMessage = '';
      } catch (_) {
        if (this.gcdAlive) this.gcdError = 'Could not load GCD settings. Your draft is kept; retry.';
      } finally { this.gcdBusy = false; }
    },
    async saveGcd(enabled) {
      if (this.gcdBusy) return;
      const base = this.gcdBase;
      this.gcdBusy = true; this.gcdError = ''; this.gcdMessage = '';
      this.gcdController = new AbortController();
      const timeout = setTimeout(() => this.gcdController?.abort(), 310000);
      try {
        const response = await fetch('/api/v1/metadata/sources/gcd_local', {
          method: 'PUT', signal: this.gcdController.signal,
          headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
          body: JSON.stringify({revision: base.revision, enabled, priority: base.priority,
            domain_priorities: base.domain_priorities,
            settings: enabled ? {database_path: this.gcdPath.trim()} : base.settings}),
        });
        const data = await response.json();
        if (!this.gcdAlive) return;
        if (!response.ok) {
          this.gcdError = response.status === 409
            ? 'GCD settings changed in another session. Load saved GCD settings before retrying.'
            : typeof data.detail === 'string' ? data.detail : 'GCD validation failed. Check the dump and retry.';
          return;
        }
        this.gcdBase = data;
        this.gcdPath = data.settings.database_path || '';
        this.gcdMessage = enabled ? 'GCD is enabled. You can search, preview and add series.' : 'GCD is disabled. The database and its saved path are unchanged.';
        window.dispatchEvent(new CustomEvent('metadata-credentials-updated', {
          detail: {source: 'gcd_local', previousRevision: base.revision, policy: data},
        }));
      } catch (_) {
        if (this.gcdAlive) this.gcdError = 'Validation stopped or the connection was lost. Load saved GCD settings to confirm the current source before retrying.';
      } finally {
        clearTimeout(timeout); this.gcdController = null; this.gcdBusy = false;
      }
    },
  };
}

function gcdApiSettings(seed, csrf) {
  const clone = value => JSON.parse(JSON.stringify(value));
  return {
    base: clone(seed), enabled: Boolean(seed?.enabled), token: '', clearToken: false,
    busy: false, error: '', message: '', conflict: false, alive: true, controller: null,
    username: '', password: '',
    clearSignIn() { this.username = ''; this.password = ''; },
    destroy() { this.alive = false; this.token = ''; this.clearSignIn(); this.controller?.abort(); },
    get dirty() {
      return this.enabled !== this.base.enabled || Boolean(this.token) || this.clearToken;
    },
    prioritiesUpdated(detail) {
      const policy = detail?.policies?.find(item => item.source === 'gcd_api_v2');
      if (policy && this.base.revision === detail.previousRevisions?.gcd_api_v2) {
        this.base = clone(policy);
      }
    },
    async request(url, options = {}) {
      const controller = new AbortController();
      this.controller = controller;
      const timeout = setTimeout(() => controller.abort(), 20000);
      try {
        const response = await fetch(url, {...options, signal: controller.signal,
          headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}});
        return {ok: response.ok, status: response.status, data: response.ok ? await response.json() : null};
      } finally { clearTimeout(timeout); this.controller = null; }
    },
    async reload() {
      if (this.busy) return;
      this.busy = true;
      try {
        const response = await this.request('/api/v1/metadata/sources');
        if (!this.alive) return;
        if (!response.ok) throw new Error('load');
        this.base = clone(response.data.find(item => item.source === 'gcd_api_v2'));
        this.enabled = this.base.enabled;
        this.token = ''; this.clearToken = false; this.clearSignIn(); this.conflict = false;
        this.error = ''; this.message = '';
      } catch (_) {
        if (this.alive) this.error = 'Could not load GCD API settings. Your draft is kept; retry.';
      } finally { this.busy = false; }
    },
    async save() {
      if (this.busy || !this.dirty) return;
      this.error = ''; this.message = '';
      if (this.enabled && this.clearToken) {
        this.error = 'Disable GCD API v2 before removing its saved token.'; return;
      }
      if (this.enabled && !this.token && !this.base.credential_configured) {
        this.error = 'Enter a token before enabling GCD API v2.'; return;
      }
      if (this.token && (!/^[\x21-\x7e]+$/.test(this.token) || this.token.length > 4096 || this.token.startsWith('enc:'))) {
        this.error = 'Enter your GCD API token without spaces or line breaks.'; return;
      }
      const base = clone(this.base);
      this.busy = true;
      try {
        const response = await this.request('/api/v1/metadata/sources/gcd_api_v2', {
          method: 'PUT', body: JSON.stringify({revision: base.revision, enabled: this.enabled,
            priority: base.priority, domain_priorities: base.domain_priorities, settings: base.settings,
            ...(this.token && !this.clearToken ? {credential: this.token} : {}), clear_credential: this.clearToken}),
        });
        if (!this.alive) return;
        if (response.status === 409) {
          this.conflict = true;
          this.error = 'GCD API settings changed in another session. Your draft is kept. Load saved GCD API settings before retrying.';
          return;
        }
        if (!response.ok) throw new Error('save');
        this.base = clone(response.data); this.enabled = this.base.enabled;
        this.token = ''; this.clearToken = false; this.conflict = false;
        this.message = 'GCD API settings saved. Connection checks use this saved configuration.';
        window.dispatchEvent(new CustomEvent('metadata-credentials-updated', {
          detail: {source: 'gcd_api_v2', previousRevision: base.revision, policy: response.data},
        }));
      } catch (_) {
        if (this.alive) {
          this.conflict = true;
          this.error = 'Could not save GCD API settings. Your draft is kept. Load saved settings to confirm before retrying.';
        }
      } finally { this.busy = false; }
    },
    async signIn() {
      if (this.busy || this.dirty || !this.username || !this.password) return;
      const base = clone(this.base);
      const input = {revision: base.revision, username: this.username, password: this.password};
      this.busy = true; this.error = ''; this.message = ''; this.clearSignIn();
      try {
        const pending = this.request('/api/v1/metadata/sources/gcd_api_v2/sign-in', {
          method: 'POST', body: JSON.stringify(input),
        });
        input.username = ''; input.password = '';
        const response = await pending;
        if (!this.alive) return;
        if (response.status === 409) {
          this.conflict = true;
          this.error = 'GCD API settings changed. Your sign-in fields were cleared. Load saved GCD API settings before retrying.';
          return;
        }
        if (!response.ok) {
          const messages = new Map([
            [400, 'GCD did not accept the sign-in or token. Check your GCD credentials. The saved source is unchanged.'],
            [429, 'GCD is rate-limited. Wait before signing in again. The saved source is unchanged.'],
            [502, 'GCD sign-in or its connection check failed. Try again later. The saved source is unchanged.'],
            [504, 'GCD sign-in or its connection check timed out. Try again later. The saved source is unchanged.'],
          ]);
          if (!messages.has(response.status)) throw new Error('sign-in');
          this.error = messages.get(response.status);
          return;
        }
        this.base = clone(response.data); this.enabled = this.base.enabled;
        this.token = ''; this.clearToken = false; this.conflict = false;
        this.message = 'GCD connected. A verified token is saved; your username and password were not saved.';
        window.dispatchEvent(new CustomEvent('metadata-credentials-updated', {
          detail: {source: 'gcd_api_v2', previousRevision: base.revision, policy: response.data},
        }));
      } catch (_) {
        if (this.alive) {
          this.conflict = true;
          this.error = 'The connection was lost. Your sign-in fields were cleared. Load saved GCD API settings to confirm before retrying.';
        }
      } finally { input.username = ''; input.password = ''; this.clearSignIn(); this.busy = false; }
    },
  };
}

function metadataSourceSettings(seed, csrf) {
  const clone = value => JSON.parse(JSON.stringify(value));
  const labels = {
    comicvine_local: 'ComicVine Local Catalog', comicvine_api: 'ComicVine API',
    metron_api: 'Metron', gcd_local: 'GCD Local Database', gcd_api_v2: 'GCD API v2',
  };
  return {
    sources: clone(seed), savedSources: clone(seed), order: [], domainOrders: {},
    domains: {core: 'Core metadata', issues: 'Issue catalogs', artwork: 'Artwork', story_arcs: 'Story arcs'},
    labels, saving: false, testing: null, message: '', error: '', conflict: false,
    healthMessage: '', controllers: new Set(), alive: true, savedOrder: '', refreshing: false,
    metronBase: null, metronEnabled: false, metronToken: '', metronClear: false,
    metronSaving: false, metronError: '', metronMessage: '', metronConflict: false,
    healthRefreshPending: false,
    retryOpen: false, retrySource: null, retryPage: {items: [], total: 0, offset: 0, limit: 10, has_more: false},
    retryLoading: false, retryError: '', retryTrigger: null,
    init() { this.accept(seed); this.acceptMetron(seed.find(item => item.source === 'metron_api')); },
    destroy() {
      this.alive = false; this.metronToken = '';
      this.controllers.forEach(controller => controller.abort());
    },
    get busy() { return this.saving || this.testing || this.refreshing || this.metronSaving; },
    get metronDirty() {
      return Boolean(this.metronBase && (this.metronEnabled !== this.metronBase.enabled ||
        this.metronToken || this.metronClear));
    },
    acceptMetron(item) {
      this.metronBase = item ? clone(item) : null;
      this.metronEnabled = Boolean(item?.enabled);
      this.metronToken = ''; this.metronClear = false;
      this.metronError = ''; this.metronMessage = ''; this.metronConflict = false;
    },
    async saveMetron() {
      if (this.busy || !this.metronDirty) return;
      this.metronError = ''; this.metronMessage = '';
      if (this.metronEnabled && this.metronClear) {
        this.metronError = 'Disable Metron before removing its saved token.';
        return;
      }
      if (this.metronEnabled && !this.metronToken && !this.metronBase.credential_configured) {
        this.metronError = 'Enter a token before enabling Metron.';
        return;
      }
      if (this.metronToken && (!/^[\x21-\x7e]+$/.test(this.metronToken) ||
          this.metronToken.length > 4096 || this.metronToken.startsWith('enc:'))) {
        this.metronError = 'Enter the API token from your Metron account, without spaces or line breaks.';
        return;
      }
      this.metronSaving = true;
      const base = clone(this.metronBase);
      try {
        const response = await this.request('/api/v1/metadata/sources/metron_api', {
          method: 'PUT', body: JSON.stringify({
            revision: base.revision, enabled: this.metronEnabled,
            priority: base.priority, domain_priorities: base.domain_priorities, settings: base.settings,
            ...(this.metronToken && !this.metronClear ? {credential: this.metronToken} : {}),
            clear_credential: this.metronClear,
          }),
        });
        if (!this.alive) return;
        if (response.status === 409) {
          this.metronConflict = true;
          this.metronError = 'Metron settings changed in another session. Your draft is kept. Load saved Metron settings before trying again.';
          return;
        }
        if (!response.ok) throw new Error('save');
        const policy = response.data;
        const current = this.sources.find(item => item.source === 'metron_api');
        const descriptor = {...current, ...policy, availability: policy.configuration_status ||
          (!policy.enabled ? 'disabled' : !policy.credential_configured ? 'unconfigured' : null)};
        this.sources = this.sources.map(item => item.source === 'metron_api' ? descriptor : item);
        // Only advance the priority revision when that draft saw the same policy.
        this.savedSources = this.savedSources.map(item =>
          item.source === 'metron_api' && item.revision === base.revision ? clone(descriptor) : item);
        this.acceptMetron(policy);
        this.healthRefreshPending = true;
        this.healthMessage = '';
        this.metronMessage = 'Metron settings saved. Connection checks use this saved configuration.';
      } catch (_) {
        if (this.alive) this.metronError = 'Could not save Metron settings. Your draft is kept; check the connection and retry.';
      } finally { this.metronSaving = false; this.flushHealthRefresh(); }
    },
    async reloadMetron() {
      if (this.busy) return;
      this.metronSaving = true;
      try {
        const response = await this.request('/api/v1/metadata/sources');
        if (!response.ok) throw new Error('load');
        if (!this.alive) return;
        this.sources = response.data;
        this.acceptMetron(response.data.find(item => item.source === 'metron_api'));
      } catch (_) {
        if (this.alive) this.metronError = 'Could not load Metron settings. Your draft is kept; check the connection and retry.';
      } finally { this.metronSaving = false; this.flushHealthRefresh(); }
    },
    flushHealthRefresh() {
      if (this.alive && this.healthRefreshPending && !this.busy) {
        this.healthRefreshPending = false;
        this.refreshHealth();
      }
    },
    sourceUpdated(detail) {
      if (detail?.policy && detail.policy.source === detail.source) {
        const current = this.sources.find(item => item.source === detail.source);
        const descriptor = {...current, ...detail.policy};
        // A same-page save may advance its own baseline, not an unseen external edit.
        this.savedSources = this.savedSources.map(item =>
          item.source === detail.source && item.revision === detail.previousRevision
            ? clone(descriptor) : item);
      }
      this.refreshHealth();
    },
    eligible(domain) {
      return this.order.filter(source => domain !== 'artwork' || !source.startsWith('gcd_'));
    },
    accept(data) {
      this.sources = clone(data);
      this.savedSources = clone(data);
      this.order = data.map(item => item.source);
      this.domainOrders = {};
      for (const domain of Object.keys(this.domains)) {
        if (data.some(item => Object.hasOwn(item.domain_priorities, domain))) {
          this.domainOrders[domain] = this.eligible(domain).sort((a, b) => {
            const left = data.find(item => item.source === a);
            const right = data.find(item => item.source === b);
            return (left.domain_priorities[domain] ?? left.priority) -
              (right.domain_priorities[domain] ?? right.priority) || a.localeCompare(b);
          });
        }
      }
      this.savedOrder = this.signature();
    },
    signature() {
      return JSON.stringify({order: this.order, domain_orders: Object.fromEntries(
        Object.keys(this.domains).filter(domain => this.domainOrders[domain]).map(domain => [domain, this.domainOrders[domain]])
      )});
    },
    get dirty() {
      return this.signature() !== this.savedOrder;
    },
    move(domain, index, direction) {
      if (this.busy) return;
      const order = domain === 'global' ? this.order : this.domainOrders[domain];
      const target = index + direction;
      if (!order || target < 0 || target >= order.length) return;
      const focused = document.activeElement;
      const row = focused?.closest('[data-source-row]');
      [order[index], order[target]] = [order[target], order[index]];
      this.message = '';
      if (row && this.$root.contains(row)) {
        this.$nextTick(() => {
          const button = focused.disabled ? row.querySelector('button:not(:disabled)') : focused;
          button?.focus({preventScroll: true});
        });
      }
    },
    toggleDomain(domain, enabled) {
      if (this.busy) return;
      if (enabled) this.domainOrders[domain] = this.eligible(domain);
      else delete this.domainOrders[domain];
      this.message = '';
    },
    reset() {
      if (this.busy) return;
      this.accept(this.savedSources);
      this.message = ''; this.error = ''; this.conflict = false;
    },
    async request(url, options = {}) {
      const controller = new AbortController();
      this.controllers.add(controller);
      const timeout = setTimeout(() => controller.abort(), 20000);
      try {
        const response = await fetch(url, {
          ...options, signal: controller.signal,
          headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
        });
        const data = await response.json();
        return {ok: response.ok, status: response.status, data};
      } finally {
        clearTimeout(timeout);
        this.controllers.delete(controller);
      }
    },
    async save() {
      if (this.busy || !this.dirty) return;
      this.saving = true; this.error = ''; this.message = ''; this.conflict = false;
      const revisions = Object.fromEntries(this.savedSources.map(item => [item.source, item.revision]));
      try {
        const response = await this.request('/api/v1/metadata/priorities', {
          method: 'PUT', body: JSON.stringify({
            order: this.order, domain_orders: this.domainOrders,
            revisions,
          }),
        });
        if (response.status === 409) {
          this.conflict = true;
          this.error = 'Settings changed in another session. Your draft is kept. Load saved priority before trying again.';
          return;
        }
        if (!response.ok) throw new Error('save');
        const data = response.data;
        if (!this.alive) return;
        const previous = this.savedSources.find(item => item.source === 'metron_api');
        if (this.metronBase?.revision === previous?.revision) {
          this.metronBase = clone(data.find(item => item.source === 'metron_api'));
        }
        this.accept(data);
        window.dispatchEvent(new CustomEvent('metadata-priorities-updated', {
          detail: {previousRevisions: revisions, policies: data},
        }));
        this.message = 'Metadata priority saved.';
      } catch (_) {
        if (this.alive) this.error = 'Could not save metadata priority. Your draft is kept; check the connection and retry.';
      } finally { this.saving = false; this.flushHealthRefresh(); }
    },
    async reload() {
      if (this.busy) return;
      this.saving = true;
      try {
        const response = await this.request('/api/v1/metadata/sources');
        if (!response.ok) throw new Error('load');
        const data = response.data;
        if (!this.alive) return;
        this.accept(data); this.error = ''; this.conflict = false; this.message = '';
      } catch (_) {
        if (this.alive) this.error = 'Could not load saved priority. Your draft is kept; check the connection and retry.';
      } finally { this.saving = false; this.flushHealthRefresh(); }
    },
    status(item) {
      if (!item.capabilities.length && item.availability !== 'feature_disabled') return 'Not available in this build';
      const state = item.availability || item.account?.status || item.last_status || 'not_checked';
      return {
        ok: 'Ready', empty: 'Ready', disabled: 'Disabled', feature_disabled: 'Not released yet',
        not_implemented: 'Not available in this build', unconfigured: 'Not configured',
        invalid_configuration: 'Configuration needs attention', authentication_failed: 'Authentication required',
        rate_limited: 'Rate limited', timeout: 'Timed out', unavailable: 'Unavailable',
        incompatible_response: 'Unexpected provider response', unsupported: 'Not supported',
        not_checked: 'Not checked yet',
      }[state] || 'Needs attention';
    },
    dateLabel(value) { return value ? new Date(value).toLocaleString() : ''; },
    holdMessage(item) {
      if (item.availability) return '';
      const account = item.account;
      if (account?.probe_until && new Date(account.probe_until) > new Date()) return 'A connection check is in progress.';
      if (account?.status === 'authentication_failed') return 'Automatic requests are paused. Check saved access, then test the connection to resume waiting work.';
      if (account?.retry_at && new Date(account.retry_at) > new Date()) return 'Requests can resume after ' + this.dateLabel(account.retry_at) + '. Other sources can still run.';
      return '';
    },
    retryState(row) {
      return {
        ready: 'Ready for the next scheduled run',
        waiting: 'Waiting until ' + this.dateLabel(row.retry_at),
        authentication_required: 'Check saved access, then test the connection',
        source_disabled: 'Enable and configure this source to resume',
      }[row.state] || 'Needs attention';
    },
    async showRetries(source, trigger) {
      if (this.retryLoading) return;
      this.retrySource = source; this.retryTrigger = trigger; this.retryOpen = true;
      this.retryPage = {items: [], total: 0, offset: 0, limit: 10, has_more: false};
      await this.loadRetries(0);
    },
    closeRetries() {
      if (this.retryLoading) return;
      this.retryOpen = false;
      this.$nextTick(() => this.retryTrigger?.focus({preventScroll: true}));
    },
    async loadRetries(offset) {
      if (this.retryLoading) return;
      this.retryLoading = true; this.retryError = '';
      const query = new URLSearchParams({limit: '10', offset: String(offset)});
      if (this.retrySource) query.set('source', this.retrySource);
      try {
        const response = await this.request('/api/v1/metadata/retries?' + query);
        if (!response.ok) throw new Error('load');
        if (this.alive) this.retryPage = response.data;
      } catch (_) {
        if (this.alive) this.retryError = 'Could not load deferred work. Check the connection and retry.';
      } finally { this.retryLoading = false; }
    },
    canTest(item) { return item.enabled && item.capabilities.length > 0 && !item.availability; },
    async refreshHealth() {
      if (this.busy) { this.healthRefreshPending = true; return; }
      this.refreshing = true;
      try {
        const response = await this.request('/api/v1/metadata/sources');
        if (!response.ok) throw new Error('load');
        if (this.alive) this.sources = response.data;
      } catch (_) {
        if (this.alive) this.healthMessage = 'Could not refresh source status. Reload Metadata settings to check the saved configuration.';
      } finally { this.refreshing = false; this.flushHealthRefresh(); }
    },
    async test(item) {
      if (this.busy || !this.canTest(item)) return;
      this.testing = item.source;
      this.healthMessage = 'Checking ' + this.labels[item.source] + '...';
      try {
        const response = await this.request('/api/v1/metadata/sources/' + item.source + '/test', {method: 'POST'});
        if (!response.ok) throw new Error('test');
        const data = response.data;
        if (!this.alive) return;
        if (!data.recorded) {
          this.healthMessage = 'Settings changed during this check. Test the saved configuration again.';
          return;
        }
        item.last_status = data.outcome.status;
        this.healthMessage = this.labels[item.source] + ': ' + this.status(item) + '.';
        if (data.outcome.retry_after_seconds != null) {
          this.healthMessage += ' Retry in ' + data.outcome.retry_after_seconds + ' seconds.';
        }
      } catch (_) {
        if (this.alive) this.healthMessage = 'Could not check ' + this.labels[item.source] + '. Check the connection and retry.';
      } finally {
        this.testing = null;
        await this.refreshHealth();
        if (this.retryOpen && !this.retryLoading) await this.loadRetries(this.retryPage.offset);
        this.flushHealthRefresh();
      }
    },
  };
}
