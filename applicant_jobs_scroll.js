/* Fixed read-only scroll: load more job cards, no clicks, no input, no storage. */
(() => {
  const root = document.scrollingElement || document.documentElement;
  const heightOf = () => Math.max(root ? root.scrollHeight : 0, document.body ? document.body.scrollHeight : 0);
  const cards = () => document.querySelectorAll('li.job-card-box,.job-card-wrapper').length;
  const before = {height: heightOf(), cards: cards(), y: root ? root.scrollTop : 0};
  if (root) root.scrollTop = before.height;
  window.scrollTo(0, before.height);
  return {status: 'scrolled', before};
})()
