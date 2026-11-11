const ENTER_DURATION = 200;
const EXIT_DURATION = 140;
const REDUCED_DURATION = 90;
const EASING = 'cubic-bezier(0.16, 1, 0.3, 1)';

const elementStates = new WeakMap();
const dialogStates = new WeakMap();
const detailsInstallations = new WeakMap();

function stateFor(store, element) {
  let state = store.get(element);
  if (!state) {
    state = {
      intent: undefined,
      token: 0,
      animation: null,
      promise: Promise.resolve(),
      pending: false,
      returnFocus: null,
      cancelHandler: null,
    };
    store.set(element, state);
  }
  return state;
}

function computedStyle(element) {
  return element.ownerDocument?.defaultView?.getComputedStyle(element) ?? null;
}

function reducedMotion(element) {
  try {
    return (
      element.ownerDocument?.defaultView?.matchMedia?.(
        '(prefers-reduced-motion: reduce)',
      ).matches ?? false
    );
  } catch {
    return false;
  }
}

function setInert(element, inert) {
  if ('inert' in element) element.inert = inert;
}

function cancelAnimation(state) {
  const animation = state.animation;
  state.animation = null;
  if (animation) animation.cancel();
}

function play(element, frames, duration, state, token, finish = () => {}) {
  if (typeof element.animate !== 'function') {
    if (state.token === token) finish();
    state.pending = false;
    state.promise = Promise.resolve();
    return state.promise;
  }

  let animation;
  try {
    animation = element.animate(frames, {
      duration,
      easing: EASING,
      fill: 'both',
    });
  } catch {
    if (state.token === token) finish();
    state.pending = false;
    state.promise = Promise.resolve();
    return state.promise;
  }

  state.animation = animation;
  state.pending = true;
  const promise = animation.finished
    .catch(() => undefined)
    .then(() => {
      if (state.token !== token) return;
      state.animation = null;
      animation.cancel();
      finish();
      state.pending = false;
    });
  state.promise = promise;
  return promise;
}

function resolvePanel(panels, selectedPanel) {
  if (panels.includes(selectedPanel)) return selectedPanel;
  if (Number.isInteger(selectedPanel)) return panels[selectedPanel] ?? null;
  if (typeof selectedPanel === 'string') {
    return (
      panels.find(
        panel =>
          panel.getAttribute('id') === selectedPanel ||
          panel.dataset?.panel === selectedPanel ||
          panel.dataset?.mainPanel === selectedPanel,
      ) ?? null
    );
  }
  return null;
}

/** Switch a panel set while applying its active and hidden state immediately. */
export function switchPanels(panels, selectedPanel) {
  const items = Array.from(panels ?? []).filter(panel => panel?.nodeType === 1);
  const selected = resolvePanel(items, selectedPanel);
  const animations = [];

  for (const panel of items) {
    const state = stateFor(elementStates, panel);
    const active = panel === selected;

    if (!active) {
      state.intent = false;
      state.token += 1;
      cancelAnimation(state);
      state.pending = false;
      state.promise = Promise.resolve();
      panel.classList.remove('active');
      panel.hidden = true;
      panel.setAttribute('aria-hidden', 'true');
      setInert(panel, true);
      continue;
    }

    const wasVisible = state.intent ?? !panel.hidden;
    panel.classList.add('active');
    panel.hidden = false;
    panel.setAttribute('aria-hidden', 'false');
    setInert(panel, false);

    if (state.intent === true) {
      animations.push(state.promise);
      continue;
    }
    if (wasVisible) {
      state.intent = true;
      state.promise = Promise.resolve();
      continue;
    }

    const before = computedStyle(panel);
    const startOpacity = wasVisible ? before?.opacity ?? '1' : '0';
    const startTransform = wasVisible
      ? before?.transform ?? 'none'
      : reducedMotion(panel)
        ? 'none'
        : 'translateY(6px)';
    const targetTransform = computedStyle(panel)?.transform ?? 'none';
    state.intent = true;
    const token = ++state.token;
    cancelAnimation(state);
    animations.push(
      play(
        panel,
        [
          { opacity: startOpacity, transform: startTransform },
          { opacity: '1', transform: targetTransform },
        ],
        reducedMotion(panel) ? REDUCED_DURATION : ENTER_DURATION,
        state,
        token,
      ),
    );
  }

  return Promise.all(animations).then(() => undefined);
}

/** Expand or collapse a custom panel, hiding it only after its exit finishes. */
export function setExpanded(panel, open) {
  if (!panel || panel.nodeType !== 1) return Promise.resolve();
  const expanded = Boolean(open);
  const state = stateFor(elementStates, panel);

  if (state.intent === expanded) return state.promise;
  const wasExpanded = state.intent ?? !panel.hidden;
  if (wasExpanded === expanded) {
    state.intent = expanded;
    panel.hidden = !expanded;
    panel.setAttribute('aria-hidden', String(!expanded));
    setInert(panel, !expanded);
    return Promise.resolve();
  }

  const before = !panel.hidden ? computedStyle(panel) : null;
  const reversing = Boolean(state.animation && !panel.hidden);
  const startHeight =
    before && (wasExpanded || reversing) ? before.height : '0px';
  const startOpacity =
    before && (wasExpanded || reversing) ? before.opacity : '0';

  const reduced = reducedMotion(panel);
  const duration = reduced
    ? REDUCED_DURATION
    : expanded
      ? ENTER_DURATION
      : EXIT_DURATION;

  if (expanded) {
    panel.hidden = false;
    panel.setAttribute('aria-hidden', 'false');
    setInert(panel, false);
  } else {
    panel.setAttribute('aria-hidden', 'true');
    setInert(panel, true);
  }

  const stateToken = ++state.token;
  state.intent = expanded;
  cancelAnimation(state);
  const targetHeight = expanded
    ? computedStyle(panel)?.height ?? `${panel.scrollHeight}px`
    : '0px';

  return play(
    panel,
    [
      { height: startHeight, opacity: startOpacity },
      { height: targetHeight, opacity: expanded ? '1' : '0' },
    ],
    duration,
    state,
    stateToken,
    () => {
      panel.hidden = !expanded;
      panel.setAttribute('aria-hidden', String(!expanded));
      setInert(panel, !expanded);
    },
  );
}

function detailContent(details) {
  return Array.from(details.children).filter(
    child => child.localName !== 'summary',
  );
}

/** Animate native details content while keeping its summary behavior intact. */
export function setDetailsOpen(details, open) {
  if (details?.localName !== 'details') return Promise.resolve();
  const expanded = Boolean(open);
  const state = stateFor(elementStates, details);
  const wasExpanded = state.intent ?? details.open;

  if (
    state.intent === expanded &&
    (state.pending || details.open === expanded)
  ) {
    return state.promise;
  }
  if (wasExpanded === expanded && details.open === expanded) {
    state.intent = expanded;
    return Promise.resolve();
  }

  const content = detailContent(details);
  if (expanded && !wasExpanded) {
    for (const panel of content) {
      const panelState = stateFor(elementStates, panel);
      if (panelState.intent === undefined) {
        panelState.intent = false;
        panel.hidden = true;
        panel.setAttribute('aria-hidden', 'true');
        setInert(panel, true);
      }
    }
  }

  details.open = true;
  state.intent = expanded;
  const token = ++state.token;
  const operations = content.map(panel => setExpanded(panel, expanded));
  state.pending = true;
  const promise = Promise.all(operations)
    .then(() => {
      if (state.token === token && !expanded) details.open = false;
    })
    .finally(() => {
      if (state.token === token) state.pending = false;
    });
  state.promise = promise;
  return promise;
}

/** Install delegated summary handling once; the returned disposer is idempotent. */
export function installDetails(root = globalThis.document) {
  if (!root || typeof root.addEventListener !== 'function') return () => {};
  const installed = detailsInstallations.get(root);
  if (installed) return installed;

  const onClick = event => {
    const target =
      event.target?.nodeType === 1 ? event.target : event.target?.parentElement;
    const summary = target?.closest?.('summary');
    const details = summary?.parentElement;
    if (
      details?.localName !== 'details' ||
      !root.contains(details) ||
      event.defaultPrevented
    ) {
      return;
    }

    event.preventDefault();
    const state = elementStates.get(details);
    const currentIntent = state?.intent ?? details.open;
    void setDetailsOpen(details, !currentIntent);
  };

  root.addEventListener('click', onClick);
  let active = true;
  const dispose = () => {
    if (!active) return;
    active = false;
    root.removeEventListener('click', onClick);
    if (detailsInstallations.get(root) === dispose) {
      detailsInstallations.delete(root);
    }
  };
  detailsInstallations.set(root, dispose);
  return dispose;
}

function dialogStateFor(dialog) {
  const state = stateFor(dialogStates, dialog);
  if (!state.cancelHandler) {
    state.cancelHandler = event => {
      event.preventDefault();
      void closeDialog(dialog);
    };
    dialog.addEventListener('cancel', state.cancelHandler);
  }
  return state;
}

function removeCancelHandler(dialog, state) {
  if (!state.cancelHandler) return;
  dialog.removeEventListener('cancel', state.cancelHandler);
  state.cancelHandler = null;
}

function restoreFocus(state) {
  const target = state.returnFocus;
  state.returnFocus = null;
  if (!target?.isConnected || typeof target.focus !== 'function') return;
  try {
    target.focus({ preventScroll: true });
  } catch {
    target.focus();
  }
}

/** Open a modal dialog and animate its entrance without replacing native focus behavior. */
export function showDialog(dialog) {
  if (
    !dialog ||
    dialog.localName !== 'dialog' ||
    !dialog.isConnected ||
    typeof dialog.showModal !== 'function'
  ) {
    return Promise.resolve();
  }

  const state = dialogStateFor(dialog);
  if (state.intent === true && dialog.open) return state.promise;
  if (dialog.open && state.intent === undefined) {
    state.intent = true;
    return Promise.resolve();
  }

  const wasOpen = dialog.open;
  const before = wasOpen ? computedStyle(dialog) : null;
  if (!wasOpen) {
    state.returnFocus = dialog.ownerDocument.activeElement;
    try {
      dialog.showModal();
    } catch {
      state.returnFocus = null;
      removeCancelHandler(dialog, state);
      return Promise.resolve();
    }
  }

  const reduced = reducedMotion(dialog);
  const startOpacity = before?.opacity ?? '0';
  const startTransform =
    before?.transform ?? (reduced ? 'none' : 'translateY(6px)');
  const targetTransform = computedStyle(dialog)?.transform ?? 'none';

  state.intent = true;
  const token = ++state.token;
  cancelAnimation(state);
  return play(
    dialog,
    [
      { opacity: startOpacity, transform: startTransform },
      { opacity: '1', transform: targetTransform },
    ],
    reduced ? REDUCED_DURATION : ENTER_DURATION,
    state,
    token,
  );
}

/** Animate a modal dialog out, then close it and return focus to its opener. */
export function closeDialog(dialog) {
  if (!dialog || dialog.localName !== 'dialog' || !dialog.open) {
    return Promise.resolve();
  }

  const state = dialogStateFor(dialog);
  if (state.intent === false && state.pending) return state.promise;
  const before = computedStyle(dialog);
  const reduced = reducedMotion(dialog);
  const startOpacity = before?.opacity ?? '1';
  const startTransform = before?.transform ?? 'none';
  const endTransform = reduced ? 'none' : 'translateY(-4px)';

  state.intent = false;
  const token = ++state.token;
  cancelAnimation(state);
  return play(
    dialog,
    [
      { opacity: startOpacity, transform: startTransform },
      { opacity: '0', transform: endTransform },
    ],
    reduced ? REDUCED_DURATION : EXIT_DURATION,
    state,
    token,
    () => {
      if (state.token !== token || state.intent || !dialog.open) return;
      dialog.close();
      restoreFocus(state);
      removeCancelHandler(dialog, state);
    },
  );
}
