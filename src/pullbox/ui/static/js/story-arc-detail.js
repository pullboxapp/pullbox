function storyArcDetail() {
  return {
    coverModalOpen: false, coverModalUrl: '', reordering: false, reorderError: '',
    deletePreview: null, deletePhase: '', deleteError: '', deleteUrl: '', deleteGeneration: 0,
    showPaths: false, pathItems: [], pathCursors: [0], pathNext: null, pathBusy: false, pathError: '', pathRetry: [0],
    selectedPaths: [], cleanupReview: null,
    reviewDelete(arcId) {
      if (this.$refs.deleteDialog.open) return;
      this.deleteUrl = '/story-arcs/' + Number(arcId) + '/delete';
      this.$refs.deleteDialog.showModal();
      this.$refs.deleteCancel.focus();
      this.loadDeletePreview();
    },
    closeDelete() {
      if (this.deletePhase === 'deleting') return;
      ++this.deleteGeneration;
      this.$refs.deleteDialog.close();
      this.$refs.deleteOpener?.focus({preventScroll: true});
    },
    async loadDeletePreview() {
      const generation = ++this.deleteGeneration;
      this.deletePreview = null;
      this.deleteError = '';
      this.deletePhase = 'loading';
      this.showPaths = false;
      this.selectedPaths = [];
      this.cleanupReview = null;
      this.pathBusy = false;
      this.pathItems = [];
      this.pathCursors = [0];
      this.pathNext = null;
      this.pathError = '';
      try {
        const response = await fetch(this.deleteUrl + '/preview', {cache: 'no-store'});
        if (!response.ok) throw new Error();
        const preview = await response.json();
        if (generation === this.deleteGeneration) this.deletePreview = preview;
      } catch {
        if (generation === this.deleteGeneration) this.deleteError = 'Could not load the deletion preview.';
      } finally {
        if (generation === this.deleteGeneration) this.deletePhase = '';
      }
    },
    async togglePaths() {
      this.showPaths = !this.showPaths;
      if (this.showPaths && !this.pathItems.length && !this.pathBusy) await this.loadPaths([0]);
    },
    async loadPaths(cursors) {
      if (this.pathBusy) return;
      const generation = this.deleteGeneration;
      this.pathBusy = true;
      this.pathRetry = cursors;
      this.pathError = '';
      try {
        const response = await fetch(this.deleteUrl + '/placements?after=' + cursors.at(-1), {cache: 'no-store'});
        if (!response.ok) throw new Error();
        const data = await response.json();
        if (generation !== this.deleteGeneration) return;
        this.pathItems = data.items;
        this.pathNext = data.next_after;
        this.pathCursors = cursors;
      } catch {
        if (generation === this.deleteGeneration) this.pathError = 'Could not load the arc files.';
      } finally {
        if (generation === this.deleteGeneration) this.pathBusy = false;
      }
    },
    selectPath(id, selected) {
      this.cleanupReview = null;
      this.selectedPaths = selected ? [...this.selectedPaths, id] : this.selectedPaths.filter(x => x !== id);
    },
    async confirmCleanup() {
      if (this.deletePhase || this.deleteError || !this.selectedPaths.length) return;
      const confirm = !!this.cleanupReview;
      const generation = this.deleteGeneration;
      this.deletePhase = confirm ? 'deleting' : 'reviewing';
      try {
        const response = await fetch(this.deleteUrl + (confirm ? '/cleanup' : '/cleanup/preview'), {
          method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': readCsrfTokenFromBody()},
          body: JSON.stringify({ids: this.selectedPaths, ...(confirm ? {token: this.cleanupReview.token} : {})}),
        });
        const result = await response.json();
        if (generation !== this.deleteGeneration) return;
        if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Review the selected files again.');
        if (confirm && result.job_id) window.location.assign('/utilities?tab=queue');
        else if (!confirm && result.token) {
          for (const key of ['name', 'entries', 'issues', 'series', 'files', 'shared_issues', 'placements', 'managed_placements', 'referenced_placements']) {
            if (this.deletePreview[key] !== result.impact[key]) throw new Error('The arc changed. Refresh the preview before selecting files again.');
          }
          this.cleanupReview = result;
        }
        else throw new Error('Pullbox could not confirm the request. Refresh the preview.');
      } catch (error) {
        if (generation === this.deleteGeneration) this.deleteError = error.message;
      } finally {
        if (generation === this.deleteGeneration) this.deletePhase = '';
      }
    },
    async confirmDelete() {
      if (this.selectedPaths.length) return this.confirmCleanup();
      if (this.deletePhase || !this.deletePreview?.token || this.deleteError) return;
      this.deletePhase = 'deleting';
      try {
        const response = await fetch(this.deleteUrl, {
          method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': readCsrfTokenFromBody()},
          body: JSON.stringify({token: this.deletePreview.token}),
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
