function storyArcDetail() {
  return {
    coverModalOpen: false, coverModalUrl: '', reordering: false, reorderError: '',
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
