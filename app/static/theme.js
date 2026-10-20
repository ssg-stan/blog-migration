try { document.documentElement.dataset.theme = localStorage.getItem('blog-migration-theme') === 'light' ? 'light' : 'dark'; } catch (error) {}
