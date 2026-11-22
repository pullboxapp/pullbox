function storyArcDetail() {
  return {
    coverModalOpen: false, coverModalUrl: '', reordering: false, reorderError: '',
    deletePhase: '', deleteError: '',
    async reviewDelete(arcId, opener) {
      if (this.deletePhase) return;
      const url = '/story-arcs/' + Number(arcId) + '/delete';
      const count = (value, singular, plural) => value + ' ' + (value === 1 ? singular : plural);
      this.deleteError = '';
      this.deletePhase = 'Loading preview…';
      try {
        const preview = await fetch(url + '/preview', {cache: 'no-store'});
        if (!preview.ok) throw new Error('Could not load this arc. Refresh the page and try again.');
        const p = await preview.json();
        if (p.blocker || !p.token) throw new Error(p.blocker || 'Deletion is unavailable.');
        const keep = [count(p.issues, 'issue', 'issues'), count(p.series, 'series', 'series'), count(p.files, 'library file', 'library files')].join(', ');
        this.deletePhase = 'Review deletion';
        const confirmed = await pbConfirm({
          title: 'Delete ' + p.name + '?',
          message: 'Permanently remove this arc and ' + count(p.entries, 'reading-order entry', 'reading-order entries') + '. Keep: ' + keep + '. ' +
            (p.shared_issues ? count(p.shared_issues, 'issue is', 'issues are') + ' also in other arcs; those arcs stay unchanged. ' : '') +
            (p.placements ? 'Stop synchronizing ' + count(p.placements, 'arc placement', 'arc placements') + '; existing copies and links stay on disk. ' : '') +
            'No files will be moved or deleted.',
          confirmText: 'Delete arc only', destructive: true,
        });
        if (!confirmed) return;
        this.deletePhase = 'Deleting arc…';
        const response = await fetch(url, {
          method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': readCsrfTokenFromBody()},
          body: JSON.stringify({token: p.token}),
        });
        const result = await response.json();
        if (!response.ok || result.deleted !== true) {
          throw new Error(typeof result.detail === 'string' ? result.detail : 'Deletion could not be confirmed. Refresh before trying again.');
        }
        window.location.assign('/story-arcs?notice=arc-deleted');
      } catch (error) {
        this.deleteError = error instanceof TypeError || error instanceof SyntaxError
          ? 'Pullbox could not confirm the request. Refresh the page before trying again.'
          : error.message;
      } finally {
        this.deletePhase = '';
        this.$nextTick(() => opener.focus({preventScroll: true}));
      }
    },
    beginReorder(event) {
      if (!event.detail.elt?.matches('[data-story-arc-move]')) return;
      if (this.reordering) { event.preventDefault(); return; }
      this.reordering = true;
      this.reorderError = '';
    },
    reorderRequestFinished(event) {
      if (!event.detail.elt?.matches('[data-story-arc-move]')) return;
      if (event.detail.successful) return;
      this.reordering = false;
      this.reorderError = 'The new order could not be confirmed. Refresh the page to check the saved order before trying again.';
    },
    finishReorder(detail) {
      this.reordering = false;
      this.$nextTick(() => {
        const row = this.$root.querySelector(`[data-membership-id="${Number(detail.membershipId)}"]`);
        const direction = detail.direction === 'up' ? 'up' : 'down';
        const button = row?.querySelector(`[data-order-direction="${direction}"]`);
        const target = button?.disabled ? row.querySelector('[data-order-direction]:not(:disabled)') : button;
        target?.focus({ preventScroll: true });
        if (detail.pageChanged) row?.scrollIntoView({ block: 'nearest' });
      });
    },
    submitMove(button, direction) {
      if (this.reordering || button.disabled) return;
      button.form.elements.direction.value = direction;
      button.form.requestSubmit();
    },
  };
}
