(() => {
  const themeToggle = document.getElementById('theme-toggle');
  const updateTheme = () => {
    const dark = document.documentElement.dataset.theme === 'dark';
    themeToggle?.setAttribute('aria-pressed', String(dark));
    const label = document.getElementById('theme-label');
    if (label) label.textContent = dark ? 'Dark mode' : 'Light mode';
  };
  updateTheme();
  themeToggle?.addEventListener('click', () => {
    const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('blog-migration-theme', theme); } catch (error) {}
    updateTheme();
  });
  for (const time of document.querySelectorAll('[data-local-time]')) {
    const date = new Date(time.dataset.localTime);
    if (!Number.isNaN(date.getTime())) {
      time.textContent = new Intl.DateTimeFormat(undefined, {dateStyle: 'medium', timeStyle: 'short'}).format(date);
      time.title = date.toISOString();
    }
  }
  const contentTabs = [...document.querySelectorAll('.content-tab')];
  const selectTab = (button) => {
    for (const tab of contentTabs) {
      const active = tab === button;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      document.getElementById(tab.dataset.panel).hidden = !active;
    }
  };
  contentTabs.forEach((button, index) => {
    button.addEventListener('click', () => selectTab(button));
    button.addEventListener('keydown', (event) => {
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % contentTabs.length;
      if (event.key === 'ArrowLeft') next = (index - 1 + contentTabs.length) % contentTabs.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = contentTabs.length - 1;
      if (next !== undefined) {
        event.preventDefault();
        selectTab(contentTabs[next]);
        contentTabs[next].focus();
      }
    });
  });
  const frame = document.getElementById('article-preview');
  for (const button of document.querySelectorAll('.device')) {
    button.addEventListener('click', () => {
      frame.className = button.dataset.width;
      for (const sibling of document.querySelectorAll('.device')) {
        const selected = sibling === button;
        sibling.classList.toggle('active', selected);
        sibling.setAttribute('aria-pressed', String(selected));
      }
    });
  }
  const api = async (url, method, payload) => {
    const response = await fetch(url, {method, headers: {'Content-Type': 'application/json', 'X-Migration-Request': '1'}, body: JSON.stringify(payload)});
    const result = await response.json();
    if (!response.ok) {
      const detail = Array.isArray(result.detail) ? result.detail.map(item => item.loc.slice(1).join('.') + ': ' + item.msg).join('; ') : result.detail;
      throw new Error(detail || 'Could not complete this action. Please try again.');
    }
    return result;
  };
  const notify = (element, text, error = false) => {
    element.hidden = false;
    element.textContent = text;
    element.classList.toggle('error', error);
  };
  const editor = document.getElementById('post-editor');
  if (editor) {
    let dirty = false;
    editor.addEventListener('input', () => { dirty = true; });
    window.addEventListener('beforeunload', (event) => {
      if (dirty) { event.preventDefault(); event.returnValue = ''; }
    });
    const message = document.getElementById('editor-message');
    const meta = document.getElementById('edit-meta');
    const count = () => { document.getElementById('meta-count').textContent = meta.value.length + ' characters · 160 or fewer recommended'; };
    meta.addEventListener('input', count); count();
    editor.addEventListener('submit', async (event) => {
      event.preventDefault();
      const submitButtons = [...document.querySelectorAll('[type="submit"][form="post-editor"], #post-editor button[type="submit"]')];
      submitButtons.forEach(button => button.disabled = true);
      const payload = Object.fromEntries(new FormData(editor));
      payload.revision = Number(editor.dataset.revision);
      payload.tags = payload.tags.split(',').map(value => value.trim()).filter(Boolean);
      payload.categories = payload.categories.split(',').map(value => value.trim()).filter(Boolean);
      payload.publish_date = payload.publish_date.trim() || null;
      try {
        await api('/api/posts/' + editor.dataset.postId, 'PUT', payload);
        dirty = false;
        window.location.href = '/posts/' + editor.dataset.postId;
      } catch (error) {
        notify(message, error.message, true);
        message.scrollIntoView({behavior: 'smooth', block: 'center'});
      } finally { submitButtons.forEach(button => button.disabled = false); }
    });
    document.getElementById('clean-html').addEventListener('click', async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const content = document.getElementById('edit-content');
        const result = await api('/api/clean-html', 'POST', {content: content.value, source_url: editor.dataset.sourceUrl});
        content.value = result.content;
        dirty = true;
        notify(message, 'HTML cleaned in this draft. Review it and select Save changes to keep it.');
      } catch (error) { notify(message, error.message, true); }
      finally { button.disabled = false; }
    });
    document.getElementById('reset-source')?.addEventListener('click', async (event) => {
      if (!window.confirm('Revert the working copy to the latest HubSpot source and return it to Pending? Previous saved content stays in local revision history.')) return;
      event.currentTarget.disabled = true;
      try {
        await api('/api/posts/' + editor.dataset.postId + '/reset', 'POST', {revision: Number(editor.dataset.revision)});
        dirty = false;
        window.location.href = '/posts/' + editor.dataset.postId;
      } catch (error) { notify(message, error.message, true); event.currentTarget.disabled = false; }
    });
  }
  const review = document.querySelector('.review-grid[data-post-id]');
  if (review) {
    const message = document.getElementById('review-message');
    const actions = [...document.querySelectorAll('.review-action')];
    actions.forEach(button => button.addEventListener('click', async () => {
      const acknowledgement = document.getElementById('acknowledge-warnings');
      if (button.dataset.action === 'APPROVED' && acknowledgement && !acknowledgement.checked) {
        notify(message, 'Review the warnings and check the acknowledgement before approving.', true);
        message.scrollIntoView({behavior: 'smooth', block: 'center'});
        acknowledgement.focus();
        return;
      }
      const disabled = actions.map(action => action.disabled);
      actions.forEach(action => action.disabled = true);
      try {
        await api('/api/posts/' + review.dataset.postId + '/review', 'POST', {
          revision: Number(review.dataset.revision), action: button.dataset.action,
          acknowledge_warnings: Boolean(acknowledgement?.checked), notes: document.getElementById('review-notes').value
        });
        window.location.reload();
      } catch (error) {
        notify(message, error.message, true);
        message.scrollIntoView({behavior: 'smooth', block: 'center'});
        actions.forEach((action, index) => action.disabled = disabled[index]);
      }
    }));
    const checkButton = document.getElementById('check-urls');
    const checkMessage = document.getElementById('url-check-status');
    let watching = false;
    const watchChecks = async () => {
      try {
        const response = await fetch('/api/posts/' + review.dataset.postId + '/check-urls');
        if (!response.ok) throw new Error('Could not check progress. Refresh the page to reconnect.');
        const status = await response.json();
        if (status.revision && status.revision !== Number(review.dataset.revision)) {
          if (watching) setTimeout(watchChecks, 1200);
          return;
        }
        if (status.status === 'IDLE' && watching) {
          setTimeout(watchChecks, 1200);
          return;
        }
        if (status.status === 'RUNNING') {
          watching = true;
          checkButton.disabled = true;
          checkMessage.textContent = 'Checking URLs… ' + status.completed + ' of ' + status.total;
          setTimeout(watchChecks, 1200);
        } else if (watching) {
          if (status.status === 'SUCCESS') window.location.reload();
          else { watching = false; checkButton.disabled = false; checkMessage.textContent = status.message || 'Check stopped. Please retry.'; }
        }
      } catch (error) {
        checkMessage.textContent = error.message;
        if (watching) setTimeout(watchChecks, 3000);
      }
    };
    checkButton.addEventListener('click', async () => {
      checkButton.disabled = true;
      try {
        await api('/api/posts/' + review.dataset.postId + '/check-urls', 'POST', {revision: Number(review.dataset.revision)});
        watching = true;
        await watchChecks();
      } catch (error) { checkMessage.textContent = error.message; checkButton.disabled = false; }
    });
    watchChecks();
  }
  const destinationCheck = document.getElementById('destination-check');
  destinationCheck?.addEventListener('click', async () => {
    destinationCheck.disabled = true;
    const message = document.getElementById('destination-message');
    notify(message, 'Checking destination connection…');
    try {
      const result = await api('/api/destination/check', 'POST', {});
      notify(message, (result.simulated ? 'Simulation ready: ' : 'Connected: ') + result.blog.name + ' · ' + result.authors + ' authors · ' + result.categories + ' categories.');
    } catch (error) { notify(message, error.message, true); }
    finally { destinationCheck.disabled = false; }
  });
  const importForm = document.getElementById('draft-import');
  importForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const message = document.getElementById('import-message');
    const data = new FormData(importForm);
    for (const input of importForm.querySelectorAll('select,button')) input.disabled = true;
    notify(message, 'Preparing the approved copy and creating one destination draft…');
    message.scrollIntoView({behavior: 'smooth', block: 'center'});
    try {
      await api('/api/posts/' + importForm.dataset.postId + '/import', 'POST', {
        revision: Number(importForm.dataset.revision), author_id: data.get('author_id'), category_ids: data.getAll('category_ids')
      });
      window.location.reload();
    } catch (error) {
      notify(message, error.message + ' Refresh this page to check the current import result before trying again.', true);
      // Keep creation disabled until refreshed: a lost response may still have created the draft.
    }
  });
  const progress = document.getElementById('import-progress');
  if (progress) {
    const pollImport = async () => {
      try {
        const response = await fetch('/api/posts/' + progress.dataset.postId + '/import');
        if (!response.ok) throw new Error('Could not check import progress.');
        const result = await response.json();
        if (!['PREPARING','SENDING'].includes(result.status)) { window.location.reload(); return; }
        progress.textContent = result.message;
      } catch (error) { progress.textContent = error.message + ' Refresh to reconnect.'; }
      setTimeout(pollImport, 1500);
    };
    pollImport();
  }
  const buttons = [...document.querySelectorAll('.sync-button')];
  const message = document.getElementById('sync-message');
  if (!buttons.length || !message) return;
  let running = false;
  let polling = false;
  let lastRun = null;
  const show = (text, error = false) => {
    message.hidden = false;
    message.textContent = text;
    message.classList.toggle('error', error);
  };
  const setBusy = (busy) => {
    running = busy;
    buttons.forEach(button => {
      button.disabled = busy;
      button.textContent = busy ? 'Syncing…' : '↻ Sync from HubSpot';
    });
  };
  const poll = async () => {
    if (polling) return;
    polling = true;
    try {
      const response = await fetch('/api/sync/status');
      if (!response.ok) throw new Error('Could not check sync status. Refresh the page to reconnect.');
      const status = await response.json();
      if (status.running) {
        setBusy(true);
        lastRun = status.run?.id;
        show('Syncing from HubSpot… ' + (status.run?.fetched || 0) + ' posts fetched.');
        setTimeout(poll, 1200);
      } else if (running) {
        setBusy(false);
        if (status.run?.status === 'SUCCESS' && status.run.id === lastRun) {
          window.location.reload();
        } else {
          show(status.run?.message || 'Sync stopped. Please try again.', true);
        }
      }
    } catch (error) {
      show(error.message, true);
      if (running) setTimeout(poll, 3000);
    } finally {
      polling = false;
    }
  };
  buttons.forEach(button => button.addEventListener('click', async () => {
    setBusy(true);
    show('Starting HubSpot sync…');
    try {
      const response = await fetch('/api/sync', {method: 'POST', headers: {'X-Migration-Request': '1'}});
      const result = await response.json();
      if (response.status === 409) { await poll(); return; }
      if (!response.ok) throw new Error(result.detail || 'Could not start sync. Please try again.');
      lastRun = result.run_id;
      await poll();
    } catch (error) {
      setBusy(false);
      show(error.message, true);
    }
  }));
  // Also resume monitoring a sync started before a page refresh.
  poll();
})();
